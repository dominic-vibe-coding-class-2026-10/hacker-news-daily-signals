"""Playwright scraper for the HKET tech section."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from playwright.async_api import Browser, Page, async_playwright

from app.db import (
    acquire_run_lock,
    connect,
    init_db,
    meta_set,
    move_story_dates,
    release_run_lock,
    replace_day,
    save_on_publication_dates,
)
from app.filter import filter_stories
from app.report import write_report
from app.scraper import USER_AGENT, _extract_text
from app.timeutil import (
    age_text_as_of,
    reference_for,
    utcnow,
)

HONG_KONG = ZoneInfo("Asia/Hong_Kong")
ARTICLE_CONCURRENCY = 4
HKET_LIST = "https://inews.hket.com/sran010/%E7%A7%91%E6%8A%80"
HKET_MAX_PAGES = 4
HKET_MAX_STORIES = 30

HKET_JS = """
() => {
  const items = [];
  const seen = new Set();
  for (const title of document.querySelectorAll("div.listing-title")) {
    const link = title.querySelector("a");
    if (!link || !link.href.includes("/article/")) continue;
    const href = link.href.split("?")[0];
    if (seen.has(href)) continue;
    seen.add(href);
    const widget = title.closest("[class*='listing-widget']");
    const parts = widget
      ? [...widget.querySelectorAll(".listing-date span")].map((node) => node.textContent.trim()).filter(Boolean)
      : [];
    items.push({
      title: (link.getAttribute("title") || link.textContent || "").trim(),
      href,
      parts,
    });
  }
  return items;
}
"""

def parse_hket_time(day: str, clock: str) -> datetime | None:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day or ""):
        return None
    if not re.fullmatch(r"\d{1,2}:\d{2}", (clock or "").strip()):
        return None
    parsed = datetime.strptime(f"{day} {(clock or '').strip()}", "%Y-%m-%d %H:%M")
    return parsed.replace(tzinfo=HONG_KONG).astimezone(timezone.utc)


def parse_hket_parts(parts: list[str], now: datetime | None = None) -> datetime | None:
    texts = [part.strip() for part in parts or [] if part and part.strip()]
    clock = next((text for text in texts if re.fullmatch(r"\d{1,2}:\d{2}", text)), "")
    dated = next((text for text in texts if re.fullmatch(r"\d{4}/\d{2}/\d{2}", text)), "")
    if not clock:
        return None
    if dated:
        year, month, day = dated.split("/")
        return parse_hket_time(f"{year}-{month}-{day}", clock)
    if "今日" in texts:
        moment = now or utcnow()
        return parse_hket_time(moment.astimezone(HONG_KONG).date().isoformat(), clock)
    return None


def publication_day(source: str, posted: datetime) -> str:
    """Hong Kong calendar date printed on an HKET card, not the scrape day."""
    zone = HONG_KONG if source == "hket" else timezone.utc
    return posted.astimezone(zone).date().isoformat()


def _absolute(href: str) -> str:
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    return href


async def _route(route) -> None:
    if route.request.resource_type in {"image", "media", "font"}:
        await route.abort()
    else:
        await route.continue_()


async def _collect(page: Page, url: str, script: str) -> list[dict]:
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(500)
    rows = await page.evaluate(script)
    return rows or []


async def _open_articles(browser: Browser, stories: list[dict]) -> None:
    context = await browser.new_context(user_agent=USER_AGENT)
    await context.route("**/*", _route)
    semaphore = asyncio.Semaphore(ARTICLE_CONCURRENCY)

    async def one(story: dict) -> None:
        if len(story.get("raw_content") or "") >= 80:
            return
        async with semaphore:
            page = await context.new_page()
            try:
                await page.goto(story["open_url"], wait_until="domcontentloaded", timeout=20000)
                story["raw_content"] = await _extract_text(page)
            except Exception as exc:
                story["raw_content"] = story.get("raw_content") or ""
                print(f"  skipped {story['title'][:60]} ({exc.__class__.__name__})", flush=True)
            finally:
                await page.close()

    await asyncio.gather(*(one(story) for story in stories))
    await context.close()


def _hket_url(href: str) -> str:
    return _absolute(href).split("?", 1)[0]


def _from_hket(row: dict) -> dict | None:
    title = " ".join((row.get("title") or "").split())
    if not title:
        return None
    url = _hket_url(row.get("href") or "")
    if "/article/" not in url:
        return None
    return {
        "title": title,
        "external_url": url,
        "hn_url": url,
        "open_url": url,
        "points": 0,
        "comments": 0,
        "posted": parse_hket_parts(row.get("parts") or []),
        "raw_content": "",
        "source": "hket",
    }


def keep_hket_page(
    stories: list[dict], reference: datetime, limit: int = HKET_MAX_STORIES
) -> list[dict]:
    """Newest dated tech-section cards. The 24-hour window does not apply."""
    dated = [story for story in stories if isinstance(story.get("posted"), datetime)]
    dated.sort(key=lambda story: story["posted"], reverse=True)
    chosen: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for story in dated:
        key = (" ".join(story["title"].split()).casefold(), story.get("external_url") or "")
        if key in seen:
            continue
        seen.add(key)
        story["age_text"] = age_text_as_of(story["posted"], reference)
        chosen.append(story)
        if len(chosen) >= limit:
            break
    return chosen


async def _collect_hket(page: Page, reference: datetime) -> list[dict]:
    rows: list[dict] = []
    for page_no in range(1, HKET_MAX_PAGES + 1):
        url = HKET_LIST if page_no == 1 else f"{HKET_LIST}?p={page_no}"
        batch = await _collect(page, url, HKET_JS)
        if not batch and page_no > 1:
            break
        rows.extend(batch)
        stories = keep_hket_page(
            [story for row in rows if (story := _from_hket(row))],
            reference,
        )
        if len(stories) >= HKET_MAX_STORIES or not batch:
            return stories
    return keep_hket_page(
        [story for row in rows if (story := _from_hket(row))],
        reference,
    )


async def _scrape_hket() -> None:
    today = utcnow().date()
    reference = reference_for(today)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            locale="zh-HK",
        )
        await context.route("**/*", _route)
        page = await context.new_page()
        try:
            stories = await _collect_hket(page, reference)
        finally:
            await context.close()
        print(f"  hket: {len(stories)} dated tech stories", flush=True)
        if stories:
            await _open_articles(browser, stories)
        await browser.close()

    filtered = _summarize("hket", stories) if stories else []
    for story in filtered:
        story["source"] = "hket"
        posted = story.get("posted")
        story["date"] = (
            publication_day("hket", posted)
            if isinstance(posted, datetime)
            else today.isoformat()
        )
    if filtered:
        dates = save_on_publication_dates("hket", filtered)
    else:
        replace_day(today.isoformat(), [], source="hket")
        dates = [today.isoformat()]
    print(f"Stored {len(filtered)} hket stories on {', '.join(dates)}", flush=True)
    for day in dates:
        write_report(day)


def _summarize(source: str, stories: list[dict]) -> list[dict]:
    if source != "hket":
        return filter_stories(stories)
    # The model prompt drops anything whose age_text is older than 24 hours.
    # These cards are the tech listing, so the request uses a recent age and
    # the stored age stays the real one.
    real_age = {story.get("external_url") or "": story.get("age_text") or "" for story in stories}
    for story in stories:
        story["age_text"] = "1 hour ago"
    kept = filter_stories(stories)
    for story in kept:
        story["age_text"] = real_age.get(story.get("external_url") or "", story.get("age_text") or "")
    return kept


def _match_keys(story: dict) -> list[str]:
    keys: list[str] = []
    for key in (story.get("external_url") or "", story.get("hn_url") or ""):
        if key and key not in keys:
            keys.append(key)
    return keys


async def _listing_index() -> dict[tuple[str, str], str]:
    """Map (source, url) to the publication day shown on the current listing."""
    reference = reference_for(utcnow().date())
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage"],
        )
        context = await browser.new_context(user_agent=USER_AGENT, locale="zh-HK")
        await context.route("**/*", _route)
        page = await context.new_page()
        try:
            hket = await _collect_hket(page, reference)
        finally:
            await context.close()
            await browser.close()
    index: dict[tuple[str, str], str] = {}
    for story in hket:
        posted = story.get("posted")
        if not isinstance(posted, datetime):
            continue
        day = publication_day("hket", posted)
        for key in _match_keys(story):
            index[("hket", key)] = day
    return index


def refile_stored() -> None:
    """Move saved HKET rows onto the Hong Kong date printed on the tech listing."""
    init_db()
    if not acquire_run_lock():
        print("A scrape is already running.", flush=True)
        return
    try:
        meta_set("scrape_message", "Filing stories by publication date")
        index = asyncio.run(_listing_index())
        conn = connect()
        try:
            rows = conn.execute(
                """
                SELECT id, source, date, external_url, hn_url
                FROM stories
                WHERE source = 'hket'
                """
            ).fetchall()
        finally:
            conn.close()
        updates: list[tuple[int, str]] = []
        unmatched = 0
        for row in rows:
            day = ""
            for key in (row["external_url"], row["hn_url"]):
                if key and (found := index.get((row["source"], key))):
                    day = found
                    break
            if not day:
                unmatched += 1
                continue
            if day != row["date"]:
                updates.append((row["id"], day))
        dates = move_story_dates(updates)
        for day in dates:
            write_report(day)
        print(
            f"Moved {len(updates)} stories onto their publication dates. "
            f"{unmatched} stayed put.",
            flush=True,
        )
        meta_set("scrape_message", "Up to date")
    except Exception as exc:
        meta_set("scrape_message", f"Scrape failed: {exc}")
        print(f"Refile failed: {exc}", flush=True)
        raise
    finally:
        release_run_lock()


def scrape_today() -> None:
    init_db()
    if not acquire_run_lock():
        print("A scrape is already running.", flush=True)
        return
    try:
        meta_set("scrape_message", "Scraping HKET tech")
        print("Scraping HKET tech", flush=True)
        asyncio.run(_scrape_hket())
        meta_set("scrape_message", "Up to date")
    except Exception as exc:
        meta_set("scrape_message", f"Scrape failed: {exc}")
        print(f"HKET scrape failed: {exc}", flush=True)
    finally:
        release_run_lock()


if __name__ == "__main__":
    scrape_today()
