# AI Calender: daily sync of your AOP work

Every workday afternoon this fills in **your own area** of the AI Calender
with what you worked on that day:

- **Ticks in the "Actual" calendar** for the AOPs in your area that you
  committed or edited.
- **A "Claude day log" in your day note**: which AOPs you worked on and where
  that work is now (on main, live in Decagon, on a branch, or not pushed yet),
  plus tools, tests and the Claude sessions you used.
- **Sprint outcomes**: once a change reaches main or goes live in Decagon, a
  `✅ <change> (<date>)` line is added under "Achieved during Sprint" in that
  AOP's notation for the month. These lines show up in the Achieved view.

It reads two things on your own laptop:

- **Your commits in the power repo**, on every branch: main, Decagon dashboard
  edits, pushed branches that aren't merged, and local branches you haven't
  pushed.
- **Your Claude Code sessions** from that day.

It only adds. Your own notes, ticks and outcomes, and other people's areas, are
never changed. It checks this before every save and stops if anything else
would change.

Setup takes about 10 minutes, once.

---

## Before you start

You need:

- **The power repo** cloned on your laptop. You already have it if you work on AOPs.
- **Python 3.10 or newer.** Check it in PowerShell:

  ```powershell
  python --version
  ```

- **Your calendar login**: the email and password you sign in to the AI Calender with.

## Step 1 – Download the script

Open **PowerShell** and run:

```powershell
New-Item -ItemType Directory -Force "$HOME\aop-calendar-sync" | Out-Null
Invoke-WebRequest "https://raw.githubusercontent.com/adonisph/aop-calender/main/tools/claude-day-sync/claude_day_sync.py" -OutFile "$HOME\aop-calendar-sync\claude_day_sync.py"
```

## Step 2 – Find your values

Get these ready. Setup asks for them one at a time.

| Setup asks for | Where to find it |
|---|---|
| Supabase URL, anon key, workspace ID | Open the AI Calender, click **Login** (top right). The dialog shows all three. The workspace is usually `nordics-team`. |
| Your login email | The email you sign in to the calendar with. |
| Your area id | `pool` = Erik · `sales` = Joona · `delivery` = Emma · `aftersales` = Saana · `others_some` = Christel · `adonis` = Adonis |
| Your first name | As it appears in dashboard commits, e.g. `Saana`. |
| Path to your power repo | The folder you cloned it to, e.g. `C:\Users\you\NCS AI\power`. |
| Your git author emails | Every email you commit with. See below. |

To see which emails you commit with (replace the path and your name):

```powershell
git -C "$HOME\NCS AI\power" log --all --format="%ae" | Sort-Object -Unique | Select-String "saana"
```

Add **all** of them. Commits made from GitHub's website can use an address like
`12345+yourname@users.noreply.github.com`.

## Step 3 – Run setup

```powershell
python "$HOME\aop-calendar-sync\claude_day_sync.py" setup
```

Answer the questions, then type your calendar password when asked. The
password isn't shown while you type and isn't saved. Only a sign-in token is
kept, in `C:\Users\<you>\.aop-calendar-sync\`. When setup works, it ends with
*"Signed in and can read workspace 'nordics-team'"*.

## Step 4 – Try it (nothing is saved)

```powershell
python "$HOME\aop-calendar-sync\claude_day_sync.py" run --dry-run
```

This reads the live calendar and prints what it **would** add for today: the
ticks, and the day log text. Check that the AOPs look right.

If an AOP shows as *"not in calendar AOP list"* but it is in the calendar
under another name, add an alias. Open
`C:\Users\<you>\.aop-calendar-sync\config.json` and fill in `aliases`:

```json
"aliases": {
  "Insurance & Extended Warranty V4": "Insurance V4"
}
```

The left side is the AOP file name from the power repo; the right side is the
name in the calendar.

## Step 5 – Run it once for real

```powershell
python "$HOME\aop-calendar-sync\claude_day_sync.py" run
```

Reload the calendar and open your area. Today should show the new ticks and the
"Claude day log" at the bottom of your day note.

### Optional – fill in your past days

To add your earlier work too, run a backfill from the date you want to start
from. Try it first with `--dry-run`, which prints one line per day and saves
nothing:

```powershell
python "$HOME\aop-calendar-sync\claude_day_sync.py" backfill --from 2026-08-01 --dry-run
python "$HOME\aop-calendar-sync\claude_day_sync.py" backfill --from 2026-08-01
```

Everything is saved in a single write. Your commits go back as far as git
history does. Claude sessions only go back as far as Claude Code has kept them
on your laptop, usually a few weeks.

## Step 6 – Run it automatically every workday

This creates a Windows task that runs it Monday–Friday at 16:30 with no window.
If your laptop is off or asleep at 16:30, it runs as soon as the laptop is on
again. Each run also does any days it missed, up to a week back.

```powershell
$script = "$HOME\aop-calendar-sync\claude_day_sync.py"
$pythonw = (python -c "import sys; print(sys.executable)") -replace 'python\.exe$', 'pythonw.exe'
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$script`" run"
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 16:30
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "AI Calender day sync" -Action $action -Trigger $trigger -Settings $settings
```

