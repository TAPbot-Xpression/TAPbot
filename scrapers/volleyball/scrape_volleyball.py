#!/usr/bin/env python3
"""
scrapers/volleyball/scrape_volleyball.py
Summit League Women's Volleyball player and team season stats scraper.

Site styles ("style" in TEAMS_CONFIG):
  - "new"  Sidearm Nuxt/Vue: Player Stats (Offensive / Defensive dropdown) + Team Stats (Totals / Per Set dropdown)
  - "old"  Sidearm jQuery UI tabs: #individual-overall-offensive, #individual-overall-defensive, #team

Output (repo-relative):
  docs/volleyball/<slug>.json
  docs/volleyball/non_conf_wvball.json   (only when scrapers/volleyball/non_conf_config.json has a stats_url)
  debug/<slug>_*.html|png                (only with --debug, when a scrape comes back empty)
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import logging
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
DEBUG_DIR = REPO_ROOT / "debug"
NON_CONF_CONFIG_PATH = SCRIPT_DIR / "non_conf_config.json"

CONCURRENCY_LIMIT = 4
DEBUG_MODE = False
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ── Teams Registry ───────────────────────────────────────────────────────────
def _team(name: str, slug: str, style: str, stats_url: str, roster_url: str) -> Dict[str, Any]:
    return {
        "name": name, "slug": slug, "gender": "women", "conference": "Summit League",
        "style": style, "stats_url": stats_url, "roster_url": roster_url,
    }

TEAMS_CONFIG: List[Dict[str, Any]] = [
    _team("St. Thomas", "st_thomas_wvball", "new",
          "https://tommiesports.com/sports/wvball/stats/2026",
          "https://tommiesports.com/sports/wvball/roster/print"),
    _team("South Dakota State", "south_dakota_state_wvball", "new",
          "https://gojacks.com/sports/womens-volleyball/stats/2026",
          "https://gojacks.com/sports/womens-volleyball/roster/print"),
    _team("Omaha", "omaha_wvball", "new",
          "https://omahamavs.com/sports/womens-volleyball/stats/2026",
          "https://omahamavs.com/sports/womens-volleyball/roster/print"),
    _team("North Dakota State", "north_dakota_state_wvball", "new",
          "https://gobison.com/sports/womens-volleyball/stats/2026",
          "https://gobison.com/sports/womens-volleyball/roster/print"),
    _team("Kansas City", "kansas_city_wvball", "new",
          "https://kcroos.com/sports/womens-volleyball/stats/2026",
          "https://kcroos.com/sports/womens-volleyball/roster/print"),
    _team("South Dakota", "south_dakota_wvball", "old",
          "https://goyotes.com/sports/womens-volleyball/stats/2026",
          "https://goyotes.com/sports/womens-volleyball/roster?print=true"),
    _team("Oral Roberts", "oral_roberts_wvball", "old",
          "https://oruathletics.com/sports/wvball/stats",
          "https://oruathletics.com/sports/wvball/roster?print=true"),
    _team("North Dakota", "north_dakota_wvball", "old",
          "https://fightinghawks.com/sports/womens-volleyball/stats",
          "https://fightinghawks.com/sports/womens-volleyball/roster?print=true"),
]

TEAM_STAT_KEYS = ["hit_pct", "k_per_s", "a_per_s", "sa_per_s", "dig_per_s", "blk_per_s"]

OFFENSE_KEYS = {"mp", "sp", "pts", "pts_per_s", "k", "k_per_s", "e", "ta", "pct",
                "a", "a_per_s", "sa", "sa_per_s", "se"}
DEFENSE_KEYS = {"dig", "dig_per_s", "re", "bs", "ba", "blk", "blk_per_s", "be", "bhe"}

DISCARD_TERMS = {
    "team", "tm", "total", "totals", "opponent", "opponents", "opp",
    "season", "overall", "conference", "view bio", "image", "unassigned",
    "player", "player uniform", "player name", "-",
    # Match-by-match opponent names filter
    "kansas city", "north dakota", "north dakota st.", "omaha", "oral roberts",
    "south dakota", "south dakota st.", "st. thomas (mn)", "denver",
    "air force", "colorado st.", "drake", "uni", "utah valley", "wyoming",
    "iowa st.", "creighton", "minnesota", "wisconsin", "nebraska"
}

_POS_MAP = {
    "OH": "OH", "OUTSIDE": "OH", "OUTSIDEHITTER": "OH", "RS": "OPP", "OPP": "OPP",
    "RIGHTSIDE": "OPP", "RIGHTSIDEHITTER": "OPP", "OPPOSITE": "OPP",
    "MB": "MB", "MIDDLE": "MB", "MIDDLEBLOCKER": "MB", "MH": "MB", "MIDDLEHITTER": "MB",
    "S": "S", "SETTER": "S",
    "L": "L/DS", "DS": "L/DS", "LIBERO": "L/DS", "DEFENSIVESPECIALIST": "L/DS", "L/DS": "L/DS",
}

_CLASS_MAP = {
    "FR": "FR", "FRESHMAN": "FR", "FY": "FR", "FIRST": "FR", "1ST": "FR",
    "SO": "SO", "SOPHOMORE": "SO", "SECOND": "SO", "2ND": "SO",
    "JR": "JR", "JUNIOR": "JR", "THIRD": "JR", "3RD": "JR",
    "SR": "SR", "SENIOR": "SR", "FOURTH": "SR", "4TH": "SR",
    "GR": "GR", "GRAD": "GR", "GRADUATE": "GR", "GS": "GR", "FIFTH": "GR", "5TH": "GR",
}

# ── Cleaning & Formatting Helpers ────────────────────────────────────────────
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _num(val: Any) -> Optional[float]:
    if val in (None, ""):
        return None
    s = str(val).strip().replace(",", "").replace("−", "-").replace("–", "-").rstrip("%")
    try:
        return float(s)
    except Exception:
        return None

def clean_stat_int(val: Any) -> str:
    f = _num(val)
    return str(int(round(f))) if f is not None else "0"

def clean_stat_float(val: Any) -> str:
    f = _num(val)
    return f"{f:.2f}" if f is not None else "0.00"

def clean_stat_pct(val: Any) -> str:
    f = _num(val)
    if f is None:
        return ".000"
    if abs(f) > 1.0:
        f /= 100.0
    s = f"{f:.3f}"
    if s.startswith("-0."):
        return "-" + s[2:]
    return s[1:] if s.startswith("0.") else s

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
            text = f"{' '.join(parts[1:])} {parts[0]}"
    return " ".join(text.split()).strip()

def clean_number(raw_num: Any) -> str:
    if raw_num is None:
        return ""
    m = re.match(r"^(\d{1,2})$", str(raw_num).strip().replace("#", ""))
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
    if low.startswith(("at ", "vs ", "#")):
        return False
    if re.match(r"^[\d\.\-\s/%:]+$", low):
        return False
    return True

# ── Non-Conference Placeholder ───────────────────────────────────────────────
def load_non_conf_config() -> List[Dict[str, Any]]:
    if not NON_CONF_CONFIG_PATH.exists():
        logger.info(f"{NON_CONF_CONFIG_PATH.name} not found - skipping non-conference slot")
        return []
    try:
        cfg = json.loads(NON_CONF_CONFIG_PATH.read_text(encoding="utf-8"))
        info = None
        if isinstance(cfg, dict):
            for key in ("wvball", "volleyball", "women"):
                if isinstance(cfg.get(key), dict):
                    info = cfg[key]
                    break
        if not info or not info.get("stats_url"):
            logger.info("Non-conf volleyball slot has no stats_url - skipping")
            return []
        style = str(info.get("style") or "new").strip().lower()
        if style not in ("new", "old"):
            style = "new"
        team = _team(info.get("name") or "Non-Conf Opponent", "non_conf_wvball", style,
                     info["stats_url"], info.get("roster_url") or "")
        team["conference"] = "Non-Conference"
        logger.info(f"Loaded non-conf volleyball opponent: {team['name']}")
        return [team]
    except Exception as e:
        logger.warning(f"Could not load {NON_CONF_CONFIG_PATH}: {e}")
        return []

# ── JSON File Management & Timestamp Tracking ────────────────────────────────
def read_existing_json(slug: str) -> Dict[str, Any]:
    try:
        with open(OUT_DIR / f"{slug}.json", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _strip_volatile(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in d.items() if k not in ("generated_at", "updated_at", "last_scan", "last_updated")}

def _write(slug: str, data: Dict[str, Any]) -> None:
    with open(OUT_DIR / f"{slug}.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def write_team_json(slug: str, team_data: Dict[str, Any]) -> Dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    existing = read_existing_json(slug)
    same_source = existing.get("source_stats_url") == team_data.get("source_stats_url")
    name = team_data.get("team", slug)

    if not team_data.get("players") and existing.get("players") and same_source:
        logger.warning(f"[{name}] No players scraped - keeping existing {slug}.json")
        return existing

    if same_source and isinstance(existing.get("team_stats"), dict):
        for k in TEAM_STAT_KEYS:
            if team_data["team_stats"].get(k, "-") == "-" and existing["team_stats"].get(k, "-") != "-":
                team_data["team_stats"][k] = existing["team_stats"][k]
                logger.warning(f"[{name}] team stat '{k}' missing - kept previous value")

    now = now_iso()
    team_data["last_scan"] = now

    if existing and same_source and _strip_volatile(existing) == _strip_volatile(team_data):
        team_data["last_updated"] = existing.get("last_updated") or existing.get("generated_at") or now
        team_data["generated_at"] = team_data["last_updated"]
        _write(slug, team_data)
        logger.info(f"[{name}] Stats unchanged - updated last_scan ({now[:16]})")
        return team_data

    team_data["last_updated"] = now
    team_data["generated_at"] = now
    _write(slug, team_data)
    logger.info(f"[{name}] Fresh stats found! Updated last_updated ({now[:16]})")
    return team_data

async def dump_debug(page: Optional[Page], slug: str, tag: str) -> None:
    if not DEBUG_MODE or page is None:
        return
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        (DEBUG_DIR / f"{slug}_{tag}.html").write_text(await page.content(), encoding="utf-8")
        await page.screenshot(path=str(DEBUG_DIR / f"{slug}_{tag}.png"), full_page=True)
        logger.info(f"[{slug}] debug snapshot saved: debug/{slug}_{tag}.*")
    except Exception as e:
        logger.debug(f"[{slug}] debug snapshot failed: {e}")

# ── Roster Resolution ────────────────────────────────────────────────────────
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
            await page.wait_for_selector("table, [class*='roster-player'], [class*='s-person-card'], .sidearm-roster-player", timeout=6000)
        except Exception:
            pass
        soup = BeautifulSoup(await page.content(), "html.parser")
        for row in soup.find_all("tr"):
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 3:
                continue
            num, pos, yr, name_cand = "", "", "", ""
            for col in cells:
                norm_pos, norm_yr = normalize_position(col), normalize_year(col)
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

# ── Header Mapping & Table Parsing ───────────────────────────────────────────
HEADER_MAP = {
    "#": "number", "NO": "number", "NUM": "number", "UNIFORM": "number",
    "PLAYER": "name", "NAME": "name", "PLAYERNAME": "name",
    "MP": "mp", "GP": "mp", "SP": "sp", "SETS": "sp",
    "PTS": "pts", "PTS/S": "pts_per_s", "PTS/SET": "pts_per_s",
    "K": "k", "KILLS": "k", "K/S": "k_per_s", "K/SET": "k_per_s",
    "E": "e", "ERRORS": "e",
    "TA": "ta", "TOTALATTACKS": "ta", "ATTEMPTS": "ta",
    "PCT": "pct", "HIT%": "pct", "ATTACK%": "pct",
    "A": "a", "AST": "a", "ASSISTS": "a", "A/S": "a_per_s", "AST/S": "a_per_s", "A/SET": "a_per_s",
    "SA": "sa", "ACES": "sa", "SA/S": "sa_per_s", "SA/SET": "sa_per_s",
    "SE": "se",
    "DIG": "dig", "DIGS": "dig", "D": "dig", "DIG/S": "dig_per_s", "DIG/SET": "dig_per_s",
    "RE": "re",
    "BS": "bs", "SOLO": "bs", "BLOCKSOLOS": "bs",
    "BA": "ba", "BLOCKASSISTS": "ba",
    "BLK": "blk", "TB": "blk", "TOTALBLOCKS": "blk", "BLK/S": "blk_per_s", "BLK/SET": "blk_per_s",
    "BE": "be", "BHE": "bhe",
}

def map_header(text: str) -> Optional[str]:
    return HEADER_MAP.get(re.sub(r"[^A-Z0-9%/#]", "", text.strip().upper()))

def _header_rows(table) -> List[Any]:
    thead = table.find("thead")
    if thead:
        rows = thead.find_all("tr")
        if rows:
            return rows
    header_rows: List[Any] = []
    for r in table.find_all("tr"):
        cells = r.find_all(["th", "td"])
        if cells and all(c.name == "th" for c in cells):
            header_rows.append(r)
        else:
            break
    if not header_rows:
        first = table.find("tr")
        if first:
            header_rows = [first]
    return header_rows

def table_header_labels(table) -> Tuple[List[str], List[Any]]:
    header_rows = _header_rows(table)
    grid: Dict[Tuple[int, int], str] = {}
    max_col = 0
    for ri, r in enumerate(header_rows):
        col = 0
        for c in r.find_all(["th", "td"]):
            while (ri, col) in grid:
                col += 1
            try:
                cs = max(1, int(c.get("colspan", 1) or 1))
                rs = max(1, int(c.get("rowspan", 1) or 1))
            except ValueError:
                cs, rs = 1, 1
            text = c.get_text(" ", strip=True)
            for dr in range(rs):
                for dc in range(cs):
                    grid[(ri + dr, col + dc)] = text
            col += cs
        max_col = max(max_col, col)

    labels: List[str] = []
    for ci in range(max_col):
        label = ""
        for ri in range(len(header_rows) - 1, -1, -1):
            t = grid.get((ri, ci), "")
            if t:
                label = t
                break
        labels.append(label)

    tbody = table.find("tbody")
    if tbody:
        body_rows = tbody.find_all("tr")
    else:
        body_rows = [r for r in table.find_all("tr") if not any(r is h for h in header_rows)]
    return labels, body_rows

def _label_mapping(labels: List[str]) -> Dict[str, int]:
    mapping: Dict[str, int] = {}
    for idx, lab in enumerate(labels):
        key = map_header(lab) if lab else None
        if key and key not in mapping:
            mapping[key] = idx
    return mapping

def parse_player_table(table, allowed: set) -> Dict[str, Dict[str, Any]]:
    """Parse player stats table by column mapping and data-label attributes."""
    players: Dict[str, Dict[str, Any]] = {}
    labels, body_rows = table_header_labels(table)
    mapping = _label_mapping(labels)

    for r in body_rows:
        if r.find_parent("tfoot"):
            continue
        cells = r.find_all(["td", "th"])
        if len(cells) < 3:
            continue

        raw_name = ""
        raw_num = ""

        # Check Sidearm DataTables structure: anchor or button inside second cell
        name_tag = (
            r.find("a", attrs={"data-player-id": True}) or
            r.find("a", class_="hide-on-medium-down") or
            r.find("a", href=re.compile(r"roster", re.I)) or
            r.find("button", class_="hide-on-large")
        )
        if name_tag:
            raw_name = name_tag.get_text(" ", strip=True)

        # First cell is jersey number in Sidearm old style
        first_td = cells[0].get_text(" ", strip=True)
        if first_td.isdigit():
            raw_num = first_td

        # Fallback to column mapping if not found
        cell_texts = [c.get_text(" ", strip=True) for c in cells]
        if not raw_name:
            if "name" in mapping and mapping["name"] < len(cell_texts):
                raw_name = cell_texts[mapping["name"]]
            elif len(cell_texts) > 1:
                raw_name = cell_texts[1]

        if not raw_num and "number" in mapping and mapping["number"] < len(cell_texts):
            raw_num = cell_texts[mapping["number"]]

        c_name = clean_name(raw_name)
        if not is_valid_player_candidate(c_name):
            continue

        p = players.setdefault(c_name.lower(), {"name": c_name, "number": clean_number(raw_num)})

        # Method A: Parse by data-label attributes (Sidearm Old Style DataTables)
        for td in cells:
            d_lbl = td.get("data-label")
            if d_lbl:
                k = map_header(d_lbl)
                if k and k in allowed:
                    p[k] = td.get_text(" ", strip=True)

        # Method B: Parse by mapped column index
        for stat_k, col_idx in mapping.items():
            if stat_k in allowed and col_idx < len(cell_texts) and stat_k not in p:
                p[stat_k] = cell_texts[col_idx]

    return players

def merge_into(target: Dict[str, Dict[str, Any]], found: Dict[str, Dict[str, Any]]) -> None:
    for key, p in found.items():
        target.setdefault(key, {}).update(p)

# ── Team Stats Parsers ───────────────────────────────────────────────────────
def parse_team_stats_from_rows(soup: BeautifulSoup, found: Dict[str, str]) -> Dict[str, str]:
    """Parse team stats rows containing label and value across Sidearm New & Old layouts."""
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            
            # First cell label (handles spans inside td)
            first_c = row.find(["td", "th"])
            full_label = first_c.get_text(" ", strip=True).lower() if first_c else cells[0].strip().lower()
            val = cells[1].strip()

            if any(term in full_label for term in ("attack percent", "hitting percentage", "percent", "att%")) and "hit_pct" not in found:
                found["hit_pct"] = clean_stat_pct(val)
            elif any(term in full_label for term in ("kills per set", "k/s")) and "k_per_s" not in found:
                found["k_per_s"] = clean_stat_float(val)
            elif any(term in full_label for term in ("assists per set", "ast/s", "a/s")) and "a_per_s" not in found:
                found["a_per_s"] = clean_stat_float(val)
            elif any(term in full_label for term in ("aces per set", "service aces per set", "sa/s")) and "sa_per_s" not in found:
                found["sa_per_s"] = clean_stat_float(val)
            elif any(term in full_label for term in ("digs per set", "dig/s")) and "dig_per_s" not in found:
                found["dig_per_s"] = clean_stat_float(val)
            elif any(term in full_label for term in ("blocks per set", "blk/s")) and "blk_per_s" not in found:
                found["blk_per_s"] = clean_stat_float(val)
    return found

def parse_team_stats_match_by_match(soup: BeautifulSoup) -> Dict[str, str]:
    """Fallback: compute per-set team stats from Match-By-Match totals row."""
    for table in soup.find_all("table"):
        labels, body_rows = table_header_labels(table)
        m = _label_mapping(labels)
        if "name" in m or not {"sp", "k", "ta", "pct"} <= set(m):
            continue

        rows = [[c.get_text(" ", strip=True) for c in r.find_all(["td", "th"])] for r in body_rows]
        opp_idx = next((i for i, r in enumerate(rows) if r and r[0].strip().lower() in ("opponents", "opponent")), None)
        if opp_idx is None or opp_idx < 1:
            continue
        team_cells = rows[opp_idx - 1]

        def val(key: str) -> Optional[float]:
            if key not in m:
                return None
            pos = len(team_cells) - (len(labels) - m[key])
            return _num(team_cells[pos]) if 0 <= pos < len(team_cells) else None

        sp = val("sp")
        if not sp:
            continue
        out = {k: "-" for k in TEAM_STAT_KEYS}
        pct_raw = val("pct")
        if pct_raw is not None:
            out["hit_pct"] = clean_stat_pct(pct_raw)
        for stat, key in (("k_per_s", "k"), ("a_per_s", "a"), ("sa_per_s", "sa"),
                          ("dig_per_s", "dig"), ("blk_per_s", "blk")):
            v = val(key)
            if v is not None:
                out[stat] = f"{v / sp:.2f}"
        return out
    return {k: "-" for k in TEAM_STAT_KEYS}

# ── Playwright UI Helpers ────────────────────────────────────────────────────
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

async def switch_view_dropdown(page: Page, target_text: str) -> bool:
    try:
        box = None
        boxes = page.locator("button[role='combobox'], button[data-test-id='s-select__box']")
        for i in range(await boxes.count()):
            if await boxes.nth(i).is_visible():
                box = boxes.nth(i)
                break
        if box is None:
            return False

        if target_text.lower() in (await box.inner_text()).strip().lower():
            return True

        await box.scroll_into_view_if_needed()
        await box.click()
        await page.wait_for_timeout(350)

        clicked = False
        for opt in (
            page.locator(f"[role='listbox'] [role='option']:has-text('{target_text}')").first,
            page.locator(f"[role='option']:has-text('{target_text}')").first,
            page.locator(f"li:has-text('{target_text}')").first,
        ):
            if await opt.is_visible():
                await opt.click(force=True)
                clicked = True
                break

        if not clicked:
            clicked = await page.evaluate(
                """(t) => {
                    const els = Array.from(document.querySelectorAll('[role="option"], li'));
                    const el = els.find(e => (e.textContent || '').toLowerCase().includes(t.toLowerCase()));
                    if (el) { el.click(); return true; }
                    return false;
                }""", target_text)
        await page.wait_for_timeout(500)
        return bool(clicked)
    except Exception as e:
        logger.debug(f"Dropdown switch to '{target_text}' failed: {e}")
        return False

async def wait_for_view(page: Page, selector: str, what: str, label: str) -> bool:
    try:
        await page.wait_for_selector(selector, state="visible", timeout=6000)
        return True
    except Exception:
        logger.warning(f"[{label}] '{what}' view did not appear - values for it may be missing")
        return False

# ── Shared Finalize ──────────────────────────────────────────────────────────
def build_and_write(team_cfg: Dict[str, Any], site_style: str, player_map: Dict[str, Dict[str, Any]],
                    team_stats: Dict[str, str], roster_lookup: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    slug, label = team_cfg["slug"], team_cfg["name"]
    merged: List[Dict[str, str]] = []
    for p in player_map.values():
        name = clean_name(p.get("name", ""))
        if not is_valid_player_candidate(name):
            continue
        num = clean_number(p.get("number", ""))
        r = find_roster_entry(name, num, roster_lookup)
        merged.append({
            "number": num or (r.get("number", "") if r else ""),
            "name": (r.get("name") if r else "") or name,
            "position": (r.get("position") if r else "") or "",
            "year": (r.get("year") if r else "") or "",
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
        })
    merged.sort(key=lambda x: int(x["number"]) if x["number"].isdigit() else 999)

    final_ts = {k: team_stats.get(k, "-") for k in TEAM_STAT_KEYS}
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
        "site_style": site_style,
        "team_stats": final_ts,
        "players": merged,
    }
    with_defense = sum(1 for p in merged if p["dig"] != "0" or p["blk"] != "0.00")
    logger.info(f"[{label}] {len(merged)} players ({with_defense} with defensive stats), "
                f"team stats: {sum(1 for v in final_ts.values() if v != '-')}/{len(TEAM_STAT_KEYS)}")
    return write_team_json(slug, team_data)

# ── NEW STYLE Pipeline (Sidearm Nuxt/Vue) ────────────────────────────────────
async def scrape_new_style(context: BrowserContext, team_cfg: Dict[str, Any],
                           semaphore: asyncio.Semaphore) -> Dict[str, Any]:
    async with semaphore:
        slug, label = team_cfg["slug"], team_cfg["name"]
        logger.info(f"Processing {label} (Women's Volleyball, new style)...")
        roster_task = asyncio.create_task(fetch_roster(context, team_cfg.get("roster_url", ""), label))
        player_map: Dict[str, Dict[str, Any]] = {}
        team_stats: Dict[str, str] = {}

        page: Optional[Page] = None
        try:
            page = await context.new_page()
            await page.goto(team_cfg["stats_url"], wait_until="domcontentloaded", timeout=35000)
            try:
                await page.wait_for_selector("table, button[role='tab']", timeout=8000)
            except Exception:
                pass

            # Player Stats -> Offensive
            await click_main_tab(page, "Player Stats")
            await switch_view_dropdown(page, "Offensive")
            await wait_for_view(page, "th:has-text('PCT')", "Offensive", label)
            soup = BeautifulSoup(await page.content(), "html.parser")
            for tbl in soup.find_all("table"):
                merge_into(player_map, parse_player_table(tbl, OFFENSE_KEYS))

            # Player Stats -> Defensive
            await switch_view_dropdown(page, "Defensive")
            await wait_for_view(page, "th:has-text('DIG')", "Defensive", label)
            soup = BeautifulSoup(await page.content(), "html.parser")
            for tbl in soup.find_all("table"):
                merge_into(player_map, parse_player_table(tbl, DEFENSE_KEYS))

            # Team Stats -> Totals, then Per Set
            await click_main_tab(page, "Team Stats")
            await switch_view_dropdown(page, "Totals")
            await wait_for_view(page, "td:text-is('Percent')", "Team Totals", label)
            parse_team_stats_from_rows(BeautifulSoup(await page.content(), "html.parser"), team_stats)

            await switch_view_dropdown(page, "Per Set")
            await wait_for_view(page, "td:text-is('Kills Per Set')", "Team Per Set", label)
            parse_team_stats_from_rows(BeautifulSoup(await page.content(), "html.parser"), team_stats)

            if not player_map or not team_stats:
                await dump_debug(page, slug, "new_style_empty")
        except Exception as e:
            logger.error(f"[{label}] Error scraping stats: {e}")
        finally:
            if page:
                await page.close()

        return build_and_write(team_cfg, "New Style (Sidearm Nuxt/Vue)", player_map, team_stats, await roster_task)

# ── OLD STYLE Pipeline (Sidearm jQuery UI tabs) ──────────────────────────────
async def scrape_old_style(context: BrowserContext, team_cfg: Dict[str, Any],
                           semaphore: asyncio.Semaphore) -> Dict[str, Any]:
    async with semaphore:
        slug, label = team_cfg["slug"], team_cfg["name"]
        logger.info(f"Processing {label} (Women's Volleyball, old style)...")
        roster_task = asyncio.create_task(fetch_roster(context, team_cfg.get("roster_url", ""), label))
        player_map: Dict[str, Dict[str, Any]] = {}
        team_stats: Dict[str, str] = {}

        page: Optional[Page] = None
        try:
            page = await context.new_page()
            await page.goto(team_cfg["stats_url"], wait_until="domcontentloaded", timeout=35000)
            try:
                await page.wait_for_selector(
                    "section#individual-overall-offensive, section#individual-overall-defensive, section#team, table",
                    timeout=10000
                )
            except Exception:
                pass

            # 1. Parse Offensive & Defensive tables directly from DOM sections
            soup = BeautifulSoup(await page.content(), "html.parser")

            # Offensive Section (#individual-overall-offensive)
            off_sec = soup.find(id=re.compile(r"individual-overall-offensive|individual-offensive", re.I))
            if off_sec:
                for tbl in off_sec.find_all("table"):
                    merge_into(player_map, parse_player_table(tbl, OFFENSE_KEYS))

            # Defensive Section (#individual-overall-defensive)
            def_sec = soup.find(id=re.compile(r"individual-overall-defensive|individual-defensive", re.I))
            if def_sec:
                for tbl in def_sec.find_all("table"):
                    merge_into(player_map, parse_player_table(tbl, DEFENSE_KEYS))

            # If section IDs weren't matched, parse tables with matching captions
            if not player_map:
                for tbl in soup.find_all("table"):
                    cap = tbl.find("caption")
                    cap_text = cap.get_text(" ", strip=True).lower() if cap else ""
                    if "match" in cap_text or "game" in cap_text or "conference" in cap_text:
                        continue
                    if "defensive" in cap_text:
                        merge_into(player_map, parse_player_table(tbl, DEFENSE_KEYS))
                    elif "offensive" in cap_text:
                        merge_into(player_map, parse_player_table(tbl, OFFENSE_KEYS))

            # 2. Parse Team Stats from #team section
            team_sec = soup.find(id=re.compile(r"^team$", re.I)) or soup
            parse_team_stats_from_rows(team_sec, team_stats)

            # Fallback for any missing team stats: calculate from Match-by-match totals
            if len(team_stats) < len(TEAM_STAT_KEYS):
                mbm_stats = parse_team_stats_match_by_match(soup)
                for k, v in mbm_stats.items():
                    if k not in team_stats or team_stats[k] == "-":
                        team_stats[k] = v

            if not player_map or all(v == "-" for v in team_stats.values()):
                logger.warning(f"[{label}] old-style parse incomplete - dumping debug")
                await dump_debug(page, slug, "old_style_incomplete")
        except Exception as e:
            logger.error(f"[{label}] Error scraping stats: {e}")
        finally:
            if page:
                await page.close()

        return build_and_write(team_cfg, "Old Style (jQuery UI tabs)", player_map, team_stats, await roster_task)

# ── Per-Team Context + Dispatcher ────────────────────────────────────────────
async def make_context(browser: Browser) -> BrowserContext:
    context = await browser.new_context(user_agent=USER_AGENT)
    await context.route(
        "**/*",
        lambda route: route.abort()
        if route.request.resource_type in {"image", "media", "font"}
        else route.continue_()
    )
    return context

async def scrape_team(browser: Browser, team_cfg: Dict[str, Any], semaphore: asyncio.Semaphore) -> Dict[str, Any]:
    context = await make_context(browser)
    try:
        if team_cfg.get("style") == "old":
            return await scrape_old_style(context, team_cfg, semaphore)
        return await scrape_new_style(context, team_cfg, semaphore)
    finally:
        await context.close()

# ── Main Runner ──────────────────────────────────────────────────────────────
async def run_scraper(only: Optional[List[str]], debug: bool) -> None:
    global DEBUG_MODE
    DEBUG_MODE = debug
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

    teams = list(TEAMS_CONFIG) + load_non_conf_config()
    if only:
        wanted = [o.strip().lower() for o in only if o.strip()]
        teams = [t for t in teams if any(w in t["name"].lower() or w in t["slug"].lower() for w in wanted)]

    logger.info(f"=== Scraping {len(teams)} volleyball team(s) -> {OUT_DIR} ===")
    start = time.time()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        results = await asyncio.gather(*[scrape_team(browser, t, semaphore) for t in teams], return_exceptions=True)
        await browser.close()

    for t, res in zip(teams, results):
        if isinstance(res, Exception):
            logger.error(f"[{t['name']}] scrape failed: {res!r}")
    logger.info(f"Finished volleyball scraping in {time.time() - start:.1f}s")

def main() -> None:
    ap = argparse.ArgumentParser(description="Summit League Volleyball Stats Scraper")
    ap.add_argument("--only", help='Comma-separated team names/slugs, e.g. "Oral Roberts,north_dakota"')
    ap.add_argument("--debug", action="store_true", help="Save HTML + screenshot to debug/ when a scrape is empty")
    args = ap.parse_args()
    asyncio.run(run_scraper(only=args.only.split(",") if args.only else None, debug=args.debug))

if __name__ == "__main__":
    main()
