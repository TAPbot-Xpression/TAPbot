#!/usr/bin/env python3
"""
St. Thomas (MN) Multi-Sport Unified Schedule Scraper:
Football, Volleyball, Baseball, Softball, Men's Basketball, Women's Basketball,
Men's Hockey, Women's Hockey, Men's Soccer, Women's Soccer.

Combines North Campus and South Campus scrapers and adds Women's Hockey.
Outputs all sport schedules to a single JSON file: 'docs/schedulers/all_schedules.json'.

Features:
  1. Pulls schedules via the NCAA API (schedule-alt route) keeping the NEXT 8
     St. Thomas games AFTER today (current day games excluded).
  2. Finds the opponent for St. Thomas's next game and pulls THAT team's next
     8 games too (opponent_schedule).
  3. Automatic fallback:
     - Football: weekly scoreboard scanning (weeks 01-16).
     - Other sports: daily scoreboard scanning forward from tomorrow.
  4. Formats ranked opponents with XPression superscript tags: '{SUP}14{}Opponent Name'.
  5. Config overrides: specify opponent override per sport in config_all.txt (or fallback config.txt).

Optimizations & Safeguards Applied:
  1. Rate Limiting: Dynamic time sleep to respect the 3 req/sec API limit without wasting idle time.
  2. Preserves Last-Good Data: If a sport's API fetch fails or errors out, the previous schedule
     data for that sport is kept instead of being overwritten with empty arrays.
  3. Smart Timestamp Caching: Skips disk write if the payload hasn't changed and is fresh (<18h old),
     preventing redundant CI git commits and deployments.
  4. Out-of-Season Guard: Avoids scanning hundreds of empty daily scoreboards when a sport is well
     outside its active season window.
  5. Optimized Game Resolution: Efficient lookup avoiding repeated full-array re-sorting per scanned day.

Usage:
    python schedule_scrape_all.py                 # next 8 games per sport
    python schedule_scrape_all.py --games 5       # next 5 games per sport
    python schedule_scrape_all.py --season 2026   # academic year start (2026 for 2026-27)
    python schedule_scrape_all.py --dump          # save raw responses
    python schedule_scrape_all.py --scan          # skip schedule-alt, use scoreboard scan
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("schedule_scraper")

# ---------- CONFIG ----------
BASE_URL = "https://ncaa-api.henrygd.me"   # swap for your self-hosted URL if you have one
TEAM_SEO = "st-thomas-mn"
TEAM_NAME = "St. Thomas (MN)"
MAX_GAMES = 8                              # upcoming games to keep PER TEAM, PER SPORT
DELAY = 0.35                               # public API limit: 3 requests/sec (~0.33s interval)
MAX_CACHE_HOURS = 18

# Dynamic paths
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1] if len(SCRIPT_DIR.parents) >= 2 else SCRIPT_DIR
OUTPUT_DIR = REPO_ROOT / "docs" / "schedulers"
OUTPUT_FILE = OUTPUT_DIR / "all_schedules.json"
LEGACY_OUTPUT_FILE = SCRIPT_DIR / "all_schedules.json"
CONFIG_FILE = SCRIPT_DIR / "config_all.txt"               # primary override file
CONFIG_FALLBACK = SCRIPT_DIR / "config.txt"               # fallback if config_all.txt isn't present

# Complete Sport Matrix (All Campuses + Women's Hockey)
SPORTS = [
    # Fall / North Campus
    {"key": "football",           "name": "Football",           "path": "football",         "division": "fcs",
     "scan": "week", "weeks": range(1, 17), "alt_year_offset": 0},
    {"key": "volleyball",         "name": "Volleyball",         "path": "volleyball-women", "division": "d1",
     "scan": "date", "start": (8, 15, 0), "end": (12, 25, 0), "alt_year_offset": 0},
    {"key": "mens_soccer",        "name": "Men's Soccer",       "path": "soccer-men",       "division": "d1",
     "scan": "date", "start": (8, 15, 0), "end": (12, 20, 0), "alt_year_offset": 0},
    {"key": "womens_soccer",      "name": "Women's Soccer",     "path": "soccer-women",     "division": "d1",
     "scan": "date", "start": (8, 15, 0), "end": (12, 10, 0), "alt_year_offset": 0},

    # Winter / Ice Hockey & Basketball
    {"key": "mens_hockey",        "name": "Men's Hockey",       "path": "icehockey-men",    "division": "d1",
     "scan": "date", "start": (10, 1, 0), "end": (4, 15, 1), "alt_year_offset": 0},
    {"key": "womens_hockey",      "name": "Women's Hockey",     "path": "icehockey-women",  "division": "d1",
     "scan": "date", "start": (9, 20, 0), "end": (4, 15, 1), "alt_year_offset": 0},
    {"key": "mens_basketball",    "name": "Men's Basketball",   "path": "basketball-men",   "division": "d1",
     "scan": "date", "start": (11, 1, 0), "end": (3, 31, 1), "alt_year_offset": 0},
    {"key": "womens_basketball",  "name": "Women's Basketball", "path": "basketball-women", "division": "d1",
     "scan": "date", "start": (11, 1, 0), "end": (3, 31, 1), "alt_year_offset": 0},

    # Spring / Diamond Sports
    {"key": "baseball",           "name": "Baseball",           "path": "baseball",         "division": "d1",
     "scan": "date", "start": (2, 1, 1),  "end": (6, 30, 1), "alt_year_offset": 1},
    {"key": "softball",           "name": "Softball",           "path": "softball",         "division": "d1",
     "scan": "date", "start": (2, 1, 1),  "end": (5, 31, 1), "alt_year_offset": 1},
]

# Preferred opponent display names.
OPPONENT_NAMES = {
    "Arizona St.": "Arizona St.",
    "Colorado College": "Colorado College",
    "Denver": "Denver",
    "Miami": "Miami",
    "Miami (OH)": "Miami",
    "Minnesota Duluth": "Minnesota Duluth",
    "North Dakota": "North Dakota",
    "Omaha": "Omaha",
    "St. Cloud State": "St. Cloud State",
    "St. Thomas": "St. Thomas",
    "St. Thomas (MN)": "St. Thomas",
    "Western Michigan": "Western Michigan",
    "Kansas City": "Kansas City",
    "UMKC": "Kansas City",
    "North Dakota St.": "North Dakota St.",
    "Oral Roberts": "Oral Roberts",
    "South Dakota": "South Dakota",
    "South Dakota St.": "South Dakota St.",
    "Butler": "Butler",
    "Davidson": "Davidson",
    "Dayton": "Dayton",
    "Drake": "Drake",
    "Marist": "Marist",
    "Morehead St.": "Morehead St.",
    "Presbyterian": "Presbyterian",
    "San Diego": "San Diego",
    "Stetson": "Stetson",
    "Valparaiso": "Valparaiso",
    "Bemidji St.": "Bemidji St.",
    "Minnesota": "Minnesota",
    "Wisconsin": "Wisconsin",
    "Ohio St.": "Ohio St.",
    "Minnesota St.": "Minnesota St.",
}

try:
    from zoneinfo import ZoneInfo
    CENTRAL = ZoneInfo("America/Chicago")
except Exception:
    CENTRAL = timezone(timedelta(hours=-6))

MONTHS = {
    1: "Jan.", 2: "Feb.", 3: "Mar.", 4: "Apr.", 5: "May", 6: "June",
    7: "July", 8: "Aug.", 9: "Sept.", 10: "Oct.", 11: "Nov.", 12: "Dec.",
}

_ABBREV = {"col": "college", "mich": "michigan", "minn": "minnesota"}
_LOOKUP = {}


def _init_lookup() -> None:
    global _LOOKUP
    _LOOKUP = {_norm(k): v for k, v in OPPONENT_NAMES.items()}


def _norm(name: Any) -> str:
    if not name:
        return ""
    words = str(name).lower().replace(".", "").replace("&", "and").split()
    words = [_ABBREV.get(w, w) for w in words]
    if len(words) > 1 and words[-1] == "st":
        words[-1] = "state"
    return " ".join(words)


def display_name(name: Any) -> str:
    if not _LOOKUP:
        _init_lookup()
    return _LOOKUP.get(_norm(name), str(name or ""))


def format_xpression_name(raw_name: Any, rank_val: Any = None) -> str:
    clean_n = display_name(raw_name)
    if not clean_n:
        return clean_n
    try:
        rank = int(str(rank_val).strip())
    except (ValueError, TypeError):
        rank = 0
    if rank > 0:
        return f"{{SUP}}{rank}{{}}{clean_n}"
    return clean_n


# ---------- Rate Limiter & HTTP ----------
_LAST_REQUEST_TIME = 0.0


def rate_limited_sleep(interval: float = DELAY) -> None:
    global _LAST_REQUEST_TIME
    elapsed = time.time() - _LAST_REQUEST_TIME
    if elapsed < interval:
        time.sleep(interval - elapsed)
    _LAST_REQUEST_TIME = time.time()


def get_json(path: str, retries: int = 3) -> Optional[Dict[str, Any]]:
    """GET an API path with backoff retry and active rate-limiting."""
    req = urllib.request.Request(
        BASE_URL + path,
        headers={"User-Agent": "ncaa-schedule-scraper-all"}
    )
    for attempt in range(retries):
        rate_limited_sleep(DELAY)
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt == retries - 1:
                raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if attempt == retries - 1:
                raise
        time.sleep(1.5 * (attempt + 1))
    return None


# ---------- Date & Normalization Helpers ----------
def default_season(today: Optional[date] = None) -> int:
    today = today or date.today()
    return today.year if today.month >= 6 else today.year - 1


def to_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def format_date(d: date) -> str:
    return f"{MONTHS[d.month]} {d.day}"


def parse_game_date(raw_date: Any, epoch: Optional[int]) -> Optional[date]:
    if raw_date:
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
            try:
                return datetime.strptime(str(raw_date)[:10], fmt).date()
            except ValueError:
                pass
    if epoch:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(CENTRAL).date()
    return None


def walk(node: Any):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def team_info(t: Dict[str, Any]) -> Dict[str, Any]:
    names = t.get("names") or {}
    rank = t.get("teamRank") or t.get("gameRank") or t.get("rank") or names.get("rank")
    return {
        "name": t.get("nameShort") or names.get("short") or t.get("nameFull") or names.get("full"),
        "seo": t.get("seoname") or names.get("seo"),
        "home": t.get("isHome"),
        "rank": rank,
    }


def is_st_thomas(t: Dict[str, Any]) -> bool:
    return t.get("seo") == TEAM_SEO or t.get("name") == TEAM_NAME


def normalize(o: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    gid = o.get("contestId") or o.get("gameID") or o.get("id")
    if gid is None:
        return None

    teams = []
    if isinstance(o.get("teams"), list) and len(o["teams"]) == 2 and all(isinstance(t, dict) for t in o["teams"]):
        teams = [team_info(t) for t in o["teams"]]
    elif isinstance(o.get("home"), dict) and isinstance(o.get("away"), dict):
        home, away = team_info(o["home"]), team_info(o["away"])
        home["home"], away["home"] = True, False
        teams = [home, away]
    else:
        return None

    raw_date = o.get("startDate") or o.get("contestDate")
    epoch = to_int(o.get("startTimeEpoch"))
    return {
        "id": str(gid),
        "teams": teams,
        "epoch": epoch,
        "date": raw_date,
        "gdate": parse_game_date(raw_date, epoch),
    }


def extract_games(data: Any) -> List[Dict[str, Any]]:
    found = {}
    for obj in walk(data):
        rec = normalize(obj)
        if rec:
            found[rec["id"]] = rec
    return list(found.values())


def build_entry(rec: Dict[str, Any], me: Dict[str, Any]) -> Dict[str, Any]:
    other = next((t for t in rec["teams"] if t is not me), {"name": None, "rank": None})

    if me.get("home") is True:
        home_away = "VS"
    elif me.get("home") is False:
        home_away = "AT"
    else:
        home_away = None

    game_date = rec["gdate"]
    return {
        "game_id": rec["id"],
        "date": format_date(game_date) if game_date else str(rec["date"]),
        "home_away": home_away,
        "opponent": format_xpression_name(other.get("name"), other.get("rank")),
        "_date": game_date,
        "_epoch": rec["epoch"] or 0,
    }


def next_games(games: List[Dict[str, Any]], today: date, limit: int) -> List[Dict[str, Any]]:
    keep = [g for g in games if g.get("_date") is None or g["_date"] > today]
    keep.sort(key=lambda g: (g.get("_date") or date.max, g.get("_epoch") or 0))
    return keep[:limit]


def games_for(records: List[Dict[str, Any]], match: Callable[[Dict[str, Any]], bool], today: date, limit: int) -> List[Dict[str, Any]]:
    entries = []
    for rec in records:
        me = next((t for t in rec["teams"] if match(t)), None)
        if me is not None:
            entries.append(build_entry(rec, me))
    return next_games(entries, today, limit)


# ---------- Opponent Lookup ----------
def matcher_for_team(team: Dict[str, Any]) -> Callable[[Dict[str, Any]], bool]:
    seo, nm = team.get("seo"), _norm(team.get("name"))
    if seo:
        return lambda t: t.get("seo") == seo
    return lambda t: _norm(t.get("name")) == nm


def matcher_for_name(text: str) -> Callable[[Dict[str, Any]], bool]:
    target = _norm(text)
    return lambda t: _norm(t.get("name")) == target or _norm(display_name(t.get("name"))) == target


def pick_opponent(records: List[Dict[str, Any]], today: date, override: Optional[str], warn: bool = False) -> Optional[Dict[str, Any]]:
    st_recs = [r for r in records
               if any(is_st_thomas(t) for t in r["teams"])
               and (r["gdate"] is None or r["gdate"] > today)]
    st_recs.sort(key=lambda r: (r["gdate"] or date.max, r["epoch"] or 0))

    if override:
        m = matcher_for_name(override)
        teams = [t for r in records for t in r["teams"] if m(t) and not is_st_thomas(t)]
        if not teams:
            return None
        seos = {t["seo"] for t in teams if t.get("seo")}
        if len(seos) > 1:
            faced = {t["seo"] for r in st_recs for t in r["teams"] if not is_st_thomas(t)}
            chosen = next((t for t in teams if t["seo"] in faced), teams[0])
            if warn:
                logger.warning("'%s' matches multiple teams (%s). Using %s.",
                               override, ", ".join(sorted(seos)), chosen["seo"])
            return chosen
        return teams[0]

    for rec in st_recs:
        opp = next((t for t in rec["teams"] if not is_st_thomas(t)), None)
        if opp:
            return opp
    return None


def is_enough(records: List[Dict[str, Any]], today: date, limit: int, override: Optional[str]) -> bool:
    st_count = sum(1 for r in records
                   if any(is_st_thomas(t) for t in r["teams"])
                   and (r["gdate"] is None or r["gdate"] > today))
    if st_count < limit:
        return False
    opp = pick_opponent(records, today, override)
    if opp is None:
        return False
    opp_matcher = matcher_for_team(opp)
    opp_count = sum(1 for r in records
                    if any(opp_matcher(t) for t in r["teams"])
                    and (r["gdate"] is None or r["gdate"] > today))
    return opp_count >= limit


# ---------- Scanning Logic ----------
def scan_window(sport: Dict[str, Any], season: int) -> Tuple[date, date]:
    s, e = sport["start"], sport["end"]
    return date(season + s[2], s[0], s[1]), date(season + e[2], e[0], e[1])


def scan_dates(sport: Dict[str, Any], season: int, today: date, limit: int, records: Dict[str, Any], override: Optional[str]) -> None:
    start, end = scan_window(sport, season)
    day = max(today + timedelta(days=1), start)

    if day > end:
        logger.info("  Season window (%s to %s) is completed. No upcoming games to scan.", start, end)
        return

    # Skip aggressive scanning if we're more than 60 days before season starts
    if today < start - timedelta(days=60):
        logger.info("  Far out of season (starts %s). Skipping deep daily scan.", start)
        return

    total = (end - day).days + 1
    logger.info("  Scanning up to %d days from %s...", total, day)
    i = 0
    while day <= end:
        i += 1
        path = f"/scoreboard/{sport['path']}/{sport['division']}/{day:%Y/%m/%d}/all-conf"
        try:
            data = get_json(path)
        except Exception as e:
            logger.debug("    %s: failed (%s)", day, e)
            data = None

        if data:
            for rec in extract_games(data):
                records[rec["id"]] = rec
            if is_enough(list(records.values()), today, limit, override):
                logger.info("    Found enough games after %d days.", i)
                break
        if i % 25 == 0:
            logger.info("    ...%d/%d days checked", i, total)
        day += timedelta(days=1)


def scan_weeks(sport: Dict[str, Any], season: int, today: date, limit: int, records: Dict[str, Any], override: Optional[str]) -> None:
    logger.info("  Scanning football weeks %d-%d...", sport["weeks"][0], sport["weeks"][-1])
    for week in sport["weeks"]:
        path = f"/scoreboard/{sport['path']}/{sport['division']}/{season}/{week:02d}/all-conf"
        try:
            data = get_json(path)
        except Exception as e:
            logger.debug("    week %d: failed (%s)", week, e)
            data = None
        if data:
            for rec in extract_games(data):
                records[rec["id"]] = rec
            if is_enough(list(records.values()), today, limit, override):
                logger.info("    Found enough games after week %d.", week)
                break


def collect_sport(
    sport: Dict[str, Any],
    season: int,
    today: date,
    limit: int,
    scan_only: bool,
    dump: bool,
    override: Optional[str]
) -> Dict[str, Any]:
    records: Dict[str, Any] = {}

    if not scan_only:
        year = season + sport["alt_year_offset"]
        path = f"/schedule-alt/{sport['path']}/{sport['division']}/{year}"
        logger.info("  Trying %s", path)
        try:
            data = get_json(path)
            if data is not None:
                if dump:
                    raw = SCRIPT_DIR / f"schedule_raw_{sport['key']}.json"
                    raw.write_text(json.dumps(data, indent=2), encoding="utf-8")
                    logger.info("  Saved raw response to %s", raw.name)
                for rec in extract_games(data):
                    records[rec["id"]] = rec
        except Exception as e:
            logger.warning("  schedule-alt failed: %s", e)

    recs_list = list(records.values())
    if not is_enough(recs_list, today, limit, override):
        logger.info("  Not enough upcoming games from schedule-alt. Scanning scoreboards...")
        if sport.get("scan") == "week":
            scan_weeks(sport, season, today, limit, records, override)
        else:
            scan_dates(sport, season, today, limit, records, override)

    recs = list(records.values())
    st_games = games_for(recs, is_st_thomas, today, limit)

    source = "config" if override else "auto"
    opp = pick_opponent(recs, today, override, warn=True)
    if opp is None:
        note = (f"'{override}' was not found in the API data." if override
                else "No upcoming St. Thomas game, so there is no opponent to look up.")
        opp_block = {"team": override or "", "source": source, "game_count": 0, "games": [], "note": note}
        logger.info("  Opponent: none (%s)", note)
    else:
        opp_games = games_for(recs, matcher_for_team(opp), today, limit)
        opp_block = {
            "team": format_xpression_name(opp.get("name"), opp.get("rank")),
            "source": source,
            "game_count": len(opp_games),
            "games": opp_games,
        }
        logger.info("  Opponent: %s (%s), %d games", opp_block["team"], source, len(opp_games))

    return {"games": st_games, "opponent": opp_block, "success": bool(st_games or recs)}


# ---------- Config & Output ----------
def load_config() -> Dict[str, str]:
    cfg_file = CONFIG_FILE if CONFIG_FILE.exists() else CONFIG_FALLBACK
    cfg = {}
    if not cfg_file.exists():
        logger.info("No config file found. Using automatic opponent detection.")
        return cfg

    valid = {s["key"] for s in SPORTS}
    for line in cfg_file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip().lower(), val.strip()
        if key not in valid:
            logger.warning("%s: ignoring unknown sport '%s'. Valid: %s",
                           cfg_file.name, key, ", ".join(sorted(valid)))
        elif val:
            cfg[key] = val
    logger.info("Loaded %s: %d opponent override(s).", cfg_file.name, len(cfg))
    return cfg


def strip(games: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for g in games:
        g.pop("_epoch", None)
        g.pop("_date", None)
    return games


def read_existing_schedules() -> Dict[str, Any]:
    for path in (OUTPUT_FILE, LEGACY_OUTPUT_FILE):
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    return {}


def _strip_volatile(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in d.items() if k not in ("updated_at", "generated_at")}


def _age_in_hours(iso_str: str) -> float:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    except Exception:
        return 999.0


def write_output(results: Dict[str, Any]) -> None:
    existing_payload = read_existing_schedules()
    existing_sports = existing_payload.get("sports", {})

    sports_out = {}
    for sport in SPORTS:
        s_key = sport["key"]
        res = results.get(s_key)
        old_sport_data = existing_sports.get(s_key)

        # Safeguard: If new scrape yielded no games or failed, preserve last known good data
        if (not res or (len(res.get("games", [])) == 0 and not res.get("success", True))) and old_sport_data:
            logger.warning("  ⚠ [%s] Failed to fetch fresh schedule — preserving previous good data.", sport["name"])
            sports_out[s_key] = old_sport_data
            continue

        if not res:
            res = {"games": [], "opponent": {"team": "", "source": "auto", "game_count": 0, "games": []}}

        opp = res["opponent"]
        opp["games"] = strip(opp.get("games", []))
        sports_out[s_key] = {
            "name": sport["name"],
            "game_count": len(res.get("games", [])),
            "games": strip(res.get("games", [])),
            "opponent_schedule": opp,
        }

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "team": TEAM_NAME,
        "sports": sports_out,
    }

    # Smart timestamp caching: Skip rewrite if data is identical and < 18h old
    if existing_payload and _strip_volatile(existing_payload) == _strip_volatile(payload):
        old_updated = existing_payload.get("updated_at", "")
        if old_updated and _age_in_hours(old_updated) < MAX_CACHE_HOURS:
            logger.info("Cache hit (<%dh old, identical schedules) — skipping rewrite of all_schedules.json.", MAX_CACHE_HOURS)
            return

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, OUTPUT_FILE)
    logger.info("\n✓ Wrote updated schedules to %s", OUTPUT_FILE)


def main() -> None:
    ap = argparse.ArgumentParser(description="St. Thomas (MN) Unified multi-sport schedule scraper")
    ap.add_argument("--season", type=int, default=default_season(),
                    help="academic year start, e.g. 2026 for 2026-27 (default: auto)")
    ap.add_argument("--games", type=int, default=MAX_GAMES,
                    help=f"upcoming games to keep per team, per sport (default: {MAX_GAMES})")
    ap.add_argument("--dump", action="store_true", help="save raw schedule-alt responses")
    ap.add_argument("--scan", action="store_true", help="skip schedule-alt and scan scoreboards")
    args = ap.parse_args()

    today = date.today()
    config = load_config()
    results = {}
    for sport in SPORTS:
        logger.info("\n--- %s ---", sport["name"])
        try:
            results[sport["key"]] = collect_sport(
                sport, args.season, today, args.games, args.scan, args.dump, config.get(sport["key"])
            )
        except Exception as e:
            logger.exception("Failed to collect schedule for %s: %s", sport["name"], e)
            results[sport["key"]] = None

    write_output(results)


if __name__ == "__main__":
    main()
