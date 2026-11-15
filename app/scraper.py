"""Playwright scrape of the Hacker News front page and its archives."""

from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime
from urllib.parse import urljoin

from playwright.async_api import Browser, Page, async_playwright

from app.db import (
    acquire_run_lock,
    completed_dates,
    has_completed_preload,
    init_db,
    meta_set,
    release_run_lock,
    replace_day,
)
from app.filter import filter_stories
from app.report import write_report
from app.timeutil import (
    age_text_as_of,
    parse_comments,
    parse_points,
    parse_posted_at,
    recent_dates,
    reference_for,
    utcnow,
    within_window,
)

FRONT = "https://news.ycombinator.com/"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
MORE_CLICKS = 3
ARTICLE_CONCURRENCY = 4

LISTING_JS = """
() => {
  const rows = [...document.querySelectorAll("tr.athing")];
  return rows.map((row) => {
    const link = row.querySelector("span.titleline > a");
    const sub = row.nextElementSibling;
    const age = sub ? sub.querySelector("span.age") : null;
    const score = sub ? sub.querySelector("span.score") : null;
    const anchors = sub ? [...sub.querySelectorAll("a")] : [];
    const comments = anchors.find((anchor) => /comment|discuss/i.test(anchor.textContent || ""));
    return {
      id: row.id || "",
      title: link ? link.textContent.trim() : "",
      href: link ? link.getAttribute("href") || "" : "",
      posted_at: age ? age.getAttribute("title") || "" : "",
      score: score ? score.textContent.trim() : "",
      comments: comments ? comments.textContent.trim() : "",
    };
  });
}
"""

EXTRACT_JS = """
() => {
  const root = document.querySelector("article")
    || document.querySelector(".fatitem")
    || document.querySelector("main")
    || document.body;
  if (!root) return "";
  const clone = root.cloneNode(true);
  clone.querySelectorAll("script,style,noscript,svg,iframe").forEach((node) => node.remove());
  const text = (clone.innerText || "").replace(/\\n{3,}/g, "\\n\\n").trim();
  return text.slice(0, 2000);
}
"""


def front_url(day: date, now: datetime) -> str:
    if day == now.date():
        return FRONT
    return f"{FRONT}front?day={day.isoformat()}"


def absolute_external(href: str) -> str:
    if not href or href.startswith("item?") or href.startswith("from?"):
        return ""
    if href.startswith("/"):
        return urljoin(FRONT, href)
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return ""


def _dedupe_key(title: str, external_url: str) -> tuple[str, str]:
    collapsed = " ".join(title.split()).casefold()
    return collapsed, external_url


