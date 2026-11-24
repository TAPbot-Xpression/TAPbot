#!/usr/bin/env python3
"""
St. Thomas (MN) multi-sport schedule scraper (with opponent schedules).

Sports: Men's Basketball, Women's Basketball, Men's Hockey, Softball,
        Men's Soccer, Women's Soccer.

For each sport it:
  1. Pulls the schedule from the NCAA API (schedule-alt route) and keeps the
     NEXT 8 St. Thomas games AFTER today (games played today are NOT included).
  2. Finds the opponent for St. Thomas's next game and pulls THAT team's next
     8 games too, in the same format.

If schedule-alt doesn't have enough games, it falls back to scanning the daily
scoreboards forward from tomorrow and stops once it has enough.

Opponent override: put a team name next to a sport in config.txt (same folder as
this script) to look up that team instead of the automatic opponent. Leave the
line blank to use the automatic opponent.

Each game has only four fields: game_id, date, home_away, opponent.
Results are grouped by sport in schedule.json (next to this script).

Opponent names: if a team is listed in OPPONENT_NAMES (see CONFIG), the name
from that list is used. If ranked (1-25), the name is prefixed with Ross
XPression superscript tags: '{SUP}14{}Tennessee'.

Usage:
    python schedule_scrape.py                 # next 8 games per sport
    python schedule_scrape.py --games 5       # next 5 games per sport
    python schedule_scrape.py --season 2026   # academic year STARTING in 2026 (2026-27)
    python schedule_scrape.py --dump          # also save raw schedule-alt responses
    python schedule_scrape.py --scan          # skip schedule-alt, use the daily scoreboard scan
"""

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# ---------- CONFIG ----------
BASE_URL = "https://ncaa-api.henrygd.me"   # swap for your self-hosted URL if you have one
DIVISION = "d1"
TEAM_SEO = "st-thomas-mn"
TEAM_NAME = "St. Thomas (MN)"
MAX_GAMES = 8                               # upcoming games to keep PER TEAM, PER SPORT
DELAY = 0.5                                 # public API limit: 3 requests/sec

# One entry per sport. Offsets are relative to the academic-year start (2026 = 2026-27).
#   start/end = (month, day, year_offset) of the window the fallback scan searches.
#   alt_year_offset = which year to send to schedule-alt.
SPORTS = [
    {"key": "mens_basketball",   "name": "Men's Basketball",   "path": "basketball-men",
     "start": (11, 1, 0), "end": (3, 31, 1), "alt_year_offset": 0},
    {"key": "womens_basketball", "name": "Women's Basketball", "path": "basketball-women",
     "start": (11, 1, 0), "end": (3, 31, 1), "alt_year_offset": 0},
    {"key": "mens_hockey",       "name": "Men's Hockey",       "path": "icehockey-men",
     "start": (10, 1, 0), "end": (4, 15, 1), "alt_year_offset": 0},
    {"key": "softball",          "name": "Softball",           "path": "softball",
     "start": (2, 1, 1),  "end": (5, 31, 1), "alt_year_offset": 1},   # spring sport
    # Soccer is a fall sport. The window runs through the NCAA tournament (mid-December).
    {"key": "mens_soccer",       "name": "Men's Soccer",       "path": "soccer-men",
     "start": (8, 15, 0), "end": (12, 20, 0), "alt_year_offset": 0},
    {"key": "womens_soccer",     "name": "Women's Soccer",     "path": "soccer-women",
     "start": (8, 15, 0), "end": (12, 10, 0), "alt_year_offset": 0},
]

# Preferred opponent display names.
#   Left side  = a name the API might send (matching ignores capitalization and periods,
#                and treats a trailing "St." as "State", "Col." as "College",
#                "Mich." as "Michigan", "Minn." as "Minnesota").
#   Right side = the name you want in the JSON.
# Any team NOT in this list keeps the name the API sends.
# To add a team, add one line. To change a display name, edit the right side.
OPPONENT_NAMES = {
    "Arizona St.": "Arizona St.",
    "Colorado College": "Colorado College",
    "Denver": "Denver",
    "Miami": "Miami",
    "Miami (OH)": "Miami",                    # alias: how the API may list it
    "Minnesota Duluth": "Minnesota Duluth",
    "North Dakota": "North Dakota",
    "Omaha": "Omaha",
    "St. Cloud State": "St. Cloud State",
    "St. Thomas": "St. Thomas",
    "St. Thomas (MN)": "St. Thomas",          # alias: how the API lists it
    "Western Michigan": "Western Michigan",
    "Kansas City": "Kansas City",
    "UMKC": "Kansas City",                    # alias: older/alternate name
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
}

SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = SCRIPT_DIR / "schedule.json"
CONFIG_FILE = SCRIPT_DIR / "config.txt"     # optional opponent overrides
# ----------------------------

try:
    from zoneinfo import ZoneInfo
    CENTRAL = ZoneInfo("America/Chicago")   # St. Thomas is in Minnesota
except Exception:                            # tzdata missing on some Windows installs
    CENTRAL = timezone(timedelta(hours=-6))  # approximate fallback (ignores daylight saving)

MONTHS = {
    1: "Jan.", 2: "Feb.", 3: "Mar.", 4: "Apr.", 5: "May", 6: "June",
    7: "July", 8: "Aug.", 9: "Sept.", 10: "Oct.", 11: "Nov.", 12: "Dec.",
}

# ---------- Team name matching & formatting ----------
_ABBREV = {"col": "college", "mich": "michigan", "minn": "minnesota"}


def _norm(name):
    """Lowercase, drop periods, expand common abbreviations so name variants match."""
    if not name:
        return ""
    words = str(name).lower().replace(".", "").replace("&", "and").split()
    words = [_ABBREV.get(w, w) for w in words]
    if len(words) > 1 and words[-1] == "st":    # "Arizona St." -> "arizona state"
        words[-1] = "state"
    return " ".join(words)


_LOOKUP = {_norm(k): v for k, v in OPPONENT_NAMES.items()}


def display_name(name):
    """Return the OPPONENT_NAMES version of a team name, or the API's name if not listed."""
    return _LOOKUP.get(_norm(name), name)


def format_xpression_name(raw_name, rank_val=None):
    """
    Cleans team name using OPPONENT_NAMES dictionary, then prefixes XPression
    superscript rank tag if rank exists and is > 0 (e.g. '{SUP}14{}Tennessee').
    """
    clean_name = display_name(raw_name)
    if not clean_name:
        return clean_name

    try:
        rank = int(str(rank_val).strip())
    except (ValueError, TypeError):
        rank = 0

    if rank > 0:
        return f"{{SUP}}{rank}{{}}{clean_name}"
    return clean_name


# ---------- config.txt ----------
def load_config():
    """Read config.txt. Returns {sport_key: team name} for lines that have a name filled in.

    Format (one line per sport, '#' lines are ignored):
        mens_hockey = Denver
        softball =
    """
    cfg = {}
    if not CONFIG_FILE.exists():
        print("No config.txt found. Using the automatic opponent for every sport.")
        return cfg

    valid = {s["key"] for s in SPORTS}
    for line in CONFIG_FILE.read_text(encoding="utf-8-sig").splitlines():   # utf-8-sig handles Notepad's BOM
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip().lower(), val.strip()
        if key not in valid:
            print(f"config.txt: ignoring unknown sport '{key}'. Valid: {', '.join(sorted(valid))}")
        elif val:
            cfg[key] = val
    print(f"Loaded config.txt: {len(cfg)} opponent override(s).")
    return cfg


