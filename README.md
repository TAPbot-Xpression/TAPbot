# TAPbot

**One repository. All TAPbots.**

TAPbot is the data engine behind Tommie Athletic Productions (TAP) broadcast graphics at the University of St. Thomas. It scrapes season stats, standings, polls, and schedules from official athletics sites and saves them as clean JSON. A set of dashboards publishes the JSON on GitHub Pages. Broadcast graphics (Ross Xpression / Datalinq) read the JSON files directly.

> **Always check the `generated_at` timestamp in a JSON file before going to air.** The Hub Site's tally lights show data freshness at a glance.

## Live pages

| Page | Path |
|---|---|
| Hub Site | https://tapbot-xpression.github.io/TAPbot/ |
| Hockey (NCHC men, WCHA women) | `/hockey/` |
| Basketball (Summit League men and women) | `/basketball/` |
| Diamond Sports (baseball and softball) | `/diamondsports/` |
| Standings & Polls | `/standings/` |
| Schedules | `/schedulers/` |

JSON files sit next to each dashboard, for example `/hockey/st_thomas_mhockey.json` and `/standings/data.json`.

### Hub Site tally lights

| Light | Meaning |
|---|---|
| Green | In season, and the data was scraped within the last 24 hours |
| Amber | In season, but the data is more than 24 hours old |
| Grey | Off season (shown even if the scraper ran recently) |
| Red | The data file is missing |

## What's covered

| Sport | Scope | Output folder | Scraper |
|---|---|---|---|
| Hockey | NCHC men: 10 programs. WCHA women: 8 programs. One Non-Conf slot per gender. | `docs/hockey/` | `scrapers/hockey/scrape_hockey.py` |
| Basketball | Summit League men and women: 8 programs each, plus Non-Conf slots | `docs/basketball/` | `scrapers/basketball/scrape_basketball.py` |
| Diamond Sports | Summit League baseball (6 programs) and softball (7 programs), plus Non-Conf slots | `docs/diamondsports/` | `scrapers/diamondsports/scrape_diamond_sports.py` |
| Standings & Polls | Summit League, PFL, NCHC and WCHA standings, plus USCHO men's and women's polls | `docs/standings/` | `scrapers/standings/scrape_standings.py` |
| Schedules | North and South Campus schedules, all sports | `docs/schedulers/` | `others/schedulers/schedule_scrape_all.py` |

