#!/usr/bin/env python3
"""Daily AI Calender sync: adds one person's AOP work to their own area.

Sources, per local calendar day:
  - their commits in the power repo, on every branch (main, pushed branches,
    local branches not pushed yet) - Decagon dashboard commits included
  - their Claude Code sessions (~/.claude/projects): which AOP files they
    edited or opened, and the titles of sessions that touched the power repo

What it writes, and nothing else:
  - a new AOP in their own area, in a color no other AOP has, for each AOP
    they committed or edited that no area of the calendar has yet (never
    re-added once someone deletes it)
  - an "actual" tick per AOP they committed or edited that weekday, marked as
    theirs (`by`), on their own AOPs and on other people's alike (fixed id,
    never re-added once someone deletes it)
  - a "Claude day log" block in their own area's day note; any text the
    person wrote stays as it is, only the block between the markers is
    replaced on a re-run
  - a "✅ <change> (<date>)" line under "Achieved during Sprint:" in the
    month's notation of each AOP of their own area, once a change of theirs
    is on main or live in Decagon (never re-added once someone removes it)

End-of-day review (`run --dry-run`, then `run --changes FILE`): the person sees
what would be added and changes it before it is saved - skip or add ticks,
drop or reword sprint outcomes, skip new AOPs, add their own note to the day
log. Skips and notes are kept in review.json, so a later run doesn't undo them.

Monthly plan (`plan-facts`, then `plan --file`), only when the person runs it:
  - `plan-facts` prints what a month's plan can build on: last month's goals
    and outcomes, commits, branches not live yet, days off. It writes nothing.
  - `plan` adds the plan the person agreed to: a "Goal:" section (and the
    one-line goal field, if empty) in each AOP's monthly notation, and planned
    ticks on working days from today on. Existing goals, ticks and notes stay.

Every write is a compare-and-swap on the shared workspace row, and is refused
unless the new state equals the old one apart from those additions.

Standard library only. Run `python claude_day_sync.py --help`.
"""
import argparse
import colorsys
import copy
import datetime as dt
import getpass
import json
import os
import re
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

HOME_DIR = Path(os.environ.get("AOP_SYNC_HOME") or Path.home() / ".aop-calendar-sync")
CONFIG_FILE = HOME_DIR / "config.json"
TOKEN_FILE = HOME_DIR / "token.json"
LAST_RUN_FILE = HOME_DIR / "last_run.json"
# Sprint-outcome lines already added once; a line someone removed stays removed.
ACHIEVED_LEDGER_FILE = HOME_DIR / "achieved.json"
# AOPs this tool added to the calendar, {base name: id}; one someone deleted or renamed is not added again.
CREATED_AOPS_FILE = HOME_DIR / "created_aops.json"
# What the person changed in an end-of-day review and must hold on a re-run:
# {"skip_ticks": {date: [aop id]}, "skip_new_aops": [base name], "notes": {date: text}}.
REVIEW_FILE = HOME_DIR / "review.json"

TABLE = "planner_workspaces"
# Mirrors AREA_VIEWS in index.html. The app maps any unknown area to "pool"
# (Erik), so an area outside this list is refused rather than written.
AREAS = {
    "pool": "Erik",
    "sales": "Joona",
    "delivery": "Emma",
    "aftersales": "Saana",
    "others_some": "Christel",
    "adonis": "Adonis",
    "special_events": "Special Events",
}
BLOCK_START = "── Claude day log (auto) ──"
BLOCK_END = "── end of Claude day log ──"
ENTRY_SOURCE = "claude-sync"
PLAN_SOURCE = "claude-plan"
# The app's APP_VERSION this script knows. A workspace saved by a newer page is refused, so an
# old copy of this script never writes into a data shape it doesn't understand.
KNOWN_APP_VERSION = 2
# From this APP_VERSION on, a tick's `by` says whose work it is; before it, the AOP's area did.
# Ticks on another person's AOP wait until a page this new has saved the workspace.
BY_APP_VERSION = 2
ACHIEVED_HEADER = "Achieved during Sprint:"
GOAL_HEADER = "Goal:"  # the app's own notation template starts with this
MAX_CATCHUP_DAYS = 7
ACHIEVED_LOOKBACK_DAYS = 14  # a branch merged within two weeks still becomes a sprint outcome
MAX_SUBJECTS_PER_AOP = 3
MAX_ACHIEVED_PER_AOP_DAY = 3
MAX_SESSION_TITLES = 8
BULK_AOP_LIMIT = 5  # a commit touching more AOPs than this is a sweep (typo pass, voice-test pruning), not AOP work
# A location starting with one of these counts as landed (a sprint outcome).
LANDED = ("on main", "live in Decagon")
CARRIED_MIN_SHARE = 0.8  # share of a commit's added lines that must be on main for "carried over"
CARRIED_MIN_LINE = 8     # shorter added lines ("},", "]") say nothing about where a change is

OTHER_KINDS = {
    "tools": "tool",
    "guardrails": "guardrail",
    "guidelines_v2": "guideline",
    "automations": "automation",
    "dify": "Dify workflow",
    "welcome_messages": "welcome message",
    "voice_agent_configs": "voice config",
    "failover_configs": "failover config",
    "code_modules": "code module",
    "tests": "test",
}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# The editor a [Dashboard] commit names: "(Martins, PR #231, ...)", ": Saana, 22 Oct 12:10, 545eee2",
# "(Erik Bredenwall, 13:49, 03a2858)". Group 1 is the first name.
DASHBOARD_EDITOR_RE = re.compile(
    r"(?:\(|: )([A-ZÅÄÖÆØ][a-zåäöæø]+)(?: [A-ZÅÄÖÆØ][a-zåäöæø]+)?, (?:PR #\d+|(?:\d{1,2} [A-Z][a-z]{2} )?\d{1,2}:\d{2})")
# Matches aops/X.py and aops/generated/X.json inside JSON-dumped tool input,
# where a Windows separator shows up as one or more backslashes.
AOP_PATH_RE = re.compile(r"aops[\\/]+(?:generated[\\/]+)?([^\"\\/\n]+?)\.(?:json|py)\b")
ACHIEVED_LINE_RE = re.compile(r"^✅ .+ \(\d{1,2} [A-Z][a-z]{2}\)$|^✅ \+\d+ more changes \(\d{1,2} [A-Z][a-z]{2}\)$")
NOTATION_SECTION_RE = re.compile(r"^(goal|achieved\s*during\s*sprint|existing\s*blockers?)\b", re.I)
GOAL_SECTION_RE = re.compile(r"^goals?\s*:", re.I)
# AOPs that mark a day off, not work. A day planned with one of these gets no planned ticks.
ABSENCE_NAMES = {"vacation", "days off", "day off", "sickleave", "sick leave", "time off"}


# ---------------------------------------------------------------- utilities

def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def local_day_bounds(day):
    start = dt.datetime.combine(day, dt.time()).astimezone()
    return start, start + dt.timedelta(days=1)


def day_range(first, last):
    return [first + dt.timedelta(days=i) for i in range((last - first).days + 1)]


def short_date(day_iso):
    d = dt.date.fromisoformat(day_iso)
    return f"{d.day} {d.strftime('%b')}"


def parse_ts(raw):
    try:
        return dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return default


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def norm_name(text):
    text = unicodedata.normalize("NFKC", str(text or "")).lower().replace("&", " and ")
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def base_name(text):
    """AOP name without version/voice suffixes: 'returns v3 voice' -> 'returns'."""
    n = norm_name(text)
    while True:
        stripped = re.sub(r"\s+(v\d+(?:\s\d+)*|voice|chat|live)$", "", n)
        if stripped == n:
            return n
        n = stripped


def load_config():
    cfg = read_json(CONFIG_FILE)
    if not cfg:
        sys.exit(f"No config at {CONFIG_FILE}. Run `python {Path(__file__).name} setup` first.")
    if cfg.get("area") not in AREAS:
        sys.exit(f"Config area {cfg.get('area')!r} is not one of {', '.join(AREAS)}.")
    return cfg


# ------------------------------------------------------------ git activity

# Under pythonw (the scheduled task) every console program would otherwise flash its own window.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {r.stderr.strip()[:300]}")
    return r


def classify_path(path):
    p = path.replace("\\", "/")
    m = re.match(r"aops/(?:generated/)?([^/]+)\.(?:json|py)$", p)
    if m:
        return "aop", m.group(1)
    parts = p.split("/")
    kind = OTHER_KINDS.get(parts[0])
    if kind and len(parts) > 1:
        # dify/flows/<workflow>.yml is named by its file; tools/<tool>/... by its folder
        return kind, Path(parts[-1] if parts[0] == "dify" else parts[1]).stem
    return None


# Each git process costs ~0.1 s on Windows, so the per-commit questions below are answered from a
# few whole-repo calls, cached per run: ancestry sets, one batched diff, one cat-file reader and
# the graph of branch-only commits.
_CACHE = {}


def _repo_cache(repo):
    return _CACHE.setdefault(str(repo), {"ancestors": {}, "diffs": {}, "files": {}, "branches": None, "cat": None})


def is_ancestor(repo, sha, ref):
    """Same answer as `git merge-base --is-ancestor sha ref`."""
    sets = _repo_cache(repo)["ancestors"]
    if ref not in sets:
        r = git(repo, "rev-list", ref, check=False)
        sets[ref] = set(r.stdout.split()) if r.returncode == 0 else set()
    return sha in sets[ref]


def _parse_added(diff_text):
    added, path = {}, None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            path = line[6:].rstrip("\t") if line.startswith("+++ b/") else None
        elif path and line.startswith("+") and len(line.strip()) > CARRIED_MIN_LINE:
            added.setdefault(path, []).append(line[1:].strip().rstrip(","))
    return added


def prefetch_diffs(repo, shas):
    """Every commit's added lines from one `git show` call per 100 commits, instead of one call each."""
    diffs = _repo_cache(repo)["diffs"]
    todo = [s for s in dict.fromkeys(shas) if s not in diffs]
    for i in range(0, len(todo), 100):
        chunk = todo[i:i + 100]
        r = git(repo, "-c", "core.quotePath=false", "show", "--format=%x1e%H", "--unified=0", "--no-renames",
                *chunk, check=False)
        for record in r.stdout.split("\x1e")[1:]:
            sha, _, body = record.partition("\n")
            diffs[sha.strip()] = _parse_added(body)


def _added_lines(repo, sha):
    diffs = _repo_cache(repo)["diffs"]
    if sha not in diffs:
        prefetch_diffs(repo, [sha])
    return diffs.get(sha, {})


def _file_lines(repo, ref, path):
    """The stripped lines of ref:path, or None if it isn't there. Read through one `git cat-file --batch`."""
    cache = _repo_cache(repo)
    files = cache["files"]
    key = (ref, path)
    if key not in files:
        cat = cache["cat"]
        if cat is None or cat.poll() is not None:
            cat = cache["cat"] = subprocess.Popen(["git", "-C", str(repo), "cat-file", "--batch"], stdin=subprocess.PIPE,
                                                  stdout=subprocess.PIPE, creationflags=NO_WINDOW)
        cat.stdin.write(f"{ref}:{path}\n".encode("utf-8"))
        cat.stdin.flush()
        # "<oid> <type> <size>", or "<name> missing" / "<name> ambiguous", where <name> may contain spaces
        header = cat.stdout.readline().decode("utf-8", "replace").rstrip("\n").rsplit(" ", 2)
        files[key] = None
        if len(header) == 3 and header[2].isdigit():
            body = cat.stdout.read(int(header[2]))
            cat.stdout.read(1)  # the newline after each object
            if header[1] == "blob":
                files[key] = {l.strip().rstrip(",") for l in body.decode("utf-8", "replace").splitlines()}
    return files[key]


def carried_into(repo, sha, ref):
    """True when the lines this commit added are in ref's files even though the commit itself is not:
    the change went live in another commit, such as a [Dashboard] re-commit or a squash merge."""
    # Lines already in the parent's file were only moved or re-indented; they say nothing.
    added = {p: [l for l in lines if l not in (_file_lines(repo, f"{sha}^", p) or set())]
             for p, lines in _added_lines(repo, sha).items()}
    total = sum(len(v) for v in added.values())
    if not total:
        return False  # only deletions or tiny edits: can't tell
    found = 0
    for path, lines in added.items():
        have = _file_lines(repo, ref, path)
        if have:
            found += sum(1 for l in lines if l in have)
    return found / total >= CARRIED_MIN_SHARE


