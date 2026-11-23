"""Desktop chat sync: list every Claude desktop app chat under every account.

The desktop app keeps one small JSON record per Code chat, in a folder per
account and organization:

    ~/Library/Application Support/Claude/claude-code-sessions/<account>/<org>/local_<id>.json

The conversation itself is the Claude Code transcript <cliSessionId>.jsonl under
~/.claude/projects, which every account shares. So after the app switches to
another account its sidebar looks empty, though nothing is lost. This module
copies the records between the account folders, so every account lists every
chat: the union of all chats, and when copies differ the freshest one wins.

It also mirrors the desktop scheduled tasks: the list of the account the app is
signed into is copied into every other account's scheduled-tasks.json, so the
same tasks are there after a switch (the rules are in desktop_tasks.py).

How it stays safe:
- It writes only local_<id>.json records, deleted_<id> tombstones and the
  scheduled-tasks.json of accounts the app is not signed into, each through a temp
  file and a rename, and never touches any other file in the app's folders.
- The folder of the account the running app is signed into only gets new records.
  The app holds the others in memory and would overwrite a change.
- Every record it replaces or retires goes to its own trash folder first, and the
  first run that writes saves a tarball of every account folder.
- A deleted chat stays deleted: its tombstone travels to the other folders.
- The signed-in account's scheduled-tasks.json is only read. A task list is saved
  to the state folder before it is replaced, and no task is ever deleted.

Entry points: cli() for `claude-accounts sync ...`, sync() returns a report,
sync_quietly() for the StopFailure hook, status_line() for `claude-accounts status`.
"""

import datetime as dt
import errno
import fcntl
import hashlib
import json
import math
import os
import plistlib
import re
import shlex
import stat
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

import claude_failover as cf
import desktop_backlog
import desktop_handover
import desktop_tasks

REPO = Path(__file__).resolve().parent
ACCOUNTS_SCRIPT = REPO / "bin" / "claude-accounts"
LABEL = "com.github.claude-failover.desktop-sync"
APP_EXECUTABLE = "/Claude.app/Contents/MacOS/Claude"   # how `ps` shows the running app

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
FOLDER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")   # account and org folder names
TRASH_NAME_RE = re.compile(r"^(.+)\.(\d+)(?:-(\d+))?$")    # <file>.<unix seconds>[-n]
MAX_RECORD_BYTES = 10 * 1024 * 1024    # the app itself skips bigger session files
MAX_TOMBSTONE_BYTES = 4096             # a tombstone holds one number
MAX_TASKS_BYTES = 10 * 1024 * 1024
TASKS_FILE = "scheduled-tasks.json"
TASKS_WRITTEN_FILE = "tasks-written.json"   # per account: a hash of the task list we wrote last
TASK_BACKUP_RE = re.compile(r"^(scheduled-tasks|backlog)\.(\d{8})-(\d{6})(?:-(\d+))?\.json$")
BACKLOG_DIR, BACKLOG_FILE = "backlog", "tasks.json"   # <account>/<org>/backlog/tasks.json
BACKLOG_SEEN_FILE = "backlog-seen.json"     # per account: the items its backlog held at the last sync
MAX_BACKLOG_BYTES = 32 * 1024 * 1024        # the app reads no bigger backlog file
TASK_BACKUP_DAYS = 30
TASK_BACKUP_KEEP = 5           # newest task-list backups kept per account, whatever their age
HANDOVER_HOURS = 48            # `sync handover` looks at chats that were active this recently
TAIL_BYTES = 512 * 1024        # how much of a transcript's end is read first
TAIL_MAX_BYTES = 32 * 1024 * 1024
PS_RE = re.compile(r"^\s*(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)\s+(.*)$")   # [[days-]hours:]minutes:seconds command
CLAUDE_COMMAND_RE = re.compile(r"(?:^|/)claude\s")
SESSION_FLAG_RE = re.compile(r"\s--(?:resume|session-id)[= ]([0-9A-Za-z-]{8,})")
ACTIVITY_FIELDS = ("lastActivityAt", "lastTurnEdgeAt", "latestUserFrameAt")
# claude.ai connectors and Remote Control belong to one account. A record without
# these fields makes the app use the account's own settings.
ACCOUNT_BOUND_FIELDS = ("bridgeSessionIds", "remoteMcpServersConfig", "mcpConnectorsAsked",
                        "mcpConnectorsAskedOut", "mcpToolsAskedOff")
LOCAL_TOOL_PREFIX = "local:"   # enabledMcpTools keys of machine-local servers: not account-bound
# Markers the app sets on a chat when it quits during a turn, so it can resume that
# turn at its next launch. They belong to the folder (the account) that ran the
# turn: a copy elsewhere would make another account resume the same turn again.
RUNTIME_FIELDS = ("interruptedByQuitAt", "interruptedUnseenResume", "interruptedAwaitingPerson",
                  "interruptedByQuitFor", "remoteControlLiveAtUpdateQuit", "armedWorkAtQuit")
MTIME_SLACK = 1.0              # seconds: file times this close count as equal
SEEN_FILE = "seen.json"        # per record: a hash of its content and when that last changed
TRASH_KEEP = 2                 # newest trashed copies kept per (account, file)
TRASH_DAYS = 30
MAX_ACTIONS = 2000             # actions listed in one report
TEMP_PREFIX = ".claude-failover-"
DEFAULT_INTERVAL = 10          # minutes between background syncs
MIN_INTERVAL = 2
WATCH_THROTTLE = 3             # seconds launchd waits between two runs of the background job
COUNTS = ("added", "added_needs_restart", "updated", "retired", "stale_in_running_app",
          "deleted_elsewhere_live", "skipped_no_transcript", "skipped_invalid",
          "skipped_unsafe", "skipped_changed")

USAGE = """usage: claude-accounts sync [--dry-run] [--quiet] [--json]
       claude-accounts sync install [--every MINUTES] | sync uninstall | sync status
       claude-accounts sync handover [--json] [--hours N]

  sync              copy desktop app chats so every account lists every chat, and
                    copy the signed-in account's scheduled tasks to the other accounts
  sync install      also sync in the background (every 10 min by default)
  sync uninstall    stop the background sync
  sync status       the background job, the last sync, and whether the
                    scheduled task lists match
  sync handover     the chats that were left in the middle of their work (cut
                    off by a sign-out, stopped by a usage limit) or wait for you"""


class Unsafe(Exception):
    """A file this module must never read or replace: a symlink, a special file, a
    file with more than one hard link, or one over the size limit."""


class Invalid(Exception):
    """A file that is not a chat record this module understands."""


class Changed(Exception):
    """A file changed between reading and writing (the app or the user was
    faster). The run leaves it alone and the next run looks again."""


# --------------------------------------------------------------------------
# Where things are
# --------------------------------------------------------------------------

def settings(cfg):
    """The config's "desktop_sync" block, {} when there is none."""
    block = cfg.get("desktop_sync") if isinstance(cfg, dict) else None
    return block if isinstance(block, dict) else {}


def desktop_dir(cfg):
    """The desktop app's data folder: $CLAUDE_FAILOVER_DESKTOP_DIR, else
    desktop_sync.app_data_dir in the config, else the app's standard place."""
    env = os.environ.get("CLAUDE_FAILOVER_DESKTOP_DIR")
    if env:
        return Path(os.path.expanduser(env))
    custom = settings(cfg).get("app_data_dir")
    if isinstance(custom, str) and custom:
        return Path(os.path.expanduser(custom))
    return Path(os.path.expanduser("~")) / "Library" / "Application Support" / "Claude"


def sync_dir():
    """Lock, last report, backups, trash and the background job's log."""
    return Path(cf.state_dir()) / "desktop-sync"


def plist_path():
    folder = os.environ.get("CLAUDE_FAILOVER_LAUNCH_AGENTS_DIR")
    folder = Path(os.path.expanduser(folder)) if folder else (
        Path(os.path.expanduser("~")) / "Library" / "LaunchAgents")
    return folder / (LABEL + ".plist")


def projects_folder(cfg):
    """The shared transcripts folder (normally ~/.claude/projects)."""
    try:
        return Path(os.path.expanduser(str(cf.projects_dir(cfg))))
    except (KeyError, TypeError):   # a config without default_config_dir
        return Path(os.path.expanduser(cfg.get("default_config_dir") or "~/.claude")) / "projects"


