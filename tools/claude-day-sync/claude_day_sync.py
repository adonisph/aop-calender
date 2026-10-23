#!/usr/bin/env python3
"""Daily AI Calender sync: adds one person's AOP work to their own area.

Sources, for one local calendar day:
  - their commits in the power repo, on every branch (main, pushed branches,
    local branches not pushed yet) - Decagon dashboard commits included
  - their Claude Code sessions (~/.claude/projects): which AOP files they
    edited or opened, and the titles of sessions that touched the power repo

What it writes, and nothing else:
  - an "actual" tick per AOP of their own area they worked on that day
    (fixed id, never re-added once someone deletes it)
  - a "Claude day log" block in their own area's day note; any text the
    person wrote stays as it is, only the block between the markers is
    replaced on a re-run

Every write is a compare-and-swap on the shared workspace row, and is refused
unless the new state equals the old one apart from those two additions.

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
MAX_CATCHUP_DAYS = 7
MAX_SUBJECTS_PER_AOP = 3
MAX_SESSION_TITLES = 8

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
DASHBOARD_EDITOR_RE = re.compile(r"(?:\(|: )([A-ZÅÄÖÆØ][a-zåäöæø]+), (?:PR #\d+|\d{1,2} [A-Z][a-z]{2} \d{1,2}:\d{2})")
# Matches aops/X.py and aops/generated/X.json inside JSON-dumped tool input,
# where a Windows separator shows up as one or more backslashes.
AOP_PATH_RE = re.compile(r"aops[\\/]+(?:generated[\\/]+)?([^\"\\/\n]+?)\.(?:json|py)\b")


# ---------------------------------------------------------------- utilities

def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def local_day_bounds(day):
    start = dt.datetime.combine(day, dt.time()).astimezone()
    return start, start + dt.timedelta(days=1)


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

def git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
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


def commit_location(repo, sha, main_ref, live_ref):
    if git(repo, "merge-base", "--is-ancestor", sha, main_ref, check=False).returncode == 0:
        return "on main"
    if live_ref and git(repo, "merge-base", "--is-ancestor", sha, live_ref, check=False).returncode == 0:
        return "live in Decagon (production)"
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


def collect_commits(cfg, day, fetch=True):
    repo = Path(cfg["power_repo"]).expanduser()
    main_ref = cfg.get("main_ref", "origin/main")
    live_ref = cfg.get("live_ref", "origin/production")
    my_name = (cfg.get("name") or "").strip().lower()
    warnings = []
    if fetch:
        r = git(repo, "fetch", "--all", "--prune", "--quiet", check=False)
        if r.returncode:
            warnings.append(f"git fetch failed, used what was already fetched: {r.stderr.strip()[:200]}")
    start, end = local_day_bounds(day)
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
        key = (subject.strip(), tuple(sorted(paths)))
        if key in seen:  # the same change rebased or cherry-picked onto another branch
            continue
        seen.add(key)
        commits.append({
            "sha": sha[:8],
            "time": authored.strftime("%H:%M"),
            "subject": subject.strip(),
            "how": "dashboard" if subject.startswith("[Dashboard]") else
                   "with Claude" if "claude" in trailers.lower() else "by hand",
            "where": commit_location(repo, sha, main_ref, live_ref),
            "items": [c for c in (classify_path(p) for p in paths) if c],
        })
    return commits, warnings


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


def collect_sessions(cfg, day, valid_names):
    root = Path(cfg.get("claude_projects_dir") or Path.home() / ".claude" / "projects").expanduser()
    repo_key = str(Path(cfg["power_repo"]).expanduser()).lower().replace("\\", "/")
    start, end = local_day_bounds(day)
    sessions = {}
    if not root.is_dir():
        return sessions
    for path in root.rglob("*.jsonl"):
        try:
            if dt.datetime.fromtimestamp(path.stat().st_mtime).astimezone() < start:
                continue
        except OSError:
            continue
        rel = path.relative_to(root).parts
        sid = path.stem if len(rel) == 2 else rel[1]  # subagent transcripts count for their parent session
        s = sessions.setdefault(sid, {"title": "", "active": False, "power": False, "aops": {}})
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"customTitle"' in line:
                    try:
                        s["title"] = json.loads(line).get("customTitle") or s["title"]
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
                s["active"] = True
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
    return {sid: s for sid, s in sessions.items() if s["active"]}


def collect(cfg, day, fetch=True):
    commits, warnings = collect_commits(cfg, day, fetch=fetch)
    committed = {name for c in commits for kind, name in c["items"] if kind == "aop"}
    sessions = collect_sessions(cfg, day, known_aop_names(cfg, committed))
    aops, other = {}, {}
    for c in commits:
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
        "date": day.isoformat(),
        "aops": sorted(aops.values(), key=lambda a: (-len(a["commits"]), a["name"].lower())),
        "other": sorted(({"kind": k.split("|")[0], "name": k.split("|")[1], "commits": len(v)}
                         for k, v in other.items()), key=lambda o: (o["kind"], o["name"].lower())),
        "sessions": {
            "power_titles": [s["title"] or "untitled session" for s in sessions.values() if s["power"]],
            "other_count": sum(1 for s in sessions.values() if not s["power"]),
        },
        "warnings": warnings,
    }


# ------------------------------------------------------------- calendar state

def match_aop(name, aops, aliases):
    target = aliases.get(name, name)
    exact = [i for i, a in aops.items() if norm_name(a.get("name")) == norm_name(target)]
    if len(exact) == 1:
        return exact[0]
    loose = [i for i, a in aops.items() if base_name(a.get("name")) == base_name(target)]
    return loose[0] if len(loose) == 1 else None


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


def render_block(activity, matches, area, cfg):
    lines = [BLOCK_START]
    worked = [a for a in activity["aops"] if is_real_work(a)]
    looked = [a["name"] for a in activity["aops"] if not is_real_work(a)]
    if worked:
        lines.append(f"AOPs worked on ({len(worked)}):")
        for a in worked:
            bits = []
            if a["commits"]:
                n = len(a["commits"])
                hows = sorted({c["how"] for c in a["commits"]})
                wheres = sorted({c["where"] for c in a["commits"]})
                bits.append(f"{n} commit{'s' if n > 1 else ''} ({', '.join(hows)}), {'; '.join(wheres)}")
            if a["claude"]:
                bits.append(f"{a['claude']} in Claude" + ("" if a["commits"] else ", no commit yet"))
            owner = matches.get(a["name"])
            if owner is None:
                bits.append("not in the calendar's AOP list")
            elif owner != area:
                bits.append(f"{AREAS.get(owner, owner)}'s area")
            lines.append(f"• {a['name']}: {'; '.join(bits)}")
            for c in a["commits"][:MAX_SUBJECTS_PER_AOP]:
                lines.append(f"   – {c['subject'][:110]}")
            if len(a["commits"]) > MAX_SUBJECTS_PER_AOP:
                lines.append(f"   – +{len(a['commits']) - MAX_SUBJECTS_PER_AOP} more")
    if looked:
        rest = len(looked) - MAX_SESSION_TITLES
        lines.append("Looked at in Claude: " + ", ".join(looked[:MAX_SESSION_TITLES]) + (f" +{rest} more" if rest > 0 else ""))
    by_kind = {}
    for o in activity["other"]:
        by_kind.setdefault(o["kind"], []).append(o["name"])
    for kind, names in by_kind.items():
        rest = len(names) - 4
        lines.append(f"{kind}s ({len(names)}): " + ", ".join(names[:4]) + (f" +{rest} more" if rest > 0 else ""))
    titles = activity["sessions"]["power_titles"]
    if cfg.get("include_session_titles", True) and titles:
        shown = ", ".join(f'"{t}"' for t in titles[:MAX_SESSION_TITLES])
        rest = len(titles) - MAX_SESSION_TITLES
        lines.append(f"Claude sessions: {shown}" + (f" +{rest} more" if rest > 0 else ""))
    other_n = activity["sessions"]["other_count"]
    if other_n:
        lines.append(f"+{other_n} other Claude session{'s' if other_n > 1 else ''} outside the power repo")
    lines.append(BLOCK_END)
    return "\n".join(lines)


def apply_day(state, activity, area, cfg, stamp=None):
    """Return (new_state, report). Never mutates `state`."""
    stamp = stamp or now_iso()
    st = copy.deepcopy(state)
    dk = activity["date"]
    aops = {i: a for i, a in (st.get("aops") or {}).items() if isinstance(a, dict) and a.get("kind", "aop") == "aop"}
    tombstones = st.get("entryTombstones") or {}
    aliases = cfg.get("aliases") or {}
    report = {"date": dk, "ticked": [], "already_ticked": [], "deleted_before": [], "other_area": [], "unmatched": []}

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

    has_work = activity["aops"] or activity["other"] or activity["sessions"]["power_titles"]
    report["note"] = None
    if has_work:
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


def check_only_additions(old, new, area, dk):
    """Raise unless `new` equals `old` apart from this tool's ticks and note block."""
    expected = copy.deepcopy(new)
    ours = f"{ENTRY_SOURCE}:{dk}:"
    old_entries = old.get("entries") or {}
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
    old_notes = old.get("dayNotes") or {}
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
    for key in ("entries", "dayNotes"):
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


