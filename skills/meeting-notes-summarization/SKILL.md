---
name: meeting-notes-summarization
description: Turn a plain-text meeting transcript, standup log, or retro notes (e.g. meeting.txt) into a structured markdown NOTES.md with attendees, decisions, action items (owner and due), and open questions. Use when asked to summarise or write up a meeting.
---

# Meeting transcript to NOTES.md

1. Read the transcript file the user names (default `meeting.txt`). If NOTES.md already exists and the user asks to update it, read it and edit it rather than starting over.
2. Write `NOTES.md` next to the transcript (or where the user says), with this structure. Omit a section only if it has no content, except Decisions and Action items, which always appear.
   - `# <Meeting type> — <date/title>`
   - `**Attendees:**` (or Present/Team) with roles if given, comma-separated.
   - Optional context sections that fit the meeting: Sprint goal, Updates (per person, for standups), What went well / What didn't go well (for retros).
   - `## Decisions`: bullets of things explicitly decided or agreed (including "decided NOT to").
   - `## Action items`: ALWAYS a markdown table with Owner and Due columns, plus an action column, e.g. `| Owner | Action | Due |`. One row per commitment. Use the owner named in the transcript; use `—` if none or if unknown. Keep the deadline wording from the transcript, and mark guesses as `(tentative)`. Use `—` if there is no deadline.
   - `## Open questions`: ALWAYS a separate section placed LAST in the file, one bullet per unresolved question, noting who is following up. Omit it only if there are truly none.
3. Dates: resolve relative weekdays/durations to calendar dates only when the meeting date is given; compute weekdays carefully against the real calendar (check the day of week) and keep the original phrase, e.g. `Friday (6 Nov)`. If unsure, keep just the original phrase.
4. Do not invent facts, owners or deadlines. Stay faithful to the transcript; fold the transcript text into concise bullets.
5. Follow any extra formatting instructions from the user over these defaults (e.g. column order, language, length).
6. Reply with the length the user asks for (typically one line): mention the file name and the key counts or highlights (decisions, action items, blockers).