def app_running():
    """True while the Claude desktop app runs. $CLAUDE_FAILOVER_APP_RUNNING=1/0
    decides in tests. When ps fails the answer is True, the safe side: then no
    folder the app might hold gets more than new chats."""
    env = os.environ.get("CLAUDE_FAILOVER_APP_RUNNING")
    if env in ("0", "1"):
        return env == "1"
    try:
        out = subprocess.run(["/bin/ps", "-axo", "comm="], stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return True
    if out.returncode != 0:
        return True
    lines = out.stdout.decode("utf-8", "replace").splitlines()
    return any(line.rstrip().endswith(APP_EXECUTABLE) for line in lines)


def active_account_uuid(ddir):
    """The account the desktop app is signed into (or was, when it last ran)."""
    data = cf.read_json(Path(ddir) / "config.json", {})
    value = data.get("lastKnownAccountUuid") if isinstance(data, dict) else None
    return value if isinstance(value, str) and value else None


# --------------------------------------------------------------------------
# Which folder belongs to which account
# --------------------------------------------------------------------------

def own_oauth_account(acct):
    """oauthAccount from the account's own <config_dir>/.claude.json, or None.

    An account without its own folder uses the shared ~/.claude.json, which
    describes whichever account last logged in there. It can name the wrong
    account, so it is never used to find a folder."""
    cdir = acct.get("config_dir")
    if not isinstance(cdir, str) or not cdir:
        return None
    path = os.path.join(os.path.expanduser(cdir), ".claude.json")
    shared = os.path.join(os.path.expanduser("~"), ".claude.json")
    if os.path.realpath(path) == os.path.realpath(shared):
        return None
    data = cf.read_json(path, {})
    oauth = data.get("oauthAccount") if isinstance(data, dict) else None
    return oauth if isinstance(oauth, dict) else None


def is_real_dir(path):
    """A folder, and not a symlink to one: writes must land where we think."""
    try:
        return stat.S_ISDIR(os.lstat(str(path)).st_mode)
    except OSError:
        return False


def count_records(folder):
    try:
        with os.scandir(str(folder)) as it:
            return sum(1 for e in it if e.name.startswith("local_") and e.name.endswith(".json"))
    except OSError:
        return 0


def busiest_org(base):
    """(org folder name, None) or (None, why). The app also creates near-empty org
    folders, so the one holding the most chat records is the real one."""
    try:
        with os.scandir(str(base)) as it:
            orgs = [e.name for e in it
                    if FOLDER_NAME_RE.match(e.name) and e.is_dir(follow_symlinks=False)]
    except OSError as e:
        return None, "cannot read %s (%s)" % (tilde(base), why(e))
    if not orgs:
        return None, "no organization folder in %s" % tilde(base)
    if len(orgs) == 1:
        return orgs[0], None
    ranked = sorted(((count_records(Path(base) / o), o) for o in orgs), reverse=True)
    if ranked[0][0] == 0 or ranked[0][0] == ranked[1][0]:
        return None, ("several organization folders and none clearly holds the chats "
                      "- set desktop_org_uuid for this account")
    return ranked[0][1], None


def on_disk_name(parent, name):
    """`name` spelled the way the folder `parent` holds it, or None. Ids are
    compared without regard to letter case: on the Mac's usual file system
    "BBBB" opens the folder "bbbb", and every check that compares ids (which
    folder the running app uses, above all) must see the same spelling."""
    try:
        entries = os.listdir(str(parent))
    except OSError:
        return None
    if name in entries:
        return name
    same = [e for e in entries if e.lower() == name.lower()]
    return same[0] if len(same) == 1 else None


def resolve_account(acct, root):
    """((account uuid, org uuid), None) for one config account, or (None, why).
    Both ids come back spelled as the folders are on disk."""
    oauth = own_oauth_account(acct) or {}
    account = acct.get("desktop_account_uuid") or oauth.get("accountUuid")
    if not account:
        return None, ("no desktop_account_uuid in the config, and no own config folder "
                      "with a login to read it from")
    if not isinstance(account, str) or not FOLDER_NAME_RE.match(account):
        return None, "desktop_account_uuid is not a plain id"
    login_account = oauth.get("accountUuid")
    account = on_disk_name(root, account)
    base = Path(root) / account if account else None
    if base is None or not is_real_dir(base):
        return None, "the desktop app has no chat folder for this account yet (sign the app into it once)"
    org = acct.get("desktop_org_uuid")
    if org:
        if isinstance(org, str) and FOLDER_NAME_RE.match(org):
            org = on_disk_name(base, org)
            if org and is_real_dir(base / org):
                return (account, org), None
        return None, "the desktop_org_uuid folder does not exist"
    # The login's own organization, when it belongs to this account and exists.
    org = None
    if isinstance(login_account, str) and login_account.lower() == account.lower():
        org = oauth.get("organizationUuid")
    if isinstance(org, str) and FOLDER_NAME_RE.match(org):
        org = on_disk_name(base, org)
        if org and is_real_dir(base / org):
            return (account, org), None
    org, reason = busiest_org(base)
    return ((account, org), None) if org else (None, reason)


def resolve_folders(cfg, ddir):
    """([folder], [{"account", "reason"}]): one chat folder per config account.
    Two accounts that resolve to the same folder are one folder."""
    root = Path(ddir) / "claude-code-sessions"
    folders, skipped, seen = [], [], {}
    accounts = cfg.get("accounts") if isinstance(cfg.get("accounts"), list) else []
    for acct in accounts:
        if not isinstance(acct, dict) or not acct.get("name"):
            continue
        name = acct["name"]
        where, reason = resolve_account(acct, root)
        if where is None:
            skipped.append({"account": name, "reason": reason})
            continue
        if where in seen:
            skipped.append({"account": name, "reason": "same desktop folder as %s" % seen[where]})
            continue
        seen[where] = name
        folders.append({"account": name, "account_uuid": where[0], "org_uuid": where[1],
                        "path": root / where[0] / where[1], "live": False,
                        "records": {}, "tombstones": {}, "blocked": set()})
    return folders, skipped


# --------------------------------------------------------------------------
# Reading and writing one file safely
# --------------------------------------------------------------------------

def read_regular(path, limit, single_link=True):
    """(bytes, stat) of a plain file of at most `limit` bytes. Opened without
    following a symlink and checked on the open file, so nothing can swap it
    between the check and the read. Raises Unsafe for anything else."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EMLINK):
            raise Unsafe("a symbolic link")
        raise
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise Unsafe("not a regular file")
        if single_link and st.st_nlink > 1:
            raise Unsafe("more than one hard link")   # the app refuses those too
        if st.st_size > limit:
            raise Unsafe("over %d bytes" % limit)
        data = f.read(limit + 1)
    if len(data) > limit:
        raise Unsafe("over %d bytes" % limit)
    return data, st


def _no_constant(name):
    # Python's json takes NaN and Infinity; the app's JSON parser does not.
    raise ValueError("%s is not valid JSON" % name)


def load_record(path, sid):
    """(raw bytes, record dict, stat) of local_<sid>.json. Raises Unsafe or Invalid."""
    data, st = read_regular(path, MAX_RECORD_BYTES)
    try:
        record = json.loads(data, parse_constant=_no_constant)
    except (ValueError, RecursionError):
        raise Invalid("not JSON")
    if not isinstance(record, dict) or record.get("sessionId") != "local_" + sid:
        raise Invalid("not a record for this file name")
    return data, record, st


def encode_record(record):
    """Compact JSON, like the app writes. allow_nan=False because the app's parser
    would reject NaN or Infinity (ValueError is raised instead)."""
    text = json.dumps(record, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    # Half an emoji (a lone surrogate) has no UTF-8 form. backslashreplace writes
    # just that one character as \udXXX, a valid JSON escape (it can only sit
    # inside a string), and leaves all other text as plain UTF-8. Escaping every
    # non-ASCII character instead could double a record's size.
    return text.encode("utf-8", "backslashreplace")


def ident(st):
    """What a file was when we read it. Any write changes one of these."""
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def current_ident(path):
    try:
        return ident(os.lstat(str(path)))
    except FileNotFoundError:
        return None


def write_file(folder, name, data, times=None, expect=None, mode=None):
    """Put `data` at folder/name through a temp file, fsync and a rename, so the
    app never sees half a file. `times` = (atime_ns, mtime_ns) for the new file.
    `expect` = ident() the current file must still have, or None when it must
    not exist yet; otherwise Changed is raised and nothing is replaced. `mode` =
    the new file's permission bits (0600 when not given)."""
    target = Path(folder) / name
    fd, tmp = tempfile.mkstemp(dir=str(folder), prefix=TEMP_PREFIX)   # mode 0600
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        if times:
            # Set before the rename: the copy never looks newer than its source,
            # so the next run doesn't copy it back (no ping-pong).
            os.utime(tmp, ns=times)
        if current_ident(target) != expect:
            raise Changed(name)
        os.replace(tmp, str(target))
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)   # only our own temp file, after a failure


def private_dir(path):
    """mkdir -p, then 0700: these folders hold copies of chat records."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(str(path), 0o700)
    return path


# --------------------------------------------------------------------------
# What a folder holds
# --------------------------------------------------------------------------

def activity(record):
    """The newest turn time in a record (ms). A rename, an archive or a model
    change leaves it as it was, so ties are broken by when the content last
    changed (see content_key)."""
    best = 0
    for key in ACTIVITY_FIELDS:
        v = record.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
            best = max(best, v)
    return best


def content_key(record):
    """A hash of what a record says, leaving out what changes with no edit:
    lastFocusedAt (the app rewrites the file with a new one whenever a chat is
    opened), the account-bound fields and runtime markers (each folder keeps its
    own) and enabledMcpTools entries that are not local:. An enabledMcpTools with
    no local: entry counts as no field, the same rule as payload_for: a copy
    written without the field must not look edited when the app later saves the
    account's own connector entries into it. None if it can't be hashed. Two
    copies with the same key say the same thing."""
    data = {}
    for key, value in record.items():
        if key == "lastFocusedAt" or key in ACCOUNT_BOUND_FIELDS or key in RUNTIME_FIELDS:
            continue
        if key == "enabledMcpTools":
            value = ({k: v for k, v in value.items() if k.startswith(LOCAL_TOOL_PREFIX)}
                     if isinstance(value, dict) else {})
            if not value:
                continue
        data[key] = value
    try:
        text = json.dumps(data, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        return None
    return hashlib.sha256(text.encode("utf-8", "backslashreplace")).hexdigest()


def seen_id(folder, sid):
    """The key of one record in seen.json."""
    return "%s/%s/%s" % (folder["account_uuid"], folder["org_uuid"], sid)


def load_seen(sdir):
    """seen.json: {seen_id: [content_key, unix time the content last changed]}.
    {} when there is none yet (the first run then goes by file times)."""
    data = cf.read_json(Path(sdir) / SEEN_FILE, {})
    return data if isinstance(data, dict) else {}


def changed_at(seen, sid_key, key, st):
    """When this record's content last changed. A file time alone is not enough:
    opening a chat rewrites its file (lastFocusedAt), and an old copy that was
    only looked at must not win over a real edit elsewhere. So while the content
    is the one seen.json remembers, the remembered time stands."""
    old = seen.get(sid_key)
    if (key is not None and isinstance(old, list) and len(old) == 2 and old[0] == key
            and isinstance(old[1], (int, float)) and not isinstance(old[1], bool)
            and math.isfinite(old[1])):
        return old[1]
    return st.st_mtime


def scan_folder(folder, report, seen=None):
    """Fill folder["records"] (id -> what we need to decide), ["tombstones"]
    (id -> its bytes) and ["blocked"] (ids with a file this module must never
    touch: a link, a hard link, too big, or not a valid record). False when the
    folder can't be listed: it then takes no part in the run, since an unreadable
    folder would look empty and get every chat. `seen` is load_seen()."""
    try:
        with os.scandir(str(folder["path"])) as it:
            names = sorted(e.name for e in it)
    except OSError as e:
        report["errors"].append("cannot read %s: %s" % (tilde(folder["path"]), why(e)))
        return False
    for name in names:
        if name.startswith("local_") and name.endswith(".json"):
            sid, is_record = name[len("local_"):-len(".json")], True
        elif name.startswith("deleted_"):
            sid, is_record = name[len("deleted_"):], False
        else:
            continue   # every other file in the folder is not ours to read
        if not SESSION_ID_RE.match(sid):
            report["skipped_invalid"] += 1
            continue
        path = folder["path"] / name
        try:
            if is_record:
                _raw, record, st = load_record(path, sid)
                title, cli_id = record.get("title"), record.get("cliSessionId")
                key = content_key(record)
                folder["records"][sid] = {
                    "fresh": (activity(record), changed_at(seen or {}, seen_id(folder, sid), key, st)),
                    "key": key,
                    "title": title if isinstance(title, str) else None,
                    # The conversation is <cliSessionId>.jsonl; older records lack
                    # the field and use the id itself.
                    "transcript": cli_id if isinstance(cli_id, str) and cli_id else sid,
                    "ident": ident(st),
                    "times": (st.st_atime_ns, st.st_mtime_ns),
                }
            else:
                data, _st = read_regular(path, MAX_TOMBSTONE_BYTES, single_link=False)
                folder["tombstones"][sid] = data
        except FileNotFoundError:
            continue   # gone since the listing
        except Unsafe:
            report["skipped_unsafe"] += 1
            folder["blocked"].add(sid)
        except Invalid:
            report["skipped_invalid"] += 1
            folder["blocked"].add(sid)
        except OSError as e:
            report["errors"].append("cannot read %s: %s" % (tilde(path), why(e)))
            folder["blocked"].add(sid)
    return True


def transcript_index(cfg):
    """Names (without .jsonl) of every transcript in the shared projects folder:
    one listing per run instead of one search per chat. None if unreadable."""
    root = projects_folder(cfg)
    try:
        with os.scandir(str(root)) as it:
            subdirs = [e.path for e in it if e.is_dir()]
    except OSError:
        return None
    names = set()
    for d in subdirs:
        try:
            with os.scandir(d) as it:
                names.update(e.name[:-len(".jsonl")] for e in it
                             if e.name.endswith(".jsonl") and e.is_file())
        except OSError:
            continue
    return names


def inactive_tasks(folders, active):
    """Enabled desktop scheduled tasks of accounts the app is not signed into.
    The app runs only the signed-in account's tasks. Read only."""
    found = []
    for f in folders:
        if active is not None and f["account_uuid"].lower() == active.lower():
            continue
        try:
            data, _st = read_regular(f["path"] / "scheduled-tasks.json", MAX_TASKS_BYTES,
                                     single_link=False)
            doc = json.loads(data)
        except (Unsafe, OSError, ValueError, RecursionError):
            continue
        tasks = doc.get("scheduledTasks") if isinstance(doc, dict) else None
        for t in tasks if isinstance(tasks, list) else []:
            if isinstance(t, dict) and t.get("enabled") is True:
                label = t.get("displayName") or t.get("name") or t.get("id")
                if isinstance(label, str) and label:
                    found.append({"account": f["account"], "task": label})
    return found