def sync_day_live(ws, cfg, activity, dry_run):
    area = cfg["area"]
    for attempt in range(5):
        row = ws.fetch()
        old = row["state"]
        new, report = apply_day(old, activity, area, cfg)
        check_only_additions(old, new, area, activity["date"])
        if new == old or dry_run:
            report["written"] = False
            return report
        if ws.write_if_unchanged(new, row["updated_at"]):
            report["written"] = True
            report["verified"] = _contains_additions(ws.fetch()["state"], new, area, activity["date"])
            return report
    raise RuntimeError("The workspace kept changing while writing; gave up after 5 tries. Nothing lost - run again.")


def _contains_additions(after, new, area, dk):
    want = [e for e in ((new.get("entries") or {}).get(dk) or {}).get("actual", [])
            if str(e.get("id", "")).startswith(ENTRY_SOURCE)]
    have = ((after.get("entries") or {}).get(dk) or {}).get("actual", [])
    note = (((after.get("dayNotes") or {}).get(dk) or {}).get("area_notes") or {}).get(area) or {}
    return all(e in have for e in want) and BLOCK_START in note.get("text", "")


# ----------------------------------------------------------------- commands

def days_to_run(explicit):
    if explicit:
        return [dt.date.fromisoformat(explicit)]
    today = dt.date.today()
    last = (read_json(LAST_RUN_FILE) or {}).get("last_complete_day")
    first = today
    if last:
        first = max(dt.date.fromisoformat(last), today - dt.timedelta(days=MAX_CATCHUP_DAYS))
    return [first + dt.timedelta(days=i) for i in range((today - first).days + 1)]


