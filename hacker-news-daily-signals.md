# Hacker News Daily Signals Tracker

Replication manifest. Build this local app exactly as specified. Do not invent story URLs, points, or comment counts. Do not commit real API keys.

## Goal

Build a daily service that:

1. Reads two front pages with Playwright, in this order:
   - HKET tech section (香港經濟日報 科技): `https://inews.hket.com/sran010/科技`
   - Hacker News: `https://news.ycombinator.com/`
2. Keeps Hacker News stories posted in the last 24 hours. HKET keeps the newest 30 dated cards on the tech section, including ones older than 24 hours. Each HKET card is stored on the Hong Kong date printed on that card, not on the day the scraper happened to run.
3. Opens each kept story and stores the first 2000 characters of clean article text.
4. Uses Ollama Cloud to write a short summary in English and Traditional Chinese.
5. Stores every result in local SQLite.
6. Serves a dashboard where the reader chooses which of the two sources to show. HKET is the first checkbox.

## Source filter (required UI)

The dashboard has one checkbox per source, all checked by default, in this order:

- HKET (`hket`) — the checkbox label is “HKET” in both languages. The footer names it 香港經濟日報科技版 in Traditional Chinese. `label_zh` on the API is 香港經濟日報. The listing URL is the tech section, not the all-news page.
- Hacker News (`hn`)

Behavior:

- Checking or unchecking a box does not change the list.
- An **Apply** button (`套用` in Traditional Chinese) commits the selection.
- The reader can apply one source or both.
- Applying zero sources shows: “Select at least one source, then press Apply.” and does not list stories.
- The applied selection is saved in `localStorage` under `hn-sources` and restored on reload.
- A saved selection that is exactly `hn,lobsters,slashdot` becomes `hket,hn`. Any other saved id for Lobsters or Slashdot is dropped. If nothing known remains, both sources are selected.
- `GET /api/stories?date=YYYY-MM-DD&sources=hket,hn` returns only those sources. Unknown ids, including `lobsters` and `slashdot`, are ignored. An empty `sources` value means both.
- Each story card shows the source name, points, and comment count.
- The language switch still changes every UI label, including Sources, Apply, and the empty-state sentence. Story summaries follow the active language.

Empty selection copy:

- English: “No stories from the selected sources for this day.”
- Traditional Chinese: “選定的來源在這一天沒有故事。”

## Architecture

- **Playwright**: open each front page, page through Hacker News, and open story URLs.
- **Ollama Cloud** at `https://ollama.com/api/chat`: recency filter plus bilingual summaries. Model comes from `OLLAMA_MODEL`.
- **SQLite** file `data/hn.db`.
- **FastAPI** plus `templates/dashboard.html`. No frontend build step.

Firecrawl and Tavily are optional fallbacks only. Do not use them for the main flow. `HF_TOKEN` is unused.

## Deterministic collection

### Hacker News

- Start at `https://news.ycombinator.com/` for today, or `https://news.ycombinator.com/front?day=YYYY-MM-DD` for an archive day.
- Click `a.morelink` at most 3 times.
- From each `tr.athing`, read title, external href, discussion `https://news.ycombinator.com/item?id=ID`, points, comment count, and the submission timestamp on `span.age[title]`.
- The visible “N hours ago” label can disagree with that timestamp. Decide recency from the timestamp, not the label.
- For an archive day, the 24-hour window ends at 23:59:59 UTC on that date. For today, it ends at the current UTC time.
- Keep a story when `0 <= reference - posted_at <= 24 hours`.
- Open the external URL when it is an `http` link. Otherwise open the discussion URL.
- First run only: preload the last 7 UTC calendar days. Later runs refresh today only.
- Replacing a day’s Hacker News rows must not delete HKET rows for that date.

### HKET

- Open the tech section `https://inews.hket.com/sran010/科技`, then `?p=2`, `?p=3`, and `?p=4` at most. Do not read the all-news listing.
- From each `div.listing-title a` read the title attribute, else the text. Skip links that are not `/article/` URLs, and skip a card that appears again on the same page.
- Read every `.listing-date span` in that card’s listing widget. A card dated `HH:MM` plus `YYYY/MM/DD` uses that calendar date. A card labeled `今日` plus `HH:MM` uses the current Hong Kong date. Cards with no time are featured items and are skipped.
- Treat that clock time as `Asia/Hong_Kong`, then convert to UTC. Do not drop a dated tech card because it is older than 24 hours. Hacker News still uses that window.
- Keep the article href with the query string removed. Ignore links that are not `/article/` URLs. The same article can appear as the hero and again in the list; store it once.
- HKET does not show points or comment counts. Store both as `0`. Do not invent them.
- Page through `?p=2` and later only until 30 unique dated cards are kept, a page is empty, or four pages have been read. Sort by posted time, newest first. Never store more than those 30.
- The model prompt still excludes an age older than 24 hours. Send HKET cards to the model with `age_text` of `1 hour ago`, then store the real age text. Do not change the system prompt.
- Open each kept article and extract text. Store under source id `hket` on the Hong Kong calendar date printed on the card (`YYYY/MM/DD`, or the current Hong Kong date when the card says `今日`). A card timestamped before 08:00 Hong Kong time still uses that printed date, even though the UTC instant falls on the previous day. Do not file every card under the scrape day.
- Days that never received a card with that printed date stay empty for HKET.

