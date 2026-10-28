#!/usr/bin/env python3
"""Daily AI Calender sync: adds one person's AOP work to their own area.

Sources, per local calendar day:
  - their commits in the power repo, on every branch (main, pushed branches,
    local branches not pushed yet) - Decagon dashboard commits included
  - their Claude Code sessions (~/.claude/projects): which AOP files they
    edited or opened, and the titles of sessions that touched the power repo

What it writes, and nothing else:
  - an "actual" tick per AOP of their own area they committed or edited that
    weekday (fixed id, never re-added once someone deletes it)
  - a "Claude day log" block in their own area's day note; any text the
    person wrote stays as it is, only the block between the markers is
    replaced on a re-run
  - a "✅ <change> (<date>)" line under "Achieved during Sprint:" in the
    month's notation of each AOP of their own area, once a change of theirs
    is on main or live in Decagon (never re-added once someone removes it)

Every write is a compare-and-swap on the shared workspace row, and is refused
unless the new state equals the old one apart from those additions.

Standard library only. Run `python claude_day_sync.py --help`.
"""
import argparse
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
from pathlib import Path

HOME_DIR = Path(os.environ.get("AOP_SYNC_HOME") or Path.home() / ".aop-calendar-sync")
CONFIG_FILE = HOME_DIR / "config.json"
TOKEN_FILE = HOME_DIR / "token.json"
LAST_RUN_FILE = HOME_DIR / "last_run.json"
# Sprint-outcome lines already added once; a line someone removed stays removed.
ACHIEVED_LEDGER_FILE = HOME_DIR / "achieved.json"

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
ACHIEVED_HEADER = "Achieved during Sprint:"
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


_FILE_LINES = {}


def _file_lines(repo, ref, path):
    key = (str(repo), ref, path)
    if key not in _FILE_LINES:
        r = git(repo, "show", f"{ref}:{path}", check=False)
        _FILE_LINES[key] = {l.strip().rstrip(",") for l in r.stdout.splitlines()} if r.returncode == 0 else None
    return _FILE_LINES[key]


def carried_into(repo, sha, ref):
    """True when the lines this commit added are in ref's files even though the commit itself is not:
    the change went live in another commit, such as a [Dashboard] re-commit or a squash merge."""
    added = {}
    path = None
    diff = git(repo, "-c", "core.quotePath=false", "show", "--format=", "--unified=0", "--no-renames", sha, check=False)
    for line in diff.stdout.splitlines():
        if line.startswith("+++ "):
            path = line[6:].rstrip("\t") if line.startswith("+++ b/") else None
        elif path and line.startswith("+") and len(line.strip()) > CARRIED_MIN_LINE:
            added.setdefault(path, []).append(line[1:].strip().rstrip(","))
    # Lines already in the parent's file were only moved or re-indented; they say nothing.
    added = {p: [l for l in lines if l not in (_file_lines(repo, f"{sha}^", p) or set())]
             for p, lines in added.items()}
    total = sum(len(v) for v in added.values())
    if not total:
        return False  # only deletions or tiny edits: can't tell
    found = 0
    for path, lines in added.items():
        have = _file_lines(repo, ref, path)
        if have:
            found += sum(1 for l in lines if l in have)
    return found / total >= CARRIED_MIN_SHARE


def commit_location(repo, sha, main_ref, live_ref):
    if git(repo, "merge-base", "--is-ancestor", sha, main_ref, check=False).returncode == 0:
        return "on main"
    if live_ref and git(repo, "merge-base", "--is-ancestor", sha, live_ref, check=False).returncode == 0:
        return "live in Decagon (production)"
    if carried_into(repo, sha, main_ref):
        return "on main, carried over in another commit"
    if live_ref and carried_into(repo, sha, live_ref):
        return "live in Decagon (production), carried over in another commit"
    remote = [b for b in git(repo, "branch", "-r", "--contains", sha, "--format=%(refname:short)").stdout.split()
              if not b.endswith("/HEAD")]
    if remote:
        name = remote[0].split("/", 1)[-1]
        more = f" +{len(remote) - 1}" if len(remote) > 1 else ""
        return f"branch {name}{more} (not merged)"
    local = git(repo, "branch", "--contains", sha, "--format=%(refname:short)").stdout.split()
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
            "sha": sha[:8],
            "day": authored.date().isoformat(),
            "time": authored.strftime("%H:%M"),
            "subject": subject.strip(),
            "how": "dashboard" if subject.startswith("[Dashboard]") else
                   "with Claude" if "claude" in trailers.lower() else "by hand",
            "where": commit_location(repo, sha, main_ref, live_ref),
            "items": [c for c in (classify_path(p) for p in paths) if c],
        })
    return commits