def _branches_containing(repo, sha, main_ref):
    """(remote branch names, local branch names) containing sha, sorted like `git branch --contains`.
    Only valid for a commit that isn't on main_ref: it walks the graph of commits not on main."""
    cache = _repo_cache(repo)
    if cache["branches"] is None:
        parents = {}
        for line in git(repo, "rev-list", "--parents", "--branches", "--remotes", f"^{main_ref}").stdout.splitlines():
            ids = line.split()
            parents[ids[0]] = ids[1:]
        contains = {}
        refs = git(repo, "for-each-ref", "--format=%(objectname) %(refname)", "refs/heads", "refs/remotes").stdout
        for line in sorted(refs.splitlines(), key=lambda l: l.split(" ", 1)[1]):
            tip, refname = line.split(" ", 1)
            if refname.endswith("/HEAD"):
                continue
            kind = "remote" if refname.startswith("refs/remotes/") else "local"
            short = refname.split("/", 2)[2]
            stack, seen = [tip], set()
            while stack:
                c = stack.pop()
                if c in seen or c not in parents:
                    continue
                seen.add(c)
                contains.setdefault(c, {"remote": [], "local": []})[kind].append(short)
                stack.extend(parents[c])
        cache["branches"] = contains
    found = cache["branches"].get(sha) or {"remote": [], "local": []}
    return found["remote"], found["local"]


def commit_location(repo, sha, main_ref, live_ref):
    if is_ancestor(repo, sha, main_ref):
        return "on main"
    if live_ref and is_ancestor(repo, sha, live_ref):
        return "live in Decagon (production)"
    if carried_into(repo, sha, main_ref):
        return "on main, carried over in another commit"
    if live_ref and carried_into(repo, sha, live_ref):
        return "live in Decagon (production), carried over in another commit"
    remote, local = _branches_containing(repo, sha, main_ref)
    if remote:
        name = remote[0].split("/", 1)[-1]
        more = f" +{len(remote) - 1}" if len(remote) > 1 else ""
        return f"branch {name}{more} (not merged)"
    if local:
        return f"local only, not pushed ({local[0]})"
    return "not on any branch"


def fetch_repo(cfg):
    r = git(Path(cfg["power_repo"]).expanduser(), "fetch", "--all", "--prune", "--quiet", check=False)
    return [] if r.returncode == 0 else [f"git fetch failed, used what was already fetched: {r.stderr.strip()[:200]}"]


def collect_commits(cfg, first, last):
    """The person's commits authored on local days first..last, on any branch."""
    repo = Path(cfg["power_repo"]).expanduser()
    main_ref = cfg.get("main_ref", "origin/main")
    live_ref = cfg.get("live_ref", "origin/production")
    my_name = (cfg.get("name") or "").strip().lower()
    start, _ = local_day_bounds(first)
    _, end = local_day_bounds(last)
    args = ["log", "--all", "--exclude=refs/stash", "--no-merges", "-F", "--regexp-ignore-case",
            f"--since={start.isoformat()}", "--date=iso-strict", "--name-only",
            "--format=%x1e%H%x1f%ad%x1f%ae%x1f%s%x1f%(trailers:key=Co-Authored-By,valueonly,separator=;)"]
    args += [f"--author={email}" for email in cfg["author_emails"]]
    out = git(repo, *args).stdout
    commits, seen = [], set()
    for record in out.split("\x1e"):
        if not record.strip():
            continue
        head, _, files = record.partition("\n")
        sha, when, email, subject, trailers = (head.split("\x1f") + [""] * 5)[:5]
        authored = parse_ts(when)
        if not authored or not (start <= authored < end):
            continue
        # A [Dashboard] commit that names its editor, e.g. "(Martins, PR #231, ...)" or
        # ": Saana, 22 Oct 12:10, 545eee2", carries someone else's dashboard work.
        editor = DASHBOARD_EDITOR_RE.search(subject) if subject.startswith("[Dashboard]") else None
        if editor and my_name and editor.group(1).lower() != my_name:
            continue
        paths = [f.strip() for f in files.splitlines() if f.strip()]
        key = (authored.date(), subject.strip(), tuple(sorted(paths)))
        if key in seen:  # the same change rebased or cherry-picked onto another branch
            continue
        seen.add(key)
        commits.append({
            "sha": sha,
            "day": authored.date().isoformat(),
            "time": authored.strftime("%H:%M"),
            "subject": subject.strip(),
            "how": "dashboard" if subject.startswith("[Dashboard]") else
                   "with Claude" if "claude" in trailers.lower() else "by hand",
            "items": [c for c in (classify_path(p) for p in paths) if c],
        })
    # the diffs of every commit that isn't on main or live, in a few calls, before checking where each one is
    prefetch_diffs(repo, [c["sha"] for c in commits if not is_ancestor(repo, c["sha"], main_ref)
                          and not (live_ref and is_ancestor(repo, c["sha"], live_ref))])
    for c in commits:
        c["where"] = commit_location(repo, c["sha"], main_ref, live_ref)
        c["sha"] = c["sha"][:8]
    return commits


# ------------------------------------------------------ Claude Code sessions

def landed_aop_names(cfg):
    """AOP names on main or live. Only these become new calendar AOPs, so test and deleted AOPs don't."""
    repo = Path(cfg["power_repo"]).expanduser()
    names = set()
    for ref in (cfg.get("main_ref", "origin/main"), cfg.get("live_ref", "origin/production")):
        r = git(repo, "ls-tree", "-r", "--name-only", ref, "aops", check=False)
        names.update(c[1] for c in (classify_path(p) for p in r.stdout.splitlines()) if c and c[0] == "aop")
    return names


def known_aop_names(cfg, extra=()):
    """AOP names that really exist: on main, live, or in the working tree."""
    repo = Path(cfg["power_repo"]).expanduser()
    names = set(extra) | landed_aop_names(cfg)
    for p in (repo / "aops").rglob("*"):
        c = classify_path(p.relative_to(repo).as_posix())
        if c and c[0] == "aop":
            names.add(c[1])
    return names


def collect_sessions(cfg, first, last, valid_names):
    """{day_iso: {session_id: {title, power, aops{name: edited}}}} for local days first..last."""
    root = Path(cfg.get("claude_projects_dir") or Path.home() / ".claude" / "projects").expanduser()
    repo_key = str(Path(cfg["power_repo"]).expanduser()).lower().replace("\\", "/")
    start, _ = local_day_bounds(first)
    _, end = local_day_bounds(last)
    by_day, titles = {}, {}
    if not root.is_dir():
        return by_day
    for path in root.rglob("*.jsonl"):
        try:
            if dt.datetime.fromtimestamp(path.stat().st_mtime).astimezone() < start:
                continue
        except OSError:
            continue
        rel = path.relative_to(root).parts
        sid = path.stem if len(rel) == 2 else rel[1]  # subagent transcripts count for their parent session
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"customTitle"' in line:
                    try:
                        titles[sid] = json.loads(line).get("customTitle") or titles.get(sid, "")
                    except ValueError:
                        pass
                    continue
                if '"timestamp"' not in line or ('"type":"assistant"' not in line and '"type":"user"' not in line):
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                ts = parse_ts(obj.get("timestamp"))
                if not ts or not (start <= ts < end):
                    continue
                s = by_day.setdefault(ts.date().isoformat(), {}).setdefault(sid, {"power": False, "aops": {}})
                if str(obj.get("cwd", "")).lower().replace("\\", "/").startswith(repo_key):
                    s["power"] = True
                content = (obj.get("message") or {}).get("content")
                if obj.get("type") != "assistant" or not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    dumped = json.dumps(block.get("input") or {}, ensure_ascii=False)
                    names = {m.group(1).strip() for m in AOP_PATH_RE.finditer(dumped)}
                    if names:
                        s["power"] = True
                    names &= valid_names  # drops placeholders such as <AOP> or $f
                    if len(names) != 1:   # a sweep over many AOPs is not work on any one of them
                        continue
                    name = names.pop()
                    s["aops"][name] = s["aops"].get(name) or block.get("name") in EDIT_TOOLS
    for sessions in by_day.values():
        for sid, s in sessions.items():
            s["title"] = titles.get(sid, "")
    return by_day


# --------------------------------------------------------------- collecting

def is_bulk(commit):
    return sum(1 for kind, _ in commit["items"] if kind == "aop") > BULK_AOP_LIMIT


def build_activity(day_iso, commits, sessions):
    aops, other, bulk = {}, {}, {}
    for c in commits:
        if is_bulk(c):
            b = bulk.setdefault(clean_subject(c["subject"]), {"subject": clean_subject(c["subject"]), "aops": 0, "where": set()})
            b["aops"] = max(b["aops"], sum(1 for kind, _ in c["items"] if kind == "aop"))
            b["where"].add(c["where"])
            continue
        for kind, name in c["items"]:
            if kind == "aop":
                a = aops.setdefault(name, {"name": name, "commits": [], "claude": None})
                a["commits"].append({k: c[k] for k in ("sha", "time", "subject", "how", "where")})
            else:
                other.setdefault(f"{kind}|{name}", set()).add(c["sha"])
    for s in sessions.values():
        for name, edited in s["aops"].items():
            a = aops.setdefault(name, {"name": name, "commits": [], "claude": None})
            a["claude"] = "edited" if edited or a["claude"] == "edited" else "opened"
    return {
        "date": day_iso,
        "aops": sorted(aops.values(), key=lambda a: (-len(a["commits"]), a["name"].lower())),
        "other": sorted(({"kind": k.split("|")[0], "name": k.split("|")[1], "commits": len(v)}
                         for k, v in other.items()), key=lambda o: (o["kind"], o["name"].lower())),
        "bulk": [{**b, "where": sorted(b["where"])} for b in bulk.values()],
        "sessions": {
            "power_titles": [s["title"] or "untitled session" for s in sessions.values() if s["power"]],
            "other_count": sum(1 for s in sessions.values() if not s["power"]),
        },
    }


def clean_subject(subject, limit=120):
    s = re.sub(r"^\[Dashboard\]\s*", "", subject).strip()
    s = re.sub(r"\s*\([A-ZÅÄÖÆØ][a-zåäöæø]+(?: [A-ZÅÄÖÆØ][a-zåäöæø]+)?, [^)]*\)\s*$", "", s)  # "(Adonis, 23 Oct 07:10, b475bf3)"
    s = " ".join(s.split()).rstrip(" .")
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0].rstrip(" ,.:;") + "…"
    return s


def build_achievements(commits):
    """Sprint-outcome lines for AOP changes that are on main or live in Decagon."""
    grouped = {}
    for c in commits:
        if not c["where"].startswith(LANDED) or is_bulk(c):
            continue
        for kind, name in c["items"]:
            if kind == "aop":
                grouped.setdefault((name, c["day"]), []).append(clean_subject(c["subject"]))
    items = []
    for (name, day), subjects in sorted(grouped.items()):
        unique = list(dict.fromkeys(s for s in subjects if s))
        for subject in unique[:MAX_ACHIEVED_PER_AOP_DAY]:
            items.append({"aop": name, "month": day[:7], "day": day,
                          "line": f"✅ {subject} ({short_date(day)})", "key": f"{day}|{norm_name(name)}|{norm_name(subject)}"})
        if len(unique) > MAX_ACHIEVED_PER_AOP_DAY:
            rest = len(unique) - MAX_ACHIEVED_PER_AOP_DAY
            items.append({"aop": name, "month": day[:7], "day": day,
                          "line": f"✅ +{rest} more changes ({short_date(day)})", "key": f"{day}|{norm_name(name)}|+more"})
    return items


def collect(cfg, first, last, fetch=True):
    """Activity per day for first..last, plus sprint outcomes from the last two weeks."""
    warnings = fetch_repo(cfg) if fetch else []
    since = min(first, dt.date.today() - dt.timedelta(days=ACHIEVED_LOOKBACK_DAYS))
    commits = collect_commits(cfg, since, last)
    committed = {name for c in commits for kind, name in c["items"] if kind == "aop"}
    sessions = collect_sessions(cfg, first, last, known_aop_names(cfg, committed))
    days = []
    for day in day_range(first, last):
        d = day.isoformat()
        days.append(build_activity(d, [c for c in commits if c["day"] == d], sessions.get(d, {})))
    return {"days": days, "achievements": build_achievements(commits), "landed_aops": sorted(landed_aop_names(cfg)),
            "warnings": warnings}


# ------------------------------------------------------------- calendar state

FILLER_WORDS = {"and", "or", "the", "a", "an", "of", "for", "to", "with"}


def word_key(text):
    """Base name without filler words or endings: 'Handling Misrouted Receipts and SMS' and
    'Handle Misrouted Receipt & SMS' both give 'handl misrouted receipt sms'."""
    out = []
    for w in base_name(text).split():
        if w in FILLER_WORDS:
            continue
        for suffix in ("ing", "es", "s", "e"):
            if w.endswith(suffix) and len(w) - len(suffix) >= 3:
                w = w[:-len(suffix)]
                break
        out.append(w)
    return " ".join(out)


def match_aop(name, aops, aliases, area=None):
    """The calendar item for an AOP file name: an exact name, then without versions, then near spelling.
    Several matches resolve to the one in `area`, if exactly one is there; otherwise to none."""
    target = aliases.get(name, name)
    for key in (norm_name, base_name, word_key):
        found = [i for i, a in aops.items() if key(a.get("name")) == key(target)]
        if len(found) > 1 and area:
            own = [i for i in found if aops[i].get("area") == area]
            found = own if len(own) == 1 else found
        if len(found) == 1:
            return found[0]
        if found and key is not norm_name:
            return None  # ambiguous: don't fall through to a looser match
    return None


SEVERAL = "\0several"  # matches[] value: more than one calendar item fits and none is the person's own


