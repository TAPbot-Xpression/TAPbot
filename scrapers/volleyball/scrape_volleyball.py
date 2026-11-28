#!/usr/bin/env python3
"""
scrapers/volleyball/scrape_volleyball.py
Collegiate Women's Volleyball player and team season stats scraper.

Target site style:
  - "new": Sidearm Nuxt/Vue (Interactive Player Stats: Offensive / Defensive views
           + Team Stats: Totals / Per Set views)

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
  3. Direct Vue 3 combobox/listbox interaction with verification loops and fallback DOM dispatch.
  4. Precise 2-row multi-header extraction targeting .s-table-header__column-label.
  5. Dual-timestamp tracking (last_scan advances every run, last_updated updates only when stats change).
  6. Empty-scrape protection (preserves last-known-good JSON).
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
def find_repo_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / ".git").exists() or (p / "docs").exists():
            return p
    return start

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = find_repo_root(SCRIPT_DIR)
OUT_DIR = REPO_ROOT / "docs" / "volleyball"

CONCURRENCY_LIMIT = 4
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
    "season", "overall", "conference", "view bio", "image", "unassigned",
    "player", "player uniform", "player name", "-"
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

# ── Precise Column Header Mapper ─────────────────────────────────────────────
HEADER_MAP = {
    "#": "number", "NO": "number", "NO.": "number", "UNIFORM": "number",
    "PLAYER": "name", "NAME": "name", "PLAYERNAME": "name",
    "SP": "sp", "MP": "mp", "MS": "ms", "GP": "mp", "SETS": "sp",
    "PTS": "pts", "PTS/S": "pts_per_s", "PTS/SET": "pts_per_s",
    "K": "k", "KILLS": "k", "K/S": "k_per_s", "K/SET": "k_per_s",
    "E": "e", "ERRORS": "e",
    "TA": "ta", "TOTAL ATTACKS": "ta", "ATTEMPTS": "ta",
    "PCT": "pct", "HIT%": "pct", "ATTACK%": "pct",
    "A": "a", "ASSISTS": "a", "A/S": "a_per_s", "A/SET": "a_per_s",
    "SA": "sa", "ACES": "sa", "SA/S": "sa_per_s", "SA/SET": "sa_per_s",
    "SE": "se",
    "DIG": "dig", "DIGS": "dig", "D": "dig", "DIG/S": "dig_per_s", "DIG/SET": "dig_per_s",
    "RE": "re", "REC%": "rec_pct", "RE/S": "re_per_s",
    "BS": "bs", "SOLO": "bs", "BLOCK SOLOS": "bs",
    "BA": "ba", "ASSIST": "ba", "BLOCK ASSISTS": "ba",
    "BLK": "blk", "TOTAL BLOCKS": "blk", "TB": "blk", "BLK/S": "blk_per_s", "BLK/SET": "blk_per_s",
    "BE": "be", "BHE": "bhe"
}

def extract_column_mapping(thead: BeautifulSoup) -> Dict[str, int]:
    mapping: Dict[str, int] = {}
    rows = thead.find_all("tr")
    if not rows:
        return mapping

    for r in reversed(rows):
        ths = r.find_all(["th", "td"])
        row_mapping: Dict[str, int] = {}
        for idx, th in enumerate(ths):
            label_span = th.select_one(".s-table-header__column-label")
            txt = label_span.get_text(" ", strip=True) if label_span else th.get_text(" ", strip=True)
            norm = re.sub(r"[^A-Z0-9%/]", "", txt.upper())
            if norm in HEADER_MAP and HEADER_MAP[norm] not in row_mapping:
                row_mapping[HEADER_MAP[norm]] = idx

        if len(row_mapping) >= 3:
            return row_mapping

    return mapping

def parse_sidearm_player_table(table: BeautifulSoup) -> Dict[str, Dict[str, Any]]:
    players: Dict[str, Dict[str, Any]] = {}
    thead = table.find("thead")
    if not thead:
        return players

    mapping = extract_column_mapping(thead)
    if not ("name" in mapping or "number" in mapping):
        return players

    tbody = table.find("tbody")
    rows = tbody.find_all("tr") if tbody else table.find_all("tr")

    for r in rows:
        if r.find_parent("tfoot") or (r.find("th") and not r.find("td")):
            continue

        cells = [c.get_text(" ", strip=True) for c in r.find_all(["td", "th"])]
        if len(cells) < 3:
            continue

        raw_name = ""
        name_a = r.find("a", href=re.compile(r"roster", re.I))
        if name_a:
            raw_name = name_a.get_text(" ", strip=True)
        elif "name" in mapping and mapping["name"] < len(cells):
            raw_name = cells[mapping["name"]]
        elif len(cells) > 1:
            raw_name = cells[1]

        c_name = clean_name(raw_name)
        if not is_valid_player_candidate(c_name):
            continue

        raw_num = ""
        if "number" in mapping and mapping["number"] < len(cells):
            raw_num = cells[mapping["number"]]
        elif cells[0].isdigit():
            raw_num = cells[0]

        key = c_name.lower()
        p = players.setdefault(key, {"name": c_name, "number": clean_number(raw_num)})

        for stat_k, col_idx in mapping.items():
            if stat_k not in ("name", "number") and col_idx < len(cells):
                p[stat_k] = cells[col_idx]

    return players

# ── Robust Vue Combobox / Dropdown Selector ──────────────────────────────────
async def switch_view_dropdown(page: Page, target_text: str) -> bool:
    """
    Directly opens the Sidearm Nuxt 3 combobox and selects the target view (e.g. 'Defensive' or 'Per Set').
    Uses JavaScript click and evaluation fallback if Playwright locator click is intercepted.
    """
    try:
        box = page.locator("button[role='combobox'], button[data-test-id='s-select__box']").first
        if not await box.is_visible():
            return False

        curr_text = (await box.inner_text()).strip()
        if target_text.lower() in curr_text.lower():
            return True

        # 1. Click combobox to open popup listbox
        await box.click()
        await page.wait_for_timeout(350)

        # 2. Locate listbox item matching target_text
        option_locators = [
            page.locator(f"[role='listbox'] [role='option']:has-text('{target_text}')").first,
            page.locator(f"[role='option']:has-text('{target_text}')").first,
            page.locator(f".s-select__options-item:has-text('{target_text}')").first,
            page.locator(f"li:has-text('{target_text}')").first,
        ]

        clicked = False
        for opt in option_locators:
            if await opt.is_visible():
                await opt.click(force=True)
                clicked = True
                break

        # JavaScript fallback if overlay element was shadowed
        if not clicked:
            js_clicked = await page.evaluate("""(target) => {
                const options = Array.from(document.querySelectorAll('[role="option"], .s-select__options-item, li'));
                for (const el of options) {
                    if (el.textContent.toLowerCase().includes(target.toLowerCase())) {
                        el.click();
                        return true;
                    }
                }
                return false;
            }""", target_text)
            if js_clicked:
                clicked = True

        await page.wait_for_timeout(700)
        return clicked

    except Exception as e:
        logger.debug(f"Failed switching to dropdown view '{target_text}': {e}")
        return False

async def click_main_tab(page: Page, tab_text: str) -> bool:
    try:
        tab = page.locator(
            f"button[role='tab']:has-text('{tab_text}'), button[data-test-id='s-tab__link']:has-text('{tab_text}')"
        ).first
        if await tab.is_visible():
            await tab.click()
            await page.wait_for_timeout(600)
            return True
    except Exception as e:
        logger.debug(f"Tab click error for '{tab_text}': {e}")
    return False

# ── Team Stats HTML Parser ───────────────────────────────────────────────────
def parse_volleyball_team_stats(soup: BeautifulSoup, found: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    if found is None:
        found = {}
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 2:
                continue

            label = cells[0].strip().lower()
            val_team = cells[1].strip()

            if ("percent" in label or "pct" in label or "hitting" in label or "attack percent" in label) and "hit_pct" not in found:
                found["hit_pct"] = clean_stat_pct(val_team)
            elif ("kills per set" in label or label == "k/s" or label == "kills/s") and "k_per_s" not in found:
                found["k_per_s"] = clean_stat_float(val_team)
            elif ("assists per set" in label or label == "a/s" or label == "assists/s") and "a_per_s" not in found:
                found["a_per_s"] = clean_stat_float(val_team)
            elif ("service aces per set" in label or "aces per set" in label or label == "sa/s" or label == "aces/s") and "sa_per_s" not in found:
                found["sa_per_s"] = clean_stat_float(val_team)
            elif ("digs per set" in label or label == "dig/s" or label == "digs/s") and "dig_per_s" not in found:
                found["dig_per_s"] = clean_stat_float(val_team)
            elif ("blocks per set" in label or label == "blk/s" or label == "blocks/s") and "blk_per_s" not in found:
                found["blk_per_s"] = clean_stat_float(val_team)

    return found

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
        player_stats_map: Dict[str, Dict[str, Any]] = {}
        team_stats_dict: Dict[str, str] = {}

        page: Optional[Page] = None
        try:
            page = await context.new_page()
            await page.goto(team_cfg["stats_url"], wait_until="domcontentloaded", timeout=35000)
            try:
                await page.wait_for_selector("table, button[role='tab']", timeout=8000)
            except Exception:
                pass

            # 1. PLAYER STATS: Ensure Player Stats Tab is Active
            await click_main_tab(page, "Player Stats")

            # 1a. Scrape Offensive View
            await switch_view_dropdown(page, "Offensive")
            try:
                await page.wait_for_selector("span.s-table-header__column-label:has-text('K'), span.s-table-header__column-label:has-text('PTS')", timeout=4000)
            except Exception:
                pass
            soup_off = BeautifulSoup(await page.content(), "html.parser")
            for tbl in soup_off.find_all("table"):
                off_players = parse_sidearm_player_table(tbl)
                for k, p in off_players.items():
                    player_stats_map.setdefault(k, {}).update(p)

            # 1b. Scrape Defensive View
            await switch_view_dropdown(page, "Defensive")
            try:
                await page.wait_for_selector("span.s-table-header__column-label:has-text('DIG'), span.s-table-header__column-label:has-text('BLK')", timeout=5000)
            except Exception:
                await page.wait_for_timeout(800)
            soup_def = BeautifulSoup(await page.content(), "html.parser")
            for tbl in soup_def.find_all("table"):
                def_players = parse_sidearm_player_table(tbl)
                for k, p in def_players.items():
                    player_stats_map.setdefault(k, {}).update(p)

            # 2. TEAM STATS: Switch to Team Stats Tab
            await click_main_tab(page, "Team Stats")

            # 2a. Scrape Totals View
            await switch_view_dropdown(page, "Totals")
            try:
                await page.wait_for_selector("td:has-text('Percent'), td:has-text('Kills')", timeout=4000)
            except Exception:
                pass
            soup_totals = BeautifulSoup(await page.content(), "html.parser")
            parse_volleyball_team_stats(soup_totals, team_stats_dict)

            # 2b. Scrape Per Set View
            await switch_view_dropdown(page, "Per Set")
            try:
                await page.wait_for_selector("td:has-text('Kills Per Set'), td:has-text('Digs Per Set')", timeout=5000)
            except Exception:
                await page.wait_for_timeout(800)
            soup_perset = BeautifulSoup(await page.content(), "html.parser")
            parse_volleyball_team_stats(soup_perset, team_stats_dict)

        except Exception as e:
            logger.error(f"[{label}] Error scraping stats: {e}")
        finally:
            if page:
                await page.close()

        roster_lookup = await roster_task

        # Build Clean Merged Player List
        merged = []
        for key, p in player_stats_map.items():
            name = clean_name(p.get("name", ""))
            if not is_valid_player_candidate(name):
                continue

            num = clean_number(p.get("number", ""))
            r_info = find_roster_entry(name, num, roster_lookup)

            player_obj = {
                "number": num or (r_info.get("number", "") if r_info else ""),
                "name": (r_info.get("name") if r_info else "") or name,
                "position": (r_info.get("position") if r_info else "") or "",
                "year": (r_info.get("year") if r_info else "") or "",
                "mp": clean_stat_int(p.get("mp")),
                "sp": clean_stat_int(p.get("sp")),
                "pts": clean_stat_float(p.get("pts")),
                "pts_per_s": clean_stat_float(p.get("pts_per_s")),
                "k": clean_stat_int(p.get("k")),
                "k_per_s": clean_stat_float(p.get("k_per_s")),
                "e": clean_stat_int(p.get("e")),
                "ta": clean_stat_int(p.get("ta")),
                "pct": clean_stat_pct(p.get("pct")),
                "a": clean_stat_int(p.get("a")),
                "a_per_s": clean_stat_float(p.get("a_per_s")),
                "sa": clean_stat_int(p.get("sa")),
                "sa_per_s": clean_stat_float(p.get("sa_per_s")),
                "se": clean_stat_int(p.get("se")),
                "dig": clean_stat_int(p.get("dig")),
                "dig_per_s": clean_stat_float(p.get("dig_per_s")),
                "re": clean_stat_int(p.get("re")),
                "bs": clean_stat_int(p.get("bs")),
                "ba": clean_stat_int(p.get("ba")),
                "blk": clean_stat_float(p.get("blk")),
                "blk_per_s": clean_stat_float(p.get("blk_per_s")),
                "be": clean_stat_int(p.get("be")),
                "bhe": clean_stat_int(p.get("bhe")),
            }
            merged.append(player_obj)

        merged.sort(key=lambda x: int(x["number"]) if x["number"].isdigit() else 999)

        # Build Standardized Team Stats
        final_team_stats = {k: team_stats_dict.get(k, "-") for k in TEAM_STAT_KEYS}

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
            "team_stats": final_team_stats,
            "players": merged,
        }

        logger.info(f"[{label}] Scraped {len(merged)} players. Team stats found: "
                    f"{sum(1 for v in final_team_stats.values() if v != '-')}/{len(TEAM_STAT_KEYS)}")
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