To change the time, change `16:30` and run the block again with `-Force` added
to the last line.

Each run is written to `C:\Users\<you>\.aop-calendar-sync\sync.log`. To see the
latest:

```powershell
Get-Content "$HOME\.aop-calendar-sync\sync.log" -Tail 40
```

To stop it:

```powershell
Unregister-ScheduledTask -TaskName "AI Calender day sync" -Confirm:$false
```

---

## What ends up in the calendar

**Ticks:**

- One per AOP per day, only for AOPs in **your** area.
- Only for AOPs you committed or edited. Just opening an AOP goes in the note,
  not the calendar.
- Weekdays only, the same as the calendar allows. Weekend work goes in the
  note.
- No tick if you already ticked that AOP yourself that day.
- Delete a tick and it stays deleted.
- A commit that changes more than 5 AOPs at once, such as a typo pass or a
  voice-test cleanup, counts as one "bulk change" line in the note. It gives no
  ticks or outcomes.

**Sprint outcomes**, in each AOP's monthly notation under "Achieved during
Sprint":

- Only for AOPs in your area.
- Only for changes that are on main or live in Decagon. A branch counts once it
  is merged, if that happens within two weeks.
- At most 3 lines per AOP per day, plus a "+N more changes" line.
- The goal, comments, blockers and any outcomes you wrote yourself are kept as
  they are.
- Remove a line and it stays removed.

**The day log** sits at the bottom of your day note, between two marker lines.
For example:

```
── Claude day log (auto) ──
AOPs worked on (2):
• Insurance & Extended Warranty V4: 3 commits (dashboard, with Claude), branch saana/insurance-v4 (not merged)
   – Insurance V4: an insurance doesn't cover a fault
• Small Product Warranty Handling & Troubleshooting: 1 commit (dashboard), live in Decagon (production)
Looked at in Claude: Customer wants to return an order V3
tools (1): check_insurance
Claude sessions: "Insurance V4 for chat"
── end of Claude day log ──
```

- Write your own text above or below the markers. A new run only replaces the
  part between them.
- AOPs from someone else's area, or not in the calendar, are listed in the
  note only.
- Session titles are listed only for Claude sessions that worked in the power
  repo. Other sessions are just counted.
- To leave session titles out, set `"include_session_titles": false` in your
  `config.json`.

## Good to know

- **Work done in Codex** shows up only through its commits. Codex sessions
  themselves aren't read.
- **Dashboard edits** count for the person named in the commit. A `[Dashboard]`
  commit you pushed that says *"(Martins, …)"* counts for Martins, not for you.
- **It never deletes anything.** Before saving it checks that the only changes
  are its own ticks and its own day log. If anything else would change, it
  stops and saves nothing.
- **Your files:** all of them live in `C:\Users\<you>\.aop-calendar-sync\`.
  - `config.json`: your settings.
  - `token.json`: your sign-in. Keep it private.
  - `sync.log`: what each run did.
  - `last_run.json`: where catch-up starts from.
  - `achieved.json`: outcome lines already added once, so a line you removed
    isn't added again.
- **What it leaves alone:** the planned calendar, monthly goals, comments,
  "required", deflection and conversation numbers, and custom statuses. Those
  stay yours to fill in.

## If something goes wrong

| You see | Do this |
|---|---|
| `sign-in expired or was replaced` | Run `setup` again. Press Enter to keep each saved answer, then type your password. |
| `could not reach the calendar` | No internet at that moment. Nothing was saved; the next run catches up. |
| `Guard: …  Nothing written` | Something other than the day log would have changed, so it stopped. Send the last lines of `sync.log` to Adonis. |
| `Config area '…' is not one of …` | Your area id is mistyped. Run `setup` again. |
| Nothing shows up for a day | Run `collect --date 2026-10-23` (with that day's date) to see what it found. If your commits are missing, add the email you committed with to `author_emails` in `config.json`. |

Other commands:

```powershell
python "$HOME\aop-calendar-sync\claude_day_sync.py" collect --date 2026-10-23   # what it found, no calendar access
python "$HOME\aop-calendar-sync\claude_day_sync.py" run --date 2026-10-22       # redo one day
python "$HOME\aop-calendar-sync\claude_day_sync.py" preview --state export.json # try on an Export JSON from the calendar
```

## Updating the script

Run Step 1 again. Your settings and sign-in are kept.

## Adding a new person or area (for Adonis)

1. Add the area in `index.html` in four places: both area dropdowns,
   `AREA_VIEWS` and `AREA_VIEW_COLORS`.
2. Add it to `AREAS` at the top of `claude_day_sync.py`.
3. Push, then have **everyone reload the calendar** before the new person runs
   the sync.

Step 3 matters because an older copy of the page that is still open treats an
area it doesn't know as Erik's. When that person saves, it moves the new area's
AOPs to Erik and replaces Erik's day note with the new person's.