def has_any_match(name, aops, aliases):
    """True if some calendar item matches at all, even when several do."""
    target = aliases.get(name, name)
    return any(key(a.get("name")) == key(target) for key in (norm_name, base_name, word_key) for a in aops.values())


def calendar_aops(state):
    """Every item an AOP can match: AOPs and custom items (some teams keep AOPs as custom items)."""
    return {i: a for i, a in (state.get("aops") or {}).items() if isinstance(a, dict)}


def app_version(state):
    try:
        return int((state or {}).get("appVersion") or 0)
    except (TypeError, ValueError):
        return 0


def check_app_version(state):
    if app_version(state) > KNOWN_APP_VERSION:
        raise RuntimeError(f"The calendar was saved by a newer page (version {app_version(state)}); this script "
                           f"knows version {KNOWN_APP_VERSION}. Update the script (README, 'Updating'). Nothing written.")


def entry_owner(entry, aops):
    """Whose work a tick is, like the app's entryOwner: its `by`, else the area of its AOP."""
    if entry.get("by") in AREAS:
        return entry["by"]
    area = (aops.get(entry.get("aopId")) or {}).get("area")
    return area if area in AREAS else "pool"  # the app reads an unknown area as Erik's


def tick_id(source, dk, aop_id, aop_area, area):
    """A tick on your own AOP keeps the id it always had (old tombstones still hold); a tick on
    another person's AOP gets your area on the end, so it never clashes with theirs."""
    base = f"{source}:{dk}:{aop_id}"
    return base if aop_area == area else f"{base}:{area}"


def has_tick_by(bucket, aop_id, area, aops):
    return any(isinstance(e, dict) and e.get("aopId") == aop_id and entry_owner(e, aops) == area for e in bucket or [])


def strip_block(text):
    pattern = re.compile(re.escape(BLOCK_START) + r".*?" + re.escape(BLOCK_END), re.S)
    return pattern.sub("", text or "").strip()


def replace_block(text, block):
    text = text or ""
    pattern = re.compile(re.escape(BLOCK_START) + r".*?" + re.escape(BLOCK_END), re.S)
    if pattern.search(text):
        return pattern.sub(lambda _: block, text, count=1).strip()
    return f"{text.rstrip()}\n\n{block}".strip()


def is_real_work(a):
    """Committed, or edited in Claude. Only opening an AOP goes in the note, not the calendar."""
    return bool(a["commits"]) or a["claude"] == "edited"


def is_weekend(day_iso):
    return dt.date.fromisoformat(day_iso).weekday() >= 5


def commit_state(where):
    """done / branch / local, from a commit's location."""
    if where.startswith(LANDED):
        return "done"
    return "branch" if where.startswith("branch ") else "local"


def short_branch(where, cfg):
    """"branch change/adonis-error-fixes-28-oct +1 (not merged)" -> "error-fixes-28-oct +1"."""
    if not where.startswith("branch "):
        return "not pushed"
    name = where[len("branch "):].replace(" (not merged)", "")
    name = re.sub(r"^change/", "", name)
    me = (cfg.get("name") or "").strip().lower()
    if me and name.lower().startswith(me + "-"):
        name = name[len(me) + 1:]
    return name


def _more(names, limit):
    rest = len(names) - limit
    return ", ".join(names[:limit]) + (f" +{rest} more" if rest > 0 else "")


def render_block(activity, matches, area, cfg, note=None):
    """The day log, grouped by where the work is: done, on a branch, or only edited.
    `note` is the person's own text from the end-of-day review, shown last."""
    sections = {"done": [], "branch": [], "edited": []}
    looked = []
    for a in activity["aops"]:
        if not is_real_work(a):
            looked.append(a["name"])
            continue
        owner = matches.get(a["name"])
        tag = ("" if owner == area else " (not in the calendar)" if owner is None
               else " (in the calendar more than once)" if owner == SEVERAL else f" ({AREAS.get(owner, owner)}'s AOP)")
        by_state = {}
        for c in a["commits"]:
            by_state.setdefault(commit_state(c["where"]), []).append(c)
        if not a["commits"]:
            sections["edited"].append(f"• {a['name']}{tag}")
        for state in ("done", "branch", "local"):
            commits = by_state.get(state)
            if not commits:
                continue
            subjects = list(dict.fromkeys(clean_subject(c["subject"], 90) for c in commits))
            subjects = [s for s in subjects if s] or ["(no description)"]
            where = ""
            if state != "done":
                where = "  ⟶ " + ", ".join(sorted({short_branch(c["where"], cfg) for c in commits}))
            key = "done" if state == "done" else "branch"
            sections[key].append(f"• {a['name']}{tag}: {subjects[0]}{where}")
            for s in subjects[1:MAX_SUBJECTS_PER_AOP]:
                sections[key].append(f"   – {s}")
            if len(subjects) > MAX_SUBJECTS_PER_AOP:
                sections[key].append(f"   – +{len(subjects) - MAX_SUBJECTS_PER_AOP} more")

    lines = [BLOCK_START]

    def section(title, items):
        if items:
            if len(lines) > 1:
                lines.append("")
            lines.append(title)
            lines.extend(items)

    section(f"✅ Done, on main or live ({sum(1 for l in sections['done'] if l.startswith('•'))})", sections["done"])
    section(f"🔧 On a branch, not live yet ({sum(1 for l in sections['branch'] if l.startswith('•'))})", sections["branch"])
    section(f"✏️ Edited in Claude, not committed yet ({len(sections['edited'])})", sections["edited"])

    bulk = []
    for b in activity.get("bulk", []):
        states = {commit_state(w) for w in b["where"]}
        status = "done" if states == {"done"} else "not live yet" if "done" not in states else "partly live"
        bulk.append(f"• {clean_subject(b['subject'], 90)} ({b['aops']} AOPs, {status})")
    section("🔁 Changes across many AOPs", bulk)

    extra = []
    by_kind = {}
    for o in activity["other"]:
        by_kind.setdefault(o["kind"], []).append(o["name"])
    if by_kind:
        parts = []
        for kind, names in by_kind.items():
            label = f"{len(names)} {kind}{'s' if len(names) > 1 else ''}"
            parts.append(label if kind == "test" else f"{label} ({_more(names, 3)})")
        extra.append("Also changed: " + "; ".join(parts))
    if looked:
        extra.append("Only opened in Claude: " + _more(looked, 3))
    titles = activity["sessions"]["power_titles"]
    other_n = activity["sessions"]["other_count"]
    if cfg.get("include_session_titles", True) and titles:
        extra.append(f"Claude sessions ({len(titles)}): " + _more([f'"{t}"' for t in titles], 4))
    if other_n:
        extra.append(f"+{other_n} Claude session{'s' if other_n > 1 else ''} outside the power repo")
    section("Other", extra)
    section("📝 My note", [l.rstrip() for l in (note or "").replace("\r\n", "\n").split("\n") if l.strip()])

    lines.append(BLOCK_END)
    return "\n".join(lines)


def apply_day(state, activity, area, cfg, stamp=None, review=None):
    """Ticks and the day-log block for one day. Returns (new_state, report); never mutates `state`.
    `review` (see resolve_review) skips or adds ticks and adds the person's note."""
    stamp = stamp or now_iso()
    review = review or empty_review()
    st = copy.deepcopy(state)
    dk = activity["date"]
    aops = calendar_aops(st)
    tombstones = st.get("entryTombstones") or {}
    aliases = cfg.get("aliases") or {}
    report = {"date": dk, "weekend": is_weekend(dk), "ticked": [], "ticked_other_area": [], "already_ticked": [],
              "deleted_before": [], "other_area": [], "unmatched": [], "ambiguous": [], "skipped_in_review": [],
              "skip_already_saved": [], "added_in_review": [], "note": None}
    tick_others = cfg.get("tick_other_areas", True) and app_version(st) >= BY_APP_VERSION
    skip = review["skip_ticks"].get(dk, set())

    def tick(aop_id, done_key):
        label = aops[aop_id].get("name")
        own = aops[aop_id].get("area") == area
        if not own and not tick_others:
            report["other_area"].append(label)
            return
        if report["weekend"]:  # the calendar itself refuses Actual ticks on weekends
            return
        entries = st.setdefault("entries", {})
        bucket = entries.get(dk) or {"planned": [], "actual": []}
        actual = list(bucket.get("actual") or [])
        entry_id = tick_id(ENTRY_SOURCE, dk, aop_id, aops[aop_id].get("area"), area)
        if entry_id in tombstones:
            report["deleted_before"].append(label)
        elif has_tick_by(actual, aop_id, area, aops):
            report["already_ticked"].append(label)
        else:
            actual.append({"id": entry_id, "aopId": aop_id, "ts": stamp, "source": ENTRY_SOURCE, "by": area})
            entries[dk] = {**bucket, "planned": list(bucket.get("planned") or []), "actual": actual}
            report[done_key or ("ticked" if own else "ticked_other_area")].append(label)

    matches = {}
    for a in activity["aops"]:
        aop_id = match_aop(a["name"], aops, aliases, area)
        several = not aop_id and has_any_match(a["name"], aops, aliases)
        matches[a["name"]] = aops[aop_id].get("area") if aop_id else SEVERAL if several else None
        if not is_real_work(a):
            continue
        if not aop_id:
            report["ambiguous" if several else "unmatched"].append(a["name"])
            continue
        if aop_id in skip:
            saved = has_tick_by(((st.get("entries") or {}).get(dk) or {}).get("actual"), aop_id, area, aops)
            report["skip_already_saved" if saved else "skipped_in_review"].append(aops[aop_id].get("name"))
            continue
        tick(aop_id, None)
    for aop_id in review["add_ticks"].get(dk, []):
        if aop_id in aops:
            tick(aop_id, "added_in_review")

    note = review["notes"].get(dk)
    if activity["aops"] or activity["other"] or activity.get("bulk") or activity["sessions"]["power_titles"] or note:
        notes = st.setdefault("dayNotes", {})
        day_note = notes.get(dk)
        if isinstance(day_note, str):  # legacy plain-text note, same shape migrateIfNeeded produces
            day_note = {"text": day_note, "planned_text": "", "actual_text": day_note, "created_at": "", "updated_at": ""}
        day_note = dict(day_note or {"text": "", "planned_text": "", "actual_text": "", "created_at": stamp, "updated_at": ""})
        area_notes = dict(day_note.get("area_notes") or {})
        current = area_notes.get(area) or {}
        block = render_block(activity, matches, area, cfg, note)
        new_text = replace_block(current.get("text", ""), block)
        if new_text != (current.get("text") or "").strip():
            area_notes[area] = {"text": new_text, "created_at": current.get("created_at") or stamp, "updated_at": stamp}
            day_note["area_notes"] = area_notes
            notes[dk] = day_note
            report["note"] = block
    return st, report


def insert_achieved(notation, new_lines):
    """Add lines at the end of the notation's "Achieved during Sprint:" section, creating it if missing."""
    lines = (notation or "").replace("\r\n", "\n").split("\n")
    header = next((i for i, l in enumerate(lines) if re.match(r"^achieved\s*during\s*sprint\b", l.strip(), re.I)), None)
    if header is None:
        body = (notation or "").rstrip()
        return f"{body}\n\n{ACHIEVED_HEADER}\n" + "\n".join(new_lines) if body else f"{ACHIEVED_HEADER}\n" + "\n".join(new_lines)
    end = next((i for i in range(header + 1, len(lines)) if NOTATION_SECTION_RE.match(lines[i].strip())), len(lines))
    while end > header + 1 and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[:end] + new_lines + lines[end:]).rstrip()


def apply_achievements(state, items, area, cfg, ledger, stamp=None, review=None):
    """Sprint-outcome lines into each own-area AOP's monthly notation. Returns (new_state, added, keys).
    `review` drops or rewords lines; a dropped line's key still goes to the ledger, so it never comes back."""
    stamp = stamp or now_iso()
    review = review or empty_review()
    st = copy.deepcopy(state)
    aops = calendar_aops(st)
    aliases = cfg.get("aliases") or {}
    pending = {}
    for item in items:
        if item["key"] in ledger:
            continue
        aop_id = match_aop(item["aop"], aops, aliases, area)
        if not aop_id or aops[aop_id].get("area") != area:
            continue
        text = outcome_text(item["line"])
        if text in review["skip_outcomes"]:
            item = {**item, "line": None, "skipped": item["line"]}
        elif text in review["reword_outcomes"]:
            item = {**item, "line": f"✅ {review['reword_outcomes'][text]} ({short_date(item['day'])})", "was": item["line"]}
        pending.setdefault((aop_id, item["month"]), []).append(item)
    added, keys = [], []
    for (aop_id, month), group in sorted(pending.items()):
        monthly = st.setdefault("aopMonthly", {}).setdefault(aop_id, {})
        record = monthly.get(month)
        notation = (record or {}).get("notation") or ""
        existing = {l.strip() for l in notation.split("\n")}
        # "GDPR Breach" and "GDPR Breach V4" files can both map to one calendar AOP
        lines = list(dict.fromkeys(i["line"] for i in group if i["line"] and i["line"] not in existing))
        keys += [i["key"] for i in group]
        skipped = list(dict.fromkeys(i["skipped"] for i in group if i.get("skipped")))
        reworded = {i["line"]: i["was"] for i in group if i.get("was") and i["line"] in lines}
        if not lines:
            if skipped:
                added.append({"aop": aops[aop_id].get("name"), "month": month, "lines": [], "skipped": skipped})
            continue
        if record is None:
            record = new_monthly_record(monthly, month, stamp)
        record = {**record, "notation": insert_achieved(notation, lines), "notation_updated_at": stamp}
        monthly[month] = record
        added.append({"aop": aops[aop_id].get("name"), "month": month, "lines": lines,
                      "skipped": skipped, "reworded": reworded})
    return st, added, keys