# --------------------------------------------------------------------------
# Scheduled tasks
# --------------------------------------------------------------------------

def tasks_on(cfg):
    """Scheduled tasks are mirrored unless the config says
    "desktop_sync": {"scheduled_tasks": false}."""
    return settings(cfg).get("scheduled_tasks") is not False


def read_tasks(path):
    """(document, raw bytes, stat) of a scheduled-tasks.json. A missing file is
    ({"scheduledTasks": []}, None, None): the app reads that as no tasks. Raises
    Unsafe or desktop_tasks.NotATaskList for a file to leave alone."""
    try:
        data, st = read_regular(path, MAX_TASKS_BYTES)
    except FileNotFoundError:
        return {"scheduledTasks": []}, None, None
    return desktop_tasks.parse(data), data, st


def task_key(folder):
    return "%s/%s" % (folder["account_uuid"], folder["org_uuid"])


def written_key(folder, kind):
    """The key of one list in tasks-written.json."""
    return task_key(folder) + ("" if kind == "scheduled-tasks" else "#" + kind)


def backup_tasks(sdir, folder, raw, now, kind="scheduled-tasks"):
    """Save an account's task list as it is, before it is replaced: the first time
    each day, and whenever the file is not the one this tool wrote last (the app
    or a person changed it since). Returns the copy's path, or None when no copy
    was needed. `kind` names the list: "scheduled-tasks" or "backlog"."""
    bdir = private_dir(Path(sdir) / "task-backups")
    for part in (folder["account_uuid"], folder["org_uuid"]):
        bdir = private_dir(bdir / part)
    written = cf.read_json(Path(sdir) / TASKS_WRITTEN_FILE, {})
    ours = (isinstance(written, dict)
            and written.get(written_key(folder, kind)) == hashlib.sha256(raw).hexdigest())
    today = "%s.%s-" % (kind, now.strftime("%Y%m%d"))
    if ours and any(name.startswith(today) for name in os.listdir(str(bdir))):
        return None
    base = "%s.%s" % (kind, now.strftime("%Y%m%d-%H%M%S"))
    dest, n = bdir / (base + ".json"), 2
    while os.path.lexists(str(dest)):
        dest, n = bdir / ("%s-%d.json" % (base, n)), n + 1
    fd = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    return dest


def remember_tasks_write(sdir, folder, data, kind="scheduled-tasks"):
    """Note the hash of the list just written, so the next run can tell its own
    work from a change by the app or a person (see backup_tasks)."""
    path = Path(sdir) / TASKS_WRITTEN_FILE
    written = cf.read_json(path, {})
    written = written if isinstance(written, dict) else {}
    written[written_key(folder, kind)] = hashlib.sha256(data).hexdigest()
    cf.atomic_write_json(path, written)


def prune_task_backups(sdir, now=None):
    """Drop list backups older than TASK_BACKUP_DAYS, but keep the
    TASK_BACKUP_KEEP newest of every account and kind of list whatever their
    age. The date comes from the file name."""
    now = cf.now_local() if now is None else now
    oldest = (now - dt.timedelta(days=TASK_BACKUP_DAYS)).strftime("%Y%m%d")
    removed = 0
    for folder, _dirs, files in os.walk(str(Path(sdir) / "task-backups")):
        kinds = {}
        for name in files:
            m = TASK_BACKUP_RE.match(name)
            if m:
                kinds.setdefault(m.group(1), []).append(
                    (m.group(2), m.group(3), int(m.group(4) or 1), name))
        for copies in kinds.values():
            copies.sort(reverse=True)   # newest first: date, time, then the -2, -3 of one second
            for day, _time, _n, name in copies[TASK_BACKUP_KEEP:]:
                if day < oldest:
                    try:
                        os.unlink(os.path.join(folder, name))
                        removed += 1
                    except OSError:
                        pass
    return removed


def org_is_named(cfg, account):
    """True if the config itself says which organization folder the account uses."""
    accounts = cfg.get("accounts") if isinstance(cfg, dict) else None
    return any(isinstance(a, dict) and a.get("name") == account and a.get("desktop_org_uuid")
               for a in (accounts if isinstance(accounts, list) else []))


def tasks_in_another_org(cfg, folder, count=None):
    """True if another organization folder of the same account also holds tasks
    and the config does not say which folder the account uses. The app's config
    names the account only, so the folder we picked may not be the one the app
    reads: a stale list would be copied, or the copy would go where the app does
    not look. Empty lists do not count: the app leaves those behind when it
    switches accounts. `count` = how many entries an organization folder holds
    (scheduled tasks when not given)."""
    if org_is_named(cfg, folder["account"]):
        return False
    base = Path(folder["path"]).parent
    try:
        names = sorted(os.listdir(str(base)))
    except OSError:
        return False
    for name in names:
        if name == folder["org_uuid"] or not is_real_dir(base / name):
            continue
        try:
            if count is not None:
                held = count(base / name)
            else:
                held = len(read_tasks(base / name / TASKS_FILE)[0]["scheduledTasks"])
        except (Unsafe, desktop_tasks.NotATaskList, desktop_backlog.NotABacklog, OSError):
            return True   # cannot tell what it holds
        if held:
            return True
    return False


TWO_ORGS = ("it has tasks in more than one organization folder (name the one to use: "
            "desktop_org_uuid in the config)")


def mirror_tasks(cfg, folders, active, sdir, dry_run, errors):
    """Make every other account's scheduled-tasks.json say what the signed-in
    account's says, by the rules in desktop_tasks.plan. Returns the
    "scheduled_tasks" part of the report. The signed-in account's file is only
    read: the app holds it in memory and rewrites it. A dry run writes nothing."""
    out = {"on": tasks_on(cfg), "source": None, "tasks": 0, "note": None, "targets": []}
    if not out["on"]:
        out["note"] = "turned off in the config (desktop_sync.scheduled_tasks)"
        return out
    if len(folders) < 2:
        return out
    mine = [f for f in folders if same_id(f["account_uuid"], active)]
    if len(mine) != 1:
        out["note"] = ("cannot tell which account the app is signed in to" if active is None
                       else "the app is signed in to an account that is not in the config"
                       if not mine else
                       "the signed-in account has more than one chat folder in the config")
        return out
    src = mine[0]
    if tasks_in_another_org(cfg, src):
        out["note"] = "%s is signed in and %s" % (src["account"], TWO_ORGS)
        return out
    try:
        doc, _raw, _st = read_tasks(src["path"] / TASKS_FILE)
    except (Unsafe, desktop_tasks.NotATaskList, OSError) as e:
        out["note"] = "cannot read the task list of %s (%s)" % (src["account"], why(e))
        return out
    out["source"], out["tasks"] = src["account"], len(doc["scheduledTasks"])
    now_ms = time.time() * 1000
    for dst in folders:
        if dst is not src:
            out["targets"].append(
                _mirror_into(cfg, doc["scheduledTasks"], dst, now_ms, sdir, dry_run, errors))
    return out


def _mirror_into(cfg, tasks, dst, now_ms, sdir, dry_run, errors):
    """One other account: plan the changes and, unless it is a dry run, save a
    copy of its list and write the new one."""
    row = {"account": dst["account"], "changes": [], "only_there": [], "written": False,
           "backup": None, "note": None}
    if tasks_in_another_org(cfg, dst):
        row["note"] = TWO_ORGS
        return row
    try:
        doc, raw, st = read_tasks(dst["path"] / TASKS_FILE)
    except (Unsafe, desktop_tasks.NotATaskList, OSError) as e:
        row["note"] = "its task list cannot be read (%s)" % why(e)
        return row
    new_list, row["changes"], row["only_there"] = desktop_tasks.plan(
        tasks, doc["scheduledTasks"], now_ms)
    if not row["changes"] or dry_run:
        return row
    try:
        data = desktop_tasks.encode(dict(doc, scheduledTasks=new_list))
        if desktop_tasks.parse(data)["scheduledTasks"] != new_list:
            raise ValueError("the new list does not read back as written")
        if len(data) > MAX_TASKS_BYTES:
            raise ValueError("the new list would be over %d bytes" % MAX_TASKS_BYTES)
        if st is not None:
            backup = backup_tasks(sdir, dst, raw, cf.now_local())
            row["backup"] = str(backup) if backup else None
        write_file(dst["path"], TASKS_FILE, data, expect=ident(st) if st is not None else None,
                   mode=stat.S_IMODE(st.st_mode) if st is not None else None)
    except Changed:
        row["note"] = "its task list changed during the sync (the next run catches up)"
        return row
    except (OSError, ValueError, desktop_tasks.NotATaskList) as e:
        errors.append("could not update the scheduled tasks of %s: %s" % (dst["account"], why(e)))
        return row
    row["written"] = True
    try:
        remember_tasks_write(sdir, dst, data)
    except (OSError, TypeError, ValueError):
        pass   # the next run then makes one backup more than needed
    return row


def _field_text(field):
    """One changed field of a task, as text."""
    old, new = field["from"], field["to"]
    if any(v in ("(a list)", "(an object)") for v in (old, new)):
        return "%s changed" % field["field"]

    def show(v):
        return "none" if v is None else json.dumps(v) if isinstance(v, bool) else str(v)
    return "%s %s -> %s" % (field["field"], show(old), show(new))


def _change_text(change):
    """One planned or finished change to a task list, as text."""
    if change["action"] == "add":
        return "add - %s (%s)" % (change["name"], change["schedule"])
    verb = "mark as run" if change["action"] == "mark_run" else "update"
    return "%s - %s: %s" % (verb, change["name"],
                            ", ".join(_field_text(f) for f in change["fields"]))


def task_lines(r):
    """What `claude-accounts sync` prints about scheduled tasks."""
    t = r.get("scheduled_tasks") or {}
    if not t.get("source"):
        if t.get("on") and t.get("note") and r["inactive_scheduled_tasks"]:
            return ["Scheduled tasks - not copied: %s" % t["note"]]
        return []
    rows = t["targets"]
    if not t["tasks"] and not any(row["changes"] or row["only_there"] or row["note"]
                                  for row in rows):
        return []   # no account has a task: nothing to say
    dry = r["dry_run"]
    lines = ["Scheduled tasks (copied from %s, the signed-in account)%s" % (
        t["source"], " - dry run, nothing written" if dry else "")]
    for row in rows:
        who, changes = row["account"], row["changes"]
        if not changes and not row["note"]:
            lines.append("  %s: in sync - nothing to copy" % who)
        elif changes:
            done = row["written"]
            words = (("add", "added", "to add"), ("update", "updated", "to update"),
                     ("mark_run", "marked as run", "to mark as run"))
            parts = ["%d %s" % (n, past if done else todo) for action, past, todo in words
                     for n in [sum(1 for c in changes if c["action"] == action)] if n]
            lines.append("  %s: %s%s" % (who, ", ".join(parts),
                                         "" if done or dry else " - NOT written"))
            lines.extend("    %s" % _change_text(c) for c in changes)
        if row["note"]:
            lines.append("  %s: left alone - %s" % (who, row["note"]))
        live = [o["name"] for o in row["only_there"] if o["live"]]
        rest = len(row["only_there"]) - len(live)
        if live:
            lines.append("  %s: only there and still on - %s (kept: sync never deletes a task)"
                         % (who, ", ".join(live)))
        if rest:
            lines.append("  %s: only there, finished or off - %s (kept)"
                         % (who, plural(rest, "task")))
        if row["backup"]:
            lines.append("  %s: backup - its list as it was: %s" % (who, tilde(row["backup"])))
    return lines