### Shared extraction

- Deduplicate within a source by normalized title plus external URL.
- Article text is the first 2000 characters of visible text from `article`, else `.fatitem`, else `main`, else `body`, with `script`, `style`, `noscript`, `svg`, and `iframe` removed.
- Send Ollama at most the first 800 characters of that text. Store the 2000-character extract.
- Never add a story the page did not contain. If the model changes a URL or a point value, discard the model’s value and keep the scraped one.
- A story is saved only when both `summary_en` and `summary_zh` are non-empty.

## System prompt for Ollama

Use this text exactly for every source:

```
You are a strict filter. Given a list of Hacker News stories with their age text, return only those that are 24 hours old or newer. Output valid JSON array of objects with keys: title, external_url, points, comment_count, summary_en (max 2 sentences in English), summary_zh (max 2 sentences in Traditional Chinese). If a story is older than 24 hours, exclude it completely. Do not add any story that was not in the input.
```

Request body:

- `POST https://ollama.com/api/chat`
- `Authorization: Bearer` the cloud key
- `stream: false`, `think: false`, `temperature: 0`
- The user message is a JSON array of `{title, external_url, points, comment_count, age_text, content}` and tells the model to copy title, URL, points, and comment_count exactly.
- `age_text` is computed from the real timestamp against the reference time (`5 hours ago`, `1 day ago`), not from a mismatched on-page label.
- Parse a JSON array even if the model wraps it in a fence or an object with a `stories` key.
- Send stories in batches of 4, up to 4 batches at a time.
- On HTTP 429 or 5xx, retry with backoff. On a bad JSON response, split the batch and retry.

Auth accepts `OLLAMA_CLOUD_API_KEY` or, if that is empty, `OLLAMA_API_KEY`.

## Environment

`.env.example` must exist. `.env` is gitignored.

```
OLLAMA_CLOUD_API_KEY=your_key_here
OLLAMA_MODEL=llama3.1:8b
FIRECRAWL_API_KEY=optional
TAVILY_API_KEY=optional
```

`OLLAMA_API_KEY` is an accepted alias of the cloud key. This deployment uses `OLLAMA_MODEL=kimi-k3.1-pro`.

## Database

`data/hn.db`.

```sql
CREATE TABLE stories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,              -- publication day, YYYY-MM-DD (Hong Kong for HKET, UTC for Hacker News)
    source TEXT NOT NULL DEFAULT 'hn',  -- hket | hn
    title TEXT NOT NULL,
    external_url TEXT NOT NULL DEFAULT '',
    hn_url TEXT NOT NULL DEFAULT '', -- discussion URL for every source
    points INTEGER NOT NULL DEFAULT 0,
    comments INTEGER NOT NULL DEFAULT 0,
    summary_en TEXT NOT NULL DEFAULT '',
    summary_zh TEXT NOT NULL DEFAULT '',
    raw_content TEXT NOT NULL DEFAULT '',
    UNIQUE(date, source, title, external_url)
);

CREATE TABLE days (
    date TEXT PRIMARY KEY,
    note TEXT NOT NULL DEFAULT '',          -- "empty" when the day has zero stories
    story_count INTEGER NOT NULL DEFAULT 0, -- total across all sources
    scraped_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

Migrate an older table that lacks `source` by adding the column (default `hn`) and rebuilding the unique key so the same URL can exist once per source per day.

`meta.preload_complete = 1` after the first successful 7-day Hacker News preload. `meta.scrape_pid` is the running scraper pid; a dead pid may be replaced.

## HTTP API

- `GET /` dashboard HTML.
- `GET /api/status` → `{running, message, preload_complete, days: [{date, note, story_count, scraped_at}]}`.
- `GET /api/sources` → the two sources with `id`, `label_en`, `label_zh`, and `url`, HKET first.
- `GET /api/stories?date=YYYY-MM-DD&sources=hket,hn`.
- `GET /reports/YYYY-MM-DD` returns that day’s Markdown file.

On an empty database, starting the server starts the Hacker News 7-day preload in a background thread. After `preload_complete`, opening the dashboard does not scrape again.

## Dashboard

One page, no framework. Paper background, serif headlines, sans-serif controls.

- Title switches between “Hacker News Daily Signals” and “Hacker News 每日訊號”.
- Language control: English / 繁體中文. Persist `hn-lang`.
- Date menu lists every `days.date`, newest first. Changing the date reloads stories published on that date for the already applied sources. HKET cards do not all appear under the scrape day.
- Source checkboxes and Apply sit under the header.
- While a scrape runs, show its status message. Translate `Scraping YYYY-MM-DD (i/n)` to `正在抓取 YYYY-MM-DD（i/n）` in Chinese mode.
- Poll `/api/status` every 3 seconds. Reload the story list only when the selected date or the stored day signatures change.
- Story link opens `external_url`, or the discussion URL when the external URL is empty, in a new tab.
- Within a day, cards are grouped by source in checkbox order (HKET, then Hacker News). Inside a source they are ordered by points, then comments, then title.

## Markdown report

Write `data/reports/YYYY-MM-DD.md` from every source stored for that day.

```
# Hacker News Daily Signals — YYYY-MM-DD

