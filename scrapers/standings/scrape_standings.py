#!/usr/bin/env python3
"""
scrapers/standings/scrape_standings.py
Fetch standings and national polls from Summit League, PFL, NCHC, WCHA, and USCHO.

Extracts:
  - Sports Standings: Team Name, Acronym, Conference W-L, Overall W-L, CPTS/PTS, GP, and OW-OL-SW
  - USCHO Polls: Rank, Team Name, Record, Pts + First-Place Votes formatted as "123 (12)",
    Last Poll rank, and Poll movement (up/down/same/new).

Output (repo-relative, resolved from this script's location):
  docs/standings/data.json

Optimizations & safeguards applied:
  1. Full async concurrency across feeds via asyncio.Semaphore(4) + asyncio.gather.
  2. Static HTTP fast-path via urllib.request before launching browser rendering.
  3. Playwright resource routing (aborts image, media, font downloads).
  4. Selector-driven DOM waits replacing long static timeouts.
  5. Last-good-data preservation: individual feed failures keep stale data so the site never empties.
  6. Smart cache & git-diff preservation: avoids rewriting identical JSON (<18h old).

Usage:
  python scrapers/standings/scrape_standings.py
  python scrapers/standings/scrape_standings.py --out docs/standings/data.json
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup
from playwright.async_api import BrowserContext, Page, async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("standings")

# ── Dynamic Path Resolution ──────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1] if len(SCRIPT_DIR.parents) >= 2 else SCRIPT_DIR
DEFAULT_OUT = REPO_ROOT / "docs" / "standings" / "data.json"

CONCURRENCY_LIMIT = 4
MAX_CACHE_HOURS = 18

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

FETCH_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ── Team Acronym Translation Map (for Standings & Polls) ──────────────────────
TEAM_ACRONYMS = {
    # ── Summit League ─────────────────────────────────────────────────────────
    "North Dakota":             "UND",
    "North Dakota State":       "NDSU",
    "South Dakota":             "USD",
    "South Dakota State":       "SDSU",
    "Kansas City":              "UMKC",
    "Omaha":                    "OMA",
    "Nebraska Omaha":           "OMA",
    "St. Thomas":               "UST",
    "St. Thomas (MN)":          "UST",
    "St. Thomas (Minn.)":       "UST",
    "University of St. Thomas": "UST",
    "Oral Roberts":             "ORU",

    # ── Other Summit League / Common Opponents ────────────────────────────────
    "UMass":                    "UM",
    "Massachusetts":            "UM",
    "Delaware":                 "UD",
    "Northern Colorado":        "UNC",

    # ── Pioneer Football League (PFL) ─────────────────────────────────────────
    "Dayton":                   "DAY",
    "Butler":                   "BUT",
    "Marist":                   "MARI",
    "Presbyterian":             "PC",
    "Valparaiso":               "VALPO",
    "San Diego":                "SAND",
    "Drake":                    "DRA",
    "Davidson":                 "DC",
    "Morehead State":           "MOSU",
    "Stetson":                  "STET",

    # ── NCHC Men's Ice Hockey ─────────────────────────────────────────────────
    "Arizona State":            "ASU",
    "Colorado College":         "CC",
    "Denver":                   "DU",
    "Miami":                    "MIA",
    "Miami (OH)":               "MIA",
    "Minnesota Duluth":         "UMD",
    "St. Cloud State":          "SCSU",
    "Western Michigan":         "WMU",

    # ── Default Fallbacks for Other Ice Hockey Programs ───────────────────────
    "Ohio State":               "OSU",
    "Minnesota":                "MINN",
    "Wisconsin":                "WIS",
    "Bemidji State":            "BSU",
    "Minnesota State":          "MSU",
}

# ── WCHA-Specific Acronym Overrides (Applies ONLY to WCHA Standings) ──────────
WCHA_TEAM_ACRONYMS = {
    "Ohio State":       "OHIOST",
    "Minnesota":        "MINNES",
    "St. Cloud State":  "STCLOU",
    "Minnesota Duluth": "MNDULU",
    "St. Thomas":       "STTHMN",
    "Wisconsin":        "WISC",
    "Bemidji State":    "BEMIDJI",
    "Minnesota State":  "MNSTMK",
}

# ── Target Configuration ──────────────────────────────────────────────────────
FEEDS_CONFIG: List[Dict[str, Any]] = [
    # ── Fall Sports ───────────────────────────────────────────────────────────
    {
        "id": "wsoc",
        "name": "Summit League Women's Soccer",
        "type": "standings",
        "url": "https://thesummitleague.org/standings.aspx?path=wsoc",
        "point_col": "cpts",
        "skip_pods": True,
    },
    {
        "id": "msoc",
        "name": "Summit League Men's Soccer",
        "type": "standings",
        "url": "https://thesummitleague.org/standings.aspx?path=msoc",
        "point_col": "cpts",
        "skip_pods": False,
    },
    {
        "id": "pfl",
        "name": "Pioneer Football League",
        "type": "standings",
        "url": "https://pioneer-football.org/standings.aspx?standings=19",
        "point_col": None,
        "skip_pods": False,
    },
    {
        "id": "wvball",
        "name": "Summit League Women's Volleyball",
        "type": "standings",
        "url": "https://thesummitleague.org/standings.aspx?path=wvball",
        "point_col": None,
        "skip_pods": False,
    },
    # ── Winter Sports & Polls ─────────────────────────────────────────────────
    {
        "id": "whockey",
        "name": "WCHA Women's Ice Hockey",
        "type": "standings",
        "url": "https://wcha.com/standings.aspx?path=whockey",
        "point_col": "pts",
        "skip_pods": False,
        "acronym_overrides": WCHA_TEAM_ACRONYMS,
    },
    {
        "id": "uscho_womens_poll",
        "name": "USCHO Women's Division I Ice Hockey Poll",
        "type": "poll",
        "url": "https://www.uscho.com/rankings/d-i-womens-poll",
    },
    {
        "id": "mhockey",
        "name": "NCHC Men's Ice Hockey",
        "type": "standings",
        "url": "https://nchchockey.com/standings.aspx?path=mhockey",
        "point_col": "pts",
        "skip_pods": False,
        "include_gp": True,
        "include_ow_ol_sw": True,
    },
    {
        "id": "uscho_mens_poll",
        "name": "USCHO Men's Division I Ice Hockey Poll",
        "type": "poll",
        "url": "https://www.uscho.com/rankings/d-i-mens-poll",
    },
    {
        "id": "wbball",
        "name": "Summit League Women's Basketball",
        "type": "standings",
        "url": "https://thesummitleague.org/standings.aspx?path=wbball",
        "point_col": None,
        "skip_pods": False,
    },
    {
        "id": "mbball",
        "name": "Summit League Men's Basketball",
        "type": "standings",
        "url": "https://thesummitleague.org/standings.aspx?path=mbball",
        "point_col": None,
        "skip_pods": False,
    },
    # ── Spring Sports ─────────────────────────────────────────────────────────
    {
        "id": "softball",
        "name": "Summit League Softball",
        "type": "standings",
        "url": "https://thesummitleague.org/standings.aspx?path=softball",
        "point_col": None,
        "skip_pods": False,
    },
    {
        "id": "baseball",
        "name": "Summit League Baseball",
        "type": "standings",
        "url": "https://thesummitleague.org/standings.aspx?path=baseball",
        "point_col": None,
        "skip_pods": False,
    },
]

# ── Helpers ───────────────────────────────────────────────────────────────────
def clean(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text) if text is not None else "").strip()

def get_team_acronym(team_name: str, overrides: Optional[Dict[str, str]] = None) -> str:
    cleaned = clean(team_name)
    active_map = {**TEAM_ACRONYMS, **(overrides or {})}

    if cleaned in active_map:
        return active_map[cleaned]

    cleaned_lower = cleaned.lower()
    for full_name, acronym in active_map.items():
        if full_name.lower() == cleaned_lower:
            return acronym

    for full_name, acronym in active_map.items():
        if full_name.lower() in cleaned_lower:
            return acronym

    words = [w for w in re.split(r"[\s\-\.]+", cleaned) if w and w[0].isupper()]
    if words:
        return "".join(w[0] for w in words).upper()
    return cleaned[:4].upper()

def header_cells(table: Any) -> List[str]:
    row = table.select_one("thead tr:last-of-type") or table.find("tr")
    return [clean(c.get_text()) for c in row.find_all(["th", "td"])] if row else []

def body_rows(table: Any) -> List[Any]:
    rows = table.select("tbody tr") or table.find_all("tr")[1:]
    return [r for r in rows if r.find_all(["td", "th"])]

def team_text(cell: Any) -> str:
    link = cell.find("a")
    return clean((link or cell).get_text())

def is_pods_table(table: Any) -> bool:
    hdrs = [h.lower() for h in header_cells(table)]
    if any("pod" in h or "group" in h for h in hdrs):
        return True
    prev = table.find_previous(["h2", "h3", "h4", "caption", "th"])
    if prev and "pod" in prev.get_text().lower():
        return True
    if len(body_rows(table)) < 6:
        return True
    return False

# ── Sidearm Standings Table Parser ────────────────────────────────────────────
def parse_standings_table(html: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    point_col_type = meta.get("point_col")
    skip_pods = meta.get("skip_pods", False)
    overrides = meta.get("acronym_overrides", None)
    include_gp = meta.get("include_gp", False)
    include_ow_ol_sw = meta.get("include_ow_ol_sw", False)

    target = None

    divisionless = soup.select_one("table.table-divisionless, .table-divisionless table")
    if divisionless:
        target = divisionless

    if target is None:
        candidate_tables = [
            t for t in soup.find_all("table")
            if any(
                "team" in h.lower() or "school" in h.lower() or "member" in h.lower()
                for h in header_cells(t)
            )
        ]

        if not candidate_tables:
            raise ValueError(f"No standings table found for {meta['name']}")

        if skip_pods:
            non_pod_tables = [t for t in candidate_tables if not is_pods_table(t)]
            if non_pod_tables:
                target = max(non_pod_tables, key=lambda t: len(body_rows(t)))
            else:
                target = max(candidate_tables, key=lambda t: len(body_rows(t)))
        else:
            target = candidate_tables[0]

    if target is None:
        raise ValueError(f"Could not identify standings table for {meta['name']}")

    headers = header_cells(target)
    hdrs_lower = [h.lower() for h in headers]

    def idx(*candidates: str) -> Optional[int]:
        for c in candidates:
            for i, h in enumerate(hdrs_lower):
                if c in h:
                    return i
        return None

    def exact_idx(*candidates: str) -> Optional[int]:
        for c in candidates:
            for i, h in enumerate(hdrs_lower):
                if h == c:
                    return i
        return None

    team_idx = exact_idx("team", "school", "member", "institution", "name") or idx("team", "school", "member", "institution", "name")

    conf_combined = exact_idx("conf", "conference", "league", "wcha", "nchc")
    if conf_combined is None:
        conf_combined = idx("conf record", "conference record", "wcha record", "nchc record", "conf. w-l-t", "conf w-l-t")
    conf_w_idx = idx("conf w", "cw", "conference w", "wcha w") if conf_combined is None else None
    conf_l_idx = idx("conf l", "cl", "conference l", "wcha l") if conf_combined is None else None

    ovr_combined = exact_idx("overall", "ovr", "total")
    if ovr_combined is None:
        ovr_combined = idx("overall record", "ovr record", "total record", "overall w-l-t", "ovr w-l-t")
    ovr_w_idx = idx("overall w", "ow", "ovr w") if ovr_combined is None else None
    ovr_l_idx = idx("overall l", "ol", "ovr l") if ovr_combined is None else None

    pts_idx = None
    if point_col_type == "cpts":
        pts_idx = exact_idx("cpts", "conf pts", "pts", "points") or idx("cpts", "conf pts")
    elif point_col_type == "pts":
        pts_idx = exact_idx("pts", "points", "total pts", "tp", "wcha pts", "nchc pts") or idx("pts", "point")

    gp_idx = None
    if include_gp:
        gp_idx = exact_idx("conf. gp", "conf gp", "cgp") or idx("conf. gp", "conf gp") or exact_idx("gp")

    ow_ol_idx = None
    sw_idx = None
    if include_ow_ol_sw:
        ow_ol_idx = exact_idx("ow-ol", "ow/ol", "ow - ol") or idx("ow-ol", "ow/ol")
        sw_idx = exact_idx("sw", "so wins", "shootout w") or idx("sw", "shootout w")

    rows = []
    for tr in body_rows(target):
        cells = tr.find_all(["td", "th"])

        def cell_text(i: Optional[int]) -> str:
            if i is None or i >= len(cells):
                return ""
            return clean(cells[i].get_text())

        team = team_text(cells[team_idx]) if team_idx is not None else ""

        if not team or team.lower() in ("team", "school", "member", "institution") or team.lower().startswith("pod"):
            continue

        acronym = get_team_acronym(team, overrides=overrides)

        if conf_combined is not None:
            conf = cell_text(conf_combined).replace("–", "-") or "0-0"
        elif conf_w_idx is not None and conf_l_idx is not None:
            conf = f"{cell_text(conf_w_idx)}-{cell_text(conf_l_idx)}"
        else:
            conf = "—"

        if ovr_combined is not None:
            ovr = cell_text(ovr_combined).replace("–", "-") or "0-0"
        elif ovr_w_idx is not None and ovr_l_idx is not None:
            ovr = f"{cell_text(ovr_w_idx)}-{cell_text(ovr_l_idx)}"
        else:
            ovr = "—"

        row_data: Dict[str, Any] = {
            "rank": len(rows) + 1,
            "team": team,
            "acronym": acronym,
            "conf": conf,
            "ovr": ovr,
        }

        if include_gp and gp_idx is not None:
            row_data["gp"] = cell_text(gp_idx) or "0"

        if point_col_type == "cpts" and pts_idx is not None:
            row_data["cpts"] = cell_text(pts_idx) or "0"
        elif point_col_type == "pts" and pts_idx is not None:
            row_data["pts"] = cell_text(pts_idx) or "0"

        if include_ow_ol_sw:
            ow_ol_val = cell_text(ow_ol_idx).replace("–", "-") if ow_ol_idx is not None else "0-0"
            sw_val = cell_text(sw_idx) if sw_idx is not None else "0"
            ow_ol_val = ow_ol_val if ow_ol_val else "0-0"
            sw_val = sw_val if sw_val else "0"
            row_data["ow_ol_sw"] = f"{ow_ol_val}-{sw_val}"

        rows.append(row_data)

    if point_col_type == "cpts":
        rows.sort(key=lambda r: int(r.get("cpts", 0) or 0), reverse=True)
        for i, row in enumerate(rows, 1):
            row["rank"] = i
    elif point_col_type == "pts":
        rows.sort(key=lambda r: int(r.get("pts", 0) or 0), reverse=True)
        for i, row in enumerate(rows, 1):
            row["rank"] = i

    if not rows:
        raise ValueError(f"No team rows parsed for {meta['name']}")

    return {
        "id": meta["id"],
        "name": meta["name"],
        "source": meta["url"],
        "rows": rows,
    }

# ── USCHO Poll Table Parser (Targets Bootstrap-Vue #btable) ───────────────────
def parse_uscho_poll_table(html: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")

    poll_date = ""
    for heading in soup.find_all(["h1", "h2", "h3", ".page-title", ".poll-title"]):
        text = clean(heading.get_text())
        m = re.search(r"-\s*([A-Za-z]+\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?)", text)
        if m:
            poll_date = m.group(1).strip()
            break

    target = soup.find("table", id="btable")

    if target is None:
        for table in soup.find_all("table"):
            hdrs = [h.lower() for h in header_cells(table)]
            if any("team" in h for h in hdrs) and any("rnk" in h or "rank" in h or "pts" in h for h in hdrs):
                target = table
                break

    if target is None:
        tables = soup.find_all("table")
        if tables:
            target = tables[0]

    if target is None:
        raise ValueError(f"Could not find USCHO table for {meta['name']}")

    headers = header_cells(target)
    hdrs_lower = [h.lower() for h in headers]

    def idx(*candidates: str) -> Optional[int]:
        for c in candidates:
            for i, h in enumerate(hdrs_lower):
                if c in h:
                    return i
        return None

    def exact_idx(*candidates: str) -> Optional[int]:
        for c in candidates:
            for i, h in enumerate(hdrs_lower):
                if h == c:
                    return i
        return None

    rank_idx = exact_idx("rnk", "rank", "#", "rk") or idx("rnk", "rank")
    team_idx = exact_idx("team", "school") or idx("team", "school")
    fpv_idx = idx("first place", "(first place votes)", "fpv", "1st")
    rec_idx = exact_idx("record", "rec", "w-l-t") or idx("record", "rec")
    pts_idx = exact_idx("pts", "points", "total") or idx("pts", "point")
    last_idx = exact_idx("last poll", "last", "prev", "last rnk") or idx("last", "prev")

    rows = []
    for tr in body_rows(target):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 4:
            continue

        def cell_text(i: Optional[int]) -> str:
            if i is None or i >= len(cells):
                return ""
            return clean(cells[i].get_text())

        raw_team = team_text(cells[team_idx]) if team_idx is not None else cell_text(1)

        if not raw_team or raw_team.lower() in ("team", "school", "rnk", "rank", "ranking"):
            continue

        raw_rank = cell_text(rank_idx) if rank_idx is not None else cell_text(0)
        rank_match = re.search(r"(\d+)", raw_rank)
        rank_val = int(rank_match.group(1)) if rank_match else len(rows) + 1

        fpv = None
        m_fpv = re.search(r"\(([\d]+)\)", raw_team)
        if m_fpv:
            fpv = m_fpv.group(1)
            cleaned_team = clean(raw_team[:m_fpv.start()])
        else:
            cleaned_team = raw_team

        if fpv is None and fpv_idx is not None:
            raw_fpv = cell_text(fpv_idx)
            m_col = re.search(r"(\d+)", raw_fpv)
            if m_col:
                fpv = m_col.group(1)

        raw_pts = cell_text(pts_idx) if pts_idx is not None else cell_text(4)
        m_pts = re.search(r"(\d+)", raw_pts)
        pts_num = m_pts.group(1) if m_pts else "0"

        if fpv and int(fpv) > 0:
            formatted_pts = f"{pts_num} ({fpv})"
        else:
            formatted_pts = pts_num

        raw_rec = cell_text(rec_idx) if rec_idx is not None else cell_text(3)
        record = raw_rec.replace("–", "-") if raw_rec else "—"

        raw_last = cell_text(last_idx) if last_idx is not None else (cell_text(5) if len(cells) > 5 else "")
        last_match = re.search(r"(\d+)", raw_last)

        if last_match:
            last_rank = int(last_match.group(1))
            diff = last_rank - rank_val
            if diff > 0:
                trend = f"+{diff}"
                trend_dir = "up"
            elif diff < 0:
                trend = f"{diff}"
                trend_dir = "down"
            else:
                trend = "="
                trend_dir = "same"
            last_display = str(last_rank)
        else:
            last_display = "NR" if ("nr" in raw_last.lower() or not raw_last) else raw_last
            trend = "NEW"
            trend_dir = "new"

        rows.append({
            "rank": rank_val,
            "team": cleaned_team,
            "record": record or "—",
            "pts": formatted_pts,
            "last_poll": last_display,
            "trend": trend,
            "trend_dir": trend_dir,
        })

    if not rows:
        raise ValueError(f"No poll rows parsed from table for {meta['name']}")

    return {
        "id": meta["id"],
        "name": meta["name"],
        "source": meta["url"],
        "poll_date": poll_date,
        "rows": rows,
    }

# ── Static HTTP Fetcher Fast-Path ─────────────────────────────────────────────
def fetch_static_html(url: str) -> Optional[str]:
    try:
        req = urllib.request.Request(url, headers=FETCH_HEADERS)
        with urllib.request.urlopen(req, timeout=12) as response:
            return response.read().decode("utf-8", errors="replace")
    except Exception as e:
        logger.debug(f"Static HTTP fetch skipped for {url}: {e}")
        return None

# ── Dynamic Async Playwright Fetcher ──────────────────────────────────────────
async def fetch_rendered_html(context: BrowserContext, feed: Dict[str, Any]) -> str:
    page: Optional[Page] = None
    url = feed["url"]
    feed_type = feed.get("type", "standings")
    try:
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        if feed_type == "poll":
            try:
                await page.wait_for_selector("#btable tbody tr", timeout=8000)
            except Exception:
                await page.wait_for_selector("table tbody tr, table tr", timeout=5000)
        else:
            try:
                await page.wait_for_selector("table tbody tr, table.table-divisionless", timeout=8000)
            except Exception:
                await page.wait_for_selector("table", timeout=4000)

        return await page.content()
    finally:
        if page:
            await page.close()

async def process_feed(
    context: BrowserContext,
    feed: Dict[str, Any],
    semaphore: asyncio.Semaphore
) -> Tuple[str, Dict[str, Any]]:
    feed_id = feed["id"]
    feed_type = feed.get("type", "standings")
    url = feed["url"]

    # 1. Try static HTTP fast path (avoids browser page allocation if HTML is pre-rendered)
    if feed_type != "poll":
        static_html = await asyncio.to_thread(fetch_static_html, url)
        if static_html:
            try:
                data = parse_standings_table(static_html, feed)
                logger.info(f"  ✓ [{feed['name']}] Parsed via static HTTP fast-path ({len(data['rows'])} rows)")
                return feed_id, {"status": "ok", **data}
            except Exception:
                pass

    # 2. Dynamic Playwright rendering path (with concurrency semaphore)
    async with semaphore:
        logger.info(f"Fetching [{feed['name']}] via browser -> {url}...")
        try:
            html = await fetch_rendered_html(context, feed)
            if feed_type == "poll":
                data = parse_uscho_poll_table(html, feed)
            else:
                data = parse_standings_table(html, feed)

            pts_note = f" (with {feed['point_col'].upper()})" if feed.get("point_col") else ""
            logger.info(f"  ✓ [{feed['name']}] Parsed {len(data['rows'])} rows{pts_note}.")
            return feed_id, {"status": "ok", **data}
        except Exception as exc:
            logger.error(f"  ✗ Error [{feed_id}]: {exc}")
            return feed_id, {
                "id": feed_id,
                "name": feed["name"],
                "source": url,
                "status": "error",
                "error": str(exc)[:300],
                "rows": [],
            }

# ── Caching & Payload Helpers ────────────────────────────────────────────────
def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()

def _strip_volatile(d: Dict[str, Any]) -> Dict[str, Any]:
    clean_d = {k: v for k, v in d.items() if k not in ("generated_at", "updated_at")}
    if "sports" in clean_d and isinstance(clean_d["sports"], dict):
        clean_sports = {}
        for s_id, s_data in clean_d["sports"].items():
            if isinstance(s_data, dict):
                clean_sports[s_id] = {k: v for k, v in s_data.items() if k != "updated_at"}
            else:
                clean_sports[s_id] = s_data
        clean_d["sports"] = clean_sports
    return clean_d

def _age_in_hours(iso_str: str) -> float:
    try:
        cleaned_iso = iso_str.replace("Z", "+00:00")
        parsed_dt = dt.datetime.fromisoformat(cleaned_iso)
        return (dt.datetime.now(dt.timezone.utc) - parsed_dt).total_seconds() / 3600.0
    except Exception:
        return 999.0

# ── Main Async Runner ──────────────────────────────────────────────────────────
async def run_scraper(out_path: Path) -> None:
    old_sports: Dict[str, Any] = {}
    old_payload: Dict[str, Any] = {}
    if out_path.exists():
        try:
            old_payload = json.loads(out_path.read_text(encoding="utf-8"))
            if isinstance(old_payload, dict):
                old_sports = old_payload.get("sports", {})
        except Exception:
            pass

    semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            user_agent=UA,
            viewport={"width": 1280, "height": 800}
        )

        # Intercept network requests to abort bandwidth-heavy assets
        await context.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in {"image", "media", "font"}
            else route.continue_()
        )

        tasks = [process_feed(context, feed, semaphore) for feed in FEEDS_CONFIG]
        results = await asyncio.gather(*tasks)
        await browser.close()

    new_scraped = dict(results)
    current_timestamp = now_iso()
    final_sports: Dict[str, Any] = {}

    for feed in FEEDS_CONFIG:
        s_id = feed["id"]
        res = new_scraped.get(s_id)
        old = old_sports.get(s_id)

        if res and res.get("status") == "ok":
            res["updated_at"] = current_timestamp
            final_sports[s_id] = res
        elif old and old.get("rows"):
            logger.warning(f"[{s_id}] Scrape failed — keeping previous data as stale.")
            final_sports[s_id] = {
                **old,
                "status": "stale",
                "error": res.get("error") if res else "Fetch failed",
            }
        else:
            final_sports[s_id] = res or {
                "id": s_id,
                "name": feed["name"],
                "source": feed["url"],
                "status": "error",
                "rows": [],
            }

    payload = {
        "generated_at": current_timestamp,
        "sports": final_sports,
    }

    # Smart cache hit check: skip rewrite if sports data hasn't changed and is < 18h old
    if old_payload and _strip_volatile(old_payload) == _strip_volatile(payload):
        old_gen = old_payload.get("generated_at", "")
        if old_gen and _age_in_hours(old_gen) < MAX_CACHE_HOURS:
            logger.info(f"Cache hit (<{MAX_CACHE_HOURS}h old, identical standings/polls) — skipping disk rewrite.")
            return

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    logger.info(f"\n✓ Successfully wrote {len(final_sports)} feeds to {out_path}")

def main() -> None:
    ap = argparse.ArgumentParser(description="Summit League, NCHC, WCHA, PFL Standings and USCHO Polls Scraper")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="Path to output data.json")
    args = ap.parse_args()

    asyncio.run(run_scraper(Path(args.out)))

if __name__ == "__main__":
    main()