def new_monthly_record(monthly, month, stamp):
    """Same starting values ensureMonthly gives a new month in the app."""
    prev = monthly.get(shift_month(month, -1)) or {}
    return {"goal": "", "comments": "", "required": False,
            "deflection_start": prev.get("deflection_end", ""), "deflection_end": "",
            "conversation_start": prev.get("conversation_end", ""), "conversation_end": "",
            "notation": "", "notation_updated_at": "", "created_at": stamp, "updated_at": stamp,
            "goal_created_at": "", "goal_updated_at": "", "comments_created_at": "",
            "comments_updated_at": "", "required_created_at": "", "required_updated_at": ""}


def shift_month(month, delta):
    y, m = map(int, month.split("-"))
    m += delta
    y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


def _rgb(color):
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", str(color or "").strip())
    return tuple(int(m.group(1)[i:i + 2], 16) for i in (0, 2, 4)) if m else None


def _color_distance(a, b):
    """'Redmean' RGB distance: close enough to how far apart two colors look."""
    r = (a[0] + b[0]) / 2
    dr, dg, db = a[0] - b[0], a[1] - b[1], a[2] - b[2]
    return ((2 + r / 256) * dr * dr + 4 * dg * dg + (2 + (255 - r) / 256) * db * db) ** 0.5


def pick_new_color(used):
    """A color no item in the calendar has, as far as possible from all of them."""
    used_rgb = [c for c in (_rgb(u) for u in used) if c]
    best = None
    for i in range(72):
        hue = (i * 137.508) % 360 / 360  # golden angle: each step lands far from the ones before
        for light in (0.62, 0.48):
            rgb = tuple(round(v * 255) for v in colorsys.hls_to_rgb(hue, light, 0.7))
            if rgb in used_rgb:
                continue
            gap = min((_color_distance(rgb, u) for u in used_rgb), default=1e9)
            if best is None or gap > best[0]:
                best = (gap, rgb)
    return "#%02x%02x%02x" % best[1]


NEW_AOP_KEYS = {"id", "name", "color", "area", "kind", "created_at", "updated_at", "source"}
GENERIC_WORDS = {"assist", "customer", "customers", "handle", "handling", "inquiry", "inquiries", "order",
                 "orders", "power", "helping", "existing", "product", "products", "request", "questions",
                 "regarding", "issues", "their", "with", "about"}


def similar_aops(name, aops, limit=2):
    """Calendar AOPs sharing the most distinctive words with `name`: maybe the same AOP under another name."""
    words = {w for w in base_name(name).split() if len(w) >= 5 and w not in GENERIC_WORDS}
    scored = {}
    for a in aops.values():
        shared = len(words & set(base_name(a.get("name")).split()))
        label = a.get("name", "").replace("​", "").strip()
        if shared and label:
            scored[label] = max(shared, scored.get(label, 0))
    return [n for n, _ in sorted(scored.items(), key=lambda x: (-x[1], x[0].lower()))[:limit]]


def apply_new_aops(state, collected, area, cfg, created, stamp=None, review=None):
    """Add each AOP the person worked on that no area has yet to their own area, in a new color.
    Only AOPs on main or live count. Returns (new_state, report); never mutates `state`."""
    report = {"created": [], "removed_before": [], "skipped_in_review": []}
    skipped = (review or empty_review())["skip_new_aops"]
    if cfg.get("add_new_aops", True) is False:
        return state, report
    stamp = stamp or now_iso()
    st = copy.deepcopy(state)
    aliases = cfg.get("aliases") or {}
    landed = set(collected["landed_aops"]) if "landed_aops" in collected else None  # None: an --activity file from before
    worked = dict.fromkeys(aliases.get(a["name"], a["name"]) for day in collected["days"]
                           for a in day["aops"] if is_real_work(a) and (landed is None or a["name"] in landed))
    for name in worked:
        items = st.setdefault("aops", {})
        # any match at all, even an ambiguous one or a custom status, means it's already in the calendar
        if any(isinstance(a, dict) and (norm_name(a.get("name")) == norm_name(name) or base_name(a.get("name")) == base_name(name)
                                        or word_key(a.get("name")) == word_key(name)) for a in items.values()):
            continue
        aops = calendar_aops(st)
        if base_name(name) in skipped:
            report["skipped_in_review"].append(name)
            continue
        if base_name(name) in created:  # added once, then deleted or renamed by someone
            if not any(base_name(n) == base_name(name) for n in report["removed_before"]):
                report["removed_before"].append(name)
            continue
        aop_id = str(uuid.uuid4())
        color = pick_new_color(a.get("color") for a in items.values() if isinstance(a, dict))
        items[aop_id] = {"id": aop_id, "name": name, "color": color, "area": area, "kind": "aop",
                         "created_at": stamp, "updated_at": stamp, "source": ENTRY_SOURCE}
        st["aopOrder"] = list(st.get("aopOrder") or []) + [aop_id]
        report["created"].append({"name": name, "id": aop_id, "color": color, "similar": similar_aops(name, aops)})
    return st, report


def apply_all(state, collected, area, cfg, ledger, created=None, stamp=None, changes=None, saved_review=None):
    """Returns (new_state, reports, added, keys, new_aops, review). The review's names are matched after
    this run's new AOPs are added, so a tick on an AOP that is new today can be skipped too."""
    stamp = stamp or now_iso()
    review = empty_review()
    review["skip_new_aops"] = new_aop_skips(changes, saved_review)
    st, new_aops = apply_new_aops(state, collected, area, cfg, created or {}, stamp, review)
    review = resolve_review(st, changes, saved_review or {}, area, cfg, [d["date"] for d in collected["days"]])
    reports = []
    for activity in collected["days"]:
        st, report = apply_day(st, activity, area, cfg, stamp, review)
        reports.append(report)
    st, added, keys = apply_achievements(st, collected["achievements"], area, cfg, ledger, stamp, review)
    return st, reports, added, keys, new_aops, review


# ---------------------------------------------------------- end-of-day review

def empty_review():
    return {"skip_ticks": {}, "add_ticks": {}, "skip_outcomes": set(), "reword_outcomes": {},
            "skip_new_aops": set(), "notes": {}}


def outcome_text(line):
    """An outcome line as the person refers to it: no ✅, no "(4 Nov)", any case or spacing."""
    text = re.sub(r"^\s*✅\s*", "", str(line or ""))
    return norm_name(re.sub(r"\s*\(\d{1,2} [A-Z][a-z]{2}\)\s*$", "", text))


def new_aop_skips(changes, saved):
    return set((saved or {}).get("skip_new_aops") or []) | {base_name(n) for n in (changes or {}).get("skip_new_aops") or []}


def read_changes(path):
    """The changes file the person agreed to in the review. Names, as the dry run prints them:
    {"skip_ticks": [{"date", "aop"}], "add_ticks": [{"date", "aop"}], "skip_outcomes": [line],
     "reword_outcomes": {line: new text}, "skip_new_aops": [name], "notes": {date: text}}"""
    raw = read_json(path)
    if not isinstance(raw, dict):
        raise RuntimeError(f"{path} is not a JSON object. Nothing written.")
    unknown = set(raw) - {"skip_ticks", "add_ticks", "skip_outcomes", "reword_outcomes", "skip_new_aops", "notes"}
    if unknown:
        raise RuntimeError(f"{path}: unknown key(s) {', '.join(sorted(unknown))}. Nothing written.")
    return raw


def resolve_review(state, changes, saved, area, cfg, dks):
    """Changes from the review (names) plus earlier reviews (review.json, ids) -> what apply_* use.
    A name that matches no calendar AOP, or a date outside this run, stops the run before anything is written."""
    aops = calendar_aops(state)
    aliases = cfg.get("aliases") or {}
    review = empty_review()
    for dk, ids in (saved.get("skip_ticks") or {}).items():
        review["skip_ticks"].setdefault(dk, set()).update(ids)
    review["skip_new_aops"] = new_aop_skips(changes, saved)
    review["notes"].update(saved.get("notes") or {})
    changes = changes or {}

    def aop_for(item, what):
        dk, name = (item or {}).get("date"), (item or {}).get("aop")
        if dk not in dks:
            raise RuntimeError(f"{what}: {dk!r} is not one of the days in this run ({', '.join(dks)}). Nothing written.")
        aop_id = match_aop(name, aops, aliases, area)
        if not aop_id:
            if what == "skip_ticks" and base_name(aliases.get(name, name)) in review["skip_new_aops"]:
                return dk, None  # its new AOP is skipped too, so there is nothing to tick
            several = has_any_match(name, aops, aliases)
            raise RuntimeError(f"{what}: {name!r} is {'in the calendar more than once' if several else 'not in the calendar'}"
                               f". Use the name the calendar shows. Nothing written.")
        return dk, aop_id

    for item in changes.get("skip_ticks") or []:
        dk, aop_id = aop_for(item, "skip_ticks")
        if aop_id:
            review["skip_ticks"].setdefault(dk, set()).add(aop_id)
    for item in changes.get("add_ticks") or []:
        dk, aop_id = aop_for(item, "add_ticks")
        if aop_id not in review["add_ticks"].setdefault(dk, []):
            review["add_ticks"][dk].append(aop_id)
    review["skip_outcomes"].update(outcome_text(l) for l in changes.get("skip_outcomes") or [])
    for old, new in (changes.get("reword_outcomes") or {}).items():
        new = " ".join(str(new).split())
        new = re.sub(r"\s*\(\d{1,2} [A-Z][a-z]{2}\)$", "", re.sub(r"^✅\s*", "", new))
        if not new:
            raise RuntimeError(f"reword_outcomes: the new text for {old!r} is empty. Nothing written.")
        review["reword_outcomes"][outcome_text(old)] = new
    for dk, text in (changes.get("notes") or {}).items():
        if dk not in dks:
            raise RuntimeError(f"notes: {dk!r} is not one of the days in this run ({', '.join(dks)}). Nothing written.")
        if BLOCK_START in str(text) or BLOCK_END in str(text):
            raise RuntimeError("notes: a note can't hold the day log's marker lines. Nothing written.")
        if str(text).strip():
            review["notes"][dk] = str(text).strip()
        else:
            review["notes"].pop(dk, None)  # an empty note removes an earlier one
    return review


def save_review(review):
    """Keep what a later run of the same days must still respect. Added ticks and outcome changes
    need no keeping: once saved they are in the calendar and the ledger."""
    write_json(REVIEW_FILE, {
        "skip_ticks": {dk: sorted(ids) for dk, ids in sorted(review["skip_ticks"].items()) if ids},
        "skip_new_aops": sorted(review["skip_new_aops"]),
        "notes": dict(sorted(review["notes"].items())),
    })


# ------------------------------------------------------------------- guard

def _check_notation(old_text, new_text):
    """Every line of text the notation had must still be there, in order; blank lines don't count."""
    old_lines = [l.rstrip() for l in (old_text or "").replace("\r\n", "\n").split("\n") if l.strip()]
    new_lines = [l.rstrip() for l in (new_text or "").replace("\r\n", "\n").split("\n") if l.strip()]
    it = iter(new_lines)
    if not all(any(o == n for n in it) for o in old_lines):  # old must be an in-order subsequence of new
        raise RuntimeError("Guard: an AOP's sprint notation would lose or change text. Nothing written.")
    extra = list(new_lines)
    for o in old_lines:
        extra.remove(o)
    for line in extra:
        if line.strip() and line.strip() != ACHIEVED_HEADER and not ACHIEVED_LINE_RE.match(line.strip()):
            raise RuntimeError(f"Guard: unexpected notation line {line[:60]!r}. Nothing written.")


NEW_TICK_KEYS = {"id", "aopId", "ts", "source", "by"}


def _check_new_tick(e, dk, area, aops, old):
    """A tick this tool adds: its fields, its owner and its id, and on another person's AOP only
    once the calendar knows `by` (an older page would count it for the AOP's area)."""
    aop = aops.get(e.get("aopId")) if isinstance(e, dict) else None
    if not (aop and set(e) == NEW_TICK_KEYS and e["source"] == ENTRY_SOURCE and e["by"] == area
            and e["id"] == tick_id(ENTRY_SOURCE, dk, e["aopId"], aop.get("area"), area)):
        raise RuntimeError("Guard: an unexpected tick would be added. Nothing written.")
    if aop.get("area") != area and app_version(old) < BY_APP_VERSION:
        raise RuntimeError("Guard: a tick on another person's AOP needs the calendar on version "
                           f"{BY_APP_VERSION}. Nothing written.")