# ------------------------------------------------------ Claude Code sessions

def known_aop_names(cfg, extra=()):
    """AOP names that really exist: on main, live, or in the working tree."""
    repo = Path(cfg["power_repo"]).expanduser()
    names = set(extra)
    for ref in (cfg.get("main_ref", "origin/main"), cfg.get("live_ref", "origin/production")):
        r = git(repo, "ls-tree", "-r", "--name-only", ref, "aops", check=False)
        names.update(c[1] for c in (classify_path(p) for p in r.stdout.splitlines()) if c and c[0] == "aop")
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
    return {"days": days, "achievements": build_achievements(commits), "warnings": warnings}


# ------------------------------------------------------------- calendar state

def match_aop(name, aops, aliases):
    target = aliases.get(name, name)
    exact = [i for i, a in aops.items() if norm_name(a.get("name")) == norm_name(target)]
    if len(exact) == 1:
        return exact[0]
    loose = [i for i, a in aops.items() if base_name(a.get("name")) == base_name(target)]
    return loose[0] if len(loose) == 1 else None


def calendar_aops(state):
    return {i: a for i, a in (state.get("aops") or {}).items()
            if isinstance(a, dict) and a.get("kind", "aop") == "aop"}


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


def render_block(activity, matches, area, cfg):
    """The day log, grouped by where the work is: done, on a branch, or only edited."""
    sections = {"done": [], "branch": [], "edited": []}
    looked = []
    for a in activity["aops"]:
        if not is_real_work(a):
            looked.append(a["name"])
            continue
        owner = matches.get(a["name"])
        tag = "" if owner == area else " (not in the calendar)" if owner is None else f" ({AREAS.get(owner, owner)}'s area)"
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

    lines.append(BLOCK_END)
    return "\n".join(lines)


def apply_day(state, activity, area, cfg, stamp=None):
    """Ticks and the day-log block for one day. Returns (new_state, report); never mutates `state`."""
    stamp = stamp or now_iso()
    st = copy.deepcopy(state)
    dk = activity["date"]
    aops = calendar_aops(st)
    tombstones = st.get("entryTombstones") or {}
    aliases = cfg.get("aliases") or {}
    report = {"date": dk, "weekend": is_weekend(dk), "ticked": [], "already_ticked": [], "deleted_before": [],
              "other_area": [], "unmatched": [], "note": None}

    matches = {}
    for a in activity["aops"]:
        aop_id = match_aop(a["name"], aops, aliases)
        matches[a["name"]] = aops[aop_id].get("area") if aop_id else None
        if not is_real_work(a):
            continue
        if not aop_id:
            report["unmatched"].append(a["name"])
            continue
        label = aops[aop_id].get("name")
        if aops[aop_id].get("area") != area:
            report["other_area"].append(label)
            continue
        if report["weekend"]:  # the calendar itself refuses Actual ticks on weekends
            continue
        entries = st.setdefault("entries", {})
        bucket = entries.get(dk) or {"planned": [], "actual": []}
        actual = list(bucket.get("actual") or [])
        entry_id = f"{ENTRY_SOURCE}:{dk}:{aop_id}"
        if entry_id in tombstones:
            report["deleted_before"].append(label)
        elif any(e.get("aopId") == aop_id for e in actual if isinstance(e, dict)):
            report["already_ticked"].append(label)
        else:
            actual.append({"id": entry_id, "aopId": aop_id, "ts": stamp, "source": ENTRY_SOURCE})
            entries[dk] = {**bucket, "planned": list(bucket.get("planned") or []), "actual": actual}
            report["ticked"].append(label)

    if activity["aops"] or activity["other"] or activity.get("bulk") or activity["sessions"]["power_titles"]:
        notes = st.setdefault("dayNotes", {})
        day_note = notes.get(dk)
        if isinstance(day_note, str):  # legacy plain-text note, same shape migrateIfNeeded produces
            day_note = {"text": day_note, "planned_text": "", "actual_text": day_note, "created_at": "", "updated_at": ""}
        day_note = dict(day_note or {"text": "", "planned_text": "", "actual_text": "", "created_at": stamp, "updated_at": ""})
        area_notes = dict(day_note.get("area_notes") or {})
        current = area_notes.get(area) or {}
        block = render_block(activity, matches, area, cfg)
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