def task_status_lines(cfg):
    """For `sync status`: do the scheduled task lists match right now? It reads
    the files and writes nothing. Never raises."""
    try:
        ddir = desktop_dir(cfg)
        folders, _skipped = resolve_folders(cfg, ddir)
        t = mirror_tasks(cfg, folders, active_account_uuid(ddir), None, True, [])
    except Exception:
        return []
    if not t["on"]:
        return ["  scheduled tasks - not copied (desktop_sync.scheduled_tasks is false in the config)"]
    if not t["source"]:
        return ["  scheduled tasks - not copied: %s" % t["note"]] if t["note"] else []
    lines = []
    for row in t["targets"]:
        who = row["account"]
        if row["note"]:
            lines.append("  scheduled tasks - %s left alone: %s" % (who, row["note"]))
            continue
        live = [o["name"] for o in row["only_there"] if o["live"]]
        rest = len(row["only_there"]) - len(live)
        if row["changes"]:
            lines.append("  scheduled tasks - %s is %s behind %s (signed in), the next sync copies "
                         "%s: %s" % (who, plural(len(row["changes"]), "change"), t["source"],
                                     "it" if len(row["changes"]) == 1 else "them",
                                     ", ".join(sorted({c["name"] for c in row["changes"]}))))
        elif not live:
            lines.append("  scheduled tasks - %s matches %s (signed in)%s" % (
                who, t["source"],
                ", plus %s only on %s that %s finished or off" % (
                    plural(rest, "task"), who, "is" if rest == 1 else "are") if rest else ""))
        if live:
            lines.append("  scheduled tasks - DIFFER: only on %s and still on: %s (sync never "
                         "deletes a task)" % (who, ", ".join(live)))
    return lines


# --------------------------------------------------------------------------
# The task backlog
# --------------------------------------------------------------------------

def backlog_on(cfg):
    """The backlog is mirrored unless the config says "desktop_sync": {"backlog": false}."""
    return settings(cfg).get("backlog") is not False


def read_backlog(folder_path):
    """(document, raw bytes, stat) of an account folder's backlog/tasks.json. A
    missing file is ({"version": 1, "items": []}, None, None). Raises Unsafe or
    desktop_backlog.NotABacklog for a file to leave alone."""
    try:
        data, st = read_regular(Path(folder_path) / BACKLOG_DIR / BACKLOG_FILE, MAX_BACKLOG_BYTES)
    except FileNotFoundError:
        return {"version": desktop_backlog.NEWEST_VERSION, "items": []}, None, None
    return desktop_backlog.parse(data), data, st


def backlog_count(folder_path):
    return len(read_backlog(folder_path)[0]["items"])


def mirror_backlog(cfg, folders, active, sdir, dry_run, errors):
    """Make every other account's task backlog hold what the signed-in account's
    holds, by the rules in desktop_backlog.plan. Returns the "backlog" part of
    the report. The signed-in account's file is only read. A dry run writes
    nothing."""
    out = {"on": backlog_on(cfg), "source": None, "items": 0, "note": None, "targets": []}
    if not out["on"]:
        out["note"] = "turned off in the config (desktop_sync.backlog)"
        return out
    mine = [f for f in folders if same_id(f["account_uuid"], active)]
    if len(folders) < 2 or len(mine) != 1:
        return out   # the scheduled-task lines say why nothing is copied
    src = mine[0]
    if tasks_in_another_org(cfg, src, backlog_count):
        out["note"] = "%s is signed in and has a backlog in more than one organization folder" % (
            src["account"])
        return out
    try:
        doc, raw, _st = read_backlog(src["path"])
    except (Unsafe, desktop_backlog.NotABacklog, OSError) as e:
        out["note"] = "cannot read the backlog of %s (%s)" % (src["account"], why(e))
        return out
    if raw is None:
        return out   # no backlog file yet: nothing to copy, and nothing known to be removed
    out["source"], out["items"] = src["account"], len(doc["items"])
    state = Path(sdir if sdir is not None else sync_dir()) / BACKLOG_SEEN_FILE
    seen = cf.read_json(state, {})
    seen = seen if isinstance(seen, dict) else {}
    before = seen.get(task_key(src))
    before = before if isinstance(before, list) else None
    now_ms = time.time() * 1000
    after = {task_key(src): desktop_backlog.keys(doc["items"])}
    for dst in folders:
        if dst is not src:
            row, left = _backlog_into(cfg, doc["items"], before, dst, now_ms, sdir, dry_run, errors)
            out["targets"].append(row)
            if left is not None:
                after[task_key(dst)] = left
    if not dry_run and any(seen.get(k) != v for k, v in after.items()):
        try:
            cf.atomic_write_json(state, dict(seen, **after))
        except (OSError, TypeError, ValueError) as e:
            errors.append("could not save %s: %s" % (tilde(state), why(e)))
    return out


def _backlog_into(cfg, items, before, dst, now_ms, sdir, dry_run, errors):
    """One other account: (its row of the report, the keys its list holds after
    this run or None when the list could not be read)."""
    row = {"account": dst["account"], "added": [], "removed": [], "only_there": 0,
           "written": False, "backup": None, "note": None}
    if tasks_in_another_org(cfg, dst, backlog_count):
        row["note"] = "it has a backlog in more than one organization folder"
        return row, None
    try:
        doc, raw, st = read_backlog(dst["path"])
    except (Unsafe, desktop_backlog.NotABacklog, OSError) as e:
        row["note"] = "its backlog cannot be read (%s)" % why(e)
        return row, None
    new_list, row["added"], row["removed"], row["only_there"] = desktop_backlog.plan(
        items, doc["items"], before, now_ms)
    old_keys = desktop_backlog.keys(doc["items"])
    if not (row["added"] or row["removed"]) or dry_run:
        return row, old_keys
    folder = dst["path"] / BACKLOG_DIR
    try:
        data = desktop_backlog.encode(dict(doc, items=new_list))
        if desktop_backlog.parse(data)["items"] != new_list:
            raise ValueError("the new list does not read back as written")
        if len(data) > MAX_BACKLOG_BYTES:
            raise ValueError("the new list would be over %d bytes" % MAX_BACKLOG_BYTES)
        if st is not None:
            backup = backup_tasks(sdir, dst, raw, cf.now_local(), "backlog")
            row["backup"] = str(backup) if backup else None
        elif not is_real_dir(folder):
            os.mkdir(str(folder), 0o700)   # the app makes this folder the same way
        write_file(folder, BACKLOG_FILE, data, expect=ident(st) if st is not None else None,
                   mode=stat.S_IMODE(st.st_mode) if st is not None else None)
    except Changed:
        row["note"] = "its backlog changed during the sync (the next run catches up)"
        return row, old_keys
    except (OSError, ValueError, desktop_backlog.NotABacklog) as e:
        errors.append("could not update the task backlog of %s: %s" % (dst["account"], why(e)))
        return row, old_keys
    row["written"] = True
    try:
        remember_tasks_write(sdir, dst, data, "backlog")
    except (OSError, TypeError, ValueError):
        pass   # the next run then makes one backup more than needed
    return row, desktop_backlog.keys(new_list)


def backlog_lines(r):
    """What `claude-accounts sync` prints about the task backlog: only changes and
    lists that were left alone."""
    b = r.get("backlog") or {}
    rows = [row for row in b.get("targets", []) if row["added"] or row["removed"] or row["note"]]
    if not b.get("source") or not rows:
        return ["Task backlog - not copied: %s" % b["note"]] if b.get("on") and b.get("note") else []
    dry = r["dry_run"]
    lines = ["Task backlog (copied from %s, the signed-in account)%s" % (
        b["source"], " - dry run, nothing written" if dry else "")]
    for row in rows:
        who, done = row["account"], row["written"]
        parts = []
        if row["added"]:
            parts.append("%d %s" % (len(row["added"]), "added" if done else "to add"))
        if row["removed"]:
            parts.append("%d %s" % (len(row["removed"]), "removed" if done else "to remove"))
        if parts:
            lines.append("  %s: %s%s" % (who, ", ".join(parts), "" if done or dry else " - NOT written"))
            lines.extend("    add - %s" % one_line(t, 100) for t in row["added"])
            lines.extend("    remove - %s (started or dropped on %s)" % (one_line(t, 100), b["source"])
                         for t in row["removed"])
        if row["note"]:
            lines.append("  %s: left alone - %s" % (who, row["note"]))
        if row["backup"]:
            lines.append("  %s: backup - its list as it was: %s" % (who, tilde(row["backup"])))
    return lines


def backlog_status_lines(cfg):
    """For `sync status`: do the task backlogs match right now? It reads the files
    and writes nothing. Never raises."""
    try:
        ddir = desktop_dir(cfg)
        folders, _skipped = resolve_folders(cfg, ddir)
        b = mirror_backlog(cfg, folders, active_account_uuid(ddir), None, True, [])
    except Exception:
        return []
    if not b["on"]:
        return ["  task backlog - not copied (desktop_sync.backlog is false in the config)"]
    if not b["source"]:
        return ["  task backlog - not copied: %s" % b["note"]] if b["note"] else []
    lines = []
    for row in b["targets"]:
        who, behind = row["account"], len(row["added"]) + len(row["removed"])
        extra = ", plus %s only on %s" % (plural(row["only_there"], "item"), who) \
            if row["only_there"] else ""
        if row["note"]:
            lines.append("  task backlog - %s left alone: %s" % (who, row["note"]))
        elif behind:
            lines.append("  task backlog - %s is %s behind %s (signed in), the next sync copies %s"
                         % (who, plural(behind, "change"), b["source"], "it" if behind == 1 else "them"))
        elif b["items"] or row["only_there"]:
            lines.append("  task backlog - %s matches %s (signed in, %s)%s" % (
                who, b["source"], plural(b["items"], "item"), extra))
    return lines


# --------------------------------------------------------------------------
# Chats to pick up
# --------------------------------------------------------------------------

def parse_ps(text, now):
    """(ids, starts) from the output of `ps -axo etime=,command=`: the session ids
    that running claude processes name (--resume or --session-id), and the start
    times (unix seconds) of the desktop app's chat processes that name none.
    A brand-new chat is one of those until the app restarts its process."""
    ids, starts = set(), []
    for line in text.splitlines():
        m = PS_RE.match(line)
        if not m or not CLAUDE_COMMAND_RE.search(m.group(5)):
            continue
        days, hours, minutes, seconds, command = m.groups()
        named = SESSION_FLAG_RE.findall(command)
        ids.update(named)
        if not named and "/claude-code/" in command and "--input-format" in command:
            elapsed = ((int(days or 0) * 24 + int(hours or 0)) * 60 + int(minutes)) * 60 + int(seconds)
            starts.append(now - elapsed)
    return ids, starts


