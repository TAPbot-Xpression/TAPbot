# TAPbot

**One repository. All TAPbots.**

TAPbot is the data engine behind Tommie Athletic Productions (TAP) broadcast graphics at the University of St. Thomas. It scrapes season stats, standings, polls, and schedules from official athletics sites and saves them as clean JSON. A set of dashboards publishes the JSON on GitHub Pages, and broadcast graphics (Ross Xpression / Datalinq) read the JSON files directly.

> Always check the `generated_at` timestamp in a JSON file before going to air. The Hub Site's tally lights show data freshness at a glance.

## Live site

| Page | Path |
|---|---|
| **Hub Site** | https://tapbot-xpression.github.io/TAPbot/ |
| Hockey (NCHC men, WCHA women) | `/hockey/` |
| Basketball (Summit League men and women) | `/basketball/` |
| Diamond Sports (baseball and softball) | `/diamondsports/` |
| Standings & Polls | `/standings/` |
| Schedules | `/schedulers/` |

JSON files sit next to each dashboard, for example `/hockey/st_thomas_mhockey.json` and `/standings/data.json`.

**Hub Site tally lights**

| Light | Meaning |
|---|---|
| Green | In season, and the data was scraped within the last 24 hours |
| Amber | In season, but the data is more than 24 hours old |
| Grey | Off season (shown even if the scraper ran recently) |
| Red | The data file is missing |

## What's covered

| Sport | Scope | Output folder | Scraper |
|---|---|---|---|
| Hockey | NCHC men (10 programs), WCHA women (8 programs), one Non-Conf slot per gender | `docs/hockey/` | `scrapers/hockey/scrape_hockey.py` |
| Basketball | Summit League men and women (8 programs each), plus Non-Conf slots | `docs/basketball/` | `scrapers/basketball/scrape_basketball.py` |
| Diamond Sports | Summit League baseball (6 programs) and softball (7 programs), plus Non-Conf slots | `docs/diamondsports/` | `scrapers/diamondsports/scrape_diamond_sports.py` |
| Standings & Polls | Summit League, PFL, NCHC and WCHA standings, plus USCHO men's and women's polls | `docs/standings/` | `scrapers/standings/scrape_standings.py` |
| Schedules | North and South Campus schedules, all sports | `docs/schedulers/` | `others/schedulers/schedule_scrape_all.py` |