def apply_achievements(state, items, area, cfg, ledger, stamp=None):
    """Sprint-outcome lines into each own-area AOP's monthly notation. Returns (new_state, added, keys)."""
    stamp = stamp or now_iso()
    st = copy.deepcopy(state)
    aops = calendar_aops(st)
    aliases = cfg.get("aliases") or {}
    pending = {}
    for item in items:
        if item["key"] in ledger:
            continue
        aop_id = match_aop(item["aop"], aops, aliases)
        if not aop_id or aops[aop_id].get("area") != area:
            continue
        pending.setdefault((aop_id, item["month"]), []).append(item)
    added, keys = [], []
    for (aop_id, month), group in sorted(pending.items()):
        monthly = st.setdefault("aopMonthly", {}).setdefault(aop_id, {})
        record = monthly.get(month)
        notation = (record or {}).get("notation") or ""
        existing = {l.strip() for l in notation.split("\n")}
        # "GDPR Breach" and "GDPR Breach V4" files can both map to one calendar AOP
        lines = list(dict.fromkeys(i["line"] for i in group if i["line"] not in existing))
        keys += [i["key"] for i in group]
        if not lines:
            continue
        if record is None:  # same starting values ensureMonthly gives a new month in the app
            prev = monthly.get(shift_month(month, -1)) or {}
            record = {"goal": "", "comments": "", "required": False,
                      "deflection_start": prev.get("deflection_end", ""), "deflection_end": "",
                      "conversation_start": prev.get("conversation_end", ""), "conversation_end": "",
                      "notation": "", "notation_updated_at": "", "created_at": stamp, "updated_at": stamp,
                      "goal_created_at": "", "goal_updated_at": "", "comments_created_at": "",
                      "comments_updated_at": "", "required_created_at": "", "required_updated_at": ""}
        record = {**record, "notation": insert_achieved(notation, lines), "notation_updated_at": stamp}
        monthly[month] = record
        added.append({"aop": aops[aop_id].get("name"), "month": month, "lines": lines})
    return st, added, keys


def shift_month(month, delta):
    y, m = map(int, month.split("-"))
    m += delta
    y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


def apply_all(state, collected, area, cfg, ledger, stamp=None):
    stamp = stamp or now_iso()
    st, reports = state, []
    for activity in collected["days"]:
        st, report = apply_day(st, activity, area, cfg, stamp)
        reports.append(report)
    st, added, keys = apply_achievements(st, collected["achievements"], area, cfg, ledger, stamp)
    return st, reports, added, keys


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


def check_only_additions(old, new, area, dks):
    """Raise unless `new` equals `old` apart from this tool's ticks, note blocks and sprint-outcome lines."""
    expected = copy.deepcopy(new)
    old_entries = old.get("entries") or {}
    old_notes = old.get("dayNotes") or {}
    for dk in dks:
        ours = f"{ENTRY_SOURCE}:{dk}:"
        new_entries = expected.get("entries")
        if isinstance(new_entries, dict) and dk in new_entries:
            old_actual = (old_entries.get(dk) or {}).get("actual") or []
            bucket = new_entries[dk]
            bucket["actual"] = [e for e in bucket.get("actual") or []
                                if e in old_actual or not str((e or {}).get("id", "")).startswith(ours)]
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
    own = {i for i, a in (old.get("aops") or {}).items() if isinstance(a, dict) and a.get("area") == area}
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
    for key in ("entries", "dayNotes", "aopMonthly"):
        if key not in old and expected.get(key) == {}:
            expected.pop(key)
    if expected != old:
        raise RuntimeError("Guard: the new state differs from the stored one beyond this tool's additions. Nothing written.")


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