def check_only_additions(old, new, area, dks):
    """Raise unless `new` equals `old` apart from this tool's new AOPs, ticks, note blocks and sprint-outcome lines."""
    check_app_version(old)
    expected = copy.deepcopy(new)
    new_aops = calendar_aops(new)
    old_aops = old.get("aops") or {}
    new_ids = [i for i in (expected.get("aops") or {}) if i not in old_aops]
    for i in new_ids:
        a = expected["aops"].pop(i)
        if not (isinstance(a, dict) and set(a) == NEW_AOP_KEYS and a["id"] == i and a["source"] == ENTRY_SOURCE
                and a["area"] == area and a["kind"] == "aop" and _rgb(a["color"])):
            raise RuntimeError("Guard: an unexpected AOP would be added. Nothing written.")
    if new_ids:
        if list(expected.get("aopOrder") or []) != list(old.get("aopOrder") or []) + new_ids:
            raise RuntimeError("Guard: the AOP order would change beyond the new AOPs. Nothing written.")
        if "aopOrder" in old:
            expected["aopOrder"] = copy.deepcopy(old["aopOrder"])
        else:
            expected.pop("aopOrder", None)
    old_entries = old.get("entries") or {}
    old_notes = old.get("dayNotes") or {}
    for dk in dks:
        ours = f"{ENTRY_SOURCE}:{dk}:"
        new_entries = expected.get("entries")
        if isinstance(new_entries, dict) and dk in new_entries:
            old_actual = (old_entries.get(dk) or {}).get("actual") or []
            bucket = new_entries[dk]
            kept = []
            for e in bucket.get("actual") or []:
                if e in old_actual or not str((e or {}).get("id", "")).startswith(ours):
                    kept.append(e)
                else:
                    _check_new_tick(e, dk, area, new_aops, old)
            bucket["actual"] = kept
            if dk not in old_entries:
                if bucket["actual"] or bucket.get("planned"):
                    raise RuntimeError("Guard: unexpected calendar entries would be added. Nothing written.")
                del new_entries[dk]
        new_notes = expected.get("dayNotes")
        if isinstance(new_notes, dict) and dk in new_notes:
            note = new_notes[dk]
            old_note = old_notes.get(dk)
            old_area = ((old_note.get("area_notes") or {}).get(area) or {}) if isinstance(old_note, dict) else {}
            new_area = (note.get("area_notes") or {}).get(area)
            if new_area and strip_block(new_area.get("text")) != strip_block(old_area.get("text")):
                raise RuntimeError("Guard: the person's own note text would change. Nothing written.")
            other_areas = set(note.get("area_notes") or {}) - {area}
            if old_note is None:
                if any(note.get(k) for k in ("text", "planned_text", "actual_text")) or other_areas:
                    raise RuntimeError("Guard: a new day note would hold more than the day log. Nothing written.")
                del new_notes[dk]
            elif isinstance(old_note, str):
                if note.get("text") != old_note or other_areas:
                    raise RuntimeError("Guard: a legacy day note would change. Nothing written.")
                new_notes[dk] = old_note
            else:
                area_notes = dict(note.get("area_notes") or {})
                if area in (old_note.get("area_notes") or {}):
                    area_notes[area] = old_note["area_notes"][area]
                else:
                    area_notes.pop(area, None)
                note = {**note, "area_notes": area_notes}
                if "area_notes" not in old_note and not area_notes:
                    note.pop("area_notes")
                new_notes[dk] = note
    old_monthly = old.get("aopMonthly") or {}
    new_monthly = expected.get("aopMonthly") or {}
    own = {i for i, a in old_aops.items() if isinstance(a, dict) and a.get("area") == area} | set(new_ids)
    for aop_id, months in list(new_monthly.items()):
        for month, record in list(months.items()):
            before = (old_monthly.get(aop_id) or {}).get(month)
            if record == before:
                continue
            if aop_id not in own:
                raise RuntimeError("Guard: another area's AOP outcome would change. Nothing written.")
            _check_notation((before or {}).get("notation"), record.get("notation"))
            rest = {k: v for k, v in record.items() if k not in ("notation", "notation_updated_at")}
            if before is None:
                if rest.get("goal") or rest.get("comments") or rest.get("required"):
                    raise RuntimeError("Guard: a new monthly AOP record would hold more than outcomes. Nothing written.")
                del months[month]
            else:
                if rest != {k: v for k, v in before.items() if k not in ("notation", "notation_updated_at")}:
                    raise RuntimeError("Guard: a monthly AOP field other than its outcomes would change. Nothing written.")
                months[month] = before
        if aop_id not in old_monthly and not months:
            del new_monthly[aop_id]
    for key in ("entries", "dayNotes", "aopMonthly", "aops"):
        if key not in old and expected.get(key) == {}:
            expected.pop(key)
    if expected != old:
        raise RuntimeError("Guard: the new state differs from the stored one beyond this tool's additions. Nothing written.")


# ------------------------------------------------------------ monthly plan

def parse_notation(text):
    """Goals and outcomes of an AOP's monthly notation, read like the app's parseAopNotationModel,
    except that a bare "Achieved:" heading also starts the outcomes (people write it that way)."""
    goals, achieved, section = [], [], "achieved"
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^goals?\s*:?\s*(.*)$", line, re.I)
        if m:
            section = "goal"
            rest = re.sub(r"^[-*•]\s*", "", m.group(1)).strip()
            if rest:
                goals.append(rest)
            continue
        m = re.match(r"^achieved(?:\s*during\s*sprint)?\s*:\s*(.*)$", line, re.I)
        if m:
            section = "achieved"
            line = m.group(1).strip()
            if not line:
                continue
        elif re.match(r"^existing\s*blockers?", line, re.I):
            section = "ignore"
            continue
        cleaned = re.sub(r"^[-*•]\s*", "", re.sub(r"^[✅✔☑]\s*", "", line)).strip()
        if cleaned and section in ("goal", "achieved"):
            (goals if section == "goal" else achieved).append(cleaned)
    return {"goals": goals, "achieved": achieved}


def insert_goals(notation, new_goals):
    """Add "- goal" lines at the end of the notation's Goal section, or start the notation with one."""
    lines = (notation or "").replace("\r\n", "\n").split("\n")
    bullets = [f"- {g}" for g in new_goals]
    header = next((i for i, l in enumerate(lines) if GOAL_SECTION_RE.match(l.strip())), None)
    if header is None:
        body = (notation or "").strip()
        block = "\n".join([GOAL_HEADER] + bullets)
        return f"{block}\n\n{body}" if body else block
    # People also write a bare "Achieved:" heading; new goals go above it too.
    end = next((i for i in range(header + 1, len(lines))
                if re.match(r"^(achieved|existing\s*blockers?)\b", lines[i].strip(), re.I)), len(lines))
    while end > header + 1 and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[:end] + bullets + lines[end:]).rstrip()


def is_absence(name):
    return norm_name(name) in ABSENCE_NAMES


def area_items(state, area):
    """Everything an area can plan: its AOPs and its own items such as "Cleanup day" or "Days off"."""
    return {i: a for i, a in (state.get("aops") or {}).items() if isinstance(a, dict) and a.get("area") == area}


def month_days(month):
    first = dt.date.fromisoformat(f"{month}-01")
    last = dt.date.fromisoformat(f"{shift_month(month, 1)}-01") - dt.timedelta(days=1)
    return day_range(first, last)


def _names(bucket, aops):
    return sorted({aops[e["aopId"]]["name"] for e in bucket or [] if isinstance(e, dict) and e.get("aopId") in aops})


def plan_facts(state, cfg, area, month, commits):
    """What a month's plan can build on, for one area. Reads only."""
    aops = area_items(state, area)
    all_aops = calendar_aops(state)
    aliases = cfg.get("aliases") or {}
    prev = shift_month(month, -1)
    entries = state.get("entries") or {}

    def mine(bucket):  # this person's ticks only, not another person's tick on one of their AOPs
        return [e for e in bucket or [] if isinstance(e, dict) and entry_owner(e, all_aops) == area]

    days, days_off = [], []
    for day in month_days(month):
        dk = day.isoformat()
        if day.weekday() >= 5:
            continue
        bucket = entries.get(dk) or {}
        planned, actual = _names(mine(bucket.get("planned")), aops), _names(mine(bucket.get("actual")), aops)
        if any(is_absence(n) for n in planned + actual):
            days_off.append(dk)
        days.append({"date": dk, "planned": planned, "actual": actual})

    per_aop = {}
    for aop_id, a in sorted(aops.items(), key=lambda x: x[1]["name"].lower()):
        if is_absence(a["name"]):
            continue
        monthly = (state.get("aopMonthly") or {}).get(aop_id) or {}
        info = {"kind": a.get("kind", "aop"), "required": bool((monthly.get(month) or {}).get("required"))}
        for key, m in (("last_month", prev), ("this_month", month)):
            rec = monthly.get(m) or {}
            notes = parse_notation(rec.get("notation"))
            info[key] = {"goal_field": rec.get("goal", ""), "goals": notes["goals"], "achieved": notes["achieved"],
                         "planned_days": 0, "actual_days": 0}
            for dk, bucket in entries.items():
                if dk.startswith(m):
                    for kind in ("planned", "actual"):
                        if any(e.get("aopId") == aop_id for e in mine(bucket.get(kind))):
                            info[key][f"{kind}_days"] += 1
        per_aop[a["name"]] = {**info, "commit_days": [], "landed": [], "not_live": []}

    elsewhere = {}
    for c in commits:
        if is_bulk(c):
            continue
        for kind, name in c["items"]:
            if kind != "aop":
                continue
            aop_id = match_aop(name, aops, aliases)
            if not aop_id:
                elsewhere.setdefault(name, set()).add(c["day"])
                continue
            info = per_aop.get(aops[aop_id]["name"])
            if info is None:
                continue
            if c["day"] not in info["commit_days"]:
                info["commit_days"].append(c["day"])
            line = f"{c['day']}: {clean_subject(c['subject'], 100)}"
            if commit_state(c["where"]) == "done":
                info["landed"].append(line)
            else:
                info["not_live"].append(f"{line}  ⟶ {short_branch(c['where'], cfg)}")
    for info in per_aop.values():
        info["commit_days"] = len(info["commit_days"])
        info["landed"] = list(dict.fromkeys(info["landed"]))[-8:]
        info["not_live"] = list(dict.fromkeys(info["not_live"]))[-8:]
    today = dt.date.today().isoformat()
    return {
        "area": area, "person": AREAS.get(area, area), "month": month, "today": today,
        "workdays": len(days), "workdays_from_today": sum(1 for d in days if d["date"] >= today),
        "days_off": days_off,
        "already_planned": {d["date"]: d["planned"] for d in days if d["planned"]},
        "aops": per_aop,
        "other_aops_committed": {n: len(d) for n, d in sorted(elsewhere.items())},
    }


def apply_plan(state, plan, area, cfg, today=None, stamp=None):
    """Goals and planned ticks from an agreed plan, own area only. Returns (new_state, report)."""
    stamp = stamp or now_iso()
    today = today or dt.date.today()
    month = str(plan.get("month") or "")
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise RuntimeError("The plan file needs \"month\": \"YYYY-MM\".")
    st = copy.deepcopy(state)
    aops = area_items(st, area)
    aliases = cfg.get("aliases") or {}
    tombstones = st.get("entryTombstones") or {}
    report = {"month": month, "goals": [], "goal_field": [], "ticked": [], "skipped": []}

    for name, spec in (plan.get("aops") or {}).items():
        aop_id = match_aop(name, aops, aliases)
        if not aop_id:
            report["skipped"].append(f"{name}: not an AOP in {AREAS.get(area, area)}'s area")
            continue
        label = aops[aop_id]["name"]
        monthly = (st.get("aopMonthly") or {}).get(aop_id) or {}
        record = dict(monthly.get(month) or new_monthly_record(monthly, month, stamp))
        before = dict(record)
        goal = " ".join(str(spec.get("goal") or "").split())
        if goal and not (record.get("goal") or "").strip():
            record.update(goal=goal, goal_updated_at=stamp, goal_created_at=record.get("goal_created_at") or stamp)
            report["goal_field"].append(f"{label}: {goal}")
        elif goal and record.get("goal", "").strip() != goal:
            report["skipped"].append(f"{label}: goal field already says {record['goal'][:60]!r}, left as it is")
        have = {norm_name(g) for g in parse_notation(record.get("notation"))["goals"]}
        new = []
        for g in spec.get("goals") or []:
            g = re.sub(r"^[-*•]\s*", "", " ".join(str(g).split()))
            if g and norm_name(g) not in have:
                have.add(norm_name(g))
                new.append(g)
        if new:
            record.update(notation=insert_goals(record.get("notation"), new), notation_updated_at=stamp)
            report["goals"].append({"aop": label, "lines": new})
        if record != before:
            st.setdefault("aopMonthly", {}).setdefault(aop_id, {})[month] = record

    entries = st.setdefault("entries", {})
    for dk, names in sorted((plan.get("days") or {}).items()):
        try:
            day = dt.date.fromisoformat(dk)
        except ValueError:
            report["skipped"].append(f"{dk}: not a date")
            continue
        if not dk.startswith(month) or day.weekday() >= 5 or day < today:
            why = "outside the month" if not dk.startswith(month) else "weekend" if day.weekday() >= 5 else "in the past"
            report["skipped"].append(f"{dk}: {why}")
            continue
        bucket = entries.get(dk) or {"planned": [], "actual": []}
        planned = list(bucket.get("planned") or [])
        if any(is_absence(n) for n in _names(planned, aops)):
            report["skipped"].append(f"{dk}: planned as a day off")
            continue
        added = False
        for name in names or []:
            aop_id = match_aop(name, aops, aliases)
            if not aop_id:
                report["skipped"].append(f"{dk} {name}: not an AOP in {AREAS.get(area, area)}'s area")
                continue
            label = aops[aop_id]["name"]
            entry_id = f"{PLAN_SOURCE}:{dk}:{aop_id}"
            if entry_id in tombstones:
                report["skipped"].append(f"{dk} {label}: someone deleted this planned tick before")
            elif has_tick_by(planned, aop_id, area, calendar_aops(st)):
                report["skipped"].append(f"{dk} {label}: already planned")
            else:
                planned.append({"id": entry_id, "aopId": aop_id, "ts": stamp, "source": PLAN_SOURCE, "by": area})
                report["ticked"].append(f"{dk} {label}")
                added = True
        if added:
            entries[dk] = {**bucket, "planned": planned, "actual": list(bucket.get("actual") or [])}
    if not entries and "entries" not in state:
        del st["entries"]
    return st, report


