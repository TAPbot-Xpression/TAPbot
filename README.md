# TAPbot

**One repository. All TAPbots.**

TAPbot is the data engine behind Tommie Athletic Productions (TAP) broadcast graphics at the University of St. Thomas. It scrapes season stats, standings, and polls from official athletics sites, saves them as clean JSON, and publishes them with a set of dashboards on GitHub Pages. Broadcast graphics (Ross Xpression / Datalinq) read the JSON files directly.

> Always check the `generated_at` timestamp in a JSON file before going to air. The Hub Site's tally lights show data freshness at a glance.

---

## Live site

| Page | Path |
|---|---|
| **Hub Site** | `https://tapbot-xpression.github.io/TAPbot/` |
| NCHC Hockey | `/hockey/` |
| Summit Basketball | `/basketball/` |
| Diamond Sports (Baseball & Softball) | `/diamondsports/` |
| Standings & Polls | `/standings/` |

JSON files sit next to each dashboard, for example `/hockey/st_thomas_mhockey.json` and `/standings/data.json`.

---

## What's covered

| Sport | Scope | Output folder | Scraper |
|---|---|---|---|
| **Hockey** | NCHC men's ice hockey: 9 conference programs plus a Non-Conf slot | `docs/hockey/` | `scrapers/hockey/scrape_nchc_hockey.py` |
| **Basketball** | Summit League men's and women's basketball (8 programs each) plus Non-Conf slots | `docs/basketball/` | `scrapers/basketball/scrape_basketball.py` |
| **Diamond Sports** | Summit League baseball and softball plus Non-Conf slots | `docs/diamondsports/` | `scrapers/diamondsports/scrape_diamond_sports.py` |
| **Standings & Polls** | Summit League, PFL, NCHC, and WCHA standings plus USCHO men's and women's polls | `docs/standings/` | `scrapers/standings/scrape_standings.py` |

---

## How it works

```
Athletics sites  →  Python scrapers (Playwright)  →  JSON in docs/<sport>/  →  GitHub Pages  →  Dashboards + Xpression graphics
```

1. A scheduled **GitHub Actions** workflow runs a sport's scraper.
2. The scraper writes JSON into `docs/<sport>/`.
3. If anything changed, the workflow commits the JSON to `main`.
4. The workflow then calls the shared **`deploy.yml`**, which publishes the whole `docs/` folder to GitHub Pages.

### Design rules

- **Scrapers are independent.** Each sport has its own folder, `requirements.txt`, and workflow. Editing one sport never triggers another sport's run.
- **One deploy workflow.** Only `deploy.yml` publishes the site. It deploys all of `docs/` as a single artifact, so one sport's deploy can't wipe out the others.
- **Each dashboard sits next to its data.** Pages fetch their JSON with relative paths such as `fetch("denver_mhockey.json")`.
- **Scrapers keep the last good data.** If a scrape returns nothing, such as during a site outage or after a layout change, the previous JSON is kept instead of being overwritten with blanks. The standings scraper marks the affected feed `"status": "stale"`.

---

## Repository structure

```
TAPbot/
├─ .github/workflows/
│  ├─ hockey.yml            Scrape NCHC hockey
│  ├─ basketball.yml        Scrape Summit basketball
│  ├─ diamondsports.yml     Scrape baseball + softball
│  ├─ standings.yml         Scrape standings + polls
│  └─ deploy.yml            Publish docs/ to GitHub Pages
│
├─ scrapers/                Python only. Not published.
│  ├─ hockey/               scrape_nchc_hockey.py, requirements.txt, non_conf_config.json
│  ├─ basketball/           scrape_basketball.py, requirements.txt, non_conf_config.json
│  ├─ diamondsports/        scrape_diamond_sports.py, requirements.txt, non_conf_config.json
│  └─ standings/            scrape_standings.py, requirements.txt
│
└─ docs/                    The live website
   ├─ index.html            Hub Site
   ├─ hockey/               index.html + one JSON per team
   ├─ basketball/           index.html + one JSON per team and gender
   ├─ diamondsports/        index.html + one JSON per team and sport
   └─ standings/            index.html + data.json
```

---

## Automation schedule

All times are shown in St. Paul local time. GitHub cron runs in UTC.

| Workflow | Schedule | Manual inputs |
|---|---|---|
| `hockey.yml` | Daily, 14:15 UTC (about 8:15 AM CST / 9:15 AM CDT) | `only_teams`, `enable_debug` |
| `diamondsports.yml` | Daily, 14:30 UTC | `sport`, `school`, `year` |
| `basketball.yml` | Daily, 14:45 UTC | none |
| `standings.yml` | 4:00 AM, 11:00 AM, and 4:00 PM Central, all year | none |
| `deploy.yml` | Called by each scraper after new data is committed. Also runs when `docs/**` changes. | none |