def sync_live(ws, cfg, collected, dry_run):
    """One compare-and-swap write for all collected days and sprint outcomes."""
    area = cfg["area"]
    dks = [d["date"] for d in collected["days"]]
    ledger = set(read_json(ACHIEVED_LEDGER_FILE, []) or [])
    for _ in range(5):
        row = ws.fetch()
        old = row["state"]
        new, reports, added, keys = apply_all(old, collected, area, cfg, ledger)
        check_only_additions(old, new, area, dks)
        result = {"reports": reports, "added": added, "written": False}
        if dry_run:
            return result
        if new != old and not ws.write_if_unchanged(new, row["updated_at"]):
            continue  # someone saved in between: re-read and apply again
        if new != old:
            result["written"] = True
            result["verified"] = _contains_additions(ws.fetch()["state"], new, area, dks)
        write_json(ACHIEVED_LEDGER_FILE, sorted(ledger | set(keys)))
        return result
    raise RuntimeError("The workspace kept changing while writing; gave up after 5 tries. Nothing lost - run again.")


def _contains_additions(after, new, area, dks):
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
    for report in result["reports"]:
        line = f"{report['date']}{' (weekend, note only)' if report['weekend'] else ''}: "
        line += f"{len(report['ticked'])} new tick(s)" + (", day log" if report["note"] else "")
        if not report["ticked"] and not report["note"]:
            line += " - nothing new"
        print(line)
        for key, label in (("ticked", "new tick"), ("already_ticked", "already ticked"),
                           ("deleted_before", "skipped, someone deleted the tick"),
                           ("other_area", "other person's area, note only"),
                           ("unmatched", "not in calendar AOP list, note only")):
            for name in report[key]:
                print(f"    {label}: {name}")
        if full and report["note"]:
            print("\n" + report["note"] + "\n")
    if result["added"]:
        print(f"\nSprint outcomes ({sum(len(a['lines']) for a in result['added'])} line(s)):")
        for a in result["added"]:
            print(f"  {a['aop']} · {a['month']}")
            for line in a["lines"]:
                print(f"    {line}")
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

    def ask(key, prompt, default=""):
        current = cfg.get(key, default)
        value = input(f"{prompt}{f' [{current}]' if current else ''}: ").strip()
        cfg[key] = value or current

    print("AI Calender daily sync setup. The first three values are in the calendar's Login dialog.")
    ask("supabase_url", "Supabase URL")
    ask("supabase_anon_key", "Supabase anon key")
    ask("workspace_id", "Workspace ID", "nordics-team")
    ask("email", "Your calendar login email")
    print("Areas: " + ", ".join(f"{k} = {v}" for k, v in AREAS.items()))
    ask("area", "Your area id")
    if cfg["area"] not in AREAS:
        sys.exit("Unknown area id.")
    ask("name", "Your first name, as dashboard commits write it", AREAS[cfg["area"]])
    ask("power_repo", "Path to your power repo clone", str(Path.home() / "NCS AI" / "power"))
    emails = cfg.get("author_emails") or [cfg["email"]]
    raw = input(f"Your git author emails, comma separated [{', '.join(emails)}]: ").strip()
    cfg["author_emails"] = [e.strip() for e in raw.split(",") if e.strip()] if raw else emails
    cfg.setdefault("include_session_titles", True)
    cfg.setdefault("aliases", {})
    write_json(CONFIG_FILE, cfg)
    ws = Workspace(cfg)
    ws.login_password(cfg["email"], read_password("Calendar password (not stored): "))
    ws.fetch()
    print(f"Signed in and can read workspace {cfg['workspace_id']!r}. Config: {CONFIG_FILE}")


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
    new, reports, added, keys = apply_all(old, collected, cfg["area"], cfg, ledger)
    check_only_additions(old, new, cfg["area"], [d["date"] for d in collected["days"]])
    print_result({"reports": reports, "added": added}, full=not args.brief)
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
    result = sync_live(ws, cfg, collected, args.dry_run)
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
    if len(sys.argv) > 1 and sys.argv[1] in ("run", "backfill"):
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
    v.add_argument("--no-fetch", action="store_true")
    v.set_defaults(fn=cmd_preview)

    r = sub.add_parser("run", help="sync today (and missed days) into the shared calendar")
    r.add_argument("--date", help="only this day")
    r.add_argument("--dry-run", action="store_true", help="read the live calendar, write nothing")
    r.set_defaults(fn=cmd_run)

    b = sub.add_parser("backfill", help="fill in a range of past days in one save")
    b.add_argument("--from", required=True, help="first day, e.g. 2026-08-01")
    b.add_argument("--to", help="last day (default today)")
    b.add_argument("--dry-run", action="store_true", help="read the live calendar, write nothing")
    b.add_argument("--full", action="store_true", help="print each day's full day log")
    b.set_defaults(fn=cmd_backfill, date=None)

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