def check_plan_additions(old, new, area, month):
    """Raise unless `new` equals `old` apart from this plan's goals and planned ticks in `month`."""
    check_app_version(old)
    expected = copy.deepcopy(new)
    old_entries = old.get("entries") or {}
    own = {i for i, a in (old.get("aops") or {}).items() if isinstance(a, dict) and a.get("area") == area}
    for dk, bucket in list((expected.get("entries") or {}).items()):
        before = old_entries.get(dk)
        if bucket == before:
            continue
        if not dk.startswith(month):
            raise RuntimeError("Guard: a day outside the planned month would change. Nothing written.")
        before = before or {"planned": [], "actual": []}
        if (bucket.get("actual") or []) != (before.get("actual") or []):
            raise RuntimeError("Guard: an Actual tick would change. Nothing written.")
        old_planned = before.get("planned") or []
        for e in bucket.get("planned") or []:
            if e not in old_planned and not (str(e.get("id", "")).startswith(PLAN_SOURCE + ":")
                                             and e.get("aopId") in own and e.get("by") == area):
                raise RuntimeError("Guard: an unexpected planned tick would be added. Nothing written.")
        bucket["planned"] = [e for e in bucket.get("planned") or [] if e in old_planned]
        if dk not in old_entries:
            if bucket["planned"] or bucket.get("actual"):
                raise RuntimeError("Guard: unexpected calendar entries would be added. Nothing written.")
            del expected["entries"][dk]
    old_monthly = old.get("aopMonthly") or {}
    for aop_id, months in list((expected.get("aopMonthly") or {}).items()):
        for m, record in list(months.items()):
            before = (old_monthly.get(aop_id) or {}).get(m)
            if record == before:
                continue
            if aop_id not in own or m != month:
                raise RuntimeError("Guard: another AOP's or month's record would change. Nothing written.")
            old_lines = [l.rstrip() for l in ((before or {}).get("notation") or "").replace("\r\n", "\n").split("\n") if l.strip()]
            new_lines = [l.rstrip() for l in (record.get("notation") or "").replace("\r\n", "\n").split("\n") if l.strip()]
            it = iter(new_lines)
            if not all(any(o == n for n in it) for o in old_lines):
                raise RuntimeError("Guard: an AOP's notation would lose or change text. Nothing written.")
            extra = list(new_lines)
            for o in old_lines:
                extra.remove(o)
            if any(l.strip() != GOAL_HEADER and not l.startswith("- ") for l in extra):
                raise RuntimeError("Guard: an unexpected notation line would be added. Nothing written.")
            if (before or {}).get("goal", "").strip() and record.get("goal") != before.get("goal"):
                raise RuntimeError("Guard: an existing goal would change. Nothing written.")
            plan_fields = ("notation", "notation_updated_at", "goal", "goal_created_at", "goal_updated_at")
            rest = {k: v for k, v in record.items() if k not in plan_fields}
            if before is None:
                if rest.get("comments") or rest.get("required"):
                    raise RuntimeError("Guard: a new monthly record would hold more than a plan. Nothing written.")
                del months[m]
            else:
                if rest != {k: v for k, v in before.items() if k not in plan_fields}:
                    raise RuntimeError("Guard: a monthly field other than goals would change. Nothing written.")
                months[m] = before
        if aop_id not in old_monthly and not months:
            del expected["aopMonthly"][aop_id]
    for key in ("entries", "aopMonthly"):
        if key not in old and expected.get(key) == {}:
            expected.pop(key)
    if expected != old:
        raise RuntimeError("Guard: the new state differs from the stored one beyond the plan. Nothing written.")


# ------------------------------------------------------ copy migration

COPY_NOTE_HEADER = "From {person}'s copy:"


def find_copies(state, suffix, area):
    """{copy id: original id} for each "<name> <suffix>" item in `area` whose original, the same
    name without the suffix, is exactly one item in another area. Also returns the copies left alone."""
    aops = calendar_aops(state)
    suffix_re = re.compile(r"\s*" + re.escape(suffix.strip()) + r"\s*$", re.I)
    copies = [i for i in (state.get("aopOrder") or []) + sorted(aops) if i in aops
              and aops[i].get("area") == area and suffix_re.search(aops[i].get("name") or "")]
    others = {i: a for i, a in aops.items() if a.get("area") != area}
    mapping, left = {}, []
    for cid in dict.fromkeys(copies):
        orig = match_aop(suffix_re.sub("", aops[cid]["name"]), others, {})
        if orig:
            mapping[cid] = orig
        else:
            left.append(aops[cid]["name"])
    return mapping, left


def _copy_note_lines(record):
    """Everything a person wrote in a copy's month: notation, and goal/comments fields as text."""
    lines = [l.rstrip() for l in (record.get("notation") or "").replace("\r\n", "\n").split("\n") if l.strip()]
    for key, label in (("goal", "Goal field"), ("comments", "Comments")):
        text = " ".join(str(record.get(key) or "").split())
        if text and not any(text in l for l in lines):
            lines.append(f"{label}: {text}")
    return lines


def migrate_copies(state, suffix, area, stamp=None):
    """Moves each copy's ticks onto its original as ticks by `area`, its month notes into the
    original's notation, and deletes the copy the way the app does. Returns (new_state, report)."""
    stamp = stamp or now_iso()
    st = copy.deepcopy(state)
    mapping, left = find_copies(st, suffix, area)
    aops = calendar_aops(st)
    tombstones = st.setdefault("entryTombstones", {})
    person = AREAS.get(area, area)
    report = {"copies": [], "left": left}
    for cid, orig in mapping.items():
        row = {"copy": aops[cid]["name"], "original": aops[orig]["name"], "owner": AREAS.get(aops[orig].get("area"), "?"),
               "moved": 0, "dropped": 0, "notes": []}
        for dk, bucket in sorted((st.get("entries") or {}).items()):
            for kind in ("planned", "actual"):
                out = []
                for e in bucket.get(kind) or []:
                    if not (isinstance(e, dict) and e.get("aopId") == cid):
                        out.append(e)
                        continue
                    tombstones[e["id"]] = {"deleted_at": stamp, "dateKey": dk, "type": kind}
                    if has_tick_by(out + (bucket.get(kind) or []), orig, area, aops):
                        row["dropped"] += 1  # the original already has this person's tick that day
                        continue
                    source = e.get("source")
                    new_id = (tick_id(source, dk, orig, aops[orig].get("area"), area)
                              if source in (ENTRY_SOURCE, PLAN_SOURCE) else str(uuid.uuid4()))
                    if new_id in tombstones:
                        new_id = str(uuid.uuid4())
                    out.append({**e, "id": new_id, "aopId": orig, "by": area})
                    row["moved"] += 1
                bucket[kind] = out
        monthly = st.get("aopMonthly") or {}
        for month, record in sorted((monthly.get(cid) or {}).items()):
            lines = _copy_note_lines(record or {})
            if not lines:
                continue
            target_months = monthly.setdefault(orig, {})
            target = dict(target_months.get(month) or new_monthly_record(target_months, month, stamp))
            block = [COPY_NOTE_HEADER.format(person=person)] + lines
            target["notation"] = "\n".join(l for l in [(target.get("notation") or "").rstrip()] + block if l)
            target["notation_updated_at"] = stamp
            target_months[month] = target
            row["notes"].append({"month": month, "lines": lines})
        monthly.pop(cid, None)
        st["aops"].pop(cid)
        st["aopOrder"] = [i for i in st.get("aopOrder") or [] if i != cid]
        st.setdefault("aopTombstones", {})[cid] = {"deleted_at": stamp}
        report["copies"].append(row)
    return st, report


def check_migration(old, new, suffix, area):
    """Raise unless `new` is `old` with only the copies moved onto their originals."""
    check_app_version(old)
    mapping, _ = find_copies(old, suffix, area)
    copies = set(mapping)
    old_aops = calendar_aops(old)
    exp = copy.deepcopy(new)
    if {i: a for i, a in old_aops.items() if i not in copies} != exp.get("aops"):
        raise RuntimeError("Guard: AOPs other than the copies would change. Nothing written.")
    if [i for i in old.get("aopOrder") or [] if i not in copies] != (exp.get("aopOrder") or []):
        raise RuntimeError("Guard: the AOP order would change beyond the copies. Nothing written.")
    for key in ("aops", "aopOrder"):
        if key in old:
            exp[key] = copy.deepcopy(old[key])
    new_aop_tomb = exp.get("aopTombstones") or {}
    if set(new_aop_tomb) - set(old.get("aopTombstones") or {}) - copies:
        raise RuntimeError("Guard: an AOP other than a copy would be deleted. Nothing written.")
    exp["aopTombstones"] = {i: t for i, t in new_aop_tomb.items() if i not in copies or i in (old.get("aopTombstones") or {})}
    old_tomb = old.get("entryTombstones") or {}
    copy_ids = {e["id"] for b in (old.get("entries") or {}).values() for k in ("planned", "actual")
                for e in b.get(k) or [] if isinstance(e, dict) and e.get("aopId") in copies}
    added_tomb = set(exp.get("entryTombstones") or {}) - set(old_tomb)
    if added_tomb - copy_ids:
        raise RuntimeError("Guard: a tick other than a copy's would be deleted. Nothing written.")
    exp["entryTombstones"] = {i: t for i, t in (exp.get("entryTombstones") or {}).items() if i not in added_tomb}
    if not old_tomb and "entryTombstones" not in old and not exp["entryTombstones"]:
        exp.pop("entryTombstones")
    originals = set(mapping.values())
    moved = 0
    for dk, bucket in (exp.get("entries") or {}).items():
        before = (old.get("entries") or {}).get(dk) or {}
        for kind in ("planned", "actual"):
            fresh = [e for e in bucket.get(kind) or [] if e not in (before.get(kind) or [])]
            for e in fresh:
                if not (e.get("aopId") in originals and e.get("by") == area):
                    raise RuntimeError("Guard: an unexpected tick would be added. Nothing written.")
            moved += len(fresh)
            # Without the moved ticks, the day must be the old day without the copy's ticks.
            rest = [e for e in before.get(kind) or [] if not (isinstance(e, dict) and e.get("aopId") in copies)]
            if [e for e in bucket.get(kind) or [] if e not in fresh] != rest:
                raise RuntimeError("Guard: another tick would change. Nothing written.")
            if kind in bucket or kind in before:
                bucket[kind] = copy.deepcopy(before.get(kind) or [])
    if moved > len(copy_ids):
        raise RuntimeError("Guard: more ticks would be added than the copies had. Nothing written.")
    old_monthly = old.get("aopMonthly") or {}
    new_monthly = exp.get("aopMonthly") or {}
    for cid in copies:
        if cid in new_monthly:
            raise RuntimeError("Guard: a copy's month records would stay. Nothing written.")
        if cid in old_monthly:
            new_monthly[cid] = old_monthly[cid]
    for aop_id, months in new_monthly.items():
        for month, record in list(months.items()):
            before = (old_monthly.get(aop_id) or {}).get(month)
            if record == before:
                continue
            if aop_id not in originals:
                raise RuntimeError("Guard: another AOP's month record would change. Nothing written.")
            old_lines = [l.rstrip() for l in ((before or {}).get("notation") or "").replace("\r\n", "\n").split("\n") if l.strip()]
            new_lines = [l.rstrip() for l in (record.get("notation") or "").replace("\r\n", "\n").split("\n") if l.strip()]
            if new_lines[:len(old_lines)] != old_lines:
                raise RuntimeError("Guard: an original's notation would lose or change text. Nothing written.")
            rest = {k: v for k, v in record.items() if k not in ("notation", "notation_updated_at")}
            if before is None:
                del months[month]
            elif rest != {k: v for k, v in before.items() if k not in ("notation", "notation_updated_at")}:
                raise RuntimeError("Guard: a month field other than the notation would change. Nothing written.")
            else:
                months[month] = before
        if aop_id not in old_monthly and not months:
            new_monthly.pop(aop_id)
    if exp != old:
        raise RuntimeError("Guard: the new state differs from the stored one beyond the migration. Nothing written.")