def running_chats():
    """Which chats have a claude process right now, as parse_ps gives them. None
    when ps cannot say. $CLAUDE_FAILOVER_RUNNING_CHATS decides in tests: session
    ids separated by commas, empty for none."""
    env = os.environ.get("CLAUDE_FAILOVER_RUNNING_CHATS")
    if env is not None:
        return {s for s in env.split(",") if s}, []
    try:
        out = subprocess.run(["/bin/ps", "-axo", "etime=,command="], stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return parse_ps(out.stdout.decode("utf-8", "replace"), time.time())


def transcript_paths(cfg):
    """{session id: path of its transcript} for the shared projects folder, from
    one listing. {} if the folder cannot be read."""
    paths = {}
    try:
        with os.scandir(str(projects_folder(cfg))) as it:
            subdirs = [e.path for e in it if e.is_dir()]
    except OSError:
        return paths
    for d in subdirs:
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.name.endswith(".jsonl") and e.is_file():
                        paths[e.name[:-len(".jsonl")]] = e.path
        except OSError:
            continue
    return paths


def read_tail(path, nbytes):
    """(entries, whole): the JSON objects in the last `nbytes` of a transcript, in
    file order, and whether that was the whole file. A first line cut in half is
    dropped, and so is any line that is not JSON."""
    with open(str(path), "rb") as f:
        size = os.fstat(f.fileno()).st_size
        f.seek(max(0, size - nbytes))
        data = f.read(nbytes)
    lines = data.split(b"\n")
    if size > nbytes:
        lines = lines[1:]
    entries = []
    for line in lines:
        if line.strip():
            try:
                entries.append(json.loads(line))
            except (ValueError, RecursionError):
                pass
    return entries, size <= nbytes


def chat_state(path):
    """How a transcript ends (desktop_handover.tail_state). One big tool result
    can fill the first read, so a tail with no conversation entry is read again,
    four times as far back."""
    nbytes = TAIL_BYTES
    while True:
        entries, whole = read_tail(path, nbytes)
        state = desktop_handover.tail_state(entries)
        if state["state"] != desktop_handover.UNKNOWN or whole or nbytes >= TAIL_MAX_BYTES:
            return state
        nbytes *= 4


def one_line(text, limit):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def handover(cfg, hours=HANDOVER_HOURS):
    """The chats that were left in the middle of their work in the last `hours`,
    newest first, the ones that need a push before the ones that wait for the
    person. It only reads: chat records, the ends of transcripts and the process
    list. Returns {"hours", "note", "chats": [{"session", "cli_session", "title",
    "state", "at", "detail", "cwd", "scheduled_task"}]} where state is
      cut_off  it stopped in the middle of a turn and no process runs it any more
      failed   its turn ended with an error message ("why": limit, auth or error)
      waiting  its turn ended and it needs something from the person"""
    now = time.time()
    oldest = now - hours * 3600
    out = {"hours": hours, "note": None, "chats": []}
    folders, _skipped = resolve_folders(cfg, desktop_dir(cfg))
    newest = {}   # chat id -> its freshest record over every account folder
    for f in folders:
        try:
            with os.scandir(str(f["path"])) as it:
                names = [e.name for e in it
                         if e.name.startswith("local_") and e.name.endswith(".json")
                         and e.stat(follow_symlinks=False).st_mtime >= oldest]
        except OSError:
            continue
        for name in names:
            sid = name[len("local_"):-len(".json")]
            try:
                _raw, record, _st = load_record(f["path"] / name, sid)
            except (Unsafe, Invalid, OSError):
                continue
            if sid not in newest or activity(record) > activity(newest[sid]):
                newest[sid] = record
    running = running_chats()
    if running is None:
        out["note"] = "could not check which chats are still running, so none is listed as cut off"
    paths = transcript_paths(cfg)
    found = []
    for sid, record in newest.items():
        if record.get("isArchived") is True:
            continue
        cli_id = record.get("cliSessionId")
        cli_id = cli_id if isinstance(cli_id, str) and cli_id else sid
        path = paths.get(cli_id)
        if path is None:
            continue
        try:
            st = os.stat(path)
            if st.st_mtime < oldest:
                continue
            tail = chat_state(path)
        except OSError:
            continue
        state, detail, why_failed = None, tail["detail"], None
        if tail["state"] == desktop_handover.CUT_OFF:
            born = getattr(st, "st_birthtime", None)
            if running is not None and not desktop_handover.is_alive(cli_id, born, running):
                state = "cut_off"
        elif tail["state"] == desktop_handover.FAILED:
            state, why_failed = "failed", cf.classify(1, detail, "").get("kind", "error")
        elif tail["state"] == desktop_handover.DONE:
            detail = desktop_handover.waiting_on_person(record)
            state = "waiting" if detail else None
        if state is None:
            continue
        at = cf.parse_iso(tail["at"]) or dt.datetime.fromtimestamp(st.st_mtime, cf.LOCAL_TZ)
        title = record.get("title")
        task = record.get("scheduledTaskId")
        found.append({"session": "local_" + sid, "cli_session": cli_id,
                      "title": one_line(title, 70) if isinstance(title, str) and title.strip()
                      else "(no title)",
                      "state": state, "why": why_failed, "at": cf.iso(at.astimezone(cf.LOCAL_TZ)),
                      "detail": one_line(detail, 160),
                      "cwd": record.get("cwd") if isinstance(record.get("cwd"), str) else None,
                      "scheduled_task": task if isinstance(task, str) and task else None,
                      "_when": at.timestamp()})
    found.sort(key=lambda c: (c["state"] == "waiting", -c["_when"]))
    for chat in found:
        del chat["_when"]
    out["chats"] = found
    return out


def clock(stamp):
    """An ISO time as "1:03 AM" when it is today, else "Nov 22, 11:40 PM"."""
    at = cf.parse_iso(stamp)
    if at is None:
        return "?"
    at = at.astimezone(cf.LOCAL_TZ)
    hour = "%d:%02d %s" % (at.hour % 12 or 12, at.minute, "AM" if at.hour < 12 else "PM")
    return hour if at.date() == cf.now_local().date() else "%s, %s" % (at.strftime("%b %-d"), hour)


def chat_line(chat):
    """One chat of the hand-over list, as text."""
    text = chat["title"]
    if chat["scheduled_task"]:
        text += " [scheduled run of %s]" % chat["scheduled_task"]
    if chat["state"] == "cut_off":
        text += " - cut off mid-turn at %s" % clock(chat["at"])
        if chat["detail"]:
            text += " (last step: %s)" % one_line(chat["detail"], 70)
    elif chat["state"] == "failed":
        how = {"limit": "stopped at a usage limit", "auth": "stopped because the login ended"}.get(
            chat["why"], "stopped by an error")
        text += " - %s at %s" % (how, clock(chat["at"]))
        if chat["detail"]:
            text += ": %s" % one_line(chat["detail"], 80)
    else:
        text += " - waits for you since %s: %s" % (clock(chat["at"]), one_line(chat["detail"], 90))
    return text


def handover_lines(cfg):
    """The hand-over list as short text lines, the chats that need a push first.
    For `claude-accounts switch`, which prints them after a switch. It only
    reads, and never raises."""
    try:
        h = handover(cfg)
        lines = [chat_line(c) for c in h["chats"]]
        return lines + ([h["note"]] if h["note"] else [])
    except Exception:
        return []


def handover_status_lines(cfg):
    """For `sync status`: how many chats are there to pick up. Never raises."""
    try:
        chats = handover(cfg)["chats"]
    except Exception:
        return []
    stopped = sum(1 for c in chats if c["state"] != "waiting")
    waiting = len(chats) - stopped
    if not stopped and not waiting:
        return []
    parts = []
    if stopped:
        parts.append("%s stopped in the middle of %s work" % (plural(stopped, "chat"),
                                                             "its" if stopped == 1 else "their"))
    if waiting:
        parts.append("%s for you" % plural(waiting, "chat waits", "chats wait"))
    return ["  to pick up - %s (claude-accounts sync handover)" % ", ".join(parts)]


def cmd_handover(cfg, args):
    args = list(args)
    hours = _pop_option(args, "--hours")
    as_json = "--json" in args
    if any(a != "--json" for a in args) or hours == "" or (hours is not None and not hours.isdigit()):
        print(USAGE)
        return 2
    h = handover(cfg, int(hours) if hours else HANDOVER_HOURS)
    if as_json:
        print(json.dumps(h, indent=2))
        return 0
    lines = ["Chats to pick up (active in the last %s)" % plural(h["hours"], "hour")]
    groups = (("cut_off", "cut off mid-turn - the app was signed out or quit; they do not go on "
                          "by themselves"),
              ("failed", "stopped by a usage limit or an error"),
              ("waiting", "waiting for you"))
    for state, head in groups:
        chats = [c for c in h["chats"] if c["state"] == state]
        if chats:
            lines.append("  %s" % head)
            for c in chats:
                lines.append("    %s" % chat_line(c))
                lines.append("      %s" % c["session"])
    if h["note"]:
        lines.append("  note - %s" % h["note"])
    if not h["chats"]:
        lines.append("  nothing - no chat was left in the middle of its work")
    elif any(c["state"] != "waiting" for c in h["chats"]):
        lines.append('  to go on - open the chat and say "continue". A chat in the app can send '
                     "that to another chat for you.")
    print("\n".join(lines))
    return 0


# --------------------------------------------------------------------------
# Deciding and copying
# --------------------------------------------------------------------------

def newer(a, b):
    """True if freshness a = (activity, mtime) is strictly newer than b. Activity
    counts first. File times only break a tie (a rename or an archive changes
    the file, not the activity), and times within MTIME_SLACK count as equal."""
    if a[0] != b[0]:
        return a[0] > b[0]
    return a[1] - b[1] > MTIME_SLACK


def payload_for(record, same_account, old=None):
    """What to write into another folder. The runtime markers never travel, not
    even to another organization folder of the same account. Across accounts the
    account-bound fields are left out too. Either way the target's own values for
    them (from its old copy, when there is one) are kept: they belong to it."""
    out = {}
    for key, value in record.items():
        if key in RUNTIME_FIELDS:
            continue
        if not same_account and key in ACCOUNT_BOUND_FIELDS:
            continue
        if not same_account and key == "enabledMcpTools":
            value = ({k: v for k, v in value.items() if k.startswith(LOCAL_TOOL_PREFIX)}
                     if isinstance(value, dict) else {})
            if not value:
                continue
        out[key] = value
    if isinstance(old, dict):
        for key in RUNTIME_FIELDS:
            if key in old:
                out[key] = old[key]
    if isinstance(old, dict) and not same_account:
        for key in ACCOUNT_BOUND_FIELDS:
            if key in old:
                out[key] = old[key]
        tools = old.get("enabledMcpTools")
        theirs = ({k: v for k, v in tools.items() if not k.startswith(LOCAL_TOOL_PREFIX)}
                  if isinstance(tools, dict) else {})
        if theirs:
            merged = dict(out.get("enabledMcpTools") or {})
            merged.update(theirs)
            out["enabledMcpTools"] = merged
    return out


def record_name(sid):
    return "local_%s.json" % sid


class Run:
    """One sync run: decides what every folder needs, does it (unless it is a dry
    run) and counts everything in the report."""

    def __init__(self, report, sdir, folders, dry_run):
        self.r = report
        self.sdir = sdir
        self.folders = folders
        self.dry_run = dry_run
        self.now = time.time()
        self.backup_ok = None   # None = not checked yet, then True/False
        self.pairs = {}         # (from, to) -> counts, for the summary lines
        self.seen_written = {}  # seen_id -> [key, changed_at] of every copy written
        self.seen_gone = set()  # seen_ids of records moved to the trash

    # -- bookkeeping --

    def error(self, text):
        self.r["errors"].append(text)

    def done(self, action, sid, title, src, dst):
        key = (src["account"], dst["account"])
        pair = self.pairs.setdefault(key, {"from": key[0], "to": key[1],
                                           "added": 0, "updated": 0, "retired": 0})
        pair[{"add": "added", "update": "updated", "retire": "retired"}[action]] += 1
        if len(self.r["actions"]) < MAX_ACTIONS:
            self.r["actions"].append({"action": action, "session": "local_" + sid,
                                      "title": title, "from": src["account"],
                                      "to": dst["account"]})
        else:
            self.r["actions_truncated"] = True

    def may_write(self):
        """Every real write waits for the first-run backup. If the backup fails,
        this run writes nothing."""
        if self.dry_run:
            return True
        if self.backup_ok is None:
            try:
                path = make_backup(self.sdir, self.folders)
                self.r["backup"] = str(path) if path else None
                self.backup_ok = True
            except (OSError, tarfile.TarError, ValueError) as e:
                self.backup_ok = False
                self.error("could not save the backup of the chat folders (%s), so nothing "
                           "was changed" % why(e))
        return self.backup_ok

    def encode(self, payload, sid, dst):
        """The bytes to write, or None (after saying why) when they must not be."""
        try:
            data = encode_record(payload)
        except ValueError:
            self.error("%s holds a number JSON cannot store, so it was not copied" % record_name(sid))
            return None
        if len(data) > MAX_RECORD_BYTES:
            # The target keeps its own account fields, so a copy can come out bigger
            # than its source. The app skips files over the limit: the chat would
            # vanish from that account's list.
            self.error("%s would be over 10 MiB in %s, where the app would skip it, so it "
                       "was not copied" % (record_name(sid), dst["account"]))
            return None
        return data

    def remember(self, dst, sid, payload, info):
        """A copy was written: it says what its source says, as of the source's
        last content change."""
        self.seen_written[seen_id(dst, sid)] = [content_key(payload), info["fresh"][1]]

    def trash_dest(self, folder, name):
        """trash/<accountUuid>/<file>.<unix seconds>, never over an earlier copy."""
        d = private_dir(private_dir(self.sdir / "trash") / folder["account_uuid"])
        dest = d / ("%s.%d" % (name, int(self.now)))
        n = 2
        while os.path.lexists(str(dest)):
            dest, n = d / ("%s.%d-%d" % (name, int(self.now), n)), n + 1
        return dest

    def trash_bytes(self, folder, name, data):
        dest = self.trash_dest(folder, name)
        fd = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())

    def trash_move(self, folder, path):
        """Move a record out of the app's folder into the trash."""
        dest = self.trash_dest(folder, path.name)
        try:
            os.rename(str(path), str(dest))
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise
            # The state folder is on another disk: copy, then remove the original.
            data, _st = read_regular(path, MAX_RECORD_BYTES)
            self.trash_bytes(folder, path.name, data)
            os.unlink(str(path))
            return
        try:
            os.chmod(str(dest), 0o600)
        except OSError:
            pass

    # -- the three actions --

    def add(self, sid, src, dst, record, info):
        payload = payload_for(record, src["account_uuid"] == dst["account_uuid"])
        data = self.encode(payload, sid, dst)
        if data is None or not self.may_write():
            return
        if not self.dry_run:
            try:
                write_file(dst["path"], record_name(sid), data, times=info["times"], expect=None)
            except Changed:
                self.r["skipped_changed"] += 1
                return
            except OSError as e:
                self.error("could not add %s to %s: %s" % (record_name(sid), dst["account"], why(e)))
                return
            self.remember(dst, sid, payload, info)
        self.r["added"] += 1
        if dst["live"]:
            # The app never touches a file it doesn't know, and lists it after a restart.
            self.r["added_needs_restart"] += 1
        self.done("add", sid, info["title"], src, dst)

    def update(self, sid, src, dst, record, info, mine):
        path = dst["path"] / record_name(sid)
        try:
            old_raw, old, st = load_record(path, sid)
        except (Unsafe, Invalid, FileNotFoundError):
            self.r["skipped_changed"] += 1
            return
        except OSError as e:
            self.error("cannot read %s: %s" % (tilde(path), why(e)))
            return
        if ident(st) != mine["ident"]:
            self.r["skipped_changed"] += 1
            return
        payload = payload_for(record, src["account_uuid"] == dst["account_uuid"], old)
        key = content_key(payload)
        if payload == old or (key is not None and key == content_key(old)):
            return   # it says the same already (maybe with another lastFocusedAt)
        if dst["live"]:
            # The running app holds this chat in memory and would write its own copy
            # back over ours. A later run updates it once the app quits or switches.
            self.r["stale_in_running_app"] += 1
            return
        data = self.encode(payload, sid, dst)
        if data is None or not self.may_write():
            return
        if not self.dry_run:
            try:
                self.trash_bytes(dst, record_name(sid), old_raw)
                write_file(dst["path"], record_name(sid), data, times=info["times"],
                           expect=ident(st))
            except Changed:
                self.r["skipped_changed"] += 1
                return
            except OSError as e:
                self.error("could not update %s in %s: %s" % (record_name(sid), dst["account"], why(e)))
                return
            self.remember(dst, sid, payload, info)
        self.r["updated"] += 1
        self.done("update", sid, info["title"], src, dst)

    def retire(self, sid, grave, holders):
        """The chat was deleted on grave's account: every other copy goes to the
        trash and gets the same tombstone, so the union never brings it back."""
        text = grave["tombstones"][sid]
        for dst in holders:
            if sid in dst["tombstones"]:
                continue   # deleted there too
            if dst["live"]:
                # The running app lists this chat. Moving its file away now would
                # not remove it there, and the app could write it back.
                self.r["deleted_elsewhere_live"] += 1
                continue
            info = dst["records"][sid]
            if not self.may_write():
                return
            if not self.dry_run:
                path = dst["path"] / record_name(sid)
                try:
                    if current_ident(path) != info["ident"]:
                        raise Changed(path.name)
                    self.trash_move(dst, path)
                except (Changed, Unsafe, FileNotFoundError):
                    self.r["skipped_changed"] += 1
                    continue
                except OSError as e:
                    self.error("could not move %s in %s to the trash: %s"
                               % (path.name, dst["account"], why(e)))
                    continue
                self.seen_gone.add(seen_id(dst, sid))
                try:
                    write_file(dst["path"], "deleted_" + sid, text, expect=None)
                except Changed:
                    pass   # a tombstone appeared meanwhile: the same result
                except OSError as e:
                    self.error("moved %s in %s to the trash but could not write its tombstone: %s"
                               % (path.name, dst["account"], why(e)))
            self.r["retired"] += 1
            self.done("retire", sid, info["title"], grave, dst)

    # -- one chat --

    def session(self, sid, transcripts):
        holders = [f for f in self.folders if sid in f["records"]]
        graves = [f for f in self.folders if sid in f["tombstones"]]
        if graves:
            self.retire(sid, graves[0], holders)
            return
        if not holders:
            return   # only files this module skips
        src = max(holders, key=lambda f: f["records"][sid]["fresh"])
        info = src["records"][sid]
        targets = [f for f in self.folders
                   if f is not src and sid not in f["blocked"]
                   and (sid not in f["records"] or newer(info["fresh"], f["records"][sid]["fresh"]))]
        if not targets:
            return
        if transcripts is None or info["transcript"] not in transcripts:
            # The record only points at the conversation. Without the transcript a
            # copy would be an empty chat, and the app removes the transcript first
            # when it deletes a chat.
            self.r["skipped_no_transcript"] += 1
            return
        try:
            _raw, record, st = load_record(src["path"] / record_name(sid), sid)
        except (Unsafe, Invalid, FileNotFoundError):
            self.r["skipped_changed"] += 1
            return
        except OSError as e:
            self.error("cannot read %s: %s" % (tilde(src["path"] / record_name(sid)), why(e)))
            return
        if ident(st) != info["ident"]:
            self.r["skipped_changed"] += 1   # it changed since the scan: next run
            return
        for dst in targets:
            if sid in dst["records"]:
                self.update(sid, src, dst, record, info, dst["records"][sid])
            else:
                self.add(sid, src, dst, record, info)