Notes:
- **Staggered start times.** Run times are offset so the pushes to `main` don't collide. Each workflow also retries its push up to three times with a rebase.
- **Standings and daylight saving.** GitHub cron has no daylight saving support, so each standings run is scheduled for both CST and CDT. A "Check Central time" step lets only the correct one through.
- **Push triggers.** Pushing changes to `scrapers/<sport>/**` runs that sport's workflow only.

---

## Running a scraper manually

### From GitHub
**Actions → pick the workflow → Run workflow.** Use the inputs to narrow the run:

- Hockey: `only_teams` = `St. Thomas,Denver`
- Diamond Sports: `sport` = `softball`, `school` = `omaha`, `year` = `2026`

### Locally

```bash
pip install -r scrapers/<sport>/requirements.txt
playwright install chromium

python scrapers/hockey/scrape_nchc_hockey.py --only "St. Thomas,Denver"
python scrapers/basketball/scrape_basketball.py
python scrapers/diamondsports/scrape_diamond_sports.py --sport softball --school omaha
python scrapers/standings/scrape_standings.py
```

Each script finds the repo root from its own location, so you can run it from any folder.

---

## Non-conference opponents

Hockey, basketball, and diamond sports each have a **Non-Conf** slot for any opponent on a Sidearm athletics site. Set it in that sport's config file:

```
scrapers/<sport>/non_conf_config.json
```

- **Diamond Sports** format, one block per sport (use `new`, `old`, or `ndsu` for `style`):

  ```json
  {
    "baseball": { "name": "Opponent", "style": "new", "stats_url": "https://…/sports/baseball/stats/2026", "roster_url": "https://…/roster/print" },
    "softball": { "name": "", "style": "new", "stats_url": "", "roster_url": "" }
  }
  ```

- **Basketball** uses `men` and `women` blocks with `name`, `stats_url`, and `roster_url`.
- **Hockey** uses the same fields. Check the existing `scrapers/hockey/non_conf_config.json` for its exact layout.

Leave `stats_url` blank and the slot writes an empty placeholder, so dashboards never get a 404. Commit the change and that sport's workflow runs automatically.

---

## Output files

Every JSON file includes a `generated_at` timestamp.

| Sport | Files | Main contents |
|---|---|---|
| Hockey | `<team>_mhockey.json`, `non_conf_mhockey.json` | `team_stats` (goals/game, shots/game, shot %, PP %, PK %, penalties, PIM, faceoff %) and `players` (skaters and goaltenders) |
| Basketball | `<team>_men.json`, `<team>_women.json`, `non_conf_men.json`, `non_conf_women.json` | `team_stats`, `conference_team_stats`, and `players`. Status fields show how each section was obtained. |
| Diamond Sports | `<team>_baseball.json`, `<team>_softball.json`, `non_conf_baseball.json`, `non_conf_softball.json` | `hitting`, `pitching`, `players`, and lookups by jersey number |
| Standings | `data.json` | `sports.<id>.rows`, each feed with a `status` of `ok`, `stale`, or `error` |

---

## Setup (for a fresh fork or new repo)

1. Create the repo, then go to **Settings → Pages → Source: GitHub Actions**.
2. Go to **Settings → Actions → General → Workflow permissions** and select **Read and write permissions**.
3. Make sure each `scrapers/<sport>/requirements.txt` is present.
4. Run each workflow once from the **Actions** tab.
5. Open the Hub Site and confirm each source shows a green **LIVE** tally light.

---

## Adding a sport

1. Create `scrapers/<sport>/` with the scraper and a `requirements.txt`.
2. Make the scraper write to `docs/<sport>/`. Resolve the path from `__file__`, not from the working directory.
3. Copy an existing workflow, then change its name, cron time, and paths.
4. Put the dashboard at `docs/<sport>/index.html`.
5. Add a block to the `SPORTS` array in `docs/index.html`.

---

## Hub Site tally lights

| Light | Meaning |
|---|---|
| 🟢 **LIVE** | Data updated within the last 36 hours |
| 🟠 **STALE** | Data is old and the sport is in season |
| ⚪ **OFF-SEASON** | Data is old and the sport is out of season |
| 🔴 **NO DATA FILE** | The status file wasn't found |

Each sport's `statusFile` and `seasonMonths` are set in the `SPORTS` array in `docs/index.html`.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Workflow succeeds but the site doesn't update | The scraper wrote outside `docs/<sport>/`, so no changes were staged. |
| A team's JSON has empty `players` or all `-` stats | The site changed its layout or blocked the scraper. The previous data is kept where possible. Check the workflow log. |
| Hub shows **NO DATA FILE** | The `statusFile` in `docs/index.html` doesn't match a real filename. |
| Workflow fails on `git push` | Another workflow pushed first. Re-run it. |
| Non-Conf slot is empty | `stats_url` is blank in that sport's `non_conf_config.json`. |

---

## Credits

Developed by **St. Thomas Athletics · Tommie Athletic Productions**. Hosted on GitHub Pages.
