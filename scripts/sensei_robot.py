#!/usr/bin/env python3
"""
Sensei robot: the Japanese-lesson voice studio.

In plain words: it runs in the same GitHub routine as the briefing robot. Each time it:
  1. asks Notion "is there a Sensei lesson marked 'Script ready'?"
  2. if yes, reads the lesson script, where every piece of text is tagged:
       [ES] ...      Spanish  -> read by the sensei voice (Alvaro, Castilian Spanish)
       [JA] ...      Japanese -> read by a native Japanese voice (Nanami), a bit slower
       [PAUSA 3]     silence (seconds) so Iván can repeat out loud
  3. stitches all the pieces into one MP3 with Microsoft Edge's free voices
     (no Gemini quota used)
  4. adds it to the separate "Sensei" podcast feed (sensei/feed.xml)
  5. marks the Notion row as 'Voiced' with a link to the episode

Commands (used by the GitHub workflow):
  check              -> is a lesson waiting? (writes found=true/false; never fails the run)
  build --site DIR   -> voice it and update the Sensei feed
  mark               -> tell Notion it worked
  note --message TXT -> leave an error note on the waiting lesson
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import html
import json
import os
import re
import secrets
import sys
import time
from email.utils import format_datetime
from pathlib import Path

# Reuse the briefing robot's helpers (Notion access, MP3 writing, logging).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from briefing_robot import (  # noqa: E402
    HEADINGS, MADRID, SAMPLE_RATE, SPOKEN_TYPES, block_text, ffmpeg_to_pcm, gh_output,
    hms, log, notion, page_title, read_blocks, site_url, write_mp3,
)

# ---------------------------------------------------------------- settings
SENSEI_DATABASE_ID = os.environ.get("SENSEI_DATABASE_ID") or "a349812ef7744b609a56f8c5d6aba9bf"
VOICE_ES = os.environ.get("SENSEI_VOICE_ES") or "es-ES-AlvaroNeural"   # the sensei, Castilian Spanish
VOICE_JA = os.environ.get("SENSEI_VOICE_JA") or "ja-JP-NanamiNeural"   # native Japanese
RATE_ES = os.environ.get("SENSEI_RATE_ES") or "-5%"
RATE_JA = os.environ.get("SENSEI_RATE_JA") or "-15%"                   # slower, for a beginner
GAP_SEGMENT = 0.25      # seconds between tagged pieces inside a paragraph
GAP_PARAGRAPH = 0.7
GAP_SECTION = 1.5
DEFAULT_PAUSE = 3.0
MAX_PAUSE = 8.0
KEEP_EPISODES = 40
FEED_DIR = "sensei"
FEED_TITLE = "Sensei"
RESULT_FILE = Path("sensei_result.json")

TAG = re.compile(r"\[\s*(ES|JA|PAUSA)(?:\s+(\d+(?:[.,]\d+)?))?\s*\]", re.I)


# ---------------------------------------------------------------- Notion
def find_ready_lesson() -> dict | None:
    """Oldest lesson still marked 'Script ready' (any date, so a missed one still gets voiced)."""
    res = notion("POST", f"/databases/{SENSEI_DATABASE_ID}/query", json={
        "filter": {"property": "Status", "select": {"equals": "Script ready"}},
        "sorts": [{"property": "Date", "direction": "ascending"}],
        "page_size": 1})
    return res["results"][0] if res["results"] else None


# ---------------------------------------------------------------- script parsing
def clean(text: str) -> str:
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_#`>|]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def speakable(text: str) -> bool:
    return bool(re.search(r"\w", text))


def split_tags(text: str) -> list[tuple[str, object]]:
    """'[ES] La palabra [JA] ありがとう [PAUSA 3]' -> [('es', 'La palabra'), ('ja', 'ありがとう'), ('pause', 3.0)].
    Untagged text at the start is read in Spanish."""
    out, lang, pos = [], "es", 0
    for m in TAG.finditer(text):
        chunk = clean(text[pos:m.start()])
        if speakable(chunk):
            out.append((lang, chunk))
        tag = m.group(1).upper()
        if tag == "PAUSA":
            secs = float((m.group(2) or str(DEFAULT_PAUSE)).replace(",", "."))
            out.append(("pause", min(max(secs, 0.5), MAX_PAUSE)))
        else:
            lang = tag.lower()
        pos = m.end()
    chunk = clean(text[pos:])
    if speakable(chunk):
        out.append((lang, chunk))
    return out


def parse_lesson(blocks: list[dict]) -> list[list[list[tuple[str, object]]]]:
    """Sections -> paragraphs -> tagged pieces, from 'Audio script' up to 'Text version'."""
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
                current = []
                sections.append(current)
            continue
        if in_audio and kind in SPOKEN_TYPES and text:
            if current is None:
                current = []
                sections.append(current)
            pieces = split_tags(text)
            if pieces:
                current.append(pieces)
    return [s for s in sections if s]


# ---------------------------------------------------------------- voices
_cache: dict[tuple[str, str], bytes] = {}


def edge_say(text: str, lang: str) -> bytes:
    """One piece of speech as 24 kHz PCM. Identical pieces (repeated words) are voiced once."""
    key = (lang, text)
    if key in _cache:
        return _cache[key]
    import edge_tts
    voice, rate = (VOICE_JA, RATE_JA) if lang == "ja" else (VOICE_ES, RATE_ES)
    tmp = Path(f"sensei_piece_{secrets.token_hex(4)}.mp3")
    last = None
    for attempt in range(1, 4):
        try:
            asyncio.run(edge_tts.Communicate(text, voice, rate=rate).save(str(tmp)))
            pcm = ffmpeg_to_pcm(tmp.read_bytes())
            tmp.unlink(missing_ok=True)
            _cache[key] = pcm
            return pcm
        except Exception as e:  # network hiccup or an empty result: wait and retry
            last = e
            log(f"Edge {lang} attempt {attempt} failed for '{text[:40]}': {e}")
            time.sleep(3 * attempt)
    tmp.unlink(missing_ok=True)
    raise RuntimeError(f"Edge voice failed for '{text[:60]}': {last}")


def silence(seconds: float) -> bytes:
    return b"\x00\x00" * int(SAMPLE_RATE * seconds)


def voice_lesson(sections) -> tuple[bytes, dict]:
    pcm, stats = b"", {"es": 0, "ja": 0, "pause": 0}
    for si, section in enumerate(sections):
        for paragraph in section:
            for i, (kind, value) in enumerate(paragraph):
                stats[kind] += 1
                if kind == "pause":
                    pcm += silence(float(value))
                else:
                    pcm += edge_say(str(value), kind)
                    if i < len(paragraph) - 1 and paragraph[i + 1][0] != "pause":
                        pcm += silence(GAP_SEGMENT)
            pcm += silence(GAP_PARAGRAPH)
        if si < len(sections) - 1:
            pcm += silence(GAP_SECTION)
    return pcm, stats


# ---------------------------------------------------------------- podcast
def write_feed(folder: Path, episodes: list[dict]) -> None:
    base = site_url() + FEED_DIR + "/"
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
    <title>{FEED_TITLE}</title>
    <link>{base}</link>
    <atom:link href="{base}feed.xml" rel="self" type="application/rss+xml"/>
    <description>Lecciones privadas de japonés para el camino a la oficina. No aparece en los directorios de podcasts.</description>
    <language>es</language>
    <lastBuildDate>{now}</lastBuildDate>
    <itunes:author>Claude</itunes:author>
    <itunes:image href="{base}cover.png"/>
    <itunes:category text="Education"><itunes:category text="Language Learning"/></itunes:category>
    <itunes:explicit>false</itunes:explicit>
    <itunes:block>Yes</itunes:block>
{chr(10).join(items)}
  </channel>
</rss>
"""
    (folder / "feed.xml").write_text(feed, encoding="utf-8")