def print_report(report):
    print(f"\n{report['date']}: ticked {len(report['ticked'])}"
          + (", written" if report.get("written") else ", not written"))
    for key, label in (("ticked", "new tick"), ("already_ticked", "already ticked"),
                       ("deleted_before", "skipped, someone deleted the tick"),
                       ("other_area", "other person's area, note only"),
                       ("unmatched", "not in calendar AOP list, note only")):
        for name in report[key]:
            print(f"  {label}: {name}")
    if report.get("note"):
        print("\n" + report["note"])


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
    ws.login_password(cfg["email"], getpass.getpass("Calendar password (not stored): "))
    ws.fetch()
    print(f"Signed in and can read workspace {cfg['workspace_id']!r}. Config: {CONFIG_FILE}")


def cmd_collect(args):
    cfg = load_config() if not args.config else read_json(args.config)
    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    print(json.dumps(collect(cfg, day, fetch=not args.no_fetch), ensure_ascii=False, indent=2))


def cmd_preview(args):
    """Apply to a state file (an Export JSON from the app) without any network write."""
    cfg = read_json(args.config) if args.config else load_config()
    if args.area:
        cfg["area"] = args.area
    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    activity = read_json(args.activity) if args.activity else collect(cfg, day, fetch=not args.no_fetch)
    raw = read_json(args.state)
    old = raw.get("state", raw)
    new, report = apply_day(old, activity, cfg["area"], cfg)
    check_only_additions(old, new, cfg["area"], activity["date"])
    print_report(report)
    print("\nGuard: only additions - OK")
    if args.out:
        write_json(args.out, {**raw, "state": new} if "state" in raw else new)
        print(f"Wrote {args.out}")


def cmd_run(args):
    cfg = load_config()
    ws = Workspace(cfg)
    try:
        ws.login_refresh()
    except RuntimeError as e:
        raise RuntimeError(f"sign-in expired or was replaced ({e}). Run `setup` again.") from None
    days = days_to_run(args.date)
    for i, day in enumerate(days):
        activity = collect(cfg, day, fetch=(i == 0))
        for w in activity["warnings"]:
            print(f"warning: {w}")
        report = sync_day_live(ws, cfg, activity, args.dry_run)
        print_report(report)
        if report.get("written") and not report.get("verified"):
            print("warning: the write went through but a re-read did not show it; it will be retried next run")
    if not args.dry_run and not args.date:
        # Today stays open: the next run starts from today, so late work is picked up.
        write_json(LAST_RUN_FILE, {"last_complete_day": dt.date.today().isoformat(), "at": now_iso()})


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
    log = open(log_path, "a", encoding="utf-8")
    log.write(f"\n=== {dt.datetime.now().isoformat(timespec='seconds')} {' '.join(sys.argv[1:])}\n")
    sys.stdout = _Tee(sys.stdout, log)
    sys.stderr = _Tee(sys.stderr, log)


def main():
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        open_log()
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="one-time: workspace, area, git emails, sign in").set_defaults(fn=cmd_setup)
    c = sub.add_parser("collect", help="print a day's activity as JSON (no calendar access)")
    c.add_argument("--date")
    c.add_argument("--config", help="config file to use instead of the saved one")
    c.add_argument("--no-fetch", action="store_true")
    c.set_defaults(fn=cmd_collect)
    v = sub.add_parser("preview", help="apply a day to an exported state file, offline")
    v.add_argument("--state", required=True, help="Export JSON from the calendar")
    v.add_argument("--date")
    v.add_argument("--area")
    v.add_argument("--config")
    v.add_argument("--activity", help="activity JSON from `collect` instead of collecting now")
    v.add_argument("--out")
    v.add_argument("--no-fetch", action="store_true")
    v.set_defaults(fn=cmd_preview)
    r = sub.add_parser("run", help="sync today (and missed days) into the shared calendar")
    r.add_argument("--date", help="only this day")
    r.add_argument("--dry-run", action="store_true", help="read the live calendar, write nothing")
    r.set_defaults(fn=cmd_run)
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
