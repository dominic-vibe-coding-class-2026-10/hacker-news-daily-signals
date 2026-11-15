# Deployment log — Hacker News daily signals

Recorded on Sunday, 15 November 2026, Asia/Hong Kong (HKT), for the Thursday class report.

## Live service

- Public URL: https://359c-64-177-61-125.ngrok-free.app
- App on the Atlanta VM (`64.177.61.125`), listening on `127.0.0.1:8000`
- systemd unit: `hn-signals-dashboard.service` (enabled, starts on boot)
- Daily scrape: `hn-signals-scraper.timer` at 07:00 HKT (`Persistent=true`, up to 2 minutes randomized delay)
- Next scheduled run at the time of this log: Monday, 16 November 2026, 07:01 HKT
- Attribution on the dashboard header and page title: Dominic Yu, [dominic@data-science-hk.org](mailto:dominic@data-science-hk.org)

The ngrok free URL can change if the tunnel service is recreated. The dashboard itself is served locally on port 8000.

## Git log

Commits on `main` as of this deployment note:

| Commit | Time (HKT) | Author | Message |
| --- | --- | --- | --- |
| [`3637e50`](https://github.com/dominic-vibe-coding-class-2026-10/hacker-news-daily-signals/commit/3637e50) | 2026-11-15 20:44 | Dominic Yu | Add the daily signals tracker, with HKET limited to the tech section. |
| [`03754fe`](https://github.com/dominic-vibe-coding-class-2026-10/hacker-news-daily-signals/commit/03754fe) | 2026-11-15 20:58 | Dominic Yu | Keep the newest HKET tech cards even when they are older than a day. |
| [`8246409`](https://github.com/dominic-vibe-coding-class-2026-10/hacker-news-daily-signals/commit/8246409) | 2026-11-15 21:10 | Dominic Yu | Drop Lobsters and Slashdot, and file HKET cards on their printed date. |
| [`281e4aa`](https://github.com/dominic-vibe-coding-class-2026-10/hacker-news-daily-signals/commit/281e4aaef08105c27b98c86d8f880b1429ea7b98) | 2026-11-15 | Dominic Yu | Show author details on the dashboard and record deployment outcomes. |

Remote: `git@github.com:dominic-vibe-coding-class-2026-10/hacker-news-daily-signals.git`

## Still to do before daily scrapes can fully refresh

The `.env` file is present. `OLLAMA_CLOUD_API_KEY` still needs a real value on the VM so the scraper and summaries can run as designed. Until that is set, the dashboard can stay up on whatever data is already stored.