def make_backup(sdir, folders):
    """Before the first write ever: a tarball of every account folder's records and
    tombstones as they were. Returns its path, or None when one exists already."""
    bdir = private_dir(Path(sdir) / "backups")
    if any(n.endswith(".tar.gz") for n in os.listdir(str(bdir))):
        return None
    final = bdir / (cf.now_local().strftime("%Y%m%d-%H%M%S") + ".tar.gz")
    fd, tmp = tempfile.mkstemp(dir=str(bdir), prefix=".partial-")   # mode 0600
    try:
        with os.fdopen(fd, "wb") as out:
            with tarfile.open(fileobj=out, mode="w:gz") as tar:
                for f in folders:
                    for name in sorted(os.listdir(str(f["path"]))):
                        if (name.startswith("local_") and name.endswith(".json")) \
                                or name.startswith("deleted_"):
                            arcname = "%s/%s/%s" % (f["account_uuid"], f["org_uuid"], name)
                            _tar_add(tar, f["path"] / name, arcname)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, str(final))
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)
    return final


def _tar_add(tar, path, arcname):
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return   # a symlink, or gone: not part of the backup
    with os.fdopen(fd, "rb") as f:
        info = tar.gettarinfo(arcname=arcname, fileobj=f)
        if info.isreg():
            tar.addfile(info, f)
        elif info.islnk():
            tar.addfile(info)   # a second name for a file already in the tarball


def prune_trash(sdir, now=None):
    """Keep the TRASH_KEEP newest copies per (account, file) and drop copies older
    than TRASH_DAYS. Age comes from the name, since a trashed file keeps its old
    modification time. The only deleting this module does."""
    now = time.time() if now is None else now
    root = Path(sdir) / "trash"
    try:
        with os.scandir(str(root)) as it:
            accounts = [e.path for e in it if e.is_dir(follow_symlinks=False)]
    except OSError:
        return 0
    removed = 0
    for d in accounts:
        groups = {}
        try:
            with os.scandir(d) as it:
                for e in it:
                    m = TRASH_NAME_RE.match(e.name)
                    if m and e.is_file(follow_symlinks=False):
                        groups.setdefault(m.group(1), []).append(
                            (int(m.group(2)), int(m.group(3) or 1), e.path))
        except OSError:
            continue
        for copies in groups.values():
            copies.sort(reverse=True)   # newest first
            for i, (stamp, _n, path) in enumerate(copies):
                if i >= TRASH_KEEP or now - stamp > TRASH_DAYS * 86400:
                    try:
                        os.unlink(path)
                        removed += 1
                    except OSError:
                        pass
    return removed


# --------------------------------------------------------------------------
# One sync
# --------------------------------------------------------------------------

def new_report(dry_run):
    report = {"dry_run": bool(dry_run), "app_running": None, "active_account": None,
              "folders": [], "skipped_accounts": [], "chats": 0, "note": None}
    for key in COUNTS:
        report[key] = 0
    report.update({"errors": [], "backup": None, "inactive_scheduled_tasks": [],
                   "scheduled_tasks": None, "backlog": None, "by_pair": [], "actions": [],
                   "actions_truncated": False})
    return report


def sync(cfg, dry_run=False):
    """Copy chat records between the account folders and return a report.
    Expected problems go into report["errors"] instead of raising. A dry run
    decides exactly the same things and writes nothing anywhere."""
    if dry_run:
        return _run(cfg, None, dry_run=True)[0]
    sdir = sync_dir()
    try:
        private_dir(sdir)
        lock = open(str(sdir / ".lock"), "a+")
    except OSError as e:
        report = new_report(False)
        report["errors"].append("cannot use the state folder %s: %s" % (tilde(sdir), why(e)))
        return report
    with lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # The hook and the background job can start at the same moment.
            return {"skipped": "another sync is running"}
        report, seen = _run(cfg, sdir, dry_run=False)
        try:
            cf.atomic_write_json(sdir / SEEN_FILE, seen)
        except (OSError, TypeError, ValueError) as e:
            report["errors"].append("could not save %s: %s" % (tilde(sdir / SEEN_FILE), why(e)))
        prune_trash(sdir)
        prune_task_backups(sdir)
        _save_last(sdir, report)
        return report


