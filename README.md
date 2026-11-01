# Morning Briefing robot

Turns the briefing script Claude writes in Notion each morning (weekends included) into a podcast episode.

**How it works, in one line:** Claude writes the script (≈6:15 Madrid) → this robot checks Notion every 15 minutes between 5:00 and 8:45 → voices it with Gemini (voice *Iapetus*; Microsoft Edge voice as backup) → publishes it to a podcast feed your phone is subscribed to.

| Piece | What it is |
|---|---|
| `.github/workflows/briefing.yml` | The robot's alarm clock and to-do list |
| `scripts/briefing_robot.py` | The voice studio: reads Notion, makes the MP3, updates the feed |
| `assets/cover.png` | Podcast cover art |
| `pages` branch | Created automatically — the published podcast (feed + last 14 episodes) |

## Settings (repo → Settings → Secrets and variables → Actions)

**Secrets** (hidden): `GEMINI_API_KEY`, `NOTION_TOKEN`
**Variables** (visible): `NOTION_DATABASE_ID` = `0da22f0b669e49a2bdf52b8657dbf2d6`
Optional variables: `GEMINI_VOICE` (default Iapetus), `GEMINI_STYLE` (e.g. "calm, warm morning news host"), `GEMINI_TTS_MODEL` (default gemini-3.8-flash-tts), `PAGES_URL`.

## Test it
Actions tab → *Voice morning briefing* → *Run workflow* → tick **test** → Run. A 30-second episode appears in the feed within a couple of minutes.

## Feed address
`https://<your-github-username>.github.io/<repo-name>/feed.xml`

The feed is public but hidden from podcast directories (`itunes:block`). The audio never names meetings, people or tasks — those stay in the private Notion text version.

## Sensei (Japanese lessons)

The same routine also voices **Sensei**, a ~13-minute Japanese lesson every day (weekdays: the walk to the office).
Claude's scheduled task *Sensei writer* writes the lesson into the Notion **Lessons** database (under Jarvis → Sensei) at 05:50 Madrid every day. On its next run, this robot voices it with two free Microsoft Edge voices: Álvaro (Castilian Spanish, the sensei) and Nanami (Japanese). It uses no Gemini quota.

| Piece | What it is |
|---|---|
| `scripts/sensei_robot.py` | Reads the lesson, voices `[ES]` / `[JA]` / `[PAUSA n]` tags, updates the Sensei feed |
| `assets/sensei-cover.png` | Sensei cover art |
| `sensei/` folder on `pages` | The published Sensei podcast (feed + last 40 lessons) |

Feed address: `https://ivanantiba.github.io/morning-briefing/sensei/feed.xml`
The robot's Notion connection ("Briefing robot") must be added to the Sensei page in Notion. Sensei steps never fail the run, so a Sensei problem can't stop the briefing.
Optional variables: `SENSEI_DATABASE_ID`, `SENSEI_VOICE_ES`, `SENSEI_VOICE_JA`, `SENSEI_RATE_ES` (default -5%), `SENSEI_RATE_JA` (default -15%).
