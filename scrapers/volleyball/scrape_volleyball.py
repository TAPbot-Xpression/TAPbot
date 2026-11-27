#!/usr/bin/env python3
"""
scrapers/volleyball/scrape_volleyball.py
Collegiate Women's Volleyball player and team season stats scraper.

Target site style:
  - "new": Sidearm Nuxt/Vue (Offensive / Defensive dropdowns/tabs + Team Stats tab)

Output (repo-relative):
  docs/volleyball/<slug>.json

Player Stats Captured:
  - Offense: MP, SP, PTS, PTS/S, K, K/S, E, TA, PCT, A, A/S, SA, SA/S, SE
  - Defense: DIG, DIG/S, RE, BS, BA, BLK, BLK/S, BE, BHE

Team Stats Captured:
  - Totals: Attack Percentage (hit_pct)
  - Per Set: Kills/Set (k_per_s), Assists/Set (a_per_s), Aces/Set (sa_per_s),
             Digs/Set (dig_per_s), Blocks/Set (blk_per_s)

Features:
  1. Full async concurrency across teams via asyncio.Semaphore.
  2. Isolated Playwright browser contexts with resource blocking (images/fonts/media).
  3. Nuxt JSON payload parser with fallback to DOM table parsing.
  4. Dual-timestamp tracking (last_scan advances every run, last_updated updates only when stats change).
  5. Empty-scrape protection (preserves last-known-good JSON).
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("volleyball_scraper")
sys.setrecursionlimit(20000)

# ── Dynamic Path Resolution ──────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1] if len(SCRIPT_DIR.parents) >= 2 else SCRIPT_DIR
OUT_DIR = REPO_ROOT / "docs" / "volleyball"
DEBUG_DIR = REPO_ROOT / "debug"

CONCURRENCY_LIMIT = 4
MIN_ROSTER_ENTRIES = 6
DEBUG_MODE = False

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ── Teams Registry ───────────────────────────────────────────────────────────
TEAMS_CONFIG: List[Dict[str, Any]] = [
    {
        "name": "St. Thomas",
        "slug": "st_thomas_wvball",
        "gender": "women",
        "conference": "Summit League",
        "style": "new",
        "stats_url": "https://tommiesports.com/sports/wvball/stats/2026",
        "roster_url": "https://tommiesports.com/sports/wvball/roster/print",
    }
]

TEAM_STAT_KEYS = [
    "hit_pct", "k_per_s", "a_per_s", "sa_per_s", "dig_per_s", "blk_per_s"
]

DISCARD_TERMS = {
    "team", "tm", "total", "totals", "opponent", "opponents", "opp",
    "season", "overall", "conference", "view bio", "image", "unassigned"
}

_POS_MAP = {
    "OH": "OH", "OUTSIDE": "OH", "OUTSIDEHITTER": "OH", "RS": "OPP", "OPP": "OPP",
    "RIGHTSIDE": "OPP", "RIGHTSIDEHITTER": "OPP", "OPPOSITE": "OPP",
    "MB": "MB", "MIDDLE": "MB", "MIDDLEBLOCKER": "MB", "MH": "MB", "MIDDLEHITTER": "MB",
    "S": "S", "SETTER": "S",
    "L": "L/DS", "DS": "L/DS", "LIBERO": "L/DS", "DEFENSIVESPECIALIST": "L/DS", "L/DS": "L/DS"
}

_CLASS_MAP = {
    "FR": "FR", "FRESHMAN": "FR", "FY": "FR", "FIRST": "FR", "1ST": "FR",
    "SO": "SO", "SOPHOMORE": "SO", "SECOND": "SO", "2ND": "SO",
    "JR": "JR", "JUNIOR": "JR", "THIRD": "JR", "3RD": "JR",
    "SR": "SR", "SENIOR": "SR", "FOURTH": "SR", "4TH": "SR",
    "GR": "GR", "GRAD": "GR", "GRADUATE": "GR", "GS": "GR", "FIFTH": "GR", "5TH": "GR"
}

# ── Cleaning & Formatting Helpers ────────────────────────────────────────────
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def clean_stat_int(val: Any) -> str:
    if val is None:
        return "0"
    s = str(val).strip()
    if not s or s in ("-", "--", "—", "null", "None", "N/A", "na"):
        return "0"
    try:
        return str(int(round(float(s))))
    except Exception:
        m = re.match(r"^(\d+)", s)
        return m.group(1) if m else "0"

def clean_stat_float(val: Any) -> str:
    if val is None:
        return "0.00"
    s = str(val).strip()
    if not s or s in ("-", "--", "—", "null", "None", "N/A", "na"):
        return "0.00"
    try:
        return f"{float(s):.2f}"
    except Exception:
        return "0.00"

def clean_stat_pct(val: Any) -> str:
    if val is None or val == "":
        return ".000"
    s = str(val).strip().replace("%", "")
    try:
        f = float(s)
        if f > 1.0:
            f = f / 100.0
        formatted = f"{f:.3f}"
        if formatted.startswith("-0."):
            return f"-{formatted[2:]}"
        return formatted.lstrip("0") if formatted.startswith("0.") else formatted
    except Exception:
        return ".000"

def clean_name(raw_name: str) -> str:
    if not raw_name:
        return ""
    text = raw_name.replace("View Bio", "").replace("Image", "").strip()
    text = re.sub(r"['\"]+", "", text).strip()
    text = re.sub(r"#\d{1,2}\b", " ", text)
    text = text.strip().rstrip(",").strip()
    if "," in text:
        parts = [p.strip() for p in text.split(",") if p.strip()]
        if len(parts) >= 2:
            last = parts[0]
            first = " ".join(parts[1:])
            text = f"{first} {last}"
    return " ".join(text.split()).strip()

def clean_number(raw_num: Any) -> str:
    if raw_num is None:
        return ""
    text = str(raw_num).strip().replace("#", "")
    m = re.match(r"^(\d{1,2})$", text)
    return str(int(m.group(1))) if m else ""

def normalize_position(pos_str: str) -> str:
    if not pos_str:
        return ""
    p = re.sub(r"[^A-Z/]", "", str(pos_str).upper())
    for token in p.split("/"):
        if token in _POS_MAP:
            return _POS_MAP[token]
    return ""

def normalize_year(yr_str: str) -> str:
    if not yr_str:
        return ""
    y = re.sub(r"[^A-Z0-9]", "", str(yr_str).upper())
    if y in _CLASS_MAP:
        return _CLASS_MAP[y]
    for prefix in ("REDSHIRT", "RS", "R"):
        if y.startswith(prefix):
            rem = y[len(prefix):]
            if rem in _CLASS_MAP:
                base = _CLASS_MAP[rem]
                return f"R-{base}" if base != "GR" else "GR"
    return ""

def is_valid_player_candidate(name: str) -> bool:
    if not name or len(name) < 3:
        return False
    low = name.lower().strip()
    if low in DISCARD_TERMS:
        return False
    if low.startswith("at ") or low.startswith("vs ") or low.startswith("#"):
        return False
    if re.match(r"^[\d\.\-\s/%:]+$", low):
        return False
    return True

# ── JSON File Management & Timestamp Tracking ─────────────────────────────────
def read_existing_json(slug: str) -> Dict[str, Any]:
    try:
        with open(OUT_DIR / f"{slug}.json", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _strip_volatile(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in d.items() if k not in ("generated_at", "updated_at", "last_scan", "last_updated")}

def write_team_json(slug: str, team_data: Dict[str, Any]) -> Dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    existing = read_existing_json(slug)
    same_source = existing.get("source_stats_url") == team_data.get("source_stats_url")

    # Safeguard against empty player scrape
    if not team_data.get("players") and existing.get("players") and same_source:
        logger.warning(f"[{team_data.get('team', slug)}] No players scraped — keeping existing {slug}.json")
        return existing

    now = now_iso()
    team_data["last_scan"] = now

    if existing and same_source:
        old_clean = _strip_volatile(existing)
        new_clean = _strip_volatile(team_data)

        if old_clean == new_clean:
            team_data["last_updated"] = existing.get("last_updated") or existing.get("generated_at") or now
            team_data["generated_at"] = team_data["last_updated"]
            with open(OUT_DIR / f"{slug}.json", "w", encoding="utf-8") as f:
                json.dump(team_data, f, indent=2)
            logger.info(f"[{team_data.get('team', slug)}] Stats unchanged — updated last_scan ({now[:16]})")
            return team_data

    team_data["last_updated"] = now
    team_data["generated_at"] = now
    with open(OUT_DIR / f"{slug}.json", "w", encoding="utf-8") as f:
        json.dump(team_data, f, indent=2)
    logger.info(f"[{team_data.get('team', slug)}] Fresh stats found! Updated last_updated ({now[:16]})")
    return team_data

# ── Roster Resolution Helpers ────────────────────────────────────────────────
def _add_roster(lookup: Dict[str, Dict[str, str]], name: str, num: str, pos: str, yr: str) -> None:
    info = {"name": name, "number": num, "position": pos, "year": yr}
    lookup[name.lower()] = info
    if num != "":
        lookup[f"#{num}"] = info

def find_roster_entry(name: str, number: str, roster_lookup: Dict[str, Dict[str, str]]) -> Optional[Dict[str, str]]:
    if not roster_lookup:
        return None
    raw_low = name.lower().strip()
    if raw_low in roster_lookup:
        return roster_lookup[raw_low]
    if number and f"#{number}" in roster_lookup:
        return roster_lookup[f"#{number}"]
    close = difflib.get_close_matches(raw_low, [k for k in roster_lookup if not k.startswith("#")], n=1, cutoff=0.72)
    return roster_lookup[close[0]] if close else None

async def fetch_roster(context: BrowserContext, roster_url: str, label: str) -> Dict[str, Dict[str, str]]:
    roster_lookup: Dict[str, Dict[str, str]] = {}
    if not roster_url:
        return roster_lookup

    page: Optional[Page] = None
    try:
        page = await context.new_page()
        await page.goto(roster_url, wait_until="domcontentloaded", timeout=25000)
        try:
            await page.wait_for_selector("table, [class*='roster-player'], [class*='s-person-card']", timeout=6000)
        except Exception:
            pass

        soup = BeautifulSoup(await page.content(), "html.parser")
        for row in soup.find_all("tr"):
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 3:
                continue

            num, pos, yr, name_cand = "", "", "", ""
            for col in cells:
                norm_pos = normalize_position(col)
                norm_yr = normalize_year(col)
                if not num and re.match(r"^\d{1,2}$", col):
                    num = clean_number(col)
                elif not pos and norm_pos:
                    pos = norm_pos
                elif not yr and norm_yr:
                    yr = norm_yr
                else:
                    c_clean = clean_name(col)
                    if not name_cand and is_valid_player_candidate(c_clean):
                        name_cand = c_clean

            if name_cand:
                _add_roster(roster_lookup, name_cand, num, pos, yr)

    except Exception as e:
        logger.warning(f"[{label}] Error fetching roster: {e}")
    finally:
        if page:
            await page.close()

    return roster_lookup

# ── Nuxt 3 / Vue Data Stats Parser ───────────────────────────────────────────
def parse_nuxt_data_stats(html: str) -> List[Dict[str, Any]]:
    players = []
    patterns = [
        r'<script[^>]+id=["\']__NUXT_DATA__["\'][^>]*>(.*?)</script>',
        r"__NUXT_DATA__\s*=\s*(\[.*?\]);",
        r"window\.__NUXT__\s*=\s*(.*?);\s*",
    ]

    for pat in patterns:
        m = re.search(pat, html, re.DOTALL)
        if not m:
            continue
        try:
            data = json.loads(m.group(1))
            if not isinstance(data, list):
                continue

            def resolve(val: Any) -> str:
                if isinstance(val, int) and 0 <= val < len(data):
                    target = data[val]
                    if isinstance(target, (str, int, float)):
                        return str(target)
                return str(val) if val is not None else ""

            def find_stat(item: dict, keys: List[str]) -> str:
                for k in keys:
                    v = item.get(k)
                    if v is not None:
                        res = resolve(v).strip()
                        if res and res != "None" and res.lower() not in DISCARD_TERMS:
                            return res
                return ""

            for item in data:
                if not isinstance(item, dict):
                    continue
                if "opponent" in item or "winOrLoss" in item:
                    continue

                raw_name = item.get("playerName") or item.get("name") or item.get("fullName")
                if not raw_name:
                    continue

                c_name = clean_name(resolve(raw_name))
                if not is_valid_player_candidate(c_name):
                    continue

                c_num = clean_number(resolve(item.get("playerUniform") or item.get("jersey") or item.get("number")))
                c_pos = normalize_position(resolve(item.get("position") or item.get("pos") or item.get("positionShort")))
                c_yr = normalize_year(resolve(item.get("academicYear") or item.get("year") or item.get("class")))

                mp = find_stat(item, ["matchesPlayed", "mp", "gp"])
                sp = find_stat(item, ["setsPlayed", "sp"])
                k = find_stat(item, ["kills", "k"])
                k_s = find_stat(item, ["killsPerSet", "kPerSet", "k_s"])
                e = find_stat(item, ["attackErrors", "e"])
                ta = find_stat(item, ["totalAttacks", "ta", "attempts"])
                pct = find_stat(item, ["hittingPercentage", "hitPct", "attackPct", "pct"])

                a = find_stat(item, ["assists", "a"])
                a_s = find_stat(item, ["assistsPerSet", "aPerSet", "a_s"])
                sa = find_stat(item, ["serviceAces", "aces", "sa"])
                sa_s = find_stat(item, ["serviceAcesPerSet", "saPerSet", "sa_s"])
                se = find_stat(item, ["serviceErrors", "se"])

                dig = find_stat(item, ["digs", "dig"])
                dig_s = find_stat(item, ["digsPerSet", "digPerSet", "dig_s"])
                re_err = find_stat(item, ["receptionErrors", "re"])

                bs = find_stat(item, ["blockSolos", "bs"])
                ba = find_stat(item, ["blockAssists", "ba"])
                blk = find_stat(item, ["totalBlocks", "blk", "blocks"])
                blk_s = find_stat(item, ["blocksPerSet", "blkPerSet", "blk_s"])
                be = find_stat(item, ["blockingErrors", "be", "blockErrors"])
                bhe = find_stat(item, ["ballHandlingErrors", "bhe"])
                pts = find_stat(item, ["points", "pts", "totalPoints"])
                pts_s = find_stat(item, ["pointsPerSet", "ptsPerSet", "pts_s"])

                if not any([k, a, dig, blk, pts, sp]):
                    continue

                players.append({
                    "number": c_num,
                    "name": c_name,
                    "position": c_pos,
                    "year": c_yr,
                    "mp": clean_stat_int(mp),
                    "sp": clean_stat_int(sp),
                    "pts": clean_stat_float(pts),
                    "pts_per_s": clean_stat_float(pts_s),
                    "k": clean_stat_int(k),
                    "k_per_s": clean_stat_float(k_s),
                    "e": clean_stat_int(e),
                    "ta": clean_stat_int(ta),
                    "pct": clean_stat_pct(pct),
                    "a": clean_stat_int(a),
                    "a_per_s": clean_stat_float(a_s),
                    "sa": clean_stat_int(sa),
                    "sa_per_s": clean_stat_float(sa_s),
                    "se": clean_stat_int(se),
                    "dig": clean_stat_int(dig),
                    "dig_per_s": clean_stat_float(dig_s),
                    "re": clean_stat_int(re_err),
                    "bs": clean_stat_int(bs),
                    "ba": clean_stat_int(ba),
                    "blk": clean_stat_float(blk),
                    "blk_per_s": clean_stat_float(blk_s),
                    "be": clean_stat_int(be),
                    "bhe": clean_stat_int(bhe),
                })
        except Exception:
            continue

        if len(players) >= 5:
            break

    return players

# ── HTML Table Stats Parser (Fallback) ────────────────────────────────────────
HEADER_MAP = {
    "PLAYER": "name", "NAME": "name", "#": "number", "NO": "number",
    "SP": "sp", "MP": "mp", "GP": "sp", "SETS": "sp",
    "PTS": "pts", "PTS/S": "pts_per_s", "PTS/SET": "pts_per_s",
    "K": "k", "KILLS": "k", "K/S": "k_per_s", "K/SET": "k_per_s",
    "E": "e", "ERRORS": "e",
    "TA": "ta", "TOTAL ATTACKS": "ta", "ATTEMPTS": "ta",
    "PCT": "pct", "HIT%": "pct", "ATTACK%": "pct",
    "A": "a", "ASSISTS": "a", "A/S": "a_per_s", "A/SET": "a_per_s",
    "SA": "sa", "ACES": "sa", "SA/S": "sa_per_s", "SA/SET": "sa_per_s",
    "SE": "se",
    "DIG": "dig", "DIGS": "dig", "D": "dig", "DIG/S": "dig_per_s", "DIG/SET": "dig_per_s",
    "RE": "re",
    "BS": "bs", "SOLO": "bs", "BLOCK SOLOS": "bs",
    "BA": "ba", "ASSIST": "ba", "BLOCK ASSISTS": "ba",
    "BLK": "blk", "TOTAL BLOCKS": "blk", "TB": "blk", "BLK/S": "blk_per_s", "BLK/SET": "blk_per_s",
    "BE": "be", "BHE": "bhe"
}

def parse_html_table(soup: BeautifulSoup) -> Dict[str, Dict[str, Any]]:
    players: Dict[str, Dict[str, Any]] = {}
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        headers = [c.get_text(" ", strip=True).upper() for c in rows[0].find_all(["th", "td"])]
        mapping: Dict[str, int] = {}
        for idx, h in enumerate(headers):
            clean_h = re.sub(r"[^A-Z0-9%/]", "", h)
            if clean_h in HEADER_MAP and HEADER_MAP[clean_h] not in mapping:
                mapping[HEADER_MAP[clean_h]] = idx

        if not ("k" in mapping or "dig" in mapping or "a" in mapping or "pts" in mapping):
            continue

        name_idx = mapping.get("name", 1)
        num_idx = mapping.get("number", 0)

        for r in rows[1:]:
            cells = [c.get_text(" ", strip=True) for c in r.find_all(["td", "th"])]
            if len(cells) < 4:
                continue

            raw_name = cells[name_idx] if name_idx < len(cells) else ""
            c_name = clean_name(raw_name)
            if not is_valid_player_candidate(c_name):
                continue

            raw_num = cells[num_idx] if num_idx < len(cells) else ""
            key = c_name.lower()
            p = players.setdefault(key, {"name": c_name, "number": clean_number(raw_num)})

            for stat_k, col_idx in mapping.items():
                if stat_k not in ("name", "number") and col_idx < len(cells):
                    p[stat_k] = cells[col_idx]

    return players

# ── Team Stats Parser ────────────────────────────────────────────────────────
def parse_volleyball_team_stats(soup: BeautifulSoup) -> Dict[str, str]:
    found: Dict[str, str] = {}
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 2:
                continue

            label = cells[0].strip().lower()
            val_team = cells[1].strip()

            if ("attack percentage" in label or "hit percentage" in label or "hitting pct" in label) and "hit_pct" not in found:
                found["hit_pct"] = clean_stat_pct(val_team)
            elif ("kills/set" in label or "kills per set" in label or label == "k/s") and "k_per_s" not in found:
                found["k_per_s"] = clean_stat_float(val_team)
            elif ("assists/set" in label or "assists per set" in label or label == "a/s") and "a_per_s" not in found:
                found["a_per_s"] = clean_stat_float(val_team)
            elif ("aces/set" in label or "service aces per set" in label or label == "sa/s") and "sa_per_s" not in found:
                found["sa_per_s"] = clean_stat_float(val_team)
            elif ("digs/set" in label or "digs per set" in label or label == "dig/s") and "dig_per_s" not in found:
                found["dig_per_s"] = clean_stat_float(val_team)
            elif ("blocks/set" in label or "blocks per set" in label or label == "blk/s") and "blk_per_s" not in found:
                found["blk_per_s"] = clean_stat_float(val_team)

    return {k: found.get(k, "-") for k in TEAM_STAT_KEYS}

# ── Single Team Scraper Pipeline ─────────────────────────────────────────────
async def scrape_volleyball_team(
    context: BrowserContext,
    team_cfg: Dict[str, Any],
    semaphore: asyncio.Semaphore
) -> Dict[str, Any]:
    async with semaphore:
        slug, label = team_cfg["slug"], team_cfg["name"]
        logger.info(f"Processing {label} (Women's Volleyball)...")

        roster_task = asyncio.create_task(fetch_roster(context, team_cfg.get("roster_url", ""), label))

        raw_players: List[Dict[str, Any]] = []
        team_stats: Dict[str, str] = {k: "-" for k in TEAM_STAT_KEYS}

        page: Optional[Page] = None
        try:
            page = await context.new_page()
            await page.goto(team_cfg["stats_url"], wait_until="domcontentloaded", timeout=35000)
            try:
                await page.wait_for_selector("table, script#__NUXT_DATA__", timeout=8000)
            except Exception:
                pass

            html = await page.content()
            soup = BeautifulSoup(html, "html.parser")

            # 1. Try Nuxt JSON Data first
            raw_players = parse_nuxt_data_stats(html)

            # 2. Fallback to HTML table extraction
            if not raw_players:
                logger.info(f"[{label}] Nuxt data not found — parsing HTML tables...")
                html_p_map = parse_html_table(soup)
                for p_dict in html_p_map.values():
                    raw_players.append({
                        "number": clean_number(p_dict.get("number")),
                        "name": p_dict.get("name", ""),
                        "position": "",
                        "year": "",
                        "mp": clean_stat_int(p_dict.get("mp")),
                        "sp": clean_stat_int(p_dict.get("sp")),
                        "pts": clean_stat_float(p_dict.get("pts")),
                        "pts_per_s": clean_stat_float(p_dict.get("pts_per_s")),
                        "k": clean_stat_int(p_dict.get("k")),
                        "k_per_s": clean_stat_float(p_dict.get("k_per_s")),
                        "e": clean_stat_int(p_dict.get("e")),
                        "ta": clean_stat_int(p_dict.get("ta")),
                        "pct": clean_stat_pct(p_dict.get("pct")),
                        "a": clean_stat_int(p_dict.get("a")),
                        "a_per_s": clean_stat_float(p_dict.get("a_per_s")),
                        "sa": clean_stat_int(p_dict.get("sa")),
                        "sa_per_s": clean_stat_float(p_dict.get("sa_per_s")),
                        "se": clean_stat_int(p_dict.get("se")),
                        "dig": clean_stat_int(p_dict.get("dig")),
                        "dig_per_s": clean_stat_float(p_dict.get("dig_per_s")),
                        "re": clean_stat_int(p_dict.get("re")),
                        "bs": clean_stat_int(p_dict.get("bs")),
                        "ba": clean_stat_int(p_dict.get("ba")),
                        "blk": clean_stat_float(p_dict.get("blk")),
                        "blk_per_s": clean_stat_float(p_dict.get("blk_per_s")),
                        "be": clean_stat_int(p_dict.get("be")),
                        "bhe": clean_stat_int(p_dict.get("bhe")),
                    })

            # 3. Parse Team Stats
            team_stats = parse_volleyball_team_stats(soup)

        except Exception as e:
            logger.error(f"[{label}] Error scraping stats: {e}")
        finally:
            if page:
                await page.close()

        roster_lookup = await roster_task

        # Merge with Roster
        merged = []
        seen = set()
        for p in raw_players:
            name = p.get("name", "")
            if not name or name.lower() in seen or not is_valid_player_candidate(name):
                continue
            seen.add(name.lower())

            r_info = find_roster_entry(name, p.get("number", ""), roster_lookup)
            if r_info:
                p["name"] = r_info.get("name") or name
                p["number"] = p.get("number") or r_info.get("number", "")
                p["position"] = r_info.get("position") or p.get("position", "")
                p["year"] = r_info.get("year") or p.get("year", "")

            merged.append(p)

        now = now_iso()
        team_data = {
            "team": label,
            "slug": slug,
            "gender": team_cfg.get("gender", "women"),
            "sport": "volleyball",
            "conference": team_cfg.get("conference", "Summit League"),
            "last_scan": now,
            "last_updated": now,
            "generated_at": now,
            "source_stats_url": team_cfg["stats_url"],
            "source_roster_url": team_cfg.get("roster_url", ""),
            "team_stats": team_stats,
            "players": merged,
        }

        logger.info(f"[{label}] Scraped {len(merged)} players. Team stats found: "
                    f"{sum(1 for v in team_stats.values() if v != '-')}/{len(TEAM_STAT_KEYS)}")
        return write_team_json(slug, team_data)

# ── Main Runner ──────────────────────────────────────────────────────────────
async def run_scraper(only: Optional[List[str]] = None) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

    teams = list(TEAMS_CONFIG)
    if only:
        wanted = [o.strip().lower() for o in only if o.strip()]
        teams = [t for t in teams if any(w in t["name"].lower() or w in t["slug"].lower() for w in wanted)]

    start = time.time()
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(user_agent=USER_AGENT)
        await context.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in {"image", "media", "font"}
            else route.continue_()
        )

        results = await asyncio.gather(
            *[scrape_volleyball_team(context, t, semaphore) for t in teams],
            return_exceptions=True
        )
        await browser.close()

    for t, res in zip(teams, results):
        if isinstance(res, Exception):
            logger.error(f"[{t['name']}] scrape failed: {res!r}")

    logger.info(f"Finished volleyball scraping in {time.time() - start:.1f}s")

def main() -> None:
    ap = argparse.ArgumentParser(description="Summit League Volleyball Stats Scraper")
    ap.add_argument("--only", help="Comma-separated team names/slugs to scrape")
    args = ap.parse_args()

    asyncio.run(run_scraper(only=args.only.split(",") if args.only else None))

if __name__ == "__main__":
    main()