def same_id(a, b):
    """Account ids compare without regard to letter case (see on_disk_name)."""
    return isinstance(a, str) and isinstance(b, str) and a.lower() == b.lower()


def _run(cfg, sdir, dry_run):
    """One run: (report, the seen.json to save). A dry run reads seen.json too, so
    it decides exactly what a real run would."""
    report = new_report(dry_run)
    ddir = desktop_dir(cfg)
    running = app_running()
    active = active_account_uuid(ddir)
    folders, report["skipped_accounts"] = resolve_folders(cfg, ddir)
    resolved = list(folders)   # the task step reads one named file, so it takes them all
    report["app_running"] = running
    for f in folders:
        # The running app holds its signed-in account's chats in memory. When we
        # can't tell which account that is, every folder is treated that way.
        f["live"] = running and (active is None or same_id(f["account_uuid"], active))
        if same_id(f["account_uuid"], active):
            report["active_account"] = f["account"]
    seen = load_seen(sdir if sdir is not None else sync_dir())
    folders = [f for f in folders if scan_folder(f, report, seen)]
    report["folders"] = [{"account": f["account"], "account_uuid": f["account_uuid"],
                          "org_uuid": f["org_uuid"], "path": str(f["path"]),
                          "chats": len(f["records"]), "live": f["live"]} for f in folders]
    deleted = set().union(*[f["tombstones"] for f in folders]) if folders else set()
    ids = set().union(*[f["records"] for f in folders]) if folders else set()
    report["chats"] = len(ids - deleted)
    try:
        tasks = mirror_tasks(cfg, resolved, active, sdir, dry_run, report["errors"])
    except Exception as e:   # a bug in this step must not stop the chats from syncing
        tasks = {"on": tasks_on(cfg), "source": None, "tasks": 0, "note": why(e), "targets": []}
        report["errors"].append("the scheduled tasks were not copied: %s" % why(e))
    report["scheduled_tasks"] = tasks
    try:
        report["backlog"] = mirror_backlog(cfg, resolved, active, sdir, dry_run, report["errors"])
    except Exception as e:   # like the tasks: never in the way of the chats
        report["backlog"] = {"on": backlog_on(cfg), "source": None, "items": 0, "note": why(e),
                             "targets": []}
        report["errors"].append("the task backlog was not copied: %s" % why(e))
    # Once the lists are mirrored, the task lines say what only another account has.
    report["inactive_scheduled_tasks"] = ([] if tasks["source"]
                                          else inactive_tasks(folders, active))
    if len(folders) < 2:
        if not is_real_dir(ddir / "claude-code-sessions"):
            report["note"] = "no desktop app chat folders in %s" % tilde(ddir)
        else:
            report["note"] = "found chat folders for %s, need at least 2" % plural(
                len(folders), "account")
        return report, next_seen(seen, folders, None)
    transcripts = transcript_index(cfg)
    if transcripts is None:
        report["errors"].append("cannot read the transcripts folder %s, so no chat was copied"
                                % tilde(projects_folder(cfg)))
    run = Run(report, sdir, folders, dry_run)
    for sid in sorted(ids | deleted):
        run.session(sid, transcripts)
    report["by_pair"] = list(run.pairs.values())
    return report, next_seen(seen, folders, run)


def next_seen(seen, folders, run):
    """seen.json after this run: every record now in a folder that took part, with
    the copies this run wrote, without the ones it moved to the trash. Entries
    of folders that took no part (an account skipped this time) are kept."""
    scanned = {"%s/%s/" % (f["account_uuid"], f["org_uuid"]) for f in folders}
    out = {k: v for k, v in seen.items()
           if isinstance(k, str) and not any(k.startswith(p) for p in scanned)}
    for f in folders:
        for sid, info in f["records"].items():
            if info["key"] is not None:
                out[seen_id(f, sid)] = [info["key"], info["fresh"][1]]
    if run is not None:
        out.update(run.seen_written)
        for k in run.seen_gone:
            out.pop(k, None)
    return out


def _save_last(sdir, report):
    """last.json feeds `sync status` and the status line. The event log gets a line
    only when something changed or failed: the background job runs every few
    minutes, and a line per quiet run would bury the handoff events."""
    data = {"at": cf.iso(cf.now_local()),
            "report": {k: v for k, v in report.items() if k != "actions"}}
    try:
        cf.atomic_write_json(sdir / "last.json", data)
    except (OSError, TypeError, ValueError) as e:
        report["errors"].append("could not save %s: %s" % (tilde(sdir / "last.json"), why(e)))
    if report["added"] or report["updated"] or report["retired"] or report["errors"]:
        fields = {k: report[k] for k in COUNTS}
        fields["errors"] = len(report["errors"])
        cf.log_event("desktop_sync", **fields)
    for row in (report.get("scheduled_tasks") or {}).get("targets", []):
        # A task that ran moves its run times every time: no line for that alone.
        news = [c["action"] for c in row["changes"] if not desktop_tasks.only_run_times(c)]
        if row["written"] and news:
            cf.log_event("desktop_task_sync", account=row["account"], added=news.count("add"),
                         updated=news.count("update"), marked_run=news.count("mark_run"))
    for row in (report.get("backlog") or {}).get("targets", []):
        if row["written"]:
            cf.log_event("desktop_backlog_sync", account=row["account"], added=len(row["added"]),
                         removed=len(row["removed"]))


def sync_quietly(cfg):
    """For the StopFailure hook: one sync, and never an exception."""
    try:
        sync(cfg)
    except (Exception, SystemExit):
        pass


# --------------------------------------------------------------------------
# Text for people
# --------------------------------------------------------------------------

def tilde(path):
    path, home = str(path), os.path.expanduser("~")
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def why(e):
    return getattr(e, "strerror", None) or str(e) or e.__class__.__name__


def plural(n, word, many=None):
    return "%d %s" % (n, word if n == 1 else (many or word + "s"))


def report_lines(r):
    """The plain text `claude-accounts sync` prints."""
    if "folders" not in r:
        return ["Desktop chats - skipped, %s" % r.get("skipped", "nothing to do")]
    dry, folders = r["dry_run"], r["folders"]
    if not r["app_running"]:
        head = "Desktop chats (app not running)"
    elif r["active_account"]:
        head = "Desktop chats (app running, signed in as %s)" % r["active_account"]
    elif folders and all(f["live"] for f in folders):
        head = "Desktop chats (app running, signed-in account unknown)"
    else:
        head = "Desktop chats (app running, signed in to an account not in the config)"
    lines = [head + (" - dry run, nothing written" if dry else "")]
    width = max([len(f["account"]) for f in folders] or [0])
    for f in folders:
        mark = "  (signed in)" if f["account"] == r["active_account"] else ""
        lines.append("  %s  %s%s" % (f["account"].ljust(width), plural(f["chats"], "chat"), mark))
    live = {f["account"] for f in folders if f["live"]}
    for p in r["by_pair"]:
        parts = []
        if p["added"]:
            parts.append("%d %s" % (p["added"], "to add" if dry else "added"))
        if p["updated"]:
            parts.append("%d %s" % (p["updated"], "to update" if dry else "updated"))
        if p["retired"]:
            parts.append("%d deleted %s" % (p["retired"], "to move to the trash" if dry
                                            else "moved to the trash"))
        line = "  %s -> %s: %s" % (p["from"], p["to"], ", ".join(parts))
        if p["added"] and p["to"] in live:
            line += " - they %s after you quit and reopen the Claude app" % (
                "would appear" if dry else "appear")
        lines.append(line)
    if r["stale_in_running_app"]:
        lines.append("  waiting - %s to update once the Claude app quits or switches accounts"
                     % plural(r["stale_in_running_app"], "chat"))
    if r["deleted_elsewhere_live"]:
        lines.append("  waiting - %s deleted on another account, to remove once the Claude app "
                     "quits or switches accounts" % plural(r["deleted_elsewhere_live"], "chat"))
    if r["skipped_no_transcript"]:
        lines.append("  skipped - %d with no transcript on disk" % r["skipped_no_transcript"])
    if r["skipped_unsafe"]:
        lines.append("  skipped - %s the app would refuse (a link, a hard link or over 10 MiB)"
                     % plural(r["skipped_unsafe"], "file"))
    if r["skipped_invalid"]:
        lines.append("  skipped - %s that %s not valid chat records" % (
            plural(r["skipped_invalid"], "file"), "is" if r["skipped_invalid"] == 1 else "are"))
    if r["skipped_changed"]:
        lines.append("  skipped - %s that changed during the sync (the next run catches up)"
                     % plural(r["skipped_changed"], "chat"))
    for s in r["skipped_accounts"]:
        lines.append("  skipped %s - %s" % (s["account"], s["reason"]))
    tasks = {}
    for t in r["inactive_scheduled_tasks"]:
        tasks.setdefault(t["account"], []).append(t["task"])
    for account, names in tasks.items():
        lines.append("  heads-up - %s only %s while the app is signed in as %s: %s" % (
            plural(len(names), "scheduled task"), "runs" if len(names) == 1 else "run",
            account, ", ".join(names)))
    if r["backup"]:
        lines.append("  backup - the chat folders as they were: %s" % tilde(r["backup"]))
    if r["note"]:
        lines.append("  nothing to do - %s" % r["note"])
    elif not r["by_pair"] and not r["errors"] and not r["stale_in_running_app"] \
            and not r["deleted_elsewhere_live"]:
        lines.append("  in sync - nothing to copy")
    lines.extend(task_lines(r))
    lines.extend(backlog_lines(r))
    for e in r["errors"]:
        lines.append("  error - %s" % e)
    return lines


def read_last():
    data = cf.read_json(sync_dir() / "last.json", None)
    if isinstance(data, dict) and isinstance(data.get("report"), dict):
        return data
    return None


