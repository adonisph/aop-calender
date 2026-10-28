---
name: ai-calender-day-sync
description: Set up, run, check and fix the AI Calender day sync (tools/claude-day-sync) that fills a team member's own area every workday from their power-repo commits and Claude Code sessions - ticks, the "Claude day log" in the day note, and sprint outcomes. Use when someone says "set up the calendar sync", "day sync", "fill my calendar", "backfill my calendar", "the 16:30 task", "my day log is empty", "sync didn't run", "sign-in expired", "Guard: ... Nothing written", "update the sync script", or asks why an AOP got no tick or outcome.
---

# AI Calender day sync

The sync is one Python script, `tools/claude-day-sync/claude_day_sync.py`, run on
each person's own laptop. It reads that person's commits in the power repo and
their Claude Code sessions, and adds to **their own area** of the AI Calender
(https://adonisph.github.io/aop-calender/): ticks in the Actual calendar, a
"Claude day log" in the day note, and `✅` sprint-outcome lines. It only adds,
and refuses to save if anything else would change.

`tools/claude-day-sync/README.md` is the full guide for people. Read it when you
need a detail this file doesn't have, and follow it rather than inventing steps.

## Hard rules

- **Never type, ask for, store or echo the calendar password.** The person types
  it themselves at the `setup` prompt. Don't put it in a command, a file, a
  commit or chat.
- **Never put the Supabase URL, anon key, token or password in this repo.** The
  repo is public. They belong in `~\.aop-calendar-sync\` only.
- **Always dry-run first** (`run --dry-run`, `backfill ... --dry-run`) and show
  the person what would be added before a real run.
- **Never edit the calendar's data by hand** (Supabase row, Export JSON) to
  "fix" a day. Re-run the sync for that day instead (`run --date YYYY-MM-DD`).
- **Never push to this repo** without the person's explicit yes. A push to main
  publishes the live site.

## Where things are

| What | Where |
|---|---|
| Installed script | `$HOME\aop-calendar-sync\claude_day_sync.py` |
| Settings, sign-in, log | `$HOME\.aop-calendar-sync\` (`config.json`, `token.json`, `sync.log`, `last_run.json`, `achieved.json`) |
| Scheduled task | Windows Task Scheduler, name `AI Calender day sync`, Mon–Fri 16:30, runs `pythonw.exe` |
| Area ids | `pool` Erik · `sales` Joona · `delivery` Emma · `aftersales` Saana · `others_some` Christel · `adonis` Adonis |

## Finding Python

`python` is often not on PATH in the Claude desktop app's Terminal panel. Find
the real interpreter first and use its full path in every command:

```powershell
py -0p                      # lists installed Pythons with paths
(Get-Command python -ErrorAction SilentlyContinue).Source
```

It needs Python 3.10 or newer. Use `python.exe` for commands you run, and
`pythonw.exe` from the same folder for the scheduled task.

## Set up a new person

Walk them through README steps 1–6 in order. What to do yourself, and what
they must do:

1. **Download the script** (README step 1). You can run this.
2. **Collect the values** (README step 2). You can look up their git author
   emails with the command in the README; add every address they commit with.
   The Supabase URL, anon key and workspace come from the calendar's Login
   dialog. The person reads them off, or pastes them in at the prompt.
3. **Run setup in its own PowerShell window.** The password prompt can't take
   input in the app's Terminal panel. Open a real window for them:

   ```powershell
   Start-Process powershell -ArgumentList '-NoExit', '-Command', "& '<full path to python.exe>' `"$HOME\aop-calendar-sync\claude_day_sync.py`" setup"
   ```

   They answer the questions and type the password themselves (it shows as
   `*`). Wait until they say it ended with *"Signed in and can read workspace"*.
   Then confirm `$HOME\.aop-calendar-sync\token.json` exists before going on;
   if it doesn't, setup did not finish.
4. **Dry run** (`run --dry-run`) and show them the day log it would write.
   AOPs marked *"not in calendar AOP list"* that do exist under another name
   need an alias in `config.json` (README step 4).
5. **Real run** (`run`), then ask them to reload the calendar.
6. **Optional backfill:** `backfill --from YYYY-MM-DD --dry-run`, show the
   one-line-per-day summary, then the real `backfill --from YYYY-MM-DD` on
   their yes. It saves everything in one write.
7. **Scheduled task** (README step 6). It must use `pythonw.exe`. The script
   starts git with no console window, so the run is silent; if windows flash,
   their script is old: update it.

## Run, check, re-run

```powershell
$py = "<full path to python.exe>"
$s  = "$HOME\aop-calendar-sync\claude_day_sync.py"
& $py $s run --dry-run                          # today, nothing saved
& $py $s run                                    # today, saved
& $py $s run --date 2026-10-22                  # redo one day
& $py $s backfill --from 2026-08-01 --dry-run   # past days, one line each
& $py $s backfill --from 2026-08-01 --full      # past days, saved, full logs printed
& $py $s collect --date 2026-10-22              # what it found; no calendar access
```

Set `$env:PYTHONIOENCODING = "utf-8"` first if the output shows `?` or
mojibake for `✅`, `•` or `å ä ö`.

Re-running is safe. A run replaces only the block between
`── Claude day log (auto) ──` and `── end of Claude day log ──`; text the person
wrote outside it stays. Ticks or outcome lines they deleted are never added
again.

## Diagnose

Start with the log and the task state:

```powershell
Get-Content "$HOME\.aop-calendar-sync\sync.log" -Tail 40
Get-ScheduledTaskInfo -TaskName "AI Calender day sync" | Select-Object LastRunTime, LastTaskResult, NextRunTime
```

| Symptom | Cause and fix |
|---|---|
| `sign-in expired or was replaced` | Run `setup` again in its own window (step 3). Enter keeps each saved answer. |
| `Guard: … Nothing written` | Something besides its own additions would change. Don't work around it. Send the log lines to Adonis. |
| A day has no log | `collect --date` that day. No commits: an `author_emails` address is missing. No sessions: Claude Code no longer keeps that day's transcripts. |
| AOP worked on, but no tick | The AOP is in someone else's area, isn't in the calendar (alias needed), was only opened, or it's a weekend. The day log says which. |
| No sprint outcome | The change isn't on main or live yet, touched more than 5 AOPs (a bulk change), or only moved or deleted text. |
| Red "!" on a past weekday | The calendar's missing-note marker: that day has no day note, usually because the sync found no work. |
| Console windows flash at 16:30 | Old script. Update it. |
| Task never ran | `LastTaskResult` and `sync.log`. A laptop that was off runs it at next start; a missed week is caught up up to 7 days back. |

## Update the script

Download it again (README step 1), or copy it from this repo's
`tools/claude-day-sync/`. Settings and sign-in are kept. Then do a
`run --dry-run` to check it works.

## Changing the sync or the page (maintainers)

- The day log's text comes from `render_block()` in `claude_day_sync.py`; the
  Notes page shows it via `readableNoteText()` in `index.html`. The sync finds
  its block by the two marker lines, so don't change them.
- A change to what the calendar **stores** (new area, new field) must raise
  `APP_VERSION` in `index.html`. Adding an area also means `AREAS` in the
  script. See the README's "Adding a new person or area".
- `index.html` has mixed line endings. Edit it so each changed line keeps its
  own ending; normalising the file turns the whole file into a diff.
- After changing the script, test with `--dry-run` against the live calendar,
  show the person the diff, and push only on their yes. Then copy the new
  script to `$HOME\aop-calendar-sync\` and, if the log format changed, offer a
  `backfill` so old day logs match.
