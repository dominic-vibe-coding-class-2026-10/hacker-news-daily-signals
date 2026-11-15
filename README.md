# Hacker News Daily Signals

A local daily tracker for [Hacker News](https://news.ycombinator.com/). Playwright opens the front page, clicks **More** at most three times, and opens each story title. Ollama Cloud keeps stories from the last 24 hours and writes a short summary in English and Traditional Chinese. Results are stored in SQLite and shown on a small dashboard.

The first run preloads the last 7 calendar days from Hacker News archive front pages (`/front?day=YYYY-MM-DD`). Later runs refresh today only.

Firecrawl and Tavily keys are optional and are not used. The scrape always goes through Playwright.

## Install on Linux and macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
```

Edit `.env` and set `OLLAMA_CLOUD_API_KEY` and `OLLAMA_MODEL`. `OLLAMA_API_KEY` is accepted as an alias of the cloud key.

## Install on Windows

```powershell
py -3 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
copy .env.example .env
```

Edit `.env` the same way as on Linux.

## Run the scraper once

From the project directory, with the virtual environment activated:

```bash
python -m app.scraper
```

On a new database this loads the last 7 days. After that it scrapes the current front page and keeps stories posted in the last 24 hours. Each day is also written to `data/reports/YYYY-MM-DD.md`.

## Run the dashboard

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. The page has an English / 繁體中文 switch and a date menu of every day in the database. Starting the dashboard on an empty database also runs the first-week preload.

## Schedule it daily

The scheduled command is `python -m app.scraper` from this directory, using the virtual environment's Python.

Linux cron, every day at 07:00:

```cron
0 7 * * * cd /absolute/path/to/hacker-news-daily-signals && .venv/bin/python -m app.scraper >> data/scraper.log 2>&1
```

Windows Task Scheduler:

```powershell
schtasks /create /tn "HN Daily Signals" /sc daily /st 07:00 /tr "C:\absolute\path\to\hacker-news-daily-signals\.venv\Scripts\python.exe -m app.scraper"
```

Set the task's "Start in" directory to the project folder so `.env` and `data/` resolve correctly.

The first run preloads one week of archive. Scheduled runs after that refresh the current day.
