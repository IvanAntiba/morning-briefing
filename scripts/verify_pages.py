#!/usr/bin/env python3
"""Make sure what we just published is really live on GitHub Pages.

Why: on 10 Nov 2026 GitHub's own "pages build and deployment" step timed out,
so a finished episode sat in the `pages` branch but never reached the public feed.
Re-running that failed job retries the same stuck deployment; a fresh (even empty)
commit on `pages` starts a brand-new one, which is what fixed it.

What this does, after the "Publish the podcast folder" step:
  1. Reads <lastBuildDate> from each feed in the local site folder (what we pushed).
  2. Polls the live feeds until they show the same <lastBuildDate>.
  3. If they don't within --wait seconds, pushes an empty commit to `pages`
     (a fresh deployment) and waits again, up to --retries times.
  4. If it is still not live, writes a note on today's Briefings row in Notion
     and exits with an error, so the run shows red in GitHub Actions.

Usage: python scripts/verify_pages.py --site site [--wait 300] [--retries 2] [--no-push]
Env:   PAGES_URL (optional), GITHUB_REPOSITORY, PUSH_URL (git URL with token),
       NOTION_TOKEN + NOTION_DATABASE_ID (for the failure note).
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

MADRID = ZoneInfo("Europe/Madrid")
FEEDS = ["feed.xml", "sensei/feed.xml"]
POLL_EVERY = 20  # seconds
BUILD_DATE = re.compile(r"<lastBuildDate>(.*?)</lastBuildDate>", re.S)


def log(msg: str) -> None:
    print(f"[verify] {msg}", flush=True)


def site_url() -> str:
    if os.environ.get("PAGES_URL"):
        return os.environ["PAGES_URL"].rstrip("/") + "/"
    owner, repo = os.environ.get("GITHUB_REPOSITORY", "owner/repo").split("/")
    return f"https://{owner.lower()}.github.io/{repo}/"


def build_date(text: str) -> str | None:
    m = BUILD_DATE.search(text)
    return m.group(1).strip() if m else None


def expected(site: Path) -> dict[str, str]:
    """Feed path -> the lastBuildDate we just pushed."""
    out = {}
    for feed in FEEDS:
        f = site / feed
        if f.exists():
            d = build_date(f.read_text(encoding="utf-8"))
            if d:
                out[feed] = d
    return out


def live_matches(want: dict[str, str]) -> dict[str, bool]:
    base, result = site_url(), {}
    for feed, date in want.items():
        try:
            # A unique query string skips the CDN's cached copy.
            r = requests.get(f"{base}{feed}?v={time.time_ns()}", timeout=30,
                             headers={"Cache-Control": "no-cache"})
            result[feed] = r.ok and build_date(r.text) == date
        except requests.RequestException as e:
            log(f"{feed}: fetch failed ({e})")
            result[feed] = False
    return result


def wait_until_live(want: dict[str, str], seconds: int) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        status = live_matches(want)
        if all(status.values()):
            log("Live feeds match what was published: " + ", ".join(status))
            return True
        if time.monotonic() >= deadline:
            log("Still not live: " + ", ".join(f for f, ok in status.items() if not ok))
            return False
        time.sleep(POLL_EVERY)


def republish(site: Path) -> None:
    """Empty commit on `pages` -> GitHub starts a fresh Pages deployment."""
    push_url = os.environ["PUSH_URL"]
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    subprocess.run(["git", "commit", "--quiet", "--allow-empty", "-m",
                    f"Republish {stamp} (previous Pages deployment did not go live)"],
                   cwd=site, check=True)
    subprocess.run(["git", "push", "--quiet", push_url, "HEAD:pages"], cwd=site, check=True)
    log("Pushed an empty commit to `pages` to start a fresh deployment.")


def note_in_notion(message: str) -> None:
    """Append a line to today's Briefings row (any status), if there is one."""
    token, db = os.environ.get("NOTION_TOKEN"), os.environ.get("NOTION_DATABASE_ID")
    if not token or not db:
        return
    headers = {"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28",
               "Content-Type": "application/json"}
    today = dt.datetime.now(MADRID).date().isoformat()
    try:
        res = requests.post(f"https://api.notion.com/v1/databases/{db}/query", headers=headers,
                            timeout=30, json={"filter": {"property": "Date", "date": {"equals": today}},
                                              "page_size": 1}).json()
        if not res.get("results"):
            return
        page = res["results"][0]
        old = "".join(t.get("plain_text", "") for t in
                      page["properties"].get("Robot notes", {}).get("rich_text", []))
        text = (old + " · " if old else "") + f"{dt.datetime.now(MADRID):%H:%M} {message}"
        requests.patch(f"https://api.notion.com/v1/pages/{page['id']}", headers=headers, timeout=30,
                       json={"properties": {"Robot notes": {"rich_text": [
                           {"text": {"content": text[:1900]}}]}}})
    except Exception as e:  # a note is best-effort; never hide the real failure
        log(f"Could not write the Notion note: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True)
    ap.add_argument("--wait", type=int, default=300, help="seconds to wait for each deployment")
    ap.add_argument("--retries", type=int, default=2, help="fresh deployments to try")
    ap.add_argument("--no-push", action="store_true", help="check only (for testing)")
    a = ap.parse_args()

    site = Path(a.site)
    want = expected(site)
    if not want:
        log("No feeds found in the site folder; nothing to check.")
        return 0
    log(f"Waiting for {site_url()} to show: " + ", ".join(f"{f} @ {d}" for f, d in want.items()))

    if wait_until_live(want, a.wait):
        return 0
    tried = 0
    for attempt in range(1, a.retries + 1):
        if a.no_push:
            break
        tried = attempt
        log(f"Retry {attempt}/{a.retries}: starting a fresh Pages deployment.")
        republish(site)
        if wait_until_live(want, a.wait):
            return 0

    msg = (f"Pages problem: episode published to the `pages` branch but not live after "
           f"{tried} fresh deployment(s). See GitHub Actions log.")
    log(msg)
    if not a.no_push:
        note_in_notion(msg)
    return 1


if __name__ == "__main__":
    sys.exit(main())
