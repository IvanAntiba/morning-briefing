#!/usr/bin/env python3
"""
Morning briefing robot.

In plain words: this script is the "voice studio". GitHub runs it every
15 minutes on weekday mornings. Each time it:
  1. asks Notion "is there a briefing script marked 'Script ready'?"
  2. if yes, reads the script, sends it section by section to Gemini's
     text-to-speech (voice: Iapetus) and stitches the pieces into one MP3
     (if Gemini fails, it re-does the whole thing with Microsoft Edge's free voice)
  3. adds the MP3 to the podcast folder and rewrites the podcast feed (feed.xml)
  4. marks the Notion row as 'Voiced' with a link to the episode

Commands (used by the GitHub workflow):
  check                      -> is there a script waiting? (writes found=true/false)
  build --site DIR [--test]  -> voice it and update the podcast folder
  build --site DIR --audition -> publish short samples of candidate voices
  mark  --result FILE        -> tell Notion it worked
  note  --message TEXT       -> leave an error note in Notion
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as dt
import html
import json
import os
import re
import secrets
import subprocess
import sys
import time
import wave
from email.utils import format_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ---------------------------------------------------------------- settings
MADRID = ZoneInfo("Europe/Madrid")
NOTION_VERSION = "2022-06-28"
GEMINI_MODEL = os.environ.get("GEMINI_TTS_MODEL") or "gemini-3.8-flash-tts"
GEMINI_VOICE = os.environ.get("GEMINI_VOICE") or "Iapetus"
GEMINI_STYLE = os.environ.get("GEMINI_STYLE") or ""  # Google advises testing with no style first
EDGE_VOICE = os.environ.get("EDGE_VOICE") or "en-GB-RyanNeural"  # British backup voice
MAX_CHUNK_WORDS = 1000         # ~6 minutes of speech per Gemini request (free tier = 10 requests/day)
PAUSE_BETWEEN_CHUNKS = 0.5     # seconds
PAUSE_BETWEEN_SECTIONS = 1.2   # seconds
SAMPLE_RATE = 24000
KEEP_EPISODES = 14             # older MP3s are deleted to keep the site small
PODCAST_TITLE = os.environ.get("PODCAST_TITLE") or "Iván's Morning Briefing"
# How the voice should say Iván's name. Empty = leave "Iván" as written.
NAME_SPOKEN = os.environ.get("NAME_SPOKEN") or ""

TEST_SCRIPT = [
    {"title": "Test", "paragraphs": [
        "Morning, Iván. This is a test of your briefing robot.",
        "If you can hear this in your podcast app, the whole chain works: "
        "Notion, GitHub, the voice, and the feed. From tomorrow, this slot "
        "will hold your real twenty-minute briefing. Have a good session at the gym.",
    ]},
]


def log(msg: str) -> None:
    print(f"[robot] {msg}", flush=True)


def gh_output(**pairs) -> None:
    """Pass values to later workflow steps."""
    path = os.environ.get("GITHUB_OUTPUT")
    lines = "".join(f"{k}={v}\n" for k, v in pairs.items())
    if path:
        with open(path, "a") as f:
            f.write(lines)
    print(lines, end="")


# ---------------------------------------------------------------- Notion
def notion(method: str, path: str, **kw) -> dict:
    r = requests.request(
        method, f"https://api.notion.com/v1{path}", timeout=30,
        headers={"Authorization": f"Bearer {os.environ['NOTION_TOKEN']}",
                 "Notion-Version": NOTION_VERSION,
                 "Content-Type": "application/json"}, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"Notion {method} {path} -> {r.status_code}: {r.text[:400]}")
    return r.json()


def plain(rich: list) -> str:
    return "".join(t.get("plain_text", "") for t in rich or [])


def page_title(page: dict) -> str:
    for prop in page["properties"].values():
        if prop.get("type") == "title":
            return plain(prop["title"])
    return "Morning briefing"


def find_ready_page() -> dict | None:
    db = os.environ["NOTION_DATABASE_ID"]
    res = notion("POST", f"/databases/{db}/query", json={
        "filter": {"property": "Status", "select": {"equals": "Script ready"}},
        "sorts": [{"property": "Date", "direction": "descending"}],
        "page_size": 5})
    today = dt.datetime.now(MADRID).date()
    for page in res["results"]:
        date = (page["properties"].get("Date") or {}).get("date")
        if not date:
            continue
        d = dt.date.fromisoformat(date["start"][:10])
        if d == today:                     # only today's script; old ones are never voiced
            return page
    return None


def read_blocks(page_id: str) -> list[dict]:
    blocks, cursor = [], None
    while True:
        params = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
        res = notion("GET", f"/blocks/{page_id}/children", params=params)
        blocks += res["results"]
        if not res.get("has_more"):
            return blocks
        cursor = res["next_cursor"]


def block_text(block: dict) -> str:
    return plain(block.get(block["type"], {}).get("rich_text")).strip()


SPOKEN_TYPES = {"paragraph", "quote", "callout", "bulleted_list_item", "numbered_list_item"}
HEADINGS = {"heading_1", "heading_2", "heading_3"}


def parse_script(blocks: list[dict]) -> list[dict]:
    """Take everything between the 'Audio script' heading and the 'Text version' heading."""
    sections, in_audio, current = [], False, None
    for b in blocks:
        kind, text = b["type"], block_text(b)
        if kind in HEADINGS:
            low = text.lower()
            if low.startswith("audio script"):
                in_audio = True
            elif low.startswith("text version"):
                if in_audio:
                    break
            elif in_audio:
                current = {"title": text, "paragraphs": []}
                sections.append(current)
            continue
        if in_audio and kind in SPOKEN_TYPES and text:
            if current is None:
                current = {"title": "Intro", "paragraphs": []}
                sections.append(current)
            current["paragraphs"].append(clean_for_speech(text))
    return [s for s in sections if s["paragraphs"]]


def say_names(text: str) -> str:
    """Swap names for spellings the voice pronounces correctly (audio only; Notion keeps the real spelling)."""
    if NAME_SPOKEN:
        text = re.sub(r"\bIv[aá]n\b", NAME_SPOKEN, text)
    return text


def clean_for_speech(text: str) -> str:
    text = say_names(text)
    text = re.sub(r"https?://\S+", "", text)       # never read links aloud
    text = re.sub(r"[*_#`>|]+", "", text)            # stray markdown
    return re.sub(r"\s+", " ", text).strip()


def chunk_sections(sections: list[dict]) -> list[dict]:
    """Pack paragraphs into as few Gemini requests as possible (never mid-paragraph).
    Sections can share a request; the script's own transitions carry the breaks."""
    chunks, buf, words = [], [], 0
    for s in sections:
        for i, p in enumerate(s["paragraphs"]):
            n = len(p.split())
            if buf and words + n > MAX_CHUNK_WORDS:
                chunks.append({"text": "\n\n".join(buf), "pause": PAUSE_BETWEEN_CHUNKS})
                buf, words = [], 0
            buf.append(p)
            words += n
    if buf:
        chunks.append({"text": "\n\n".join(buf), "pause": PAUSE_BETWEEN_SECTIONS})
    return chunks