def ago(at):
    try:
        then = dt.datetime.fromisoformat(at)
    except (TypeError, ValueError):
        return "at an unknown time"
    now = cf.now_local()
    if then.tzinfo is None:
        then = then.replace(tzinfo=now.tzinfo)
    secs = (now - then).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return "%d min ago" % (secs // 60)
    if secs < 2 * 86400:
        return "%d h ago" % (secs // 3600)
    return "%d days ago" % (secs // 86400)


def installed_job():
    """(minutes between background runs, the script the job runs) from the
    installed plist, or (None, None) when there is no usable one."""
    try:
        with open(str(plist_path()), "rb") as f:
            job = plistlib.load(f)
    except Exception:   # missing, unreadable or not a plist
        return None, None
    if not isinstance(job, dict):
        return None, None
    secs, args = job.get("StartInterval"), job.get("ProgramArguments")
    minutes = secs // 60 if isinstance(secs, int) and secs > 0 else None
    script = args[0] if isinstance(args, list) and args and isinstance(args[0], str) else None
    return minutes, script


def installed_interval():
    """Minutes between background runs, from the installed plist; None if none."""
    return installed_job()[0]


def watched_file(cfg):
    """The file whose change starts a background sync: the desktop app's own
    config.json, which the app saves when it is signed out or in."""
    return str(desktop_dir(cfg) / "config.json")


def job_watches(cfg):
    """True if the installed background job also runs when the app is signed out
    or in. A job installed by an older version only runs on its interval."""
    try:
        with open(str(plist_path()), "rb") as f:
            job = plistlib.load(f)
    except Exception:   # missing, unreadable or not a plist
        return False
    paths = job.get("WatchPaths") if isinstance(job, dict) else None
    return isinstance(paths, list) and watched_file(cfg) in paths


def job_is_broken(script):
    """True if the job's script is gone: this copy was moved or deleted after
    `sync install`, so launchd fails at every interval and nothing says so."""
    return not script or not os.path.exists(script)


def _num(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def status_line(cfg):
    """One line for `claude-accounts status`, from the last saved report (no scan).
    None when desktop sync is not enabled. Never raises."""
    try:
        if not settings(cfg).get("enabled"):
            return None
        minutes, script = installed_job()
        if not minutes:
            auto = "auto-sync off"
        elif job_is_broken(script):
            auto = "auto-sync broken (run: claude-accounts sync install)"
        else:
            auto = "auto-sync every %d min" % minutes
        last = read_last()
        if last is None:
            return "Desktop chats - not synced yet (claude-accounts sync), %s" % auto
        r, when = last["report"], ago(last.get("at"))
        accounts = len(r.get("folders") or [])
        chats = plural(_num(r.get("chats")), "chat")
        errors = r.get("errors") or []
        if errors:
            return "Desktop chats - last sync %s had %s (run: claude-accounts sync), %s" % (
                when, plural(len(errors), "error"), auto)
        if accounts < 2:
            return ("Desktop chats - fewer than 2 accounts have a chat folder (sign the desktop "
                    "app into each account once), last sync %s, %s" % (when, auto))
        waiting = _num(r.get("stale_in_running_app")) + _num(r.get("deleted_elsewhere_live"))
        if waiting:
            return ("Desktop chats - %d accounts, %s, %s waiting for the app to quit, "
                    "last sync %s, %s" % (accounts, chats, plural(waiting, "change"), when, auto))
        return "Desktop chats - %d accounts in sync (%s), last sync %s, %s" % (
            accounts, chats, when, auto)
    except Exception:
        return None


# --------------------------------------------------------------------------
# claude-accounts sync ...
# --------------------------------------------------------------------------

def cli(cfg, argv):
    args = list(argv)
    sub = args[0] if args else ""
    if sub in ("help", "-h", "--help"):
        print(USAGE)
        return 0
    if sub == "install":
        return cmd_install(cfg, args[1:])
    if sub == "handover":
        return cmd_handover(cfg, args[1:])
    if sub in ("uninstall", "status"):
        if len(args) != 1:
            print(USAGE)
            return 2
        return cmd_uninstall(cfg) if sub == "uninstall" else cmd_status(cfg)
    if any(a not in ("--dry-run", "--quiet", "--json") for a in args):
        print(USAGE)
        return 2
    report = sync(cfg, dry_run="--dry-run" in args)
    errors = report.get("errors") or []
    if "--quiet" in args and not errors:
        return 0   # the background job: its log only gets runs that went wrong
    if "--quiet" in args:
        print("claude-accounts sync at %s" % cf.iso(cf.now_local()))
    if "--json" in args:
        print(json.dumps(report, indent=2, default=str))
    else:
        print("\n".join(report_lines(report)))
    return 1 if errors else 0


def config_file(cfg):
    """The config file in use, or None."""
    path = cfg.get("_config_path") if isinstance(cfg, dict) else None
    if not path:
        finder = getattr(cf, "config_path", None)
        try:
            path = finder() if callable(finder) else None
        except Exception:
            path = None
    return os.path.abspath(os.path.expanduser(str(path))) if path else None


def _pop_option(args, name):
    """Take `--name VALUE` or `--name=VALUE` out of args: VALUE, None if absent,
    "" if it has no value."""
    for i, a in enumerate(args):
        if a == name:
            del args[i]
            return args.pop(i) if i < len(args) else ""
        if a.startswith(name + "="):
            del args[i]
            return a[len(name) + 1:]
    return None


def _write_bytes(path, data, mode):
    """Temp file next to `path`, fsync, chmod, rename: never half a file."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, str(path))
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)


def _backup_copy(path):
    """<file>.bak-<YYYYmmdd-HHMMSS> next to the file, 0600, never over another."""
    base = "%s.bak-%s" % (path, cf.now_local().strftime("%Y%m%d-%H%M%S"))
    dest, n = base, 2
    while os.path.lexists(dest):
        dest, n = "%s-%d" % (base, n), n + 1
    with open(path, "rb") as src:
        data = src.read()
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return dest


def set_enabled(cfg, value):
    """Set desktop_sync.enabled in the config file on disk and change nothing else
    (the hook reads it to sync at handoff time). Returns the line to print."""
    snippet = '"desktop_sync": {"enabled": %s}' % json.dumps(value)
    path = config_file(cfg)
    if not path or not os.path.isfile(path):
        return "No config file found. Add this to your config: %s" % snippet
    real = os.path.realpath(path)   # a symlinked config: change its target, keep the link
    try:
        with open(real, "r", encoding="utf-8") as f:
            raw = json.load(f)   # the file as written: no defaults filled in
    except (OSError, ValueError) as e:
        return "Could not read %s (%s). Add this to it yourself: %s" % (tilde(path), why(e), snippet)
    block = raw.get("desktop_sync") if isinstance(raw, dict) else []
    block = {} if block is None else block
    if not isinstance(block, dict):
        return "%s has a shape this tool doesn't expect. Add this yourself: %s" % (tilde(path), snippet)
    if block.get("enabled") is value:
        return "desktop_sync.enabled is already %s in %s." % (json.dumps(value), tilde(path))
    raw["desktop_sync"] = dict(block, enabled=value)
    try:
        mode = stat.S_IMODE(os.stat(real).st_mode)
        backup = _backup_copy(real)
        _write_bytes(real, (json.dumps(raw, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
                     mode)
    except OSError as e:
        return "Could not update %s (%s). Add this to it yourself: %s" % (tilde(path), why(e), snippet)
    return "Set desktop_sync.enabled to %s in %s (old copy: %s)." % (
        json.dumps(value), tilde(path), tilde(backup))


def launchctl(*args):
    """(exit code, output). $CLAUDE_FAILOVER_LAUNCHCTL replaces /bin/launchctl
    (tests use a fake); "0" means don't call it."""
    exe = os.environ.get("CLAUDE_FAILOVER_LAUNCHCTL") or "/bin/launchctl"
    if exe == "0":
        return 0, ""
    try:
        p = subprocess.run([exe] + list(args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, why(e)
    return p.returncode, p.stdout.decode("utf-8", "replace").strip()


def cmd_install(cfg, args):
    args = list(args)
    every = _pop_option(args, "--every")
    if args or every == "":
        print(USAGE)
        return 2
    if every is None:
        minutes = settings(cfg).get("interval_minutes")
        minutes = max(MIN_INTERVAL, _num(minutes) or DEFAULT_INTERVAL)
    else:
        minutes = int(every) if every.isdigit() else 0
        if minutes < MIN_INTERVAL:
            print("--every takes whole minutes, at least %d." % MIN_INTERVAL)
            return 2
    if not os.access(str(ACCOUNTS_SCRIPT), os.X_OK):
        print("Cannot run %s, so nothing was installed." % ACCOUNTS_SCRIPT)
        return 1
    sdir = private_dir(sync_dir())
    log = str(sdir / "agent.log")
    job = {"Label": LABEL,
           "ProgramArguments": [str(ACCOUNTS_SCRIPT), "sync", "--quiet"],
           "StartInterval": minutes * 60,
           "RunAtLoad": True,
           # Also run whenever the app saves its own config file. It does when it is
           # signed out and when it is signed in, so the other account's folder is up
           # to date before the app reads it (docs/DESKTOP-SYNC.md, A sync at sign-out).
           "WatchPaths": [watched_file(cfg)],
           "ThrottleInterval": WATCH_THROTTLE,
           "StandardOutPath": log,
           "StandardErrorPath": log}
    env = {}
    path = config_file(cfg)
    if path:
        env["CLAUDE_FAILOVER_CONFIG"] = path   # launchd starts with almost no environment
    if os.environ.get("CLAUDE_FAILOVER_STATE_DIR"):
        # The job must use the same state folder as the shell that installed it.
        env["CLAUDE_FAILOVER_STATE_DIR"] = os.path.abspath(
            os.path.expanduser(os.environ["CLAUDE_FAILOVER_STATE_DIR"]))
    if env:
        job["EnvironmentVariables"] = env
    plist = plist_path()
    try:
        plist.parent.mkdir(parents=True, exist_ok=True)
        _write_bytes(plist, plistlib.dumps(job), 0o644)
    except OSError as e:
        print("Could not write %s: %s" % (tilde(plist), why(e)))
        return 1
    domain = "gui/%d" % os.getuid()
    launchctl("bootout", "%s/%s" % (domain, LABEL))   # an older copy, if loaded; may fail
    rc, out = launchctl("bootstrap", domain, str(plist))
    print("Background sync every %d min, and when the app is signed out or in: %s"
          % (minutes, tilde(plist)))
    print("Log: %s" % tilde(log))
    print(set_enabled(cfg, True))
    if rc != 0:
        print("launchctl could not load it (%s). It loads at your next login, or run: "
              "launchctl bootstrap %s %s" % (out or "exit %d" % rc, domain, shlex.quote(str(plist))))
        return 1
    return 0


def cmd_uninstall(cfg):
    launchctl("bootout", "gui/%d/%s" % (os.getuid(), LABEL))   # fails if not loaded: fine
    plist = plist_path()
    if os.path.lexists(str(plist)):
        try:
            os.unlink(str(plist))
        except OSError as e:
            print("Could not remove %s: %s" % (tilde(plist), why(e)))
            return 1
        print("Removed the background sync: %s" % tilde(plist))
    else:
        print("The background sync was not installed.")
    print(set_enabled(cfg, False))
    print("Chats already copied stay listed. They are the same chats, so that is harmless.")
    return 0


def cmd_status(cfg):
    lines = ["Desktop chat sync"]
    plist = plist_path()
    minutes, script = installed_job()
    rc = 0
    if minutes and job_is_broken(script):
        lines.append("  background job - BROKEN: it runs %s, which is missing (folder moved?) "
                     "- fix: claude-accounts sync install" % (tilde(script) if script else "nothing"))
        rc = 1
    elif minutes and os.path.realpath(script) != os.path.realpath(str(ACCOUNTS_SCRIPT)):
        lines.append("  background job - every %d min, from another copy: %s (%s)"
                     % (minutes, tilde(script), tilde(plist)))
    elif minutes and job_watches(cfg):
        lines.append("  background job - every %d min, and when the app is signed out or in (%s)"
                     % (minutes, tilde(plist)))
    elif minutes:
        lines.append("  background job - every %d min (%s) - run `claude-accounts sync install` "
                     "again to also sync when the app is signed out or in" % (minutes, tilde(plist)))
    elif os.path.lexists(str(plist)):
        lines.append("  background job - %s exists but is not a valid plist" % tilde(plist))
    else:
        lines.append("  background job - not installed (claude-accounts sync install)")
    lines.append("  sync at handoff - %s" % ("on" if settings(cfg).get("enabled") else "off"))
    last = read_last()
    if last:
        r = last["report"]
        lines.append("  last sync - %s (%s): %d added, %d updated, %d moved to the trash, %s" % (
            last.get("at"), ago(last.get("at")), _num(r.get("added")), _num(r.get("updated")),
            _num(r.get("retired")), plural(len(r.get("errors") or []), "error")))
        if _num(r.get("stale_in_running_app")) or _num(r.get("deleted_elsewhere_live")):
            lines.append("  waiting - %s for the Claude app to quit or switch accounts" % plural(
                _num(r.get("stale_in_running_app")) + _num(r.get("deleted_elsewhere_live")), "change"))
    else:
        lines.append("  last sync - never")
    lines.extend(task_status_lines(cfg))
    lines.extend(backlog_status_lines(cfg))
    lines.extend(handover_status_lines(cfg))
    lines.append("  log - %s" % tilde(sync_dir() / "agent.log"))
    print("\n".join(lines))
    return rc