def print_migration(report):
    for row in report["copies"]:
        print(f"{row['copy']}  ->  {row['original']} ({row['owner']}'s AOP): "
              f"{row['moved']} tick(s) moved, {row['dropped']} already there")
        for n in row["notes"]:
            print(f"    notes for {n['month']}, added to the original under its own heading:")
            for line in n["lines"]:
                print(f"      {line}")
    for name in report["left"]:
        print(f"left alone, no single original in another area: {name}")
    if not report["copies"] and not report["left"]:
        print("No copies found.")


# ---------------------------------------------------------------- Supabase

class Workspace:
    def __init__(self, cfg):
        self.url = cfg["supabase_url"].rstrip("/")
        self.key = cfg["supabase_anon_key"]
        self.workspace = cfg["workspace_id"]
        self.token = None
        self.user_id = None

    def _call(self, method, path, body=None, token=None, prefer=None):
        headers = {"apikey": self.key, "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if prefer:
            headers["Prefer"] = prefer
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"{method} {path.split('?')[0]} -> HTTP {e.code}: {detail}") from None
        return json.loads(raw) if raw else None

    def _store_session(self, session):
        self.token = session["access_token"]
        self.user_id = (session.get("user") or {}).get("id")
        write_json(TOKEN_FILE, {"refresh_token": session["refresh_token"], "user_id": self.user_id,
                                "email": (session.get("user") or {}).get("email"), "saved_at": now_iso()})

    def login_password(self, email, password):
        self._store_session(self._call("POST", "/auth/v1/token?grant_type=password",
                                       {"email": email, "password": password}))

    def login_refresh(self):
        saved = read_json(TOKEN_FILE)
        if not saved:
            sys.exit(f"Not signed in. Run `python {Path(__file__).name} setup`.")
        self._store_session(self._call("POST", "/auth/v1/token?grant_type=refresh_token",
                                       {"refresh_token": saved["refresh_token"]}))

    def fetch(self):
        q = urllib.parse.urlencode({"id": f"eq.{self.workspace}", "select": "id,state,updated_at"})
        rows = self._call("GET", f"/rest/v1/{TABLE}?{q}", token=self.token)
        if not rows:
            raise RuntimeError(f"Workspace {self.workspace!r} not found or not readable with this login.")
        return rows[0]

    def write_if_unchanged(self, state, seen_updated_at):
        q = urllib.parse.urlencode({"id": f"eq.{self.workspace}", "updated_at": f"eq.{seen_updated_at}"})
        rows = self._call("PATCH", f"/rest/v1/{TABLE}?{q}",
                          {"state": state, "updated_by": self.user_id, "updated_at": now_iso()},
                          token=self.token, prefer="return=representation")
        return bool(rows)


def sync_live(ws, cfg, collected, dry_run, changes=None):
    """One compare-and-swap write for all collected days and sprint outcomes.
    `changes` is the end-of-day review's changes file, already read."""
    area = cfg["area"]
    dks = [d["date"] for d in collected["days"]]
    ledger = set(read_json(ACHIEVED_LEDGER_FILE, []) or [])
    created = read_json(CREATED_AOPS_FILE, {}) or {}
    saved_review = read_json(REVIEW_FILE, {}) or {}
    for _ in range(5):
        row = ws.fetch()
        old = row["state"]
        new, reports, added, keys, new_aops, review = apply_all(old, collected, area, cfg, ledger, created,
                                                                changes=changes, saved_review=saved_review)
        check_only_additions(old, new, area, dks)
        result = {"reports": reports, "added": added, "new_aops": new_aops, "written": False}
        if dry_run:
            return result
        if new != old and not ws.write_if_unchanged(new, row["updated_at"]):
            continue  # someone saved in between: re-read and apply again
        if new != old:
            result["written"] = True
            result["verified"] = _contains_additions(ws.fetch()["state"], new, area, dks)
        write_json(ACHIEVED_LEDGER_FILE, sorted(ledger | set(keys)))
        if changes or saved_review:
            save_review(review)
        if new_aops["created"]:
            write_json(CREATED_AOPS_FILE, {**created, **{base_name(a["name"]): a["id"] for a in new_aops["created"]}})
        return result
    raise RuntimeError("The workspace kept changing while writing; gave up after 5 tries. Nothing lost - run again.")


def _contains_additions(after, new, area, dks):
    ours = [i for i, a in (new.get("aops") or {}).items() if isinstance(a, dict) and a.get("source") == ENTRY_SOURCE]
    if not all(i in (after.get("aops") or {}) for i in ours):
        return False
    for dk in dks:
        want = [e for e in ((new.get("entries") or {}).get(dk) or {}).get("actual", [])
                if str(e.get("id", "")).startswith(ENTRY_SOURCE)]
        have = ((after.get("entries") or {}).get(dk) or {}).get("actual", [])
        if not all(e in have for e in want):
            return False
        want_note = (((new.get("dayNotes") or {}).get(dk) or {}).get("area_notes") or {}).get(area) or {}
        have_note = (((after.get("dayNotes") or {}).get(dk) or {}).get("area_notes") or {}).get(area) or {}
        if BLOCK_START in want_note.get("text", "") and BLOCK_START not in have_note.get("text", ""):
            return False
    return True


def sync_plan_live(ws, cfg, plan, dry_run):
    """One compare-and-swap write for an agreed monthly plan."""
    area = cfg["area"]
    for _ in range(5):
        row = ws.fetch()
        old = row["state"]
        new, report = apply_plan(old, plan, area, cfg)
        check_plan_additions(old, new, area, report["month"])
        report["written"] = False
        if dry_run or new == old:
            return report
        if not ws.write_if_unchanged(new, row["updated_at"]):
            continue  # someone saved in between: re-read and apply again
        report["written"] = True
        after = ws.fetch()["state"]
        _, again = apply_plan(after, plan, area, cfg)
        report["verified"] = not again["ticked"] and not again["goals"] and not again["goal_field"]
        return report
    raise RuntimeError("The workspace kept changing while writing; gave up after 5 tries. Nothing lost - run again.")


def print_plan_report(report):
    print(f"Plan for {report['month']}:")
    for g in report["goal_field"]:
        print(f"  goal field: {g}")
    for g in report["goals"]:
        print(f"  {g['aop']}")
        for line in g["lines"]:
            print(f"    - {line}")
    by_day = {}
    for t in report["ticked"]:
        dk, label = t.split(" ", 1)
        by_day.setdefault(dk, []).append(label)
    if by_day:
        print(f"  planned ticks ({len(report['ticked'])}):")
        for dk, labels in by_day.items():
            print(f"    {dk}: {', '.join(labels)}")
    for s in report["skipped"]:
        print(f"  skipped: {s}")
    if not (report["goal_field"] or report["goals"] or report["ticked"]):
        print("  nothing new to add")
    print("\nSaved to the calendar." if report.get("written") else "\nNot saved (dry run or nothing new).")
    if report.get("written") and not report.get("verified"):
        print("warning: the save went through but a re-read did not show all of it; run `plan` again")


# ----------------------------------------------------------------- commands

def days_to_run(explicit):
    if explicit:
        d = dt.date.fromisoformat(explicit)
        return d, d
    today = dt.date.today()
    last = (read_json(LAST_RUN_FILE) or {}).get("last_complete_day")
    first = today
    if last:
        first = max(dt.date.fromisoformat(last), today - dt.timedelta(days=MAX_CATCHUP_DAYS))
    return first, today


def print_result(result, full=True):
    new_aops = result.get("new_aops") or {}
    if new_aops.get("created"):
        print(f"New AOPs in your area ({len(new_aops['created'])}):")
        for a in new_aops["created"]:
            hint = f"  - maybe the same as: {', '.join(a['similar'][:3])}" if a["similar"] else ""
            print(f"    {a['color']} {a['name']}{hint}")
        print()
    for name in new_aops.get("removed_before") or []:
        print(f"    not added again, someone deleted or renamed it: {name}")
    for name in new_aops.get("skipped_in_review") or []:
        print(f"    not added, you skipped it in a review: {name}")
    for report in result["reports"]:
        ticked = len(report["ticked"]) + len(report.get("ticked_other_area", []))
        line = f"{report['date']}{' (weekend, note only)' if report['weekend'] else ''}: "
        line += f"{ticked} new tick(s)" + (", day log" if report["note"] else "")
        if not ticked and not report["note"]:
            line += " - nothing new"
        print(line)
        for key, label in (("ticked", "new tick"), ("ticked_other_area", "new tick, on another person's AOP"),
                           ("added_in_review", "new tick, added by you in the review"),
                           ("skipped_in_review", "no tick, you skipped it in the review"),
                           ("skip_already_saved", "skipped in the review, but already ticked: remove it in the calendar"),
                           ("already_ticked", "already ticked"),
                           ("deleted_before", "skipped, someone deleted the tick"),
                           ("other_area", "another person's AOP, note only (tick_other_areas is off, "
                                          f"or the calendar isn't on version {BY_APP_VERSION} yet)"),
                           ("unmatched", "not in calendar AOP list, note only"),
                           ("ambiguous", "in the calendar more than once, none in your area: add an alias")):
            for name in report.get(key, []):
                print(f"    {label}: {name}")
        if full and report["note"]:
            print("\n" + report["note"] + "\n")
    if result["added"]:
        print(f"\nSprint outcomes ({sum(len(a['lines']) for a in result['added'])} line(s)):")
        for a in result["added"]:
            print(f"  {a['aop']} · {a['month']}")
            for line in a["lines"]:
                was = (a.get("reworded") or {}).get(line)
                print(f"    {line}" + (f"   (reworded, was: {was})" if was else ""))
            for line in a.get("skipped") or []:
                print(f"    skipped in the review: {line}")
    print("\nSaved to the calendar." if result.get("written") else "\nNot saved (dry run or nothing new).")
    if result.get("written") and not result.get("verified"):
        print("warning: the save went through but a re-read did not show it; the next run tries again")


def read_password(prompt):
    """Like getpass, but shows * for each character so people can see their typing lands."""
    if os.name != "nt" or not sys.stdin.isatty():
        return getpass.getpass(prompt)
    import msvcrt
    sys.stdout.write(prompt)
    sys.stdout.flush()
    chars = []
    while True:
        ch = msvcrt.getwch()
        if ch in ("\r", "\n"):
            break
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch in ("\x00", "\xe0"):  # arrow and function keys come as two characters
            msvcrt.getwch()
            continue
        if ch == "\b":
            if chars:
                chars.pop()
                sys.stdout.write("\b \b")
        else:
            chars.append(ch)
            sys.stdout.write("*")
        sys.stdout.flush()
    sys.stdout.write("\n")
    return "".join(chars)


def cmd_setup(args):
    cfg = read_json(CONFIG_FILE, {}) or {}

    def ask(key, prompt, default="", check=None):
        """Ask until the answer passes `check` (a function returning an error text, or None)."""
        current = cfg.get(key, default)
        if check and current and check(current):
            current = ""  # a saved value that is wrong isn't offered again
        while True:
            value = input(f"{prompt}{f' [{current}]' if current else ''}: ").strip() or current
            problem = check(value) if check else None
            if not problem:
                cfg[key] = value
                return
            print(f"  {problem} Try again.")

    print("AI Calender daily sync setup. The first three values are in the calendar's Login dialog.")
    ask("supabase_url", "Supabase URL (starts with https://)", check=check_supabase_url)
    ask("supabase_anon_key", "Supabase anon key (starts with sb_publishable_)", check=check_anon_key)
    ask("workspace_id", "Workspace ID", "nordics-team", check=lambda v: None if v else "It can't be empty.")
    ask("email", "Your calendar login email", check=lambda v: None if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v) else "That isn't an email address.")
    print("Areas: " + ", ".join(f"{k} = {v}" for k, v in AREAS.items()))
    ask("area", "Your area id", check=lambda v: None if v in AREAS else "That isn't one of the area ids above.")
    ask("name", "Your first name, as dashboard commits write it", AREAS[cfg["area"]])
    ask("power_repo", "Path to your power repo clone", str(Path.home() / "NCS AI" / "power"))
    emails = cfg.get("author_emails") or [cfg["email"]]
    raw = input(f"Your git author emails, comma separated [{', '.join(emails)}]: ").strip()
    cfg["author_emails"] = [e.strip() for e in raw.split(",") if e.strip()] if raw else emails
    cfg.setdefault("include_session_titles", True)
    cfg.setdefault("aliases", {})
    ws = Workspace(cfg)
    for attempt in range(3):
        try:
            ws.login_password(cfg["email"], read_password("Calendar password (not stored): "))
            ws.fetch()
            break
        except RuntimeError as e:
            text = str(e).lower()
            if "invalid api key" in text or "http 401" in text or "no api key" in text:
                sys.exit("The calendar refused the anon key. Copy it again from the Login dialog "
                         "(it starts with sb_publishable_) and run setup again. Nothing was saved.")
            if "invalid login credentials" in text or "http 400" in text:
                print("  Wrong email or password." + (" Try again." if attempt < 2 else ""))
                continue
            sys.exit(f"Could not sign in: {e}. Check the Supabase URL and run setup again. Nothing was saved.")
        except (urllib.error.URLError, OSError) as e:
            sys.exit(f"Could not reach {cfg['supabase_url']} ({e}). Check the Supabase URL and your internet, "
                     "then run setup again. Nothing was saved.")
    else:
        sys.exit("Three wrong passwords. Nothing was saved; run setup again.")
    write_json(CONFIG_FILE, cfg)  # only once the sign-in works, so a typo never ends up saved
    print(f"Signed in and can read workspace {cfg['workspace_id']!r}. Config: {CONFIG_FILE}")


