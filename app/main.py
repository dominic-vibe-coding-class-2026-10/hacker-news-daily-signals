"""FastAPI dashboard for Hacker News daily signals."""

from __future__ import annotations

import os
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from app.db import (
    DATA_DIR,
    count_english_headlines,
    day_row,
    has_completed_preload,
    init_db,
    list_days,
    meta_get,
    meta_set,
    stories_for,
)
from app.sources import SOURCES, SOURCE_IDS
from app.titles import localized_headline

ROOT = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def scrape_running() -> bool:
    raw = meta_get("scrape_pid")
    if not raw:
        return False
    try:
        pid = int(raw)
    except ValueError:
        return False
    return bool(pid) and _pid_alive(pid)


def _boot_scrape() -> None:
    """Preload the last week only when this database has never finished one."""
    try:
        init_db()
        try:
            from app.titles import backfill_english_titles

            updated = backfill_english_titles()
            if updated:
                print(f"Stored {updated} English headlines", flush=True)
        except Exception as exc:
            print(f"English headline backfill failed: {exc}", flush=True)
        if has_completed_preload():
            return
        from app.scraper import run_plan

        run_plan()
    except Exception as exc:
        init_db()
        meta_set("scrape_message", f"Scrape failed: {exc}")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    thread = threading.Thread(target=_boot_scrape, name="hn-scrape", daemon=True)
    thread.start()
    yield


app = FastAPI(title="Hacker News Daily Signals", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html")


@app.get("/api/status")
def status():
    return {
        "running": scrape_running(),
        "message": meta_get("scrape_message"),
        "preload_complete": has_completed_preload(),
        "english_titles": count_english_headlines(),
        "days": list_days(),
    }


@app.get("/api/sources")
def sources():
    return {"sources": list(SOURCES)}


@app.get("/api/stories")
def stories(date: str, sources: str = "", lang: str = "en"):
    if not DATE_RE.fullmatch(date):
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    if not sources.strip():
        chosen = list(SOURCE_IDS)
    else:
        chosen = [item for item in sources.split(",") if item in SOURCE_IDS]
    rows = stories_for(date, chosen)
    for row in rows:
        row["headline"] = localized_headline(row, lang)
    return {"day": day_row(date), "stories": rows}


@app.get("/reports/{day}")
def report(day: str):
    if not DATE_RE.fullmatch(day):
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    path = DATA_DIR / "reports" / f"{day}.md"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="report not found")
    return FileResponse(path, media_type="text/markdown; charset=utf-8", filename=path.name)
