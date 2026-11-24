#!/usr/bin/env python3
"""Polls the NCAA API every 2 minutes and writes minimal score data to scores.json."""

import json
import os
import re
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------- CONFIG ----------
BASE_URL = "https://ncaa-api.henrygd.me"
INTERVAL_SECONDS = 120          # 2 minutes
DELAY = 0.5                     # pause between requests (public limit: 3/sec)

# Pre-game start times are converted to this time zone (no label is added).
DISPLAY_TZ_NAME = "America/Chicago"

SCRIPT_DIR = Path(__file__).resolve().parent
IDS_FILE = SCRIPT_DIR / "game_ids.txt"

# Default: saves next to this script. To save elsewhere, replace with a full path, e.g.:
#   Windows: Path(r"C:\Users\Casey\Documents\scores.json")
#   Mac:     Path("/Users/casey/Documents/scores.json")
OUTPUT_FILE = SCRIPT_DIR / "scores.json"

# Canonical team names mapping
# If an incoming team matches any key (case/punctuation insensitive), it gets replaced with the value.
TEAM_NAMES = {
    # Minnesota Duluth
    "minnesota duluth": "Minnesota Duluth",
    "minn duluth": "Minnesota Duluth",
    "minn. duluth": "Minnesota Duluth",
    "umd": "Minnesota Duluth",

    # Denver
    "denver": "Denver",
    "university of denver": "Denver",

    # North Dakota
    "north dakota": "North Dakota",
    "und": "North Dakota",

    # Miami
    "miami": "Miami",
    "miami (oh)": "Miami",
    "miami oh": "Miami",
    "miami ohio": "Miami",
    "miami (ohio)": "Miami",

    # Omaha
    "omaha": "Omaha",
    "neb omaha": "Omaha",
    "nebraska omaha": "Omaha",
    "uno": "Omaha",

    # Western Michigan
    "western michigan": "Western Michigan",
    "western mich": "Western Michigan",
    "western mich.": "Western Michigan",
    "wmu": "Western Michigan",

    # Arizona State
    "arizona state": "Arizona State",
    "arizona st": "Arizona State",
    "arizona st.": "Arizona State",
    "asu": "Arizona State",

    # Colorado College
    "colorado college": "Colorado College",
    "colorado col": "Colorado College",
    "colorado col.": "Colorado College",
    "cc": "Colorado College",

    # St. Cloud State
    "st cloud state": "St. Cloud State",
    "st. cloud state": "St. Cloud State",
    "st cloud st": "St. Cloud State",
    "st. cloud st.": "St. Cloud State",
    "scsu": "St. Cloud State",

    # St. Thomas
    "st thomas": "St. Thomas",
    "st. thomas": "St. Thomas",
    "st thomas (mn)": "St. Thomas",
    "st. thomas (mn)": "St. Thomas",
    "st thomas mn": "St. Thomas",
    "st. thomas mn": "St. Thomas",
    "university of st thomas": "St. Thomas",
}
# ----------------------------

try:
    from zoneinfo import ZoneInfo
    DISPLAY_TZ = ZoneInfo(DISPLAY_TZ_NAME)
except Exception:  # Windows without the tzdata package
    DISPLAY_TZ = timezone(timedelta(hours=-6))  # fixed Central Standard Time (ignores daylight saving)
    print("WARNING: tzdata not found, using fixed Central Standard Time. "
          "Run 'pip install tzdata' so start times stay correct during daylight saving.")


def normalize_name(name):
    """Normalize team name for lookup: lowercase, strip punctuation and extra spaces."""
    if not name:
        return ""
    cleaned = re.sub(r"[^\w\s]", "", str(name).lower())
    return " ".join(cleaned.split())


def get_display_team_name(raw_name):
    """Return mapped team name if matched in TEAM_NAMES, otherwise raw API name."""
    if not raw_name:
        return raw_name

    if raw_name in TEAM_NAMES:
        return TEAM_NAMES[raw_name]

    key = normalize_name(raw_name)
    if key in TEAM_NAMES:
        return TEAM_NAMES[key]

    return raw_name