def check_supabase_url(value):
    if value.startswith(("sb_", "eyJ")):
        return "That's the anon key, not the URL. The URL starts with https://."
    if not re.fullmatch(r"https://[\w.-]+\.[a-z]{2,}/?", value):
        return "The URL looks like https://<project>.supabase.co."
    return None


def check_anon_key(value):
    if value.startswith("http"):
        return "That's the URL, not the anon key. The key starts with sb_publishable_."
    if value.startswith("sb_secret_"):
        return "That's a secret key; never use it here. Use the publishable key from the Login dialog."
    if not value.startswith(("sb_publishable_", "eyJ")) or " " in value:
        return "The anon key starts with sb_publishable_."
    return None


def _config_for(args):
    cfg = read_json(args.config) if getattr(args, "config", None) else load_config()
    if getattr(args, "area", None):
        cfg["area"] = args.area
    return cfg


def _range_args(args):
    first = dt.date.fromisoformat(args.date or args.__dict__.get("from") or dt.date.today().isoformat())
    last = dt.date.fromisoformat(args.date or args.to or dt.date.today().isoformat())
    if last < first:
        sys.exit("--to is before --from.")
    return first, last


def cmd_collect(args):
    cfg = _config_for(args)
    first, last = _range_args(args)
    print(json.dumps(collect(cfg, first, last, fetch=not args.no_fetch), ensure_ascii=False, indent=2))


def cmd_preview(args):
    """Apply to a state file (an Export JSON from the app) without any network write."""
    cfg = _config_for(args)
    collected = read_json(args.activity) if args.activity else collect(cfg, *_range_args(args), fetch=not args.no_fetch)
    raw = read_json(args.state)
    old = raw.get("state", raw)
    ledger = set(read_json(args.ledger, []) or []) if args.ledger else set()
    changes = read_changes(args.changes) if args.changes else None
    new, reports, added, keys, new_aops, _ = apply_all(old, collected, cfg["area"], cfg, ledger, changes=changes)
    check_only_additions(old, new, cfg["area"], [d["date"] for d in collected["days"]])
    print_result({"reports": reports, "added": added, "new_aops": new_aops}, full=not args.brief)
    print("Guard: only additions - OK")
    if args.out:
        write_json(args.out, {**raw, "state": new} if "state" in raw else new)
        print(f"Wrote {args.out}")


def _live(args, first, last):
    cfg = load_config()
    ws = Workspace(cfg)
    try:
        ws.login_refresh()
    except RuntimeError as e:
        raise RuntimeError(f"sign-in expired or was replaced ({e}). Run `setup` again.") from None
    collected = collect(cfg, first, last)
    for w in collected["warnings"]:
        print(f"warning: {w}")
    changes = read_changes(args.changes) if getattr(args, "changes", None) else None
    result = sync_live(ws, cfg, collected, args.dry_run, changes)
    print_result(result, full=getattr(args, "full", True))
    return result


def cmd_run(args):
    first, last = days_to_run(args.date)
    _live(args, first, last)
    if not args.dry_run and not args.date:
        # Today stays open: the next run starts from today, so late work is picked up.
        write_json(LAST_RUN_FILE, {"last_complete_day": dt.date.today().isoformat(), "at": now_iso()})


def cmd_backfill(args):
    first, last = _range_args(args)
    print(f"Backfilling {first} to {last} ({(last - first).days + 1} days) in one save.")
    _live(args, first, last)


def _signed_in(cfg):
    ws = Workspace(cfg)
    try:
        ws.login_refresh()
    except RuntimeError as e:
        raise RuntimeError(f"sign-in expired or was replaced ({e}). Run `setup` again.") from None
    return ws


def cmd_plan_facts(args):
    cfg = load_config()
    area = args.area or cfg["area"]
    if area not in AREAS:
        sys.exit(f"Unknown area {area!r}. Areas: {', '.join(AREAS)}")
    month = args.month or dt.date.today().strftime("%Y-%m")
    source = dict(cfg)
    if area != cfg["area"]:  # a look at someone else's area: their commits only if their emails are given
        source.update(area=area, name=AREAS[area], author_emails=args.author_email or [])
    elif args.author_email:
        source["author_emails"] = args.author_email
    state = _signed_in(cfg).fetch()["state"]
    commits = []
    if source["author_emails"]:
        if not args.no_fetch:
            for w in fetch_repo(source):
                print(f"warning: {w}", file=sys.stderr)
        first = dt.date.fromisoformat(f"{shift_month(month, -1)}-01")
        last = min(dt.date.today(), month_days(month)[-1])
        commits = collect_commits(source, first, last)
    facts = plan_facts(state, source, area, month, commits)
    facts["commits_by"] = source["author_emails"]
    text = json.dumps(facts, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Wrote {args.out}")
    else:
        print(text)


def cmd_migrate_copies(args):
    """Dry run unless --write. With --state it works on an exported file and never touches the calendar."""
    if args.area not in AREAS:
        sys.exit(f"Unknown area {args.area!r}. Areas: {', '.join(AREAS)}")
    if args.state:
        raw = read_json(args.state)
        old = raw.get("state", raw)
        new, report = migrate_copies(old, args.suffix, args.area)
        check_migration(old, new, args.suffix, args.area)
        print_migration(report)
        print("Guard: only the copies moved - OK")
        if args.out:
            write_json(args.out, {**raw, "state": new} if "state" in raw else new)
            print(f"Wrote {args.out}")
        return
    cfg = load_config()
    ws = _signed_in(cfg)
    for _ in range(5):
        row = ws.fetch()
        old = row["state"]
        if app_version(old) < BY_APP_VERSION:
            raise RuntimeError(f"The calendar isn't on version {BY_APP_VERSION} yet: open the new page once, then run this again.")
        new, report = migrate_copies(old, args.suffix, args.area)
        check_migration(old, new, args.suffix, args.area)
        print_migration(report)
        if not args.write or new == old:
            print("\nDry run, nothing saved. Add --write to save." if not args.write else "\nNothing to move.")
            return
        if not ws.write_if_unchanged(new, row["updated_at"]):
            print("Someone saved in between; reading again.\n")
            continue
        after = calendar_aops(ws.fetch()["state"])
        print("\nSaved." if not any(c in after for c in find_copies(old, args.suffix, args.area)[0])
              else "\nwarning: saved, but a re-read still shows a copy")
        return
    raise RuntimeError("The workspace kept changing while writing; gave up after 5 tries. Nothing lost - run again.")


def cmd_plan(args):
    cfg = load_config()
    plan = read_json(args.file)
    if not isinstance(plan, dict):
        sys.exit(f"Could not read a plan from {args.file}.")
    print(f"{AREAS[cfg['area']]}'s area, {'dry run' if args.dry_run else 'saving'}.")
    print_plan_report(sync_plan_live(_signed_in(cfg), cfg, plan, args.dry_run))


class _Tee:
    """Writes to the console (absent under pythonw) and to the log file."""
    def __init__(self, *streams):
        self.streams = [s for s in streams if s is not None]

    def write(self, text):
        for s in self.streams:
            s.write(text)
        return len(text)

    def flush(self):
        for s in self.streams:
            s.flush()


def open_log():
    """`run` is meant for a scheduled task with no window, so it also logs to sync.log."""
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    log_path = HOME_DIR / "sync.log"
    if log_path.exists() and log_path.stat().st_size > 512_000:
        os.replace(log_path, log_path.with_suffix(".old.log"))
    log = open(log_path, "a", encoding="utf-8", buffering=1)  # line by line, so a stopped run still logs
    log.write(f"\n=== {dt.datetime.now().isoformat(timespec='seconds')} {' '.join(sys.argv[1:])}\n")
    sys.stdout = _Tee(sys.stdout, log)
    sys.stderr = _Tee(sys.stderr, log)


def main():
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if len(sys.argv) > 1 and sys.argv[1] in ("run", "backfill", "plan"):
        open_log()
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="one-time: workspace, area, git emails, sign in").set_defaults(fn=cmd_setup)

    c = sub.add_parser("collect", help="print what was found, as JSON (no calendar access)")
    c.add_argument("--date", help="one day")
    c.add_argument("--from", help="first day of a range")
    c.add_argument("--to", help="last day of a range (default today)")
    c.add_argument("--config", help="config file to use instead of the saved one")
    c.add_argument("--no-fetch", action="store_true")
    c.set_defaults(fn=cmd_collect)

    v = sub.add_parser("preview", help="apply to an exported state file, offline")
    v.add_argument("--state", required=True, help="Export JSON from the calendar")
    v.add_argument("--date")
    v.add_argument("--from")
    v.add_argument("--to")
    v.add_argument("--area")
    v.add_argument("--config")
    v.add_argument("--activity", help="output of `collect` instead of collecting now")
    v.add_argument("--ledger", help="achieved.json to respect")
    v.add_argument("--out")
    v.add_argument("--brief", action="store_true", help="one line per day instead of the full day log")
    v.add_argument("--changes", help="end-of-day review changes file (see `run --changes`)")
    v.add_argument("--no-fetch", action="store_true")
    v.set_defaults(fn=cmd_preview)

    r = sub.add_parser("run", help="sync today (and missed days) into the shared calendar")
    r.add_argument("--date", help="only this day")
    r.add_argument("--dry-run", action="store_true", help="read the live calendar, write nothing")
    r.add_argument("--changes", help="end-of-day review: JSON with skip_ticks, add_ticks, skip_outcomes, "
                                     "reword_outcomes, skip_new_aops, notes (names as the dry run prints them)")
    r.set_defaults(fn=cmd_run)

    b = sub.add_parser("backfill", help="fill in a range of past days in one save")
    b.add_argument("--from", required=True, help="first day, e.g. 2026-08-01")
    b.add_argument("--to", help="last day (default today)")
    b.add_argument("--dry-run", action="store_true", help="read the live calendar, write nothing")
    b.add_argument("--full", action="store_true", help="print each day's full day log")
    b.set_defaults(fn=cmd_backfill, date=None)

    f = sub.add_parser("plan-facts", help="what a month's plan can build on, as JSON (reads only)")
    f.add_argument("--month", help="YYYY-MM (default this month)")
    f.add_argument("--area", help="look at another area (reads only)")
    f.add_argument("--author-email", action="append", help="git author email for the commits (repeatable)")
    f.add_argument("--out", help="write the JSON to this file")
    f.add_argument("--no-fetch", action="store_true")
    f.set_defaults(fn=cmd_plan_facts)

    n = sub.add_parser("plan", help="add an agreed monthly plan (goals, planned ticks) to your own area")
    n.add_argument("--file", required=True, help="plan JSON: month, aops{name: goal, goals[]}, days{date: [AOP names]}")
    n.add_argument("--dry-run", action="store_true", help="read the live calendar, write nothing")
    n.set_defaults(fn=cmd_plan)

    m = sub.add_parser("migrate-copies", help="one-off: move '<AOP> (Name)' copies onto the originals as ticks by that person")
    m.add_argument("--suffix", required=True, help='the copies\' name ending, e.g. "(Erik)"')
    m.add_argument("--area", required=True, help="the area the copies are in, e.g. pool")
    m.add_argument("--write", action="store_true", help="save to the live calendar (default: dry run)")
    m.add_argument("--state", help="work on an exported state file instead of the live calendar")
    m.add_argument("--out", help="with --state: write the migrated state here")
    m.set_defaults(fn=cmd_migrate_copies)

    args = p.parse_args()
    try:
        args.fn(args)
    except RuntimeError as e:
        sys.exit(f"error: {e}")
    except urllib.error.URLError as e:
        sys.exit(f"error: could not reach the calendar ({e.reason}). Nothing written; the next run catches up.")
    except Exception:
        import traceback
        traceback.print_exc()  # goes to sync.log too, so a windowless run leaves a trace
        sys.exit(1)


if __name__ == "__main__":
    main()