# ---------------------------------------------------------------- voices
class DailyQuotaUsed(RuntimeError):
    """Gemini's free daily allowance is used up; retrying is pointless until it resets."""


def post_with_retry(url: str, headers: dict, body: dict, tries: int = 4):
    wait = 20
    for attempt in range(1, tries + 1):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=300)
        except requests.RequestException as e:
            log(f"network error ({e}); retry {attempt}/{tries}")
            r = None
        if r is not None and r.status_code not in (429, 500, 502, 503, 504):
            return r
        if r is not None and r.status_code == 429 and re.search(r"per day|PerDay", r.text, re.I):
            raise DailyQuotaUsed("Gemini daily free quota used up: " + r.text[:200])
        if r is not None:
            log(f"Gemini busy/limited ({r.status_code}): {r.text[:200]}")
        if attempt < tries:
            time.sleep(wait)
            wait *= 2
    return r


def find_audio(obj):
    """Dig through the API response for base64 audio, whatever the exact shape."""
    if isinstance(obj, dict):
        mime = obj.get("mime_type") or obj.get("mimeType") or ""
        if isinstance(obj.get("data"), str) and (obj.get("type") == "audio" or mime.startswith("audio")):
            return base64.b64decode(obj["data"]), mime
        for v in obj.values():
            found = find_audio(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in reversed(obj):          # the final output is usually last
            found = find_audio(v)
            if found:
                return found
    return None


def to_pcm(audio: bytes, mime: str) -> bytes:
    """Return 16-bit mono 24 kHz PCM, whether Gemini sent WAV or raw PCM."""
    if audio[:4] == b"RIFF":
        import io
        with wave.open(io.BytesIO(audio)) as w:
            if (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (SAMPLE_RATE, 1, 2):
                return w.readframes(w.getnframes())
        return ffmpeg_to_pcm(audio)
    rate = re.search(r"rate=(\d+)", mime or "")
    if rate and int(rate.group(1)) != SAMPLE_RATE:
        return ffmpeg_to_pcm(audio, raw_rate=int(rate.group(1)))
    return audio


def ffmpeg_to_pcm(data: bytes, raw_rate: int | None = None) -> bytes:
    src = ["-f", "s16le", "-ar", str(raw_rate), "-ac", "1"] if raw_rate else []
    out = subprocess.run(
        ["ffmpeg", "-loglevel", "error", *src, "-i", "pipe:0",
         "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "pipe:1"],
        input=data, capture_output=True, check=True)
    return out.stdout


def gemini_headers() -> dict:
    return {"x-goog-api-key": os.environ["GEMINI_API_KEY"], "Content-Type": "application/json"}


def gemini_chunk(text: str, voice: str | None = None) -> bytes:
    voice = voice or GEMINI_VOICE
    headers = gemini_headers()
    part = {"type": "text", "text": text}
    if GEMINI_STYLE:
        part["annotations"] = [{"type": "speech_metadata", "style": GEMINI_STYLE}]
    attempts = [
        # Current Gemini "Interactions" format (works with built-in, library and designed voices)
        ("https://generativelanguage.googleapis.com/v1beta/interactions", {
            "model": GEMINI_MODEL,
            "input": [{"type": "user_input", "content": [part]}],
            "response_format": {"type": "audio", "mime_type": "audio/wav"},
            "generation_config": {"speech_config": [{"voice": voice}]}}),
    ]
    if not voice.startswith(("voice_", "voicekey_")):
        # Older generateContent format, kept as a backup route for built-in voices
        attempts.append((f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent", {
            "contents": [{"parts": [{"text": text}]}],
            "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}}}))
    errors = []
    for url, body in attempts:
        r = post_with_retry(url, headers, body)
        if r is not None and r.ok:
            found = find_audio(r.json())
            if found:
                return to_pcm(*found)
            errors.append(f"{url.split('/')[-1]}: no audio in response")
        else:
            errors.append(f"{url.split('/')[-1]}: {getattr(r, 'status_code', 'no response')} "
                          f"{getattr(r, 'text', '')[:200]}")
    raise RuntimeError("Gemini failed -> " + " | ".join(errors))


def edge_chunk(text: str) -> bytes:
    import edge_tts
    tmp = Path("edge_chunk.mp3")
    asyncio.run(edge_tts.Communicate(text, EDGE_VOICE, rate="-5%").save(str(tmp)))
    pcm = ffmpeg_to_pcm(tmp.read_bytes())
    tmp.unlink(missing_ok=True)
    return pcm


def voice_all(chunks: list[dict]) -> tuple[bytes, str]:
    silence = lambda s: b"\x00\x00" * int(SAMPLE_RATE * s)
    engines = []
    if os.environ.get("GEMINI_API_KEY"):
        engines.append(("Gemini " + GEMINI_VOICE, gemini_chunk))
    engines.append(("Edge " + EDGE_VOICE, edge_chunk))
    last_error = None
    for name, fn in engines:
        try:
            pcm = b""
            for i, c in enumerate(chunks, 1):
                log(f"{name}: piece {i}/{len(chunks)} ({len(c['text'].split())} words)")
                pcm += fn(c["text"]) + silence(c["pause"])
                if fn is gemini_chunk and i < len(chunks):
                    time.sleep(7)        # stay gentle with free-tier limits
            return pcm, name
        except Exception as e:           # whole episode falls back, so voices never mix
            last_error = e
            log(f"{name} failed: {e}")
    raise RuntimeError(f"All voices failed. Last error: {last_error}")


# ---------------------------------------------------------------- podcast
def site_url() -> str:
    if os.environ.get("PAGES_URL"):
        return os.environ["PAGES_URL"].rstrip("/") + "/"
    owner, repo = os.environ.get("GITHUB_REPOSITORY", "owner/repo").split("/")
    return f"https://{owner.lower()}.github.io/{repo}/"


def write_mp3(pcm: bytes, out: Path) -> float:
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1",
         "-i", "pipe:0", "-codec:a", "libmp3lame", "-b:a", "64k", str(out)],
        input=pcm, check=True)
    return len(pcm) / 2 / SAMPLE_RATE


def hms(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def write_feed(site: Path, episodes: list[dict]) -> None:
    base = site_url()
    items = []
    for e in episodes:
        items.append(f"""    <item>
      <title>{html.escape(e["title"], quote=False)}</title>
      <description>{html.escape(e["description"], quote=False)}</description>
      <guid isPermaLink="false">{e['guid']}</guid>
      <pubDate>{e['pub_date']}</pubDate>
      <enclosure url="{base}{e['file']}" length="{e['bytes']}" type="audio/mpeg"/>
      <itunes:duration>{hms(e['seconds'])}</itunes:duration>
      <itunes:explicit>false</itunes:explicit>
    </item>""")
    now = format_datetime(dt.datetime.now(dt.timezone.utc))
    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{html.escape(PODCAST_TITLE, quote=False)}</title>
    <link>{base}</link>
    <atom:link href="{base}feed.xml" rel="self" type="application/rss+xml"/>
    <description>A private weekday news briefing. Not listed in podcast directories.</description>
    <language>en</language>
    <lastBuildDate>{now}</lastBuildDate>
    <itunes:author>Claude</itunes:author>
    <itunes:image href="{base}cover.png"/>
    <itunes:category text="News"/>
    <itunes:explicit>false</itunes:explicit>
    <itunes:block>Yes</itunes:block>
{chr(10).join(items)}
  </channel>
</rss>
"""
    (site / "feed.xml").write_text(feed, encoding="utf-8")
    (site / "index.html").write_text(
        f"<!doctype html><meta charset=utf-8><title>{html.escape(PODCAST_TITLE)}</title>"
        f"<p>Podcast feed: <code>{base}feed.xml</code></p>", encoding="utf-8")
    (site / ".nojekyll").touch()


def add_episode(site: Path, repo_root: Path, pcm: bytes, title: str, description: str,
                kind: str, engine: str) -> tuple[str, float]:
    """Save one MP3 into the podcast folder and rebuild the feed."""
    stamp = dt.datetime.now(MADRID)
    file = f"episodes/{stamp:%Y-%m-%d-%H%M%S}-{secrets.token_hex(3)}.mp3"
    seconds = write_mp3(pcm, site / file)
    store = site / "episodes.json"
    episodes = json.loads(store.read_text()) if store.exists() else []
    if kind == "briefing":
        # Auditions and tests only stay in the feed until the next real briefing + 2 days.
        cutoff = stamp - dt.timedelta(days=2)
        keep = []
        for e in episodes:
            old_extra = e.get("kind") in ("audition", "test") and \
                dt.datetime.fromisoformat(e.get("created", stamp.isoformat())) < cutoff
            if old_extra:
                (site / e["file"]).unlink(missing_ok=True)
            else:
                keep.append(e)
        episodes = keep
    episodes.insert(0, {
        "title": title, "file": file, "guid": f"{kind}-{stamp:%Y%m%d%H%M%S}-{len(episodes)}",
        "pub_date": format_datetime(stamp), "created": stamp.isoformat(),
        "bytes": (site / file).stat().st_size, "seconds": seconds,
        "engine": engine, "kind": kind, "description": description,
    })
    for old in episodes[KEEP_EPISODES:]:
        (site / old["file"]).unlink(missing_ok=True)
    episodes = episodes[:KEEP_EPISODES]
    store.write_text(json.dumps(episodes, indent=2, ensure_ascii=False))
    cover = repo_root / "assets" / "cover.png"
    if cover.exists():
        (site / "cover.png").write_bytes(cover.read_bytes())
    write_feed(site, episodes)
    return file, seconds


# ---------------------------------------------------------------- voice audition
AUDITION_TEXT = (
    "Good morning, sir. It's Friday in Barcelona: seventeen degrees, light cloud, "
    "and a sun that has promised to turn up later, which is more than can be said for most central banks. "
    "Markets were calm overnight. Mostly. "
    "I have three stories worth your attention, one of which is genuinely interesting, "
    "and I'll let you decide which. "
    "I'd suggest finishing your warm-up first, Iván. I'll wait. I'm very good at waiting."
)

PRONUNCIATION_TEST = (
    "Pronunciation test. Option A: Good morning, Iván. <short pause> "
    "Option B: Good morning, Ee-vahn. <short pause> "
    "Option C: Good morning, Ee-bahn. <short pause> "
    "Option D: Good morning, Ivahn."
)

DESIGNED_VOICES = [
    ("Butler, Received Pronunciation",
     "A refined English man in his late forties with a smooth, mid-low baritone and impeccable "
     "Received Pronunciation; unhurried, precise and quietly amused, with dry understated warmth."),
    ("Assistant, polished London",
     "A composed British man in his early forties with a light, clear baritone and a polished London accent; "
     "crisp diction, effortlessly efficient, faintly sardonic but always courteous."),
]


def list_library_voices() -> list[dict]:
    """British-English male voices from Gemini's extended voice library."""
    url = "https://generativelanguage.googleapis.com/v1beta/voices"
    params = [("language_code", "en-GB"), ("gender", "male"), ("type", "prebuilt"), ("page_size", "200")]
    r = requests.get(url, headers=gemini_headers(), params=params, timeout=60)
    if not r.ok:
        log(f"Voice library list failed ({r.status_code}): {r.text[:300]}")
        return []
    voices = r.json().get("voices") or []
    log(f"Voice library: {len(voices)} British male voices found")

    def score(v: dict) -> int:
        p = v.get("prompted") or {}
        text = json.dumps(v).lower()
        s = 0
        s += 3 if (p.get("pitch") or v.get("pitch") or "").lower() in ("low", "medium") else 0
        s += sum(2 for w in ("calm", "refined", "butler", "assistant", "professional", "narrat",
                             "polished", "warm", "sophisticat", "news", "documentary") if w in text)
        return s
    unsuitable = ("child", "teen", "young", "cartoon", "pirate", "villain", "elderly", "monster", "robot")
    voices = [v for v in voices if not any(w in json.dumps(v).lower() for w in unsuitable)]
    return sorted(voices, key=score, reverse=True)


def existing_designed_voices() -> dict:
    r = requests.get("https://generativelanguage.googleapis.com/v1beta/voices", headers=gemini_headers(),
                     params=[("type", "prompted"), ("page_size", "200")], timeout=60)
    if not r.ok:
        return {}
    return {v.get("display_name"): v.get("id") for v in r.json().get("voices") or []
            if v.get("type") == "prompted" and v.get("id")}


def design_voice(name: str, prompt: str, existing: dict | None = None) -> str | None:
    if existing and existing.get(f"Briefing — {name}"):
        vid = existing[f"Briefing — {name}"]
        log(f"Reusing designed voice '{name}': {vid}")
        return vid
    body = {"store": True, "voice": {
        "type": "prompted", "model": GEMINI_MODEL, "display_name": f"Briefing — {name}",
        "gender": "male", "language_code": "en-GB", "prompted": {"input": prompt}}}
    r = post_with_retry("https://generativelanguage.googleapis.com/v1beta/voices", gemini_headers(), body, tries=2)
    if r is None or not r.ok:
        log(f"Voice design '{name}' failed: {getattr(r, 'status_code', '?')} {getattr(r, 'text', '')[:300]}")
        return None
    vid = r.json().get("id")
    log(f"Designed voice '{name}': {vid}")
    return vid


def audition(site: Path, repo_root: Path) -> None:
    """~7 Gemini requests: pronunciation test + 4 library voices + 2 designed voices.
    Run it on a day with spare free quota (it resets around 08:00-09:00 Madrid)."""
    candidates: list[tuple[str, str, str]] = [
        ("How to say Iván (A, B, C, D)", GEMINI_VOICE, PRONUNCIATION_TEST)]
    for v in list_library_voices()[:4]:
        vid = v.get("id") or v.get("display_name")
        label = v.get("display_name") or vid
        p = v.get("prompted") or {}
        extra = ", ".join(x for x in (p.get("accent"), p.get("persona"), p.get("pitch")) if x)
        candidates.append((f"{label} — library{(' · ' + extra) if extra else ''}", vid, say_names(AUDITION_TEXT)))
    existing = existing_designed_voices()
    for name, prompt in DESIGNED_VOICES:
        try:
            vid = design_voice(name, prompt, existing)
        except DailyQuotaUsed as e:
            log(f"Voice design skipped: {e}")
            vid = None
        if vid:
            candidates.append((f"{name} — designed", vid, say_names(AUDITION_TEXT)))

    results = []
    for n, (label, vid, text) in enumerate(candidates, 1):
        try:
            log(f"Audition {n}: {label} ({vid})")
            pcm = gemini_chunk(text, voice=vid)
            add_episode(site, repo_root, pcm, f"Audition {n} — {label}",
                        f"Voice audition. Voice ID: {vid}", "audition", f"Gemini {vid}")
            results.append({"number": n, "label": label, "voice_id": vid, "ok": True})
        except DailyQuotaUsed as e:
            log(f"Stopping auditions: {e}")
            results.append({"number": n, "label": label, "voice_id": vid, "ok": False, "error": "daily quota"})
            break
        except Exception as e:
            log(f"Audition {n} failed: {e}")
            results.append({"number": n, "label": label, "voice_id": vid, "ok": False, "error": str(e)[:300]})
        time.sleep(7)
    (site / "auditions.json").write_text(json.dumps(results, indent=2, ensure_ascii=False))
    ok = sum(r["ok"] for r in results)
    log(f"Auditions published: {ok}/{len(candidates)}")
    Path("result.json").write_text(json.dumps({"page_id": None}))
    gh_output(built="true" if ok else "false")
    if not ok:
        raise RuntimeError("No audition could be voiced.")


def build(site: Path, test: bool, repo_root: Path) -> None:
    if test:
        page, sections = None, TEST_SCRIPT
        title = "Test episode — " + dt.datetime.now(MADRID).strftime("%a %d %b %Y %H:%M")
    else:
        page = find_ready_page()
        if not page:
            log("No script waiting. Nothing to do.")
            gh_output(built="false")
            return
        sections = parse_script(read_blocks(page["id"]))
        if not sections:
            raise RuntimeError("Found the row but no 'Audio script' section in the page body.")
        title = page_title(page)

    words = sum(len(p.split()) for s in sections for p in s["paragraphs"])
    chunks = chunk_sections(sections)
    log(f"'{title}': {len(sections)} sections, {words} words, {len(chunks)} voice requests")

    pcm, engine = voice_all(chunks)
    file, seconds = add_episode(
        site, repo_root, pcm, title,
        f"Weekday briefing, {len(sections)} sections, voiced by {engine}.",
        "test" if test else "briefing", engine)

    result = {"page_id": page["id"] if page else None, "url": site_url() + file,
              "engine": engine, "minutes": round(seconds / 60, 1), "words": words}
    Path("result.json").write_text(json.dumps(result))
    log(f"Done: {result}")
    gh_output(built="true")


def mark(result_file: Path) -> None:
    r = json.loads(result_file.read_text())
    if not r.get("page_id"):
        log("Test episode — nothing to update in Notion.")
        return
    note = f"Voiced {dt.datetime.now(MADRID):%H:%M} · {r['engine']} · {r['minutes']} min"
    notion("PATCH", f"/pages/{r['page_id']}", json={"properties": {
        "Status": {"select": {"name": "Voiced"}},
        "Episode URL": {"url": r["url"]},
        "Robot notes": {"rich_text": [{"text": {"content": note}}]}}})
    log("Notion updated.")


def note(message: str) -> None:
    page = find_ready_page()
    if not page:
        return
    text = f"{dt.datetime.now(MADRID):%H:%M} problem: {message}"[:1900]
    notion("PATCH", f"/pages/{page['id']}", json={"properties": {
        "Robot notes": {"rich_text": [{"text": {"content": text}}]}}})


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    b = sub.add_parser("build"); b.add_argument("--site", required=True); b.add_argument("--test", action="store_true")
    b.add_argument("--audition", action="store_true")
    m = sub.add_parser("mark"); m.add_argument("--result", default="result.json")
    n = sub.add_parser("note"); n.add_argument("--message", required=True)
    args = ap.parse_args()

    if args.cmd == "check":
        page = find_ready_page()
        log(f"Script waiting: {page_title(page)}" if page else "No script waiting.")
        gh_output(found="true" if page else "false")
    elif args.cmd == "build":
        root = Path(__file__).resolve().parent.parent
        if args.audition:
            audition(Path(args.site), root)
        else:
            build(Path(args.site), args.test, root)
    elif args.cmd == "mark":
        mark(Path(args.result))
    elif args.cmd == "note":
        note(args.message)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ERROR: {e}")
        sys.exit(1)
