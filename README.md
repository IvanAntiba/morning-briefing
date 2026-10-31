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