def add_episode(site: Path, repo_root: Path, pcm: bytes, title: str, description: str,
                kind: str = "lesson") -> tuple[str, float]:
    folder = site / FEED_DIR
    stamp = dt.datetime.now(MADRID)
    file = f"episodes/{stamp:%Y-%m-%d-%H%M%S}-{secrets.token_hex(3)}.mp3"
    seconds = write_mp3(pcm, folder / file)
    store = folder / "episodes.json"
    episodes = json.loads(store.read_text()) if store.exists() else []
    if kind == "lesson":
        # Voice auditions only stay in the feed for 2 days after they were made.
        cutoff = stamp - dt.timedelta(days=2)
        keep = []
        for e in episodes:
            if e.get("kind") == "audition" and dt.datetime.fromisoformat(e["created"]) < cutoff:
                (folder / e["file"]).unlink(missing_ok=True)
            else:
                keep.append(e)
        episodes = keep
    episodes.insert(0, {
        "title": title, "file": file, "guid": f"sensei-{kind}-{stamp:%Y%m%d%H%M%S}-{len(episodes)}",
        "pub_date": format_datetime(stamp), "created": stamp.isoformat(), "kind": kind,
        "bytes": (folder / file).stat().st_size, "seconds": seconds, "description": description,
    })
    for old in episodes[KEEP_EPISODES:]:
        (folder / old["file"]).unlink(missing_ok=True)
    episodes = episodes[:KEEP_EPISODES]
    store.write_text(json.dumps(episodes, indent=2, ensure_ascii=False))
    cover = repo_root / "assets" / "sensei-cover.png"
    if cover.exists():
        (folder / "cover.png").write_bytes(cover.read_bytes())
    write_feed(folder, episodes)
    (site / ".nojekyll").touch()
    return file, seconds


# ---------------------------------------------------------------- voice audition
AUDITION_ES_1 = ("Hola, Iván. Aquí tu clase de japonés de cada día. Vamos con una frase que vas a usar "
                 "muchísimo en Japón: en tiendas, en restaurantes y en el tren. Escucha y repite.")
AUDITION_JA = "ありがとうございます"
AUDITION_ES_2 = ("Significa muchas gracias, en forma educada. Si se lo dices a un amigo, basta con la versión corta. "
                 "Vale, ¿lo tienes? Muy bien. Mañana, más.")