def format_xpression_name(raw_name, team_obj):
    """
    Cleans team name using TEAM_NAMES dictionary, then prefixes XPression
    superscript rank tag if rank exists and is > 0 (e.g. '{SUP}14{}Tennessee').
    """
    clean_name = get_display_team_name(raw_name)
    
    # Check teamRank or fallback to gameRank
    rank_val = team_obj.get("teamRank") or team_obj.get("gameRank")
    try:
        rank = int(str(rank_val).strip())
    except (ValueError, TypeError):
        rank = 0

    if rank > 0:
        return f"{{SUP}}{rank}{{}}{clean_name}"
    return clean_name


def get_json(path, retries=3):
    """GET a path; returns parsed JSON, or None on 404. Raises after retries."""
    req = urllib.request.Request(BASE_URL + path, headers={"User-Agent": "ncaa-score-poller"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
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


def format_clock(box):
    """Build MM:SS from boxscore minutes/seconds, if present."""
    if not box or box.get("minutes") is None:
        return None
    try:
        return f"{int(box['minutes']):02d}:{int(box.get('seconds') or 0):02d}"
    except (TypeError, ValueError):
        return None


def format_start(c):
    """Start time for a game that hasn't started, in Central, e.g. '8:00 PM'."""
    if c.get("hasStartTime") is False:
        return "TBA"
    try:
        epoch = int(c.get("startTimeEpoch"))
    except (TypeError, ValueError):
        return None
    local = datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(DISPLAY_TZ)
    return local.strftime("%I:%M %p").lstrip("0")


def build_game(game_id):
    info = get_json(f"/game/{game_id}")
    time.sleep(DELAY)
    if not info or not info.get("contests"):
        raise ValueError("game not found")
    c = info["contests"][0]

    box = None
    if c.get("hasBoxscore"):
        box = get_json(f"/game/{game_id}/boxscore")
        time.sleep(DELAY)

    status = c.get("statusCodeDisplay")  # pre / live / final
    period = c.get("currentPeriod") or (box or {}).get("period") or None

    if status == "pre":
        clock = format_start(c)          # not started yet: show the start time
    else:
        clock = c.get("clock") or format_clock(box)

    return {
        "game_id": str(game_id),
        "status": status,
        "period": period,
        "time_remaining": clock,
        "teams": [
            {
                "name": format_xpression_name(t.get("nameShort"), t),
                "home": t.get("isHome"),
                "score": t.get("score"),
            }
            for t in c.get("teams", [])
        ],
    }


def read_ids():
    if not IDS_FILE.exists():
        print(f"Missing {IDS_FILE}")
        return []
    return [
        line.strip()
        for line in IDS_FILE.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]


def load_previous():
    try:
        old = json.loads(OUTPUT_FILE.read_text())
        return {g["game_id"]: g for g in old.get("games", [])}
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return {}


def run_once():
    ids = read_ids()
    previous = load_previous()
    games = []

    for gid in ids:
        try:
            games.append(build_game(gid))
        except Exception as e:  # keep last good data if a fetch fails
            print(f"  Failed {gid}: {e}")
            fallback = previous.get(gid, {"game_id": gid})
            fallback["error"] = str(e)
            games.append(fallback)

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "games": games,
    }

    # Atomic write so anything reading the file never sees a half-written version
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, OUTPUT_FILE)
    print(f"[{payload['updated_at']}] Wrote {OUTPUT_FILE} ({len(games)} games)")


if __name__ == "__main__":
    print(f"Polling every {INTERVAL_SECONDS}s. Output: {OUTPUT_FILE}. Ctrl+C to stop.")
    try:
        while True:
            started = time.monotonic()
            try:
                run_once()
            except Exception as e:
                print(f"Cycle failed: {e}")
                traceback.print_exc()    # shows the exact line that failed
            # Sleep the remainder so cycles stay ~2 min apart regardless of fetch time
            time.sleep(max(0, INTERVAL_SECONDS - (time.monotonic() - started)))
    except KeyboardInterrupt:
        print("\nStopped.")
