"""Playwright scrapers for HKET, Lobsters, and Slashdot."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from playwright.async_api import Browser, Page, async_playwright

from app.db import (
    acquire_run_lock,
    init_db,
    meta_set,
    release_run_lock,
    replace_day,
)
from app.filter import filter_stories
from app.report import write_report
from app.scraper import USER_AGENT, _extract_text
from app.timeutil import (
    age_text_as_of,
    parse_comments,
    parse_points,
    reference_for,
    utcnow,
    within_window,
)

EASTERN = ZoneInfo("America/New_York")
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

LOBSTERS_JS = """
() => [...document.querySelectorAll("div.story_liner")].map((row) => {
  const link = row.querySelector("h2.link a");
  const time = row.querySelector("time");
  const comments = row.querySelector("a.comments_label");
  const score = row.querySelector("a.upvoter");
  return {
    title: link ? link.textContent.trim() : "",
    href: link ? link.href : "",
    discussion: comments ? comments.href : "",
    posted_unix: time ? time.getAttribute("data-at-unix") || "" : "",
    comments: comments ? comments.textContent.trim() : "",
    score: score ? score.textContent.trim() : "",
  };
})
"""

SLASHDOT_JS = """
() => [...document.querySelectorAll("span.story-title")].map((title) => {
  const link = title.querySelector("a");
  const external = title.querySelector("a.story-sourcelnk");
  const bubble = title.querySelector(".comment-bubble a");
  const heading = title.closest("h2");
  const details = heading ? heading.nextElementSibling : null;
  const time = details ? details.querySelector("time") : null;
  const body = details ? details.nextElementSibling : null;
  return {
    title: link ? link.textContent.trim() : "",
    href: external ? external.href : "",
    discussion: link ? link.href : "",
    posted_label: time ? (time.getAttribute("datetime") || time.textContent || "") : "",
    comments: bubble ? bubble.textContent.trim() : "",
    content: body ? (body.innerText || "").replace(/\\s+/g, " ").trim().slice(0, 2000) : "",
  };
})
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


def parse_slashdot_time(label: str) -> datetime | None:
    match = re.search(
        r"([A-Z][a-z]+ \d{1,2}, \d{4}) @(\d{1,2}:\d{2})(AM|PM)",
        label or "",
    )
    if not match:
        return None
    parsed = datetime.strptime(
        f"{match.group(1)} {match.group(2)}{match.group(3)}",
        "%B %d, %Y %I:%M%p",
    )
    return parsed.replace(tzinfo=EASTERN).astimezone(timezone.utc)


def _absolute(href: str) -> str:
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    return href


def _recent(stories: list[dict], reference: datetime) -> list[dict]:
    chosen: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for story in stories:
        posted = story.get("posted")
        if not isinstance(posted, datetime) or not within_window(posted, reference):
            continue
        key = (" ".join(story["title"].split()).casefold(), story.get("external_url") or "")
        if key in seen:
            continue
        seen.add(key)
        story["age_text"] = age_text_as_of(posted, reference)
        chosen.append(story)
    return chosen


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


def _from_lobsters(row: dict) -> dict | None:
    title = " ".join((row.get("title") or "").split())
    if not title:
        return None
    unix = str(row.get("posted_unix") or "").strip()
    posted = None
    if unix.isdigit():
        posted = datetime.fromtimestamp(int(unix), timezone.utc)
    external = _absolute(row.get("href") or "")
    discussion = _absolute(row.get("discussion") or "")
    if external.startswith("https://lobste.rs/"):
        external = ""
    return {
        "title": title,
        "external_url": external,
        "hn_url": discussion,
        "open_url": external or discussion,
        "points": parse_points(row.get("score") or ""),
        "comments": parse_comments(row.get("comments") or ""),
        "posted": posted,
        "raw_content": "",
        "source": "lobsters",
    }


def _from_slashdot(row: dict) -> dict | None:
    title = " ".join((row.get("title") or "").split())
    if not title:
        return None
    external = _absolute(row.get("href") or "")
    discussion = _absolute(row.get("discussion") or "")
    return {
        "title": title,
        "external_url": external,
        "hn_url": discussion,
        "open_url": external or discussion,
        "points": 0,
        "comments": parse_comments(row.get("comments") or ""),
        "posted": parse_slashdot_time(row.get("posted_label") or ""),
        "raw_content": " ".join((row.get("content") or "").split())[:2000],
        "source": "slashdot",
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


async def _scrape_both() -> None:
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
        collected: dict[str, list[dict]] = {}
        try:
            collected["hket"] = await _collect_hket(page, reference)
            lobsters_rows = await _collect(page, "https://lobste.rs/", LOBSTERS_JS)
            slashdot_rows = await _collect(page, "https://slashdot.org/", SLASHDOT_JS)
        finally:
            await context.close()
        collected["lobsters"] = _recent(
            [story for row in lobsters_rows if (story := _from_lobsters(row))],
            reference,
        )
        collected["slashdot"] = _recent(
            [story for row in slashdot_rows if (story := _from_slashdot(row))],
            reference,
        )
        for source, stories in collected.items():
            label = "dated tech stories" if source == "hket" else "within 24 hours"
            print(f"  {source}: {len(stories)} {label}", flush=True)
            if stories:
                await _open_articles(browser, stories)
        await browser.close()

    for source, stories in collected.items():
        filtered = _summarize(source, stories) if stories else []
        for story in filtered:
            story["source"] = source
        replace_day(today.isoformat(), filtered, source=source)
        print(f"Stored {len(filtered)} {source} stories for {today.isoformat()}", flush=True)
    write_report(today.isoformat())


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


def scrape_today() -> None:
    init_db()
    if not acquire_run_lock():
        print("A scrape is already running.", flush=True)
        return
    try:
        meta_set("scrape_message", "Scraping HKET, Lobsters, and Slashdot")
        print("Scraping HKET, Lobsters, and Slashdot", flush=True)
        asyncio.run(_scrape_both())
        meta_set("scrape_message", "Up to date")
    except Exception as exc:
        meta_set("scrape_message", f"Scrape failed: {exc}")
        print(f"HKET/Lobsters/Slashdot scrape failed: {exc}", flush=True)
    finally:
        release_run_lock()


if __name__ == "__main__":
    scrape_today()