## Stories from last 24 hours

## HKET

### [Title](external_url)
- Source: HKET
- Points: 0 | Comments: 0
- Summary (EN): ...
- Summary (ZH): ...

## Hacker News

### [Title](external_url)
- Source: Hacker News
- Points: X | Comments: Y
- Summary (EN): ...
- Summary (ZH): ...
```

Omit a source section when that source has no rows. Rewriting the file must include every source still in the database, not only the source just scraped.

## Project layout

```
hacker-news-daily-signals/
├── .env.example
├── .gitignore              # ignores .env, .venv, data/*.db, data/reports/
├── requirements.txt        # fastapi, uvicorn, jinja2, python-dotenv, playwright, httpx
├── README.md
├── hacker-news-daily-signals.md
├── app/
│   ├── main.py             # FastAPI dashboard
│   ├── scraper.py          # Hacker News Playwright flow and daily plan
│   ├── other_sources.py    # HKET Playwright flow
│   ├── filter.py           # Ollama Cloud filter and bilingual summaries
│   ├── db.py               # SQLite
│   ├── sources.py          # the two source ids and labels, HKET first
│   ├── report.py           # Markdown writer
│   └── timeutil.py         # 24-hour window and age text
├── data/
│   ├── hn.db
│   └── reports/
│       └── YYYY-MM-DD.md
├── templates/
│   └── dashboard.html
└── tests/
    └── test_core.py
```

## Commands

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
python -m app.scraper              # Hacker News: 7 days on first run, else today; then HKET tech
python -m app.other_sources        # HKET tech only
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Daily schedule, after the first week exists, is `python -m app.scraper` from the project directory.

Linux cron at 07:00:

```cron
0 7 * * * cd /absolute/path/to/hacker-news-daily-signals && .venv/bin/python -m app.scraper >> data/scraper.log 2>&1
```

Windows Task Scheduler:

```powershell
schtasks /create /tn "HN Daily Signals" /sc daily /st 07:00 /tr "C:\absolute\path\to\hacker-news-daily-signals\.venv\Scripts\python.exe -m app.scraper"
```

Set the task’s start directory to the project folder.

## README

The README must explain install on Linux, macOS, and Windows, how to fill in `.env`, how to run the scraper once, how to run the dashboard, both schedule snippets above, and that the first run preloads one week of Hacker News while HKET tech is collected on that same run and stored on each card’s Hong Kong publication date.

## Acceptance checks

1. The source row is HKET, then Hacker News. With both boxes checked, Apply shows HKET plus Hacker News rows for the selected date. Lobsters and Slashdot are not offered.
2. Checking only one box does nothing until Apply. After Apply, cards from the other sources disappear.
3. Checking both boxes and pressing Apply brings the hidden source back.
4. Applying with every box cleared shows the “select at least one source” message.
5. English / 繁體中文 changes the chrome and which summary is rendered. The HKET checkbox stays labeled “HKET”.
6. The date menu still switches days. An HKET card printed as `2026/11/14` appears on 2026-11-14. It does not appear only on the scrape day.
7. Refreshing Hacker News does not delete HKET rows for that date. Saving HKET onto its publication dates removes that source’s copy of the same URL from the scrape day and leaves Hacker News rows in place.
8. Summaries exist in both languages. Points and URLs match the scraped page. HKET points and comments stay 0.