Live game scores are handled by a separate local tool. See [Live scores](#live-scores).

## How it works

```
Athletics sites → Python scrapers (Playwright) → JSON in docs/<sport>/ → GitHub Pages → Dashboards + Xpression graphics
```

1. A scheduled GitHub Actions workflow runs a sport's scraper.
2. The scraper writes JSON into `docs/<sport>/`.
3. If anything changed, the workflow commits the JSON to `main`.
4. The shared `deploy.yml` publishes the `docs/` folder to GitHub Pages.

**Design rules**

- **Scrapers are independent.** Each sport has its own folder, `requirements.txt` and workflow. Editing one sport never triggers another sport's run.
- **One deploy workflow.** Only `deploy.yml` publishes the site, and it deploys all of `docs/` as one artifact.
- **Last good data is kept.** If a scrape returns nothing (a site outage or layout change), the previous JSON stays in place instead of being overwritten with blanks. The standings scraper marks the affected feed `"status": "stale"`.
- **Unchanged data isn't re-committed.** If the data is identical to the last run, the file is left alone, and the timestamp refreshes at most every 18 hours. This keeps the tally lights green without a commit and deploy on every run.

## Repository layout

```
TAPbot/
├─ .github/workflows/        One workflow per sport, plus deploy.yml
├─ scrapers/                 Python scrapers (not published)
│  ├─ hockey/
│  ├─ basketball/
│  ├─ diamondsports/
│  └─ standings/
├─ others/                   Supporting tools (not published)
│  ├─ schedulers/            schedule_scrape_all.py and campus config files
│  └─ scores/                fetch_scores.py, game_ids.txt (runs locally)
└─ docs/                     The live website
   ├─ index.html             Hub Site
   └─ <sport>/               index.html + JSON data files
```

## Automation schedule

GitHub cron uses UTC only and has no daylight saving support. Each workflow schedules both the CST and CDT times, and a time-check step lets only the correct one continue.

| Workflow | When (Central) | Manual inputs |
|---|---|---|
| `basketball.yml` | Daily, about 3:30 AM | none |
| `diamondsports.yml` | Daily, about 3:45 AM | `sport`, `school`, `year` |
| `schedule_scrapers.yml` | Daily, about 4:00 AM | none |
| `hockey.yml` | Daily, about 4:30 AM | `only_teams`, `enable_debug` |
| `standings.yml` | 4:00 AM, 11:00 AM and 4:00 PM (cron fires at :05) | none |
| `deploy.yml` | Runs after any workflow commits new data, and when `docs/**` changes | none |

**Good to know**

- **Skipped runs are normal.** Each workflow with a time check shows one of its two daily cron runs as skipped.
- **Late starts.** GitHub can start scheduled runs 10 to 30 minutes late. Each time check accepts a start up to 45 minutes after its target time.
- **Push collisions.** Workflows retry `git push` up to three times with a rebase, and fail the run if every attempt fails.
- **Push triggers.** Changing files in `scrapers/<sport>/` runs only that sport's workflow.

## Running a scraper manually

**From GitHub:** Actions → pick the workflow → **Run workflow**. Use the inputs to narrow the run. Manual runs skip the time check.

**On your own computer** (Windows PowerShell, from the repo folder):

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

macOS and Linux use the same commands with forward slashes and `python3`. Each sport has its own `requirements.txt`, so install the one for the sport you're running.

## Non-conference opponents

Hockey, basketball and diamond sports each have a Non-Conf slot for any opponent on a Sidearm athletics site. Set it in that sport's config file:

```
scrapers/<sport>/non_conf_config.json
```

Each config has one block per gender (hockey, basketball) or sport (baseball, softball) with `name`, `stats_url` and `roster_url`. Hockey and diamond sports also take a `style` (hockey: `new`, `old` or `asu`; diamond sports: `new`, `old` or `ndsu`). Example for hockey:

```json
{
  "men": {
    "name": "Bemidji State",
    "style": "old",
    "stats_url": "https://bsubeavers.com/sports/mens-ice-hockey/stats/2026-27",
    "roster_url": "https://bsubeavers.com/sports/mens-ice-hockey/roster?print=true"
  }
}
```

Leave `stats_url` blank and the slot writes an empty placeholder, so dashboards never get a 404. Commit the change and that sport's workflow runs automatically.

## Live scores

`others/scores/fetch_scores.py` is a small tool for live game scores. It is not run by GitHub Actions.

- Runs locally on your machine with Python. It uses only the standard library, so there is nothing to install.
- Get game IDs from the NCAA website (the number at the end of a game's page URL, `ncaa.com/game/<ID>`) and add them to `game_ids.txt`, one per line.
- Writes `scores.json` every 2 minutes. Point an Xpression Datalinq data source at that file to update live graphics.

To start it:

```powershell
cd others\scores
python fetch_scores.py
```

Press `Ctrl+C` to stop.

## Setup (fresh fork or new repo)

1. Go to **Settings → Pages → Source** and choose **GitHub Actions**.
2. Go to **Settings → Actions → General → Workflow permissions** and select **Read and write permissions**.
3. Make sure each `scrapers/<sport>/requirements.txt` is present.
4. Run each workflow once from the Actions tab.
5. Open the Hub Site and confirm each in-season source shows a green tally light.

## Adding a sport

1. Create `scrapers/<sport>/` with the scraper and a `requirements.txt`.
2. Have the scraper write to `docs/<sport>/`, resolving the path from `__file__` rather than the working directory.
3. Copy an existing workflow and change its name, cron time and paths.
4. Put the dashboard at `docs/<sport>/index.html`.
5. Add a block to the `SPORTS` array in `docs/index.html`, setting `statusFile` and `seasonMonths`.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Workflow succeeds but the site doesn't update | The scraper wrote outside `docs/<sport>/`, or the data was unchanged. |
| A team's JSON has empty `players` or all `-` stats | The site changed its layout or blocked the scraper. Previous data is kept where possible. Check the workflow log. |
| Hub shows **NO DATA FILE** | The `statusFile` in `docs/index.html` doesn't match a real filename. |
| Hub shows **OFF-SEASON** for an active sport | The current month isn't in that sport's `seasonMonths` in `docs/index.html`. |
| Workflow fails on `git push` | Another workflow pushed first. Re-run it. |
| A scheduled run shows "skipped" | Expected. The time check skipped the cron entry that wasn't the right local time. |
