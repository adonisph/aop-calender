# AI Calender: daily sync of your AOP work

Every workday afternoon this fills in **your own area** of the AI Calender
with what you worked on that day:

- **Ticks in the "Actual" calendar** for the AOPs you committed or edited:
  your own AOPs and other people's alike. Every tick is marked as yours, so
  work on a colleague's AOP counts for you, not for them.
- **A "Claude day log" in your day note**: which AOPs you worked on and where
  that work is now (on main, live in Decagon, on a branch, or not pushed yet),
  plus tools, tests and the Claude sessions you used.
- **Sprint outcomes**: once a change reaches main or goes live in Decagon, a
  `✅ <change> (<date>)` line is added under "Achieved during Sprint" in that
  AOP's notation for the month. These lines show up in the Achieved view.

Before it saves, Claude can **show you your day and let you change it**: skip
a tick, add one, reword or drop a sprint outcome, add a note for tomorrow. See
[End-of-day review](#end-of-day-review-with-claude).

Once a month it can also help you **plan the month** (goals and planned
ticks), but only when you ask for it. See
[Plan your month](#plan-your-month-once-a-month-with-claude).

It reads two things on your own laptop:

- **Your commits in the power repo**, on every branch: main, Decagon dashboard
  edits, pushed branches that aren't merged, and local branches you haven't
  pushed.
- **Your Claude Code sessions** from that day.

It only adds. Your own notes, ticks and outcomes, and other people's areas, are
never changed. It checks this before every save and stops if anything else
would change.

Setup takes about 10 minutes, once.

**Easiest: let Claude do it.** Open Claude Code in your power repo and type
**setup AOP Calender**. Claude asks you everything and sets it all up. The
phrase only works when your power checkout has the skill: switch to `main`,
pull, and run `python -m harness sync-skills` once. If Claude still doesn't
know the phrase, tell it: *"Read skills/ai-calender-day-sync/SKILL.md from
origin/main in the power repo and follow its Guided setup."* The steps below
are the same setup by hand.

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
| Supabase URL, anon key, workspace ID | Open the AI Calender, click **Login** (top right). The dialog shows all three. The URL starts with `https://`, the anon key with `sb_publishable_`. The workspace is usually `nordics-team`. |
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

Answer the questions, then type your calendar password when asked. It shows
as `*` while you type and isn't saved. Only a sign-in token is
kept, in `C:\Users\<you>\.aop-calendar-sync\`. When setup works, it ends with
*"Signed in and can read workspace 'nordics-team'"*.

Setup checks each answer: a URL pasted into the key field, or the other way
round, is caught and asked again. Your answers are only saved once the sign-in
works, so if it stops (wrong key, three wrong passwords), nothing is saved and
you just run setup again.

## Step 4 – Try it (nothing is saved)

```powershell
python "$HOME\aop-calendar-sync\claude_day_sync.py" run --dry-run
```

This reads the live calendar and prints what it **would** add for today: new
AOPs, the ticks, and the day log text. Check that the AOPs look right.

An AOP you worked on that no area of the calendar has yet is added to your
area, in a color no other AOP has (see *New AOPs* below). If one shows under
*"New AOPs in your area"* but it is already in the calendar under another
name (the line says *"maybe the same as: …"*), add an alias instead. Open
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

Two ways. With **[End-of-day review](#end-of-day-review-with-claude)**, Claude
shows you the day at a time you pick and saves on your yes; then skip this
step. Or the silent way below, which saves without asking.

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

**New AOPs**, in your area:

- One for each AOP you committed or edited that isn't in the calendar yet, in
  any area. It gets the AOP's file name from the power repo.
- Only AOPs that are on main or live in Decagon. Test AOPs, deleted AOPs and
  AOPs that only exist on a branch are left out until they land.
- Each new AOP gets its own color, one that no other AOP or status in the
  calendar has, picked to stand out from the colors already there. Change it
  in the calendar whenever you like.
- An AOP that is already in someone else's area stays there. No copy is made;
  your work on it is a tick of yours on their AOP (see Ticks).
- Versions count as one AOP: *"Returns V3"* and *"Returns V3 Voice"* both
  belong to a calendar AOP called *"Returns"*, so no new one is added. Small
  spelling differences count as the same name too: *"Handling Installation
  issues"* is the calendar's *"Handle Installation issues"*.
- Delete or rename an AOP it added and it isn't added again.
- To switch this off, set `"add_new_aops": false` in your `config.json`.

**Ticks:**

- One per AOP per day, for every AOP you worked on, in **any** area. The tick
  is yours: it counts in your area and your dashboard, never in the AOP
  owner's. Your area's dashboard lists other people's AOPs under *"Worked on
  in other areas"*.
- Don't make copies such as *"Price match AOP (Erik)"* in your own area any
  more. Work on the original; the tick already counts for you.
- Two people can tick the same AOP on the same day. Each tick counts for the
  person who made it.
- An AOP you keep as a custom item (such as *"CSAT Survey V2"*) counts too.
- If the same AOP is in the calendar more than once, the copy in your area
  gets the tick. If none of the copies is yours, the day log says *"in the
  calendar more than once"*; add an alias to pick the right one.
- To tick only your own area's AOPs, set `"tick_other_areas": false` in your
  `config.json`. Work on other people's AOPs then goes in the note only.
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

- Only for AOPs in your area. Work on another person's AOP gets a tick, but
  its notation belongs to that person, so no outcome line goes there.
- Only for changes that are on main or live in Decagon. A branch counts once it
  is merged, if that happens within two weeks.
- A branch change that went live in another commit, such as a `[Dashboard]`
  re-commit or a squash merge, counts too. The day log then says *"on main,
  carried over in another commit"*. It's recognised when at least 80% of the
  text the change added is on main. A change that only moves or deletes text
  can't be recognised this way and stays *"not merged"*.
- At most 3 lines per AOP per day, plus a "+N more changes" line.
- The goal, comments, blockers and any outcomes you wrote yourself are kept as
  they are.
- Remove a line and it stays removed.

**The day log** sits at the bottom of your day note, between two marker lines.
For example:

```
── Claude day log (auto) ──
✅ Done, on main or live (1)
• Small Product Warranty Handling & Troubleshooting: Updated handling

🔧 On a branch, not live yet (1)
• Insurance & Extended Warranty V4: Insurance V4: an insurance doesn't cover a fault  ⟶ saana/insurance-v4

Other
Also changed: 1 tool (check_insurance)
Only opened in Claude: Customer wants to return an order V3
Claude sessions (1): "Insurance V4 for chat"
── end of Claude day log ──
```

- The work is grouped by where it is: done (on main or live in Decagon), on a
  branch that isn't live yet, or edited in Claude without a commit. Another
  person's AOP says whose it is: *"Price match AOP (Joona's AOP)"*.
- On the Notes page the marker lines are hidden and the log keeps its line
  breaks.
- Write your own text above or below the markers. A new run only replaces the
  part between them.
- Session titles are listed only for Claude sessions that worked in the power
  repo. Other sessions are just counted.
- To leave session titles out, set `"include_session_titles": false` in your
  `config.json`.

## End-of-day review (with Claude)

Say **end my day** in Claude Code, or let a Claude Desktop scheduled task
start it 30 minutes before you finish work (the setup in Claude asks when you
finish, and a weekday that ends at another time gets its own task; it's
called *AI Calender: review my day*). Claude then:

1. Runs `run --dry-run` (today plus any day not saved yet, up to a week back)
   and gives you a short summary: new AOPs, ticks, sprint outcomes, and a
   numbered list of suggested fixes, such as an outcome that belongs to
   another AOP or a vague commit subject.
2. Asks with buttons, in one go: save with the suggested fixes or as it is,
   your note on what you did (with a draft), and whether a "maybe the same
   as" AOP is a duplicate. It writes your answers to
   `C:\Users\<you>\.aop-calendar-sync\changes-YYYY-MM-DD.json`:

   ```json
   {
     "skip_ticks": [{"date": "2026-11-04", "aop": "DNB Supertilbud"}],
     "add_ticks": [{"date": "2026-11-04", "aop": "Cleanup"}],
     "skip_outcomes": ["✅ Removed force (4 Nov)"],
     "reword_outcomes": {"Moving everything to V4 Branch": "GDPR Breach: V4 is now the only GDPR AOP"},
     "skip_new_aops": ["Recycling and RePOWER Trade-in V2"],
     "notes": {"2026-11-04": "Tomorrow: read the GDPR hand-back sims"}
   }
   ```

   Names as the dry run shows them; outcome lines with or without `✅` and the
   date. A note shows in the day log under *📝 My note*.
3. Runs `run --dry-run --changes <file>`. If it shows what you agreed to, it
   saves; if anything differs, it shows you and asks again.
4. Saves with `run --changes <file>`, never before your answer. If you answer the
   next morning, it saves with `run --until <yesterday> --changes <file>`
   instead, so today stays open for this evening's review.

By hand:

```powershell
python "$HOME\aop-calendar-sync\claude_day_sync.py" run --dry-run --changes "$HOME\.aop-calendar-sync\changes-2026-11-04.json"
python "$HOME\aop-calendar-sync\claude_day_sync.py" run --changes "$HOME\.aop-calendar-sync\changes-2026-11-04.json"
```

A name, date or key the script doesn't know stops the run with a message, and
nothing is saved. Skipped ticks, skipped new AOPs and notes are kept in
`review.json`, so the next run doesn't bring them back; skipped outcome lines
never come back either. The review can't take back something already saved:
remove that in the calendar, and the sync won't add it again. If you use the
review, turn off the silent Windows task from Step 6 so it doesn't save first:

```powershell
Disable-ScheduledTask -TaskName "AI Calender day sync"
```

## Plan your month (once a month, with Claude)

At the start of a month, Claude can help you write your plan. Open Claude Code
in the power repo and say *"plan my month in the AI Calender"*. It goes like
this:

1. **Claude reads your facts** with `plan-facts`. This only reads; nothing is
   saved:
   - last month's goals and outcomes per AOP, and how many days you planned
     and actually worked on each;
   - your commits since the start of last month, and what is still on a branch;
   - days you already marked off this month, and days you already planned.
2. **You agree on the plan together.** Claude suggests 2–4 goals for the month
   and a focus per week. You change it until it's yours.
3. **Dry run.** Claude shows exactly what would be added.
4. **Saved on your yes.**

```powershell
& $py $s plan-facts --month 2026-11 --out "$HOME\.aop-calendar-sync\facts-2026-11.json"
& $py $s plan --file "$HOME\.aop-calendar-sync\plan-2026-11.json" --dry-run
& $py $s plan --file "$HOME\.aop-calendar-sync\plan-2026-11.json"
```

**What `plan` adds**, only in your own area:

- A **Goal:** section with your goals in each AOP's monthly notation, above
  "Achieved during Sprint", so the outcomes the daily sync adds land below
  your goals.
- The AOP's one-line **goal** field, but only if it's empty.
- **Planned ticks** for working days from today until the end of the month.
  Days you marked off (Days off, Vacation, Sick leave) get none.

It never changes a goal, note or tick you already have, never touches the
Actual calendar, and a planned tick you delete isn't added again. Run it again
later in the month and it only adds what's new.

**The plan file** Claude writes for you looks like this (names as they are in
the calendar):

```json
{
  "month": "2026-11",
  "aops": {
    "Large Product Warranty Handling & Troubleshooting": {
      "goal": "Warranty V4 live in all four markets",
      "goals": ["Merge the troubleshooting-V4 branch", "TVs stay in Large when the size check says small"]
    }
  },
  "days": {
    "2026-11-03": ["Large Product Warranty Handling & Troubleshooting"],
    "2026-11-04": ["Cleanup day"]
  }
}
```

## Good to know

- **Work done in Codex** shows up only through its commits. Codex sessions
  themselves aren't read.
- **Dashboard edits** count for the person named in the commit. A `[Dashboard]`
  commit you pushed that says *"(Martins, …)"* counts for Martins, not for you.
- **It never deletes anything.** Before saving it checks that the only changes
  are its own new AOPs, ticks, day log and outcome lines. If anything else
  would change, it stops and saves nothing.
- **Your files:** all of them live in `C:\Users\<you>\.aop-calendar-sync\`.
  - `config.json`: your settings.
  - `token.json`: your sign-in. Keep it private.
  - `sync.log`: what each run did.
  - `last_run.json`: where catch-up starts from.
  - `achieved.json`: outcome lines already added once, so a line you removed
    isn't added again.
  - `created_aops.json`: AOPs it added to the calendar, so one you deleted or
    renamed isn't added again.
- **What the daily run leaves alone:** the planned calendar, monthly goals,
  comments, "required", deflection and conversation numbers, and custom
  statuses. Only `plan` adds goals and planned ticks, and only when you run it
  yourself.

## If something goes wrong

| You see | Do this |
|---|---|
| `sign-in expired or was replaced` | Run `setup` again. Press Enter to keep each saved answer, then type your password. |
| `could not reach the calendar` | No internet at that moment. Nothing was saved; the next run catches up. |
| `Guard: …  Nothing written` | Something other than the day log would have changed, so it stopped. Send the last lines of `sync.log` to Adonis. |
| `Config area '…' is not one of …` | Your area id is mistyped. Run `setup` again. |
| `The calendar was saved by a newer page` | The calendar changed what it stores. Update the script (see [Updating the script](#updating-the-script)). |
| `another person's AOP, note only (… isn't on version 2 yet)` | The new calendar page hasn't saved the workspace yet. It sorts itself out once someone opens the new page. |
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
2. **Raise `APP_VERSION` by one** in `index.html`.
3. Add it to `AREAS` at the top of `claude_day_sync.py`.
4. Push.

Raising `APP_VERSION` matters. An older copy of the page that is still open
treats an area it doesn't know as Erik's: when that person saves, it moves the
new area's AOPs to Erik and replaces Erik's day note with the new person's.

Since 27 Oct 2026 the page protects itself against this:

- Every 2 minutes, and before each save, it checks whether a new version has
  been published.
- Before it applies data from the shared calendar, it checks whether that data
  was saved by a page with a higher `APP_VERSION`.
- If either is true, the tab stops saving and shows a **Reload** banner.

Without the raised number, an old tab only notices on its next check. That
leaves a gap of up to 2 minutes. Raise it for any change to what the calendar
stores: a new area or a new field.

Tabs opened before 27 Oct 2026 don't have this check. Ask everyone to reload
once after that update is pushed.

The script has the same check the other way round: `KNOWN_APP_VERSION` at the
top of `claude_day_sync.py` is the highest `APP_VERSION` it understands. When a
change to what the calendar stores also changes the script, raise both and ask
everyone to update their script.

## Ticks for a person, not an area (APP_VERSION 2, for Adonis)

Since version 2 a tick can carry `by`: the area id of the person who did the
work. A tick without `by` belongs to the AOP's area, which is what every tick
meant before. Area views, the dashboards and the day sync count a tick for its
`by`. Master view sums people: a day two people worked counts as two days.

Rollout, in this order:

1. Push `index.html`. Open it once, signed in, so the workspace is saved with
   `appVersion: 2`. Until then the script keeps other people's AOPs in the
   note only, because an older page would count the tick for the AOP's owner.
2. Everyone updates the script ([Updating the script](#updating-the-script)).
   An old script still writes correct ticks on its own AOPs. But it sees a
   colleague's tick on one of them as "already ticked" and skips its own.
3. Move the old copies, such as *"Price match AOP (Erik)"*, onto the originals:

```powershell
& $py $s migrate-copies --suffix "(Erik)" --area pool            # dry run: shows every copy, its original and what moves
& $py $s migrate-copies --suffix "(Erik)" --area pool --write    # saves
```

For each copy in that area whose name without the suffix is exactly one AOP
in another area:

- Its ticks (planned and actual) move to the original as ticks by that
  person. They keep their date and time, and the copy's ticks get a tombstone.
- Its month notes are appended to the original's notation for that month,
  under *"From Erik's copy:"*.
- The copy is deleted, the way the calendar deletes an AOP.

A copy with no single original is left alone and listed. Running it again
changes nothing. To try it offline first, export the state from the calendar
and pass `--state export.json --out migrated.json`. Also remove any `aliases`
in that person's `config.json` that point at a copy's name.