# ---------- HTTP ----------
def get_json(path, retries=3):
    """GET a path; returns parsed JSON, or None on 404. Raises after retries."""
    req = urllib.request.Request(BASE_URL + path, headers={"User-Agent": "ncaa-schedule-scraper"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt == retries - 1:
                raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if attempt == retries - 1:
                raise
        time.sleep(2 * (attempt + 1))


# ---------- helpers ----------
def default_season(today=None):
    """Academic-year start. Nov 2026 -> 2026, Feb 2027 -> 2026."""
    today = today or date.today()
    return today.year if today.month >= 6 else today.year - 1


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def format_date(d):
    """date(2026, 11, 19) -> 'Nov. 19'"""
    return f"{MONTHS[d.month]} {d.day}"


def parse_game_date(raw_date, epoch):
    """Get a date object from the API's date string, or from the start-time epoch."""
    if raw_date:
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
            try:
                return datetime.strptime(str(raw_date)[:10], fmt).date()
            except ValueError:
                pass
    if epoch:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(CENTRAL).date()
    return None


def walk(node):
    """Yield every dict nested anywhere inside the response."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def team_info(t):
    names = t.get("names") or {}
    rank = t.get("teamRank") or t.get("gameRank") or t.get("rank") or names.get("rank")
    return {
        "name": t.get("nameShort") or names.get("short") or t.get("nameFull") or names.get("full"),
        "seo": t.get("seoname") or names.get("seo"),
        "home": t.get("isHome"),
        "rank": rank,
    }


def is_st_thomas(t):
    return t["seo"] == TEAM_SEO or t["name"] == TEAM_NAME


def normalize(o):
    """Turn a game-like dict into a standard record, or None if it isn't a game.

    Handles both shapes the NCAA API uses:
      - contest shape:    {"id", "teams": [{"isHome", "nameShort"}, ...]}
      - scoreboard shape: {"gameID", "home": {"names": {...}}, "away": {...}}
    """
    gid = o.get("contestId") or o.get("gameID") or o.get("id")
    if gid is None:
        return None

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


def extract_games(data):
    """Every game (any team) found anywhere in an API response, as normalized records."""
    found = {}
    for obj in walk(data):
        rec = normalize(obj)
        if rec:
            found[rec["id"]] = rec
    return list(found.values())


def build_entry(rec, me):
    """Shape one game from team `me`'s point of view (4 fields + internal sort keys)."""
    other = next((t for t in rec["teams"] if t is not me), {"name": None, "rank": None})

    if me["home"] is True:
        home_away = "VS"
    elif me["home"] is False:
        home_away = "AT"
    else:
        home_away = None

    game_date = rec["gdate"]
    return {
        "game_id": rec["id"],
        "date": format_date(game_date) if game_date else rec["date"],
        "home_away": home_away,
        "opponent": format_xpression_name(other.get("name"), other.get("rank")),
        "_date": game_date,           # internal, removed before saving
        "_epoch": rec["epoch"] or 0,  # internal, removed before saving
    }


def next_games(games, today, limit):
    """Keep games AFTER today (tomorrow onward, soonest first), capped at `limit`.

    Games dated today are excluded.
    """
    keep = [g for g in games if g["_date"] is None or g["_date"] > today]
    keep.sort(key=lambda g: (g["_date"] or date.max, g["_epoch"]))
    return keep[:limit]


def games_for(records, match, today, limit):
    """Next `limit` games after today for whichever team `match` recognizes."""
    entries = []
    for rec in records:
        me = next((t for t in rec["teams"] if match(t)), None)
        if me is not None:
            entries.append(build_entry(rec, me))
    return next_games(entries, today, limit)


# ---------- opponent lookup ----------
def matcher_for_team(team):
    """Predicate that recognizes `team` in other records (by slug, else by name)."""
    seo, nm = team["seo"], _norm(team["name"])
    if seo:
        return lambda t: t["seo"] == seo
    return lambda t: _norm(t["name"]) == nm


def matcher_for_name(text):
    """Predicate that recognizes a team by a name typed in config.txt."""
    target = _norm(text)
    return lambda t: _norm(t["name"]) == target or _norm(display_name(t["name"])) == target


def pick_opponent(records, today, override, warn=False):
    """Choose which opponent to look up. Returns a team dict, or None.

    - override given: the team matching that name (from config.txt)
    - otherwise: St. Thomas's opponent in its next game after today
    """
    st_recs = [r for r in records
               if any(is_st_thomas(t) for t in r["teams"])
               and (r["gdate"] is None or r["gdate"] > today)]
    st_recs.sort(key=lambda r: (r["gdate"] or date.max, r["epoch"] or 0))

    if override:
        m = matcher_for_name(override)
        teams = [t for r in records for t in r["teams"] if m(t) and not is_st_thomas(t)]
        if not teams:
            return None
        seos = {t["seo"] for t in teams if t["seo"]}
        if len(seos) > 1:
            # Name matches several schools: prefer the one St. Thomas actually plays.
            faced = {t["seo"] for r in st_recs for t in r["teams"] if not is_st_thomas(t)}
            chosen = next((t for t in teams if t["seo"] in faced), teams[0])
            if warn:
                print(f"  Warning: '{override}' matches more than one school "
                      f"({', '.join(sorted(seos))}). Using {chosen['seo']}.")
            return chosen
        return teams[0]

    for rec in st_recs:
        opp = next((t for t in rec["teams"] if not is_st_thomas(t)), None)
        if opp:
            return opp
    return None


def is_enough(records, today, limit, override):
    """True once we have `limit` St. Thomas games AND `limit` games for the opponent."""
    if len(games_for(records, is_st_thomas, today, limit)) < limit:
        return False
    opp = pick_opponent(records, today, override)
    if opp is None:
        return False
    return len(games_for(records, matcher_for_team(opp), today, limit)) >= limit


# ---------- scanning ----------
def scan_window(sport, season):
    s, e = sport["start"], sport["end"]
    return date(season + s[2], s[0], s[1]), date(season + e[2], e[0], e[1])


def scan_scoreboards(sport, season, today, limit, records, override):
    """Fallback: walk the daily scoreboards from TOMORROW forward, adding every game to `records`.

    Stops once St. Thomas AND its opponent each have `limit` upcoming games.
    """
    start, end = scan_window(sport, season)
    day = max(today + timedelta(days=1), start)   # skip today
    if day > end:
        print(f"  Season window ({start} to {end}) is over. No games to scan.")
        return

    total = (end - day).days + 1
    print(f"  Scanning up to {total} days from {day} (stops once both schedules have {limit} games)...")
    i = 0
    while day <= end:
        i += 1
        path = f"/scoreboard/{sport['path']}/{DIVISION}/{day:%Y/%m/%d}/all-conf"
        try:
            data = get_json(path)
        except Exception as e:
            print(f"    {day}: failed ({e})")
            data = None
        if data:
            for rec in extract_games(data):
                records[rec["id"]] = rec
            if is_enough(list(records.values()), today, limit, override):
                print(f"    Found enough games after {i} days.")
                break
        if i % 25 == 0:
            print(f"    ...{i}/{total} days checked")
        time.sleep(DELAY)
        day += timedelta(days=1)


def collect_sport(sport, season, today, limit, scan_only, dump, override):
    """Return St. Thomas's next games plus the opponent's next games for one sport."""
    records = {}

    if not scan_only:
        year = season + sport["alt_year_offset"]
        path = f"/schedule-alt/{sport['path']}/{DIVISION}/{year}"
        print(f"  Trying {path}")
        try:
            data = get_json(path)
            time.sleep(DELAY)
            if data is not None:
                if dump:
                    raw = SCRIPT_DIR / f"schedule_raw_{sport['key']}.json"
                    raw.write_text(json.dumps(data, indent=2), encoding="utf-8")
                    print(f"  Saved raw response to {raw.name}")
                for rec in extract_games(data):
                    records[rec["id"]] = rec
        except Exception as e:
            print(f"  schedule-alt failed: {e}")

    if not is_enough(list(records.values()), today, limit, override):
        print("  Not enough upcoming games from schedule-alt. Falling back to scoreboard scan.")
        scan_scoreboards(sport, season, today, limit, records, override)

    recs = list(records.values())
    st_games = games_for(recs, is_st_thomas, today, limit)

    # Opponent schedule
    source = "config.txt" if override else "auto"
    opp = pick_opponent(recs, today, override, warn=True)
    if opp is None:
        note = (f"'{override}' was not found in the API data." if override
                else "No upcoming St. Thomas game, so there is no opponent to look up.")
        opp_block = {"team": override, "source": source, "game_count": 0, "games": [], "note": note}
        print(f"  Opponent: none ({note})")
    else:
        opp_games = games_for(recs, matcher_for_team(opp), today, limit)
        opp_block = {
            "team": format_xpression_name(opp["name"], opp.get("rank")),
            "source": source,
            "game_count": len(opp_games),
            "games": opp_games,
        }
        print(f"  Opponent: {opp_block['team']} ({source}), {len(opp_games)} games")

    return {"games": st_games, "opponent": opp_block}


# ---------- output ----------
def strip(games):
    """Remove internal sort keys so only the four public fields remain."""
    for g in games:
        g.pop("_epoch", None)
        g.pop("_date", None)
    return games


def write_output(results):
    sports_out = {}
    for sport in SPORTS:
        res = results[sport["key"]]
        opp = res["opponent"]
        opp["games"] = strip(opp["games"])
        sports_out[sport["key"]] = {
            "name": sport["name"],
            "game_count": len(res["games"]),
            "games": strip(res["games"]),
            "opponent_schedule": opp,
        }

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "team": TEAM_NAME,
        "sports": sports_out,
    }
    tmp = OUTPUT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, OUTPUT_FILE)
    print(f"\nWrote {OUTPUT_FILE}")
    for sport in SPORTS:
        s = sports_out[sport["key"]]
        o = s["opponent_schedule"]
        print(f"  {sport['name']}: {s['game_count']} games | opponent: {o['team'] or 'none'} ({o['game_count']} games)")


def main():
    ap = argparse.ArgumentParser(description="St. Thomas (MN) multi-sport schedule scraper")
    ap.add_argument("--season", type=int, default=default_season(),
                    help="academic year start, e.g. 2026 for 2026-27 (default: auto)")
    ap.add_argument("--games", type=int, default=MAX_GAMES,
                    help=f"upcoming games to keep per team, per sport (default: {MAX_GAMES})")
    ap.add_argument("--dump", action="store_true", help="save raw schedule-alt responses")
    ap.add_argument("--scan", action="store_true", help="skip schedule-alt and scan the daily scoreboards")
    args = ap.parse_args()

    today = date.today()
    config = load_config()
    results = {}
    for sport in SPORTS:
        print(f"\n{sport['name']}")
        results[sport["key"]] = collect_sport(
            sport, args.season, today, args.games, args.scan, args.dump, config.get(sport["key"])
        )

    write_output(results)


if __name__ == "__main__":
    main()