async def _collect_listing(page: Page, start_url: str) -> list[dict]:
    await page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_selector("tr.athing", timeout=20000)
    collected: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for hop in range(MORE_CLICKS + 1):
        list_url = page.url
        rows = await page.evaluate(LISTING_JS)
        for row in rows:
            title = " ".join((row.get("title") or "").split())
            if not title or not row.get("id"):
                continue
            external_url = absolute_external(row.get("href") or "")
            key = _dedupe_key(title, external_url)
            if key in seen:
                continue
            seen.add(key)
            hn_url = f"{FRONT}item?id={row['id']}"
            collected.append(
                {
                    "id": row["id"],
                    "title": title,
                    "external_url": external_url,
                    "hn_url": hn_url,
                    "open_url": external_url or hn_url,
                    "list_url": list_url,
                    "points": parse_points(row.get("score") or ""),
                    "comments": parse_comments(row.get("comments") or ""),
                    "posted_raw": row.get("posted_at") or "",
                    "raw_content": "",
                }
            )
        if hop == MORE_CLICKS:
            break
        more = page.locator("a.morelink")
        if await more.count() == 0:
            break
        href = await more.first.get_attribute("href")
        previous = page.url
        await more.first.click()
        try:
            await page.wait_for_function(
                "(previous) => location.href !== previous",
                arg=previous,
                timeout=8000,
            )
        except Exception:
            if not href:
                break
            await page.goto(urljoin(previous, href), wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_selector("tr.athing", timeout=20000)
    return collected


async def _extract_text(page: Page) -> str:
    try:
        text = await page.evaluate(EXTRACT_JS)
    except Exception:
        return ""
    text = (text or "").replace("\x00", "").strip()
    if len(text) < 80:
        await page.wait_for_timeout(1000)
        try:
            text = await page.evaluate(EXTRACT_JS)
        except Exception:
            return text
    return (text or "").replace("\x00", "").strip()[:2000]


async def _open_title(page: Page, story: dict) -> None:
    selector = f"tr.athing#{story['id']} span.titleline > a"
    try:
        await page.goto(story["list_url"], wait_until="domcontentloaded", timeout=20000)
        link = page.locator(selector).first
        if await page.locator(selector).count():
            await link.click(timeout=10000)
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
        else:
            await page.goto(story["open_url"], wait_until="domcontentloaded", timeout=20000)
    except Exception:
        try:
            await page.goto(story["open_url"], wait_until="domcontentloaded", timeout=20000)
        except Exception:
            story["raw_content"] = ""
            return
    story["raw_content"] = await _extract_text(page)


async def _open_all(browser: Browser, stories: list[dict]) -> None:
    context = await browser.new_context(user_agent=USER_AGENT)

    async def _route(route):
        if route.request.resource_type in {"image", "media", "font"}:
            await route.abort()
        else:
            await route.continue_()

    await context.route("**/*", _route)
    semaphore = asyncio.Semaphore(ARTICLE_CONCURRENCY)
    done = 0

    async def one(story: dict) -> None:
        nonlocal done
        async with semaphore:
            page = None
            try:
                page = await context.new_page()
                page.set_default_timeout(20000)
                await _open_title(page, story)
            except Exception as exc:
                story["raw_content"] = story.get("raw_content") or ""
                print(f"  skipped open ({exc.__class__.__name__}): {story['title'][:72]}", flush=True)
            finally:
                if page is not None:
                    await page.close()
                done += 1
                print(
                    f"  opened {done}/{len(stories)}: {story['title'][:72]}",
                    flush=True,
                )

    await asyncio.gather(*(one(story) for story in stories))
    await context.close()


def select_recent(stories: list[dict], reference: datetime) -> list[dict]:
    chosen: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for story in stories:
        posted = parse_posted_at(story.get("posted_raw"))
        if posted is None or not within_window(posted, reference):
            continue
        story["age_text"] = age_text_as_of(posted, reference)
        key = _dedupe_key(story["title"], story.get("external_url") or "")
        if key in seen:
            continue
        seen.add(key)
        chosen.append(story)
    return chosen


async def _scrape_day(day: date) -> None:
    now = utcnow()
    reference = reference_for(day, now)
    start = front_url(day, now)
    print(f"Scraping {day.isoformat()} from {start}", flush=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage"],
        )
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()
        try:
            listing = await _collect_listing(page, start)
        finally:
            await context.close()
        recent = select_recent(listing, reference)
        print(
            f"  {len(listing)} stories listed, {len(recent)} within 24 hours",
            flush=True,
        )
        if recent:
            await _open_all(browser, recent)
        await browser.close()

    filtered = filter_stories(recent) if recent else []
    for story in filtered:
        story["source"] = "hn"
    replace_day(day.isoformat(), filtered, source="hn")
    path = write_report(day.isoformat())
    print(
        f"Stored {len(filtered)} stories for {day.isoformat()} -> {path}",
        flush=True,
    )


def scrape_day(day: date) -> None:
    asyncio.run(_scrape_day(day))


def planned_dates() -> list[date]:
    window = recent_dates(7)
    if not has_completed_preload():
        done = completed_dates()
        missing = [day for day in window if day.isoformat() not in done]
        return list(reversed(missing))
    return [window[0]]


def run_dates(dates: list[date], *, mark_preload: bool) -> None:
    init_db()
    if not dates:
        if mark_preload:
            meta_set("preload_complete", "1")
            meta_set("scrape_message", "Up to date")
        return
    if not acquire_run_lock():
        print("A scrape is already running.", flush=True)
        return
    errors: list[str] = []
    try:
        for index, day in enumerate(dates, start=1):
            message = f"Scraping {day.isoformat()} ({index}/{len(dates)})"
            meta_set("scrape_message", message)
            print(message, flush=True)
            try:
                scrape_day(day)
            except Exception as exc:
                errors.append(f"{day.isoformat()}: {exc}")
                print(f"Failed {day.isoformat()}: {exc}", flush=True)
        if errors:
            meta_set("scrape_message", "Scrape failed: " + "; ".join(errors))
            return
        if mark_preload or has_completed_preload():
            window = {day.isoformat() for day in recent_dates(7)}
            if window.issubset(completed_dates()):
                meta_set("preload_complete", "1")
        meta_set("scrape_message", "Up to date")
    finally:
        release_run_lock()


def run_plan() -> None:
    init_db()
    mark_preload = not has_completed_preload()
    run_dates(planned_dates(), mark_preload=mark_preload)
    from app.other_sources import scrape_today

    scrape_today()


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape Hacker News daily signals")
    parser.add_argument(
        "--days",
        type=int,
        default=0,
        help="Scrape the most recent N days. Default: 7 on first run, otherwise today.",
    )
    args = parser.parse_args()
    if args.days > 0:
        run_dates(list(reversed(recent_dates(args.days))), mark_preload=args.days >= 7)
        return
    run_plan()


if __name__ == "__main__":
    main()