# (label, voice, rate). The first three are the only Castilian Spanish voices Edge offers;
# the "Multilingual" ones are Microsoft's newest, most natural voices, which also speak Spanish.
AUDITION_VOICES = [
    ("Álvaro, España (el actual, más rápido)", "es-ES-AlvaroNeural", "+5%"),
    ("Ximena, España", "es-ES-XimenaNeural", "+0%"),
    ("Elvira, España", "es-ES-ElviraNeural", "+0%"),
    ("Andrew, multilingüe", "en-US-AndrewMultilingualNeural", "+0%"),
    ("Brian, multilingüe", "en-US-BrianMultilingualNeural", "+0%"),
    ("Emma, multilingüe", "en-US-EmmaMultilingualNeural", "+0%"),
    ("Ava, multilingüe", "en-US-AvaMultilingualNeural", "+0%"),
]


def audition(site: Path, repo_root: Path) -> None:
    """Publish one short sample per candidate Spanish voice into the Sensei feed."""
    global VOICE_ES, RATE_ES
    ok = 0
    for n, (label, voice, rate) in enumerate(AUDITION_VOICES, 1):
        VOICE_ES, RATE_ES = voice, rate
        _cache.clear()
        try:
            pcm = (edge_say(AUDITION_ES_1, "es") + silence(0.4) + edge_say(AUDITION_JA, "ja") + silence(2.5)
                   + edge_say(AUDITION_JA, "ja") + silence(1.0) + edge_say(AUDITION_ES_2, "es"))
            add_episode(site, repo_root, pcm, f"Prueba de voz {n} — {label}",
                        f"Prueba de voz para el sensei: {voice} ({rate}).", kind="audition")
            log(f"Audition {n}: {voice} OK")
            ok += 1
        except Exception as e:
            log(f"Audition {n}: {voice} failed: {e}")
    log(f"Auditions published: {ok}/{len(AUDITION_VOICES)}")
    gh_output(built="true" if ok else "false")
    if not ok:
        raise RuntimeError("No audition could be voiced.")


# ---------------------------------------------------------------- commands
def build(site: Path, repo_root: Path) -> None:
    page = find_ready_lesson()
    if not page:
        log("No lesson waiting. Nothing to do.")
        gh_output(built="false")
        return
    title = page_title(page)
    sections = parse_lesson(read_blocks(page["id"]))
    if not sections:
        raise RuntimeError("Found the lesson but no tagged 'Audio script' section in the page body.")
    pcm, stats = voice_lesson(sections)
    log(f"'{title}': {stats['es']} Spanish pieces, {stats['ja']} Japanese, {stats['pause']} pauses")
    engine = f"Edge {VOICE_ES} + {VOICE_JA}"
    file, seconds = add_episode(site, repo_root, pcm, title, f"Sensei · voces: Álvaro (español) y Nanami (japonés).")
    result = {"page_id": page["id"], "url": site_url() + FEED_DIR + "/" + file,
              "engine": engine, "minutes": round(seconds / 60, 1), **stats}
    RESULT_FILE.write_text(json.dumps(result))
    log(f"Done: {result}")
    gh_output(built="true")


def mark() -> None:
    r = json.loads(RESULT_FILE.read_text())
    note = (f"Voiced {dt.datetime.now(MADRID):%d %b %H:%M} · Edge Álvaro + Nanami · {r['minutes']} min · "
            f"{r['pause']} pauses")
    notion("PATCH", f"/pages/{r['page_id']}", json={"properties": {
        "Status": {"select": {"name": "Voiced"}},
        "Episode URL": {"url": r["url"]},
        "Robot notes": {"rich_text": [{"text": {"content": note}}]}}})
    log("Notion updated.")


def note(message: str) -> None:
    page = find_ready_lesson()
    if not page:
        return
    text = f"{dt.datetime.now(MADRID):%d %b %H:%M} problem: {message}"[:1900]
    notion("PATCH", f"/pages/{page['id']}", json={"properties": {
        "Robot notes": {"rich_text": [{"text": {"content": text}}]}}})


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    b = sub.add_parser("build"); b.add_argument("--site", required=True)
    a = sub.add_parser("audition"); a.add_argument("--site", required=True)
    sub.add_parser("mark")
    n = sub.add_parser("note"); n.add_argument("--message", required=True)
    args = ap.parse_args()

    if args.cmd == "check":
        # Never let Sensei break the briefing: any problem here just means "nothing to do".
        try:
            page = find_ready_lesson()
        except Exception as e:
            log(f"WARNING: could not check the Sensei lessons ({e}). "
                "Is the 'Briefing robot' connection added to the Sensei page in Notion?")
            page = None
        log(f"Lesson waiting: {page_title(page)}" if page else "No lesson waiting.")
        gh_output(found="true" if page else "false")
    elif args.cmd == "build":
        build(Path(args.site), Path(__file__).resolve().parent.parent)
    elif args.cmd == "audition":
        audition(Path(args.site), Path(__file__).resolve().parent.parent)
    elif args.cmd == "mark":
        mark()
    elif args.cmd == "note":
        note(args.message)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ERROR: {e}")
        sys.exit(1)