Live game scores are handled by a separate local tool, `others/scores/fetch_scores.py`. See 
[Live scores](#live-scores-fetch_scorespy).

## How it works

```
Athletics sites → Python scrapers (Playwright) → JSON in docs/<sport>/ → GitHub Pages → Dashboards + Xpression graphics
```

1. A scheduled GitHub Actions workflow runs a sport's scraper.
2. The scraper writes JSON into `docs/<sport>/`.
3. If anything changed, the workflow commits the JSON to `main`.
4. The workflow then calls the shared `deploy.yml`, which publishes the whole `docs/` folder to GitHub Pages.

**Design rules**

- **Scrapers are independent.** Each sport has its own folder, `requirements.txt` and workflow. Editing one sport never triggers another sport's run.
- **One deploy workflow.** Only `deploy.yml` publishes the site. It deploys all of `docs/` as a single artifact, so one sport's deploy can't wipe out the others.
- **Each dashboard sits next to its data.** Pages fetch JSON with relative paths such as `fetch("denver_mhockey.json")`.
- **Scrapers keep the last good data.** If a scrape returns nothing, such as during a site outage or after a layout change, the previous JSON is kept instead of being overwritten with blanks. The standings scraper marks the affected feed `"status": "stale"`.

## Repository layout

```
TAPbot/
├─ .github/workflows/
│  ├─ hockey.yml               Scrape hockey
│  ├─ basketball.yml           Scrape Summit basketball
│  ├─ diamondsports.yml        Scrape baseball + softball
│  ├─ standings.yml            Scrape standings + polls
│  ├─ schedule_scrapers.yml    Scrape schedules
│  └─ deploy.yml               Publish docs/ to GitHub Pages
│
├─ scrapers/                   Python only. Not published.
│  ├─ hockey/                  scrape_hockey.py, requirements.txt, non_conf_config.json
│  ├─ basketball/              scrape_basketball.py, requirements.txt, non_conf_config.json
│  ├─ diamondsports/           scrape_diamond_sports.py, requirements.txt, non_conf_config.json
│  └─ standings/               scrape_standings.py, requirements.txt
│
├─ others/                     Supporting tools. Not published.
│  ├─ schedulers/              schedule_scrape_all.py, campus config files
│  └─ scores/                  fetch_scores.py, game_ids.txt  (runs locally)
│
└─ docs/                       The live website
   ├─ index.html               Hub Site
   ├─ hockey/                  index.html + one JSON per team
   ├─ basketball/              index.html + one JSON per team and gender
   ├─ diamondsports/           index.html + one JSON per team and sport
   ├─ standings/               index.html + data.json
   └─ schedulers/              index.html + all_schedules.json (plus per-campus JSON)
```

## Automation schedule

GitHub cron only uses UTC and has no daylight saving support. Where a run has to land at a fixed St. Paul time, the workflow schedules both the CST and CDT cron lines. A time-check step then lets only the correct one continue.

| Workflow | When | Manual inputs |
|---|---|---|
| `hockey.yml` | Daily, 4:30 AM Central | `only_teams`, `enable_debug` |
| `schedule_scrapers.yml` | Daily, 4:00 AM Central | none |
| `standings.yml` | 4:00 AM, 11:00 AM and 4:00 PM Central, all year (cron fires at :05) | none |
| `diamondsports.yml` | Daily, 14:30 UTC (8:30 AM CST / 9:30 AM CDT) | `sport`, `school`, `year` |
| `basketball.yml` | Daily, 14:45 UTC (8:45 AM CST / 9:45 AM CDT) | none |
| `deploy.yml` | Called by each workflow above after new data is committed. Also runs when `docs/**` changes. | none |

Notes:

- **Skipped runs are normal.** For workflows with a time check, one of the two daily cron runs shows as skipped in the Actions tab.
- **Late starts.** GitHub can start scheduled runs 10 to 30 minutes late. The hockey check accepts any start from 4:20 to 5:15 AM Central.
- **Push collisions.** Every workflow retries its `git push` up to three times with a rebase, so overlapping runs don't fail.
- **Push triggers.** Pushing changes to `scrapers/<sport>/**` runs that sport's workflow only.

## Running a scraper manually

### From GitHub

**Actions → pick the workflow → Run workflow.** Use the inputs to narrow the run. Manual runs skip the time check.

### On your own computer

Windows (PowerShell or Command Prompt), from the repo folder:

```powershell
python -m pip install -r scrapers\hockey\requirements.txt
python -m playwright install chromium
python scrapers\hockey\scrape_hockey.py --only "St. Thomas,Denver"
```

Other scrapers:

```powershell
python scrapers\basketball\scrape_basketball.py
python scrapers\diamondsports\scrape_diamond_sports.py --sport softball --school omaha
python scrapers\standings\scrape_standings.py
```

macOS and Linux use the same commands with forward slashes and `python3`.

Each script finds the repo root from its own location, so you can run it from any folder. Install the requirements for the sport you are running, since each sport has its own `requirements.txt`.

## Non-conference opponents

Hockey, basketball and diamond sports each have a Non-Conf slot for any opponent on a Sidearm athletics site. Set it in that sport's config file:

```
scrapers/<sport>/non_conf_config.json
```

**Hockey** uses `men` and `women` blocks. `style` is `new`, `old` or `asu`.

```json
{
  "men": {
    "name": "Bemidji State",
    "style": "old",
    "stats_url": "https://bsubeavers.com/sports/mens-ice-hockey/stats/2026-27",
    "roster_url": "https://bsubeavers.com/sports/mens-ice-hockey/roster?print=true"
  },
  "women": {
    "name": "Holy Cross",
    "style": "old",
    "stats_url": "https://goholycross.com/sports/womens-ice-hockey/stats/",
    "roster_url": "https://goholycross.com/sports/womens-ice-hockey/roster"
  }
}
```

**Diamond Sports** uses `baseball` and `softball` blocks. `style` is `new`, `old` or `ndsu`.

**Basketball** uses `men` and `women` blocks with `name`, `stats_url` and `roster_url`.

Leave `stats_url` blank and the slot writes an empty placeholder, so dashboards never get a 404. Commit the change and that sport's workflow runs automatically.

## Live scores (`fetch_scores.py`)

`others/scores/fetch_scores.py` is a local tool for live game scores. **No GitHub workflow runs it.** You run it on a PC, and it keeps running until you stop it.

**What it does**

- Reads NCAA game IDs from `game_ids.txt`.
- Polls the free 
[NCAA API](https://ncaa-api.henrygd.me) every 2 minutes.
- Writes a small `scores.json` that a graphics system can read.
- Uses only the Python standard library, so there are no packages to install. The one optional package is `tzdata`, covered below.

### Run it on a Windows PC

**1. Install Python**

Download Python 3.9 or newer from 
[python.org/downloads](https://www.python.org/downloads/). Python 3.12 is a good choice. On the first installer screen, check **Add python.exe to PATH**, then click Install Now.

You can also install it from PowerShell:

```powershell
winget install Python.Python.3.12
```

Close and reopen PowerShell, then confirm it works:

```powershell
python --version
```

**2. Get the files**

You only need the `others/scores` folder. Pick one:

- **Download ZIP:** on the repo page, click **Code → Download ZIP**, then extract it.
- **Git:** `git clone https://github.com/TAPbot-Xpression/TAPbot.git`

Keep `fetch_scores.py` and `game_ids.txt` together in the same folder.

**3. Install `tzdata` (one time)**

Windows doesn't ship with the time zone database that Python needs. Without it, pre-game start times use a fixed Central Standard Time and will be an hour off during daylight saving.

```powershell
python -m pip install tzdata
```

**4. Add the games**

Open `game_ids.txt` in Notepad. Put one NCAA game ID on each line. Lines that start with `#` are ignored.

```
# NCHC tonight
6358934
6358935
```

The ID is the number at the end of the game's page on ncaa.com (`ncaa.com/game/<ID>`).

**5. Start the poller**

```powershell
cd C:\path\to\TAPbot\others\scores
python fetch_scores.py
```

You should see something like this:

```
Polling every 120s. Output: C:\path\to\TAPbot\others\scores\scores.json. Ctrl+C to stop.

[2026-11-25T23:41:07+00:00] Wrote ...\scores.json (2 games)
```

It repeats every 2 minutes. Press **Ctrl+C** to stop. Edit `game_ids.txt` while it runs, and the next cycle picks up the changes.

**6. Optional: a double-click launcher**

Save this as `start_scores.bat` in the same folder:

```bat
@echo off
cd /d "%~dp0"
python fetch_scores.py
pause
```

### Output

`scores.json` is saved next to the script by default:

```json
{
  "updated_at": "2026-11-25T23:41:07+00:00",
  "games": 
[
    {
      "game_id": "6358934",
      "status": "live",
      "period": "2nd",
      "time_remaining": "08:42",
      "teams": 
[
        { "name": "{SUP}3{}Denver", "home": false, "score": "2" },
        { "name": "St. Thomas", "home": true, "score": "1" }
      ]
    }
  ]
}
```

- `status` is `pre`, `live` or `final`.
- For a game that hasn't started, `time_remaining` shows the start time in Central, such as `8:00 PM`, or `TBA`.
- Ranked teams get an Xpression superscript tag, such as `{SUP}3{}Denver`.
- If a game fails to load, its last good data is kept and an `error` field is added. A bad ID never wipes the file.
- The file is written atomically, so a graphics system never reads a half-written file.

### Settings (top of `fetch_scores.py`)

| Setting | Default | What it does |
|---|---|---|
| `INTERVAL_SECONDS` | `120` | Time between polls |
| `DELAY` | `0.5` | Pause between requests. The public API limit is about 3 per second. |
| `DISPLAY_TZ_NAME` | `America/Chicago` | Time zone for pre-game start times |
| `OUTPUT_FILE` | next to the script | Where `scores.json` is saved |
| `TEAM_NAMES` | NCHC teams | Maps alternate names to one clean name |

To save `scores.json` somewhere else, such as a folder your graphics machine reads from, change `OUTPUT_FILE`. Use a raw string on Windows:

```python
OUTPUT_FILE = Path(r"C:\Users\<you>\Documents\scores.json")
```

To fix a team name that shows up wrong, add a line to `TEAM_NAMES`. Use lowercase with no punctuation on the left side:

```python
"neb omaha": "Omaha",
```

### Keeping it running during a broadcast

- **Don't let the PC sleep.** Set Settings → System → Power → Screen and sleep to **Never** while plugged in.
- **Don't click inside the console window.** Windows pauses a program when you select text in its console. If it freezes, click the window and press **Enter**.
- **Minimize the window instead of closing it.** Closing it stops the poller.
- **Many games take longer per cycle.** Each game makes up to two requests, with a half-second pause after each. A long list can take longer than 2 minutes per cycle, and the next cycle then starts right away.

### Troubleshooting

| Symptom | Fix |
|---|---|
| `'python' is not recognized` | Reinstall Python and check **Add python.exe to PATH**. Or try `py fetch_scores.py`. |
| `WARNING: tzdata not found` | Run `python -m pip install tzdata`. |
| `Missing ...\game_ids.txt` | Put `game_ids.txt` in the same folder as the script. |
| `Failed <id>: game not found` | The game ID is wrong, or the game isn't in the NCAA API yet. |
| `Failed <id>: HTTP Error 429` | You are polling too fast. Raise `DELAY` or `INTERVAL_SECONDS`. |
| `scores.json` shows `0 games` | `game_ids.txt` is empty, or every line starts with `#`. |

## Setup (fresh fork or new repo)

1. Create the repo, then go to **Settings → Pages → Source: GitHub Actions**.
2. Go to **Settings → Actions → General → Workflow permissions** and select **Read and write permissions**.
3. Make sure each `scrapers/<sport>/requirements.txt` is present.
4. Run each workflow once from the Actions tab.
5. Open the Hub Site and confirm each in-season source shows a green tally light.

## Adding a sport

1. Create `scrapers/<sport>/` with the scraper and a `requirements.txt`.
2. Make the scraper write to `docs/<sport>/`. Resolve the path from `__file__`, not from the working directory.
3. Copy an existing workflow, then change its name, cron time and paths.
4. Put the dashboard at `docs/<sport>/index.html`.
5. Add a block to the `SPORTS` array in `docs/index.html`. Set each sport's `statusFile` and `seasonMonths` there.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Workflow succeeds but the site doesn't update | The scraper wrote outside `docs/<sport>/`, so no changes were staged. |
| A team's JSON has empty `players` or all `-` stats | The site changed its layout or blocked the scraper. The previous data is kept where possible. Check the workflow log. |
| Hub shows **NO DATA FILE** | The `statusFile` in `docs/index.html` doesn't match a real filename. |
| Hub shows **OFF-SEASON** for an active sport | The current month isn't in that sport's `seasonMonths` in `docs/index.html`. |
| Workflow fails on `git push` | Another workflow pushed first. Re-run it. |
| A scheduled run shows "skipped" | Expected. The time check skipped the cron entry that wasn't the right local time. |
