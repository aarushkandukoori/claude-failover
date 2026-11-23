#!/usr/bin/python3
"""account_switch.py - work with two (or more) Claude accounts as if they were one.

What takes over when an account runs out is the StopFailure hook in
claude_failover.py: a chat that hits its limit continues in a Terminal window on the
next account, and the app stays signed in where it is. This file only helps around
that. It never opens a window or signs the app out by itself (unless the config says
`"switch": {"at_limit": "app"}`):

  claude-accounts usage    how much of its week each account has used, and when it resets
  claude-accounts switch   by hand only: a guided switch of the desktop app itself. It
                           checks the other account, copies chats and scheduled tasks,
                           waits for the sign-in, then finishes the rest. Signing the app
                           out stops every chat that is working in it
  claude-accounts watch    one pass of the background watcher (a LaunchAgent runs it):
                           warns before the app's account runs out, keeps background jobs
                           on the account the app is not using (with switch.auto_order
                           on), and finishes a switch of the app it notices

Nothing here signs anything in or out, reads a token, or changes a file of the desktop
app. The usage numbers come from one tiny call per account (the call `claude-accounts
test` makes): Claude Code reports the account's limits with every answer.

The design is in docs/SWITCHING.md.
"""
import contextlib
import datetime as dt
import fcntl
import json
import os
import plistlib
import re
import shlex
import stat
import subprocess
import tempfile
import time
from pathlib import Path

import claude_failover as cf

REPO = Path(__file__).resolve().parent
ACCOUNTS_SCRIPT = REPO / "bin" / "claude-accounts"
LABEL = "com.github.claude-failover.account-watch"
STATE_FILE = "switch.json"
DEFAULT_INTERVAL = 10          # minutes between watcher passes
MIN_INTERVAL = 2
PROBE_MINUTES = 60             # how often an account's usage is read
PROBE_MINUTES_BUSY = 20        # the app's account, once it is past BUSY_AT
BUSY_AT = 0.75
STALE_MINUTES = 240            # an older reading decides nothing
TIGHT_MEMORY_MAX_AGE = 180     # minutes a reading may age while memory is tight
TIGHT_MEMORY_MAX_AGE_BUSY = 40 # the same for an account that is close to its limit
FULL_AT = 0.98                 # from here an account counts as out
WARN_AT = (90, 97)             # percent of the week: one notification each
EXPIRING_HOURS = 12            # an account this close to its reset is used first
EXPIRING_ROOM = 0.10
UNUSED_HOURS = 36              # "switch now, or this is lost" notice
UNUSED_ROOM = 0.50
RISKY_ROOM = 0.05              # extra usage on and this little left: jobs go elsewhere first
ASSIST_GAP_HOURS = 6           # at most one helper window in this long
LOG_MAX_BYTES = 512 * 1024     # the watcher's log is cut back to its tail past this
LOG_KEEP_BYTES = 128 * 1024
NOTIFIED_KEEP = 300
BACKUPS_KEEP = 10
EXTENSION_ID = "fcoeoabgfenejglbffodgkkbkcdhcgfn"   # Claude in Chrome
EXTENSION_KEY = b"accountUuid"
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
PROBE_PROMPT = "Reply with exactly: OK"

USAGE = """usage: claude-accounts usage [--fresh] [--json]
       claude-accounts switch [--to ACCOUNT] [--check] [--no-wait] [--wait MINUTES] [--force]
       claude-accounts watch [--quiet]
       claude-accounts watch install [--every MINUTES] | watch uninstall | watch status
  usage     how much of its week each account has used and when it resets
  switch    by hand: move the desktop app to the other account. Gets it ready, waits
            for you to sign the app in, then finishes the rest (--check only checks).
            Signing the app out stops every chat that is working in it. You rarely
            need this: a chat that hits its limit continues in Terminal by itself
  watch     one pass of the background watcher; `watch install` runs it every
            %d minutes""" % DEFAULT_INTERVAL


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def settings(cfg):
    """The config's "switch" block, {} when there is none."""
    block = cfg.get("switch") if isinstance(cfg, dict) else None
    return block if isinstance(block, dict) else {}


def at_limit(cfg):
    """What takes over when the app's account runs out.

    "terminal" (the default): the StopFailure hook alone. Each chat that hits the
    limit continues in a Terminal window on the next account and the app stays
    signed in where it is. The watcher only tells; it opens nothing.
    "app": the watcher also opens the guided switch of the app itself in Terminal.
    Signing the app out stops every chat that is working in it, so this is opt-in."""
    return "app" if settings(cfg).get("at_limit") == "app" else "terminal"


def handoff_problem(cfg):
    """Why a desktop chat that hits its limit is NOT continued in Terminal by the
    StopFailure hook, in a few words, or "" when it is: the hook is in the settings
    file the chats read, its script exists, and the config has not turned the
    hand-off of desktop chats off. Reads one small file, prints nothing, never
    raises. Not known counts as a problem."""
    try:
        handoff = cfg.get("handoff") if isinstance(cfg.get("handoff"), dict) else {}
        if not handoff.get("from_desktop", True):
            return "the hand-off of desktop chats is turned off (handoff.from_desktop in the config)"
        try:
            data = json.loads(cf.settings_file(cfg).read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        hooks = cf.our_hooks(data) if isinstance(data, dict) else []
        if not hooks:
            return "the hook is not installed (claude-accounts hook install)"
        if not any(os.path.exists(cf.hook_script_of(h.get("command")) or "") for h in hooks):
            return "the hook's script is missing (claude-accounts hook install)"
        return ""
    except Exception:
        return "the hook could not be checked (claude-accounts hook status)"


def handoff_armed(cfg):
    """True when a desktop chat that hits its limit is continued in Terminal."""
    return not handoff_problem(cfg)


def hook_target(cfg, app):
    """The account the StopFailure hook would hand a refused chat of `app` to, by the
    hook's own rule (the wrapper's saved limits and the account order, with an account
    on paid extra usage last; that is not always the account with the most room in the
    readings here), or None. Reads saved state only; never raises."""
    try:
        return cf.choose_handoff_target(cfg, app, None)[0]
    except (Exception, SystemExit):
        return None


def on_extra_usage(cfg, name):
    """True when the last reading says `name` has used its whole window and still
    answers, on paid extra usage. From saved readings only: no call is made and the
    Chrome extension is not read. Not known is False."""
    a = snapshot(cfg, extension={"state": "unknown"})["accounts"].get(name)
    return bool(a and a["spent"] and a["extra"] and not a["limited"])


def billing_accounts(cfg):
    """The names of the accounts that bill, or may start to before the next reading:
    extra usage is on, the account has not refused, and its last reading shows extra
    usage in use, its window used up, less than RISKY_ROOM of it left, or nothing that
    still counts (a reading too old to trust, or one that failed). An account like
    that never refuses, so one more job there can run past 100% and be billed.

    The wrapper asks this each time it picks an account (paid extra usage comes last,
    see claude_failover.included_usage_first), so it is one look at the saved
    readings: no call, no Chrome storage, and it never raises. An account that was
    never read, or anything else unknown, is not in the set."""
    try:
        acc = snapshot(cfg, extension={"state": "unknown"})["accounts"]
        return {n for n, a in acc.items() if a["extra"] and not a["limited"] and (
            a["spent"] or a["extra_in_use"] or a["room"] is None or a["room"] < RISKY_ROOM)}
    except Exception:
        return set()


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _int(value, default, low, high):
    if not _num(value):
        return default
    return max(low, min(high, int(value)))


def pct(x):
    return max(0, min(100, int(round(x * 100))))


def when(t):
    """Sat Nov 28, 11 PM"""
    if t is None:
        return "-"
    t = t.astimezone(cf.LOCAL_TZ)
    clock = t.strftime("%-I %p") if t.minute == 0 else t.strftime("%-I:%M %p")
    return "%s, %s" % (t.strftime("%a %b %-d"), clock)


def ago(t, now):
    if t is None:
        return "never"
    minutes = int((now - t).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 90:
        return "%d min ago" % minutes
    if minutes < 48 * 60:
        return "%d h ago" % (minutes // 60)
    return "%d days ago" % (minutes // 1440)


def hours_until(t, now):
    return None if t is None else (t - now).total_seconds() / 3600.0


def tilde(path):
    home = os.path.expanduser("~")
    path = str(path)
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def why(e):
    return getattr(e, "strerror", None) or str(e) or e.__class__.__name__


def state_path():
    return Path(cf.state_dir()) / STATE_FILE


def work_dir():
    """Lock, old config copies, the watcher's log and the folder test calls run in."""
    return Path(cf.state_dir()) / "switch"


def read_state():
    st = cf.read_json(state_path(), {})
    if not isinstance(st, dict):
        st = {}
    for key in ("usage", "notified"):
        if not isinstance(st.get(key), dict):
            st[key] = {}
    return st


@contextlib.contextmanager
def edit_state():
    """Lock, load, let the caller change it, save. The watcher, the hook and a
    `switch` in Terminal can all run at once."""
    d = Path(cf.state_dir())
    d.mkdir(parents=True, exist_ok=True)
    with open(str(d / "switch.lock"), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        st = read_state()
        yield st
        cf.atomic_write_json(state_path(), st)


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


def config_file(cfg):
    """The config file in use, or None."""
    path = cfg.get("_config_path") if isinstance(cfg, dict) else None
    if not path:
        try:
            path = cf.config_path()
        except Exception:
            path = None
    return os.path.abspath(os.path.expanduser(str(path))) if path else None


def _indent_of(text):
    """The indent the file uses (spaces before its first indented line), 2 if unclear."""
    for line in text.splitlines()[1:]:
        stripped = line.lstrip(" ")
        if stripped and len(line) != len(stripped):
            return max(1, min(8, len(line) - len(stripped)))
    return 2


def update_config(cfg, mutate):
    """Change the config file on disk. mutate(raw) edits the parsed file in place.
    The old file is copied to the state folder first. Returns (changed, problem)."""
    path = config_file(cfg)
    if not path or not os.path.isfile(path):
        return False, "no config file found"
    real = os.path.realpath(path)   # a symlinked config: change its target, keep the link
    d = Path(cf.state_dir())
    d.mkdir(parents=True, exist_ok=True)
    with open(str(d / "switch.lock"), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(real, "r", encoding="utf-8") as f:
                text = f.read()
            raw = json.loads(text)
        except (OSError, ValueError) as e:
            return False, "could not read %s (%s)" % (tilde(path), why(e))
        if not isinstance(raw, dict):
            return False, "%s has a shape this tool does not expect" % tilde(path)
        before = json.dumps(raw, sort_keys=True)
        mutate(raw)
        if json.dumps(raw, sort_keys=True) == before:
            return False, None
        if not os.access(real, os.W_OK):
            return False, "%s is read-only" % tilde(path)
        try:
            bdir = work_dir() / "config-backups"
            bdir.mkdir(parents=True, exist_ok=True)
            os.chmod(str(bdir), 0o700)
            stamp = cf.now_local().strftime("%Y%m%d-%H%M%S")
            dest, n = bdir / ("config.json.%s" % stamp), 2
            while dest.exists():
                dest, n = bdir / ("config.json.%s-%d" % (stamp, n)), n + 1
            _write_bytes(dest, text.encode("utf-8"), 0o600)
            for old in sorted(bdir.glob("config.json.*"))[:-BACKUPS_KEEP]:
                old.unlink()
            mode = stat.S_IMODE(os.stat(real).st_mode)
            new = json.dumps(raw, indent=_indent_of(text), ensure_ascii=False) + "\n"
            _write_bytes(real, new.encode("utf-8"), mode)
        except OSError as e:
            return False, "could not update %s (%s)" % (tilde(path), why(e))
    return True, None


# --------------------------------------------------------------------------
# Which account the desktop app and the Chrome extension are signed in to
# --------------------------------------------------------------------------

def desktop_dir(cfg):
    """The desktop app's data folder (the same rule as desktop_sync.desktop_dir)."""
    env = os.environ.get("CLAUDE_FAILOVER_DESKTOP_DIR")
    if env:
        return Path(os.path.expanduser(env))
    block = cfg.get("desktop_sync") if isinstance(cfg.get("desktop_sync"), dict) else {}
    custom = block.get("app_data_dir")
    if isinstance(custom, str) and custom:
        return Path(os.path.expanduser(custom))
    return Path(os.path.expanduser("~")) / "Library" / "Application Support" / "Claude"


def app_account_uuid(cfg):
    """The id of the account the desktop app is signed in to (or was, when it last
    ran). The app writes it when a sign-in completes and keeps it at sign-out."""
    data = cf.read_json(desktop_dir(cfg) / "config.json", {})
    value = data.get("lastKnownAccountUuid") if isinstance(data, dict) else None
    return value.lower() if isinstance(value, str) and value else None


def _own_oauth_uuid(acct):
    """accountUuid from the account's own <config_dir>/.claude.json, or None. The
    shared ~/.claude.json can name another account, so it is never used."""
    cdir = acct.get("config_dir")
    if not isinstance(cdir, str) or not cdir:
        return None
    path = os.path.join(os.path.expanduser(cdir), ".claude.json")
    shared = os.path.join(os.path.expanduser("~"), ".claude.json")
    if os.path.realpath(path) == os.path.realpath(shared):
        return None
    data = cf.read_json(path, {})
    oauth = data.get("oauthAccount") if isinstance(data, dict) else None
    value = oauth.get("accountUuid") if isinstance(oauth, dict) else None
    return value if isinstance(value, str) and value else None


def account_uuids(cfg):
    """account name -> its claude.ai account id, lower case."""
    out = {}
    for a in cfg.get("accounts") or []:
        value = a.get("desktop_account_uuid")
        if not (isinstance(value, str) and value):
            value = _own_oauth_uuid(a)
        if isinstance(value, str) and value:
            out[a["name"]] = value.lower()
    return out


def name_for_uuid(cfg, uuid):
    if not uuid:
        return None
    names = [name for name, value in account_uuids(cfg).items() if value == uuid.lower()]
    return names[0] if len(names) == 1 else None    # two folders on one login: not known


def app_account(cfg):
    """The configured account the desktop app is signed in to, or None."""
    return name_for_uuid(cfg, app_account_uuid(cfg))


def chrome_dir(cfg):
    env = os.environ.get("CLAUDE_FAILOVER_CHROME_DIR")
    if env:
        return Path(os.path.expanduser(env))
    custom = settings(cfg).get("chrome_dir")
    if isinstance(custom, str) and custom:
        return Path(os.path.expanduser(custom))
    return Path(os.path.expanduser("~")) / "Library" / "Application Support" / "Google" / "Chrome"


def _file_number(path):
    m = re.match(r"^(\d+)\.", path.name)
    return int(m.group(1)) if m else -1


# A small read-only LevelDB reader: just enough to look one key up without opening
# the database (Chrome holds its lock). It follows LevelDB's own rule, the version
# with the highest sequence number wins, over the files the MANIFEST lists as in
# use. Anything that does not parse raises _Unreadable, and the caller then answers
# "unknown" instead of guessing.

class _Unreadable(Exception):
    pass


LEVELDB_MAX_FILE = 64 * 1024 * 1024
LOG_BLOCK = 32768
TABLE_MAGIC = bytes.fromhex("57fb808b247547db")     # 0xdb4775248b80fb57, little-endian


def _varint(data, pos):
    result = shift = 0
    while True:
        if pos >= len(data) or shift > 63:
            raise _Unreadable("varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7f) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _take(data, pos, n):
    if n < 0 or pos + n > len(data):
        raise _Unreadable("short read")
    return data[pos:pos + n], pos + n


def _snappy(data):
    """Undo Snappy's raw block format (LevelDB compresses table blocks with it)."""
    size, pos = _varint(data, 0)
    if size > LEVELDB_MAX_FILE:
        raise _Unreadable("snappy size")
    out = bytearray()
    while pos < len(data):
        tag = data[pos]
        pos += 1
        kind, high = tag & 3, tag >> 2
        if kind == 0:                               # a run of literal bytes
            length = high + 1
            if high >= 60:
                raw, pos = _take(data, pos, high - 59)
                length = int.from_bytes(raw, "little") + 1
            raw, pos = _take(data, pos, length)
            out += raw
            continue
        if kind == 1:                               # a copy of bytes written earlier
            length = (high & 7) + 4
            raw, pos = _take(data, pos, 1)
            offset = ((tag >> 5) << 8) | raw[0]
        else:
            length = high + 1
            raw, pos = _take(data, pos, 2 if kind == 2 else 4)
            offset = int.from_bytes(raw, "little")
        if offset == 0 or offset > len(out) or len(out) + length > size:
            raise _Unreadable("snappy copy")
        start = len(out) - offset
        for i in range(length):                     # may overlap itself: byte by byte
            out.append(out[start + i])
    if len(out) != size:
        raise _Unreadable("snappy length")
    return bytes(out)


def _table_block(data, offset, size):
    raw, end = _take(data, offset, size)
    kind, _ = _take(data, end, 1)
    if kind == b"\x00":
        return raw
    if kind == b"\x01":
        return _snappy(raw)
    raise _Unreadable("block compression %d" % kind[0])


def _block_entries(block):
    """The (key, value) pairs of a table block, with prefix compression undone."""
    if len(block) < 8:
        raise _Unreadable("block")
    restarts = int.from_bytes(block[-4:], "little")
    limit = len(block) - 4 - 4 * restarts
    if restarts < 1 or limit < 0:
        raise _Unreadable("block restarts")
    pos, key = 0, b""
    while pos < limit:
        shared, pos = _varint(block, pos)
        fresh, pos = _varint(block, pos)
        value_len, pos = _varint(block, pos)
        if shared > len(key) or pos + fresh + value_len > limit:
            raise _Unreadable("block entry")
        key = key[:shared] + block[pos:pos + fresh]
        pos += fresh
        yield key, block[pos:pos + value_len]
        pos += value_len


def _table_versions(data, user_key):
    """Every version of user_key in one table file, as (sequence, kind, value).
    kind 1 = a value, 0 = a removal."""
    if len(data) < 48 or data[-8:] != TABLE_MAGIC:
        raise _Unreadable("table footer")
    footer = data[-48:]
    _, pos = _varint(footer, 0)
    _, pos = _varint(footer, pos)                   # the metaindex block: not needed
    index_offset, pos = _varint(footer, pos)
    index_size, pos = _varint(footer, pos)
    found, before = [], None
    for separator, handle in _block_entries(_table_block(data, index_offset, index_size)):
        last = separator[:-8] if len(separator) >= 8 else separator
        # A block holds the keys after the previous separator, up to its own.
        if last >= user_key and (before is None or before <= user_key):
            offset, hpos = _varint(handle, 0)
            size, hpos = _varint(handle, hpos)
            for key, value in _block_entries(_table_block(data, offset, size)):
                if len(key) >= 8 and key[:-8] == user_key:
                    trailer = int.from_bytes(key[-8:], "little")
                    found.append((trailer >> 8, trailer & 0xff, value))
        before = last
    return found


def _log_records(data):
    """The records of a LevelDB log file (write-ahead logs and the MANIFEST use this
    format): 32 KB blocks, each record split into fragments with a 7-byte header.
    A torn tail (Chrome is writing) ends the list; it is not an error."""
    records, pending, pos = [], None, 0
    while pos + 7 <= len(data):
        left = LOG_BLOCK - pos % LOG_BLOCK
        if left < 7:
            pos += left                             # a block's last bytes are padding
            continue
        length = int.from_bytes(data[pos + 4:pos + 6], "little")
        kind = data[pos + 6]
        if kind == 0 and length == 0:
            pos += left                             # zero fill up to the next block
            continue
        if kind not in (1, 2, 3, 4) or length > left - 7 or pos + 7 + length > len(data):
            if pos // LOG_BLOCK < (len(data) - 1) // LOG_BLOCK:
                raise _Unreadable("a damaged log record")   # later writes cannot be trusted
            break                                           # a torn tail: Chrome is writing
        chunk = data[pos + 7:pos + 7 + length]
        pos += 7 + length
        if kind == 1:
            records.append(chunk)
            pending = None
        elif kind == 2:
            pending = bytearray(chunk)
        elif pending is not None:
            pending += chunk
            if kind == 4:
                records.append(bytes(pending))
                pending = None
    return records


def _batch_versions(record, user_key):
    """Every write of user_key in one write batch, as (sequence, kind, value)."""
    if len(record) < 12:
        raise _Unreadable("write batch")
    sequence = int.from_bytes(record[:8], "little")
    count = int.from_bytes(record[8:12], "little")
    pos, found = 12, []
    for i in range(count):
        tag, pos = _take(record, pos, 1)
        size, pos = _varint(record, pos)
        key, pos = _take(record, pos, size)
        value = b""
        if tag == b"\x01":
            size, pos = _varint(record, pos)
            value, pos = _take(record, pos, size)
        elif tag != b"\x00":
            raise _Unreadable("write batch tag")
        if key == user_key:
            found.append((sequence + i, tag[0], value))
    return found


def _live_files(folder):
    """(the numbers of the table files in use, the number of the oldest log in use),
    from the MANIFEST that CURRENT names."""
    try:
        name = (folder / "CURRENT").read_text().strip()
        if not re.match(r"^MANIFEST-\d+$", name):
            raise _Unreadable("CURRENT")
        data = (folder / name).read_bytes()
    except (OSError, UnicodeDecodeError):
        raise _Unreadable("no MANIFEST")
    tables, log_number = set(), 0
    for record in _log_records(data):
        pos = 0
        while pos < len(record):
            tag, pos = _varint(record, pos)
            if tag == 1:                            # the comparator's name
                size, pos = _varint(record, pos)
                _, pos = _take(record, pos, size)
            elif tag in (2, 3, 4, 9):               # log number, next file, last sequence, old log
                value, pos = _varint(record, pos)
                if tag == 2:
                    log_number = value
            elif tag == 5:                          # a compaction pointer: level, key
                _, pos = _varint(record, pos)
                size, pos = _varint(record, pos)
                _, pos = _take(record, pos, size)
            elif tag == 6:                          # a table file was dropped: level, number
                _, pos = _varint(record, pos)
                number, pos = _varint(record, pos)
                tables.discard(number)
            elif tag == 7:                          # a table file was added
                _, pos = _varint(record, pos)
                number, pos = _varint(record, pos)
                _, pos = _varint(record, pos)       # its size
                for _ in range(2):                  # its smallest and largest key
                    size, pos = _varint(record, pos)
                    _, pos = _take(record, pos, size)
                tables.add(number)
            else:
                raise _Unreadable("MANIFEST tag %d" % tag)
    return tables, log_number


def _read_file(path):
    try:
        if path.stat().st_size > LEVELDB_MAX_FILE:
            raise _Unreadable("%s is too big" % path.name)
        return path.read_bytes()
    except OSError:
        raise _Unreadable("cannot read %s" % path.name)


def leveldb_get(folder, user_key):
    """The newest version of one key in a LevelDB folder: ("value", bytes),
    ("removed", None) or ("absent", None). Raises _Unreadable when a file that is
    part of the database does not parse."""
    tables, log_number = _live_files(folder)
    try:
        files = sorted(p for p in folder.iterdir() if p.is_file())
    except OSError:
        raise _Unreadable("cannot list the folder")
    best = None
    for path in files:
        number = _file_number(path)
        if path.suffix == ".log" and number >= log_number:
            versions = [v for record in _log_records(_read_file(path))
                        for v in _batch_versions(record, user_key)]
        elif path.suffix == ".ldb" and number in tables:
            versions = _table_versions(_read_file(path), user_key)
        else:
            continue                                # not part of the database any more
        for version in versions:
            if best is None or version[0] > best[0]:
                best = version
    missing = tables - {_file_number(p) for p in files if p.suffix == ".ldb"}
    if missing or _live_files(folder) != (tables, log_number):
        # Chrome flushed or compacted in between: the newest write may sit in a file
        # this pass did not read. Ask again later.
        raise _Unreadable("the database changed while it was read")
    if best is None:
        return "absent", None
    return ("value", best[2]) if best[1] == 1 else ("removed", None)


def _extension_uuid_in(folder):
    """The account id in one extension storage folder: an id, "" (not signed in),
    or None (could not be read for sure)."""
    try:
        state, value = leveldb_get(folder, EXTENSION_KEY)
    except (_Unreadable, IndexError, ValueError):
        return None
    if state != "value":
        return ""
    try:
        text = json.loads(value.decode("utf-8"))    # chrome.storage keeps values as JSON
    except (ValueError, UnicodeDecodeError):
        return None
    if isinstance(text, str) and UUID_RE.match(text):
        return text.lower()
    return "" if text in (None, "") else None


def extension_account(cfg):
    """Which account the Claude in Chrome extension is signed in to, read from the
    extension's own storage in the Chrome profile (read only).

    {"state": "ok", "account": name}   signed in to a configured account
    {"state": "other", "uuid": id}     signed in to an account that is not in the config
    {"state": "signed_out"}            the extension has no account
    {"state": "unknown"}               no storage found, it could not be read for sure,
                                       or two Chrome profiles with the extension do not
                                       agree (name one with switch.chrome_profile)

    This is not a documented interface. Whenever the storage does not look exactly
    as expected the answer is "unknown", and nothing is decided from it."""
    ext_id = settings(cfg).get("chrome_extension_id")
    ext_id = ext_id if isinstance(ext_id, str) and ext_id else EXTENSION_ID
    only = settings(cfg).get("chrome_profile")
    root = chrome_dir(cfg)
    try:
        profiles = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        profiles = []
    ids, unsure, signed_out = set(), False, False
    for profile in profiles:
        if isinstance(only, str) and only and profile.name != only:
            continue
        folder = profile / "Local Extension Settings" / ext_id
        if not folder.is_dir():
            continue
        value = _extension_uuid_in(folder)
        if value is None:
            unsure = True
        elif value == "":
            signed_out = True
        else:
            ids.add(value)
    # An answer only when every profile that has the extension gives the same one:
    # which profile is in use cannot be told from here.
    if unsure or len(ids) > 1 or (ids and signed_out) or not (ids or signed_out):
        return {"state": "unknown"}
    if not ids:
        return {"state": "signed_out"}
    uuid = ids.pop()
    name = name_for_uuid(cfg, uuid)
    if name:
        return {"state": "ok", "account": name, "uuid": uuid}
    return {"state": "other", "uuid": uuid}


def extension_installed(cfg):
    """True when some Chrome profile has the extension's storage folder at all."""
    ext_id = settings(cfg).get("chrome_extension_id")
    ext_id = ext_id if isinstance(ext_id, str) and ext_id else EXTENSION_ID
    try:
        return any((p / "Local Extension Settings" / ext_id).is_dir()
                   for p in chrome_dir(cfg).iterdir() if p.is_dir())
    except OSError:
        return False


# --------------------------------------------------------------------------
# Reading an account's usage
# --------------------------------------------------------------------------

def _window(w):
    util = w.get("utilization")
    util = max(0.0, min(1.0, float(util))) if _num(util) else None
    resets = w.get("resetsAt")
    resets_at = None
    if _num(resets) and resets > 0:
        resets_at = cf.iso(dt.datetime.fromtimestamp(resets, cf.LOCAL_TZ))
    status = w.get("status")
    return {"util": util, "resets_at": resets_at, "status": status if isinstance(status, str) else None}


def parse_rate_info(info):
    """What a rate_limit_event of Claude Code says about an account, or None.

    util            the fullest window's share that is used, 0.0 to 1.0 (None = not said)
    window          that window's name (seven_day, five_hour, ...)
    resets_at       when that window resets
    week_util, week_resets_at   the seven-day window, when it is reported
    status          allowed / allowed_warning / rejected
    extra           True when extra usage (billed) is turned on for the account
    extra_in_use    True when this very call ran on extra usage"""
    if not isinstance(info, dict):
        return None
    windows = {}
    unified = info.get("unifiedWindows")
    if isinstance(unified, dict):
        for name, w in unified.items():
            if isinstance(name, str) and isinstance(w, dict):
                windows[name] = _window(w)
    top = info.get("rateLimitType")
    if isinstance(top, str) and top and top not in windows:
        windows[top] = _window(info)
    status = info.get("status") if isinstance(info.get("status"), str) else None
    refused = [n for n in sorted(windows) if windows[n]["status"] == "rejected"]
    known = [n for n in sorted(windows) if windows[n]["util"] is not None]
    rejected = status == "rejected" or bool(refused)
    if refused:
        # Out until the last refused window resets.
        best = max(refused, key=lambda n: windows[n]["resets_at"] or "")
    elif rejected:
        best = top if top in windows else (known[0] if known else None)
    elif known:
        best = max(known, key=lambda n: (windows[n]["util"], windows[n]["resets_at"] or ""))
    else:
        return None
    week = windows.get("seven_day")
    out = {
        "util": 1.0 if rejected else windows[best]["util"],
        "window": best,
        "resets_at": windows[best]["resets_at"] if best else None,
        "status": status,
        "windows": windows,
        "extra": bool(info.get("overageEnabled")) or info.get("overageStatus") == "allowed",
        "extra_in_use": bool(info.get("isUsingOverage")),
    }
    if week:
        out["week_util"] = 1.0 if week["status"] == "rejected" else week["util"]
        out["week_resets_at"] = week["resets_at"]
    return out


def memory_tight(cfg=None):
    """True while the memory check reports memory in danger: then no test call is
    started unless the last reading is old. The check is the script the wrapper also
    asks before headless runs (~/Projects/memguard/memguard.py when it exists);
    $CLAUDE_FAILOVER_MEMGUARD or switch.memory_check name another one, and "0"
    turns the check off. No script = never tight."""
    script = os.environ.get("CLAUDE_FAILOVER_MEMGUARD")
    if script is None:
        script = settings(cfg).get("memory_check") if cfg else None
    if script is None or script is True:
        script = "~/Projects/memguard/memguard.py"
    if not isinstance(script, str) or script in ("", "0"):
        return False
    script = os.path.expanduser(script)
    if not os.path.exists(script):
        return False
    try:
        r = subprocess.run(["/usr/bin/python3", "-I", script, "wait", "--max", "1"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return b"waiting" in (r.stderr or b"")


def run_probe(cfg, name):
    """One tiny haiku call on an account, like `claude-accounts test`. Returns
    (verdict, rate_limit_info or None, the login's end date or None). Nothing is
    saved to the chat history and no MCP server is started."""
    if not cfg.get("real_claude"):
        return {"kind": "error", "text": "claude is not installed"}, None, None
    env = cf.child_env(cfg, name, managed=True)
    try:
        cf.ensure_profile_links(cfg, name)
    except Exception:
        pass
    cwd = work_dir() / "probe"
    try:
        cwd.mkdir(parents=True, exist_ok=True)
    except OSError:
        cwd = Path(os.path.expanduser("~"))
    cmd = [cfg["real_claude"], "-p", PROBE_PROMPT, "--model", "haiku",
           "--output-format", "stream-json", "--verbose",
           "--no-session-persistence", "--strict-mcp-config"]
    try:
        r = subprocess.run(cmd, env=env, cwd=str(cwd), capture_output=True, text=True,
                           timeout=180, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"kind": "error", "text": "timed out"}, None, None
    except OSError as e:
        return {"kind": "error", "text": why(e)}, None, None
    info, expiry = None, None
    for line in (r.stdout or "").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "rate_limit_event" and isinstance(ev.get("rate_limit_info"), dict):
            info = ev["rate_limit_info"]            # the last one is the newest
        if ev.get("type") == "result" and isinstance(ev.get("credential_expires_at"), str):
            expiry = ev["credential_expires_at"]
    return cf.classify(r.returncode, r.stdout, r.stderr), info, expiry


def probe(cfg, name, now=None):
    """Read one account's usage with a test call and return the record to keep."""
    verdict, info, expiry = run_probe(cfg, name)
    kind = verdict.get("kind")
    parsed = parse_rate_info(info)
    if kind == "limit" and not verdict.get("resets_at") and parsed and parsed.get("resets_at"):
        # The refusal named no reset time but the limits report did: without it the
        # wrapper would guess 30 minutes and ask again and again.
        verdict = dict(verdict, resets_at=cf.parse_iso(parsed["resets_at"]))
    if kind in ("ok", "limit", "auth"):
        try:
            cf.record_verdict(cfg, name, verdict, login_expires_at=expiry)
        except Exception:
            pass
    # Stamped after the wrapper's own record, so this reading is never taken for one
    # from before the limit it just found.
    now = now if (now is not None and now >= cf.now_local()) else cf.now_local()
    rec = {"at": cf.iso(now), "ok": kind in ("ok", "limit")}
    if parsed:
        rec.update(parsed)
    if kind == "limit" and not verdict.get("family"):
        rec["util"], rec["status"] = 1.0, "rejected"
        until = verdict.get("resets_at")
        if until is not None and until > now:
            rec["resets_at"] = cf.iso(until)
    elif kind not in ("ok", "limit"):
        rec["error"] = kind or "error"
        rec["text"] = (verdict.get("text") or "")[:200]
    if parsed and parsed.get("extra_in_use"):
        try:
            with cf.edit_state() as st:
                st["accounts"].setdefault(name, {})["overage_seen"] = cf.iso(now)
        except Exception:
            pass
    return rec


def _reading(rec, now, limit_seen=None):
    """(util or None, when it was read, when its window resets, still valid?).
    A reading from before the wrapper last saw this account hit a limit is not
    valid: the account ran out after it was taken."""
    if not isinstance(rec, dict):
        return None, None, None, False
    at = cf.parse_iso(rec.get("at"))
    util = rec.get("util") if _num(rec.get("util")) else None
    resets = cf.parse_iso(rec.get("resets_at"))
    if at is None or util is None:
        return None, at, resets, False
    if limit_seen is not None and limit_seen > at:
        return util, at, resets, False
    if resets is not None and resets <= now:
        return util, at, resets, False             # that window has reset since
    age = (now - at).total_seconds() / 60.0
    if util >= FULL_AT and resets is not None:
        return util, at, resets, age >= 0          # full stays full until the reset
    return util, at, resets, 0 <= age <= STALE_MINUTES


def probe_due(rec, now, every, limit_seen=None):
    """Why this account's usage should be read now, or None."""
    util, at, resets, valid = _reading(rec, now, limit_seen)
    if at is None:
        return "never read"
    if limit_seen is not None and limit_seen > at:
        return "a limit was seen since the reading"
    if resets is not None and resets <= now:
        return "its window has reset"
    if util is not None and util >= FULL_AT and resets is not None:
        return None                                # nothing changes before the reset
    limit = every
    if util is not None and util >= BUSY_AT:
        limit = min(every, PROBE_MINUTES_BUSY)     # close to its limit: look more often
    age = (now - at).total_seconds() / 60.0
    return "the reading is %d min old" % age if age >= limit or age < 0 else None


def refresh_usage(cfg, now=None, force=False, max_age=None, read=None):
    """Read the usage of every account that is due. force = read every account that
    can be read; max_age = minutes a reading may be old. An account that is known
    to be out until a reset is never read: the answer is known, and with extra usage
    turned on the call would be billed. Returns the names that were read."""
    now = now or cf.now_local()
    read = read or probe
    st, cst = read_state(), cf.read_state()
    every = _int(settings(cfg).get("probe_minutes"), PROBE_MINUTES, 5, 720)
    done, tight = [], None
    for a in cfg.get("accounts") or []:
        name = a["name"]
        if not cf.account_is_set_up(cfg, name):
            continue
        if cf.account_block(cst, name, now)[0] != "ok":
            continue        # limited: known. Login failing: the wrapper re-checks it.
        crec = cst["accounts"].get(name) if isinstance(cst.get("accounts"), dict) else None
        limit_seen = cf.parse_iso(crec.get("limit_seen")) if isinstance(crec, dict) else None
        rec = st["usage"].get(name)
        util, at, resets, valid = _reading(rec, now, limit_seen)
        due = probe_due(rec, now, every, limit_seen)
        if due is None and valid and util >= FULL_AT and resets is not None:
            continue        # out until its reset
        age = None if at is None else (now - at).total_seconds() / 60.0
        if due is None and not force and (max_age is None or age is None or age < max_age):
            continue
        if not force:
            if tight is None:
                tight = memory_tight(cfg)
            patience = TIGHT_MEMORY_MAX_AGE_BUSY if (valid and util >= BUSY_AT) else TIGHT_MEMORY_MAX_AGE
            if tight and valid and 0 <= age < patience:
                continue                # the reading it has still counts
        new = read(cfg, name, cf.now_local())
        with edit_state() as st2:
            old = st2["usage"].get(name)
            if isinstance(new, dict) and "extra" not in new and isinstance(old, dict) and old.get("extra"):
                # A reading that failed says nothing about extra usage. Keep knowing
                # that it is on: until the account can be read again it counts as one
                # that may bill, and goes last (billing_accounts).
                new["extra"] = True
            st2["usage"][name] = new
        done.append(name)
    return done


# --------------------------------------------------------------------------
# The picture: every account's state at one moment
# --------------------------------------------------------------------------

def snapshot(cfg, now=None, extension=None):
    """Everything the decisions below need, read once.

    Per account: `util`/`room` are about the fullest window (it decides whether the
    account can run anything now); `week_util`/`week_room` are about the seven-day
    window (every "of its week" text and threshold uses these)."""
    now = now or cf.now_local()
    st, cst = read_state(), cf.read_state()
    accounts = {}
    for a in cfg.get("accounts") or []:
        name = a["name"]
        set_up = cf.account_is_set_up(cfg, name)
        kind, until = cf.account_block(cst, name, now)
        rec = st["usage"].get(name) if isinstance(st["usage"].get(name), dict) else {}
        crec = cst["accounts"].get(name) if isinstance(cst.get("accounts"), dict) else None
        crec = crec if isinstance(crec, dict) else {}
        util, at, resets, valid = _reading(rec, now, cf.parse_iso(crec.get("limit_seen")))
        full = valid and util >= FULL_AT
        limited = kind == "limited"
        usable = set_up and kind != "auth_down"
        spent = limited or full
        room = 0.0 if spent else ((1.0 - util) if valid else None)
        window = rec.get("window") if isinstance(rec.get("window"), str) else None
        of_week = window in (None, "seven_day")
        week_resets = cf.parse_iso(rec.get("week_resets_at")) or (resets if of_week else None)
        week_util = rec.get("week_util") if _num(rec.get("week_util")) else (util if of_week else None)
        if not valid or (week_resets is not None and week_resets <= now):
            week_util = None
        if week_resets is not None and week_resets <= now:
            week_resets = None
        win = cf.login_window(crec)
        accounts[name] = {
            "email": a.get("email"),
            "set_up": set_up,
            "usable": usable,
            "login_failing": kind == "auth_down",
            "limited": limited,
            "spent": spent,                            # out of usage
            "out": (not usable) or spent,              # cannot run anything now
            "room": room,
            "util": util if valid else (1.0 if limited else None),
            "window": window,
            "week_util": week_util,
            "week_room": None if week_util is None else max(0.0, 1.0 - week_util),
            "read_at": at,
            "resets": resets if (resets is not None and resets > now) else None,
            "week_resets": week_resets,
            "back": until if limited else (resets if full else None),
            "extra": bool(rec.get("extra")),
            "extra_in_use": bool(rec.get("extra_in_use")) and valid,
            "login_ends": win["ends"] if win else None,
            "login_renew": win["renew"] if win else None,
        }
    app_uuid = app_account_uuid(cfg)
    return {"now": now, "app": name_for_uuid(cfg, app_uuid), "app_uuid": app_uuid,
            "accounts": accounts,
            "extension": extension if extension is not None else extension_account(cfg)}


def not_ready(snap, name):
    """Why an account could not take over the app right now ("" = it can)."""
    a = snap["accounts"].get(name)
    today = snap["now"].date()
    if a is None:
        return "it is not in the config"
    if not a["set_up"]:
        return "its folder is missing (claude-accounts setup)"
    if a["login_failing"]:
        return "its login is failing (claude-accounts login %s)" % name
    if a["login_ends"] is not None and today > a["login_ends"]:
        return "its login ended %s (claude-accounts login %s)" % (
            a["login_ends"].strftime("%b %-d"), name)
    return ""


def pick_target(snap, exclude=None):
    """The best account to move the app to: usable, with usage left, the most room
    first. None when no other account has usage."""
    best, best_key = None, None
    for name, a in snap["accounts"].items():
        if name == exclude or a["out"] or not_ready(snap, name):
            continue
        key = (1 if a["room"] is not None else 0, a["room"] or 0.0)
        if best_key is None or key > best_key:
            best, best_key = name, key
    return best


# --------------------------------------------------------------------------
# Which account background jobs use first
# --------------------------------------------------------------------------

def desired_order(snap, current):
    """The order background jobs and Terminal chats should try the accounts in.

    1. An account whose week resets within 12 hours and still has room goes first:
       whatever is left there is lost at the reset.
    2. Then the accounts the desktop app is NOT signed in to: the app's account is
       the one that needs a person to switch, so it is spared.
    3. Then the app's account.
    4. Then accounts with extra usage turned on and almost nothing left: jobs there
       would soon be billed.
    5. Accounts that are out go last, the one that comes back soonest first.

    Returns (order, reason), or (None, reason) when an account's usage is not known:
    then the order is left alone."""
    acc, now, app = snap["accounts"], snap["now"], snap["app"]
    names = [n for n in current if n in acc] + [n for n in acc if n not in current]
    if len(names) < 2:
        return None, "fewer than two accounts"
    for n in names:
        if not acc[n]["out"] and acc[n]["room"] is None:
            return None, "the usage of %s is not known yet" % n

    def reset_key(n):
        t = acc[n]["week_resets"] or acc[n]["resets"]
        return t.timestamp() if t else float("inf")

    def back_key(n):
        t = acc[n]["back"]
        return t.timestamp() if t else float("inf")

    out = sorted((n for n in names if acc[n]["out"]), key=back_key)
    live = [n for n in names if not acc[n]["out"]]
    risky = [n for n in live if acc[n]["extra"] and acc[n]["room"] < RISKY_ROOM]
    live = [n for n in live if n not in risky]
    expiring = []
    for n in live:
        h = hours_until(acc[n]["week_resets"], now)
        left = acc[n]["week_room"]
        if h is not None and h <= EXPIRING_HOURS and left is not None and left >= EXPIRING_ROOM:
            expiring.append(n)
    expiring.sort(key=reset_key)
    rest = [n for n in live if n not in expiring]
    spare = sorted((n for n in rest if n != app), key=reset_key)
    mine = [n for n in rest if n == app]
    order = expiring + spare + mine + risky + out
    if not live and not risky:
        reason = "every account is out"
    elif order[0] in expiring:
        reason = "%s first: its week resets %s and %d%% is unused" % (
            order[0], when(acc[order[0]]["week_resets"]), pct(acc[order[0]]["week_room"]))
    elif app and order[0] != app:
        reason = "%s first: the desktop app is on %s" % (order[0], app)
    elif app and order[0] == app:
        reason = "%s first: no other account has usage left" % app
    else:
        reason = "%s first: its week resets soonest" % order[0]
    return order, reason


def auto_order_modes(cfg):
    value = settings(cfg).get("auto_order")
    if value is True:
        return ("headless", "interactive")
    if isinstance(value, dict):
        return tuple(m for m in ("headless", "interactive") if value.get(m) is True)
    return ()


def apply_order(cfg, snap):
    """Put the account order in the config file in step with desired_order, for the
    modes switch.auto_order turns on. Only the order of the names changes: no account
    is added or dropped. The file is read again under the lock, so an account or a
    setting changed since this process started is respected. Returns (one line per
    change, problems)."""
    modes = auto_order_modes(cfg)
    if not modes:
        return [], []
    names = [a["name"] for a in cfg.get("accounts") or []]
    plan = {}
    for mode in modes:
        current = [n for n in (cfg.get("order") or {}).get(mode) or [] if n in names]
        base = current + [n for n in names if n not in current]
        want, reason = desired_order(snap, base)
        if want is None or want == current or sorted(want) != sorted(names):
            continue
        plan[mode] = (current, want, reason)
    if not plan:
        return [], []
    written = {}

    def mutate(raw):
        block = raw.get("switch") if isinstance(raw.get("switch"), dict) else {}
        on = block.get("auto_order")
        there = [a.get("name") for a in raw.get("accounts") or []
                 if isinstance(a, dict) and isinstance(a.get("name"), str)]
        order = raw.get("order")
        if not isinstance(order, dict):
            order = raw["order"] = {}
        for mode, (_, want, _) in plan.items():
            if not (on is True or (isinstance(on, dict) and on.get(mode) is True)):
                continue                    # turned off since this process read the config
            new = [n for n in want if n in there] + [n for n in there if n not in want]
            if order.get(mode) != new:
                order[mode] = written[mode] = new

    changed, problem = update_config(cfg, mutate)
    if problem:
        return [], ["account order not changed: %s" % problem]
    lines = []
    if changed:
        for mode, new in written.items():
            current, _, reason = plan[mode]
            cfg.setdefault("order", {})[mode] = list(new)    # the config in memory, too
            label = "background jobs" if mode == "headless" else "Terminal chats"
            lines.append("%s now use %s (%s)" % (label, " -> ".join(new), reason))
            cf.log_event("auto_order", mode=mode, frm=current, to=new, reason=reason)
        if written:
            with edit_state() as st:
                st["order_set"] = {"at": cf.iso(snap["now"]), "order": dict(written),
                                   "reason": plan[next(iter(written))][2]}
    return lines, []


# --------------------------------------------------------------------------
# Settings in other tools that must name the right account
# --------------------------------------------------------------------------

def _dig(data, dotted):
    cur = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def apply_followers(cfg, snap):
    """switch.followers lists JSON settings in other tools that must hold the name
    of the account the Chrome extension ("follows": "extension") or the desktop app
    ("follows": "app") is signed in to. When that account is known and the setting
    names another one, the one value is replaced in the file's text, so nothing else
    in the file changes. Returns (one line per change, problems)."""
    lines, problems = [], []
    specs = settings(cfg).get("followers")
    for spec in specs if isinstance(specs, list) else []:
        if not isinstance(spec, dict):
            continue
        path, key = spec.get("file"), spec.get("key")
        if not (isinstance(path, str) and path and isinstance(key, str) and key):
            continue
        if spec.get("follows") == "app":
            want = snap["app"]
        else:
            ext = snap["extension"]
            want = ext.get("account") if ext.get("state") == "ok" else None
        if not want:
            continue                    # not known: leave the setting as it is
        shown = tilde(os.path.expanduser(path))
        real = os.path.realpath(os.path.expanduser(path))   # a link: change its target
        try:
            with open(real, "r", encoding="utf-8") as f:
                text = f.read()
            data = json.loads(text)
        except (OSError, ValueError) as e:
            problems.append("%s: could not read it (%s)" % (shown, why(e)))
            continue
        current = _dig(data, key)
        if current == want:
            continue
        if not isinstance(current, str):
            problems.append("%s: %s is not a text value, left alone" % (shown, key))
            continue
        leaf = key.split(".")[-1]
        pattern = re.compile(r'("%s"\s*:\s*)%s' % (re.escape(leaf), re.escape(json.dumps(current))))
        if len(pattern.findall(text)) != 1:
            problems.append("%s: %s is not in exactly one place, left alone" % (shown, key))
            continue
        new = pattern.sub(lambda m: m.group(1) + json.dumps(want), text, count=1)
        try:
            check = json.loads(new)
        except ValueError:
            check = None
        if _dig(check, key) != want:
            problems.append("%s: could not change %s safely, left alone" % (shown, key))
            continue
        if not os.access(real, os.W_OK):
            problems.append("%s: it is read-only, left alone" % shown)
            continue
        try:
            _write_bytes(real, new.encode("utf-8"), stat.S_IMODE(os.stat(real).st_mode))
        except OSError as e:
            problems.append("%s: could not write it (%s)" % (shown, why(e)))
            continue
        cf.log_event("follower", file=real, key=key, frm=current, to=want)
        lines.append("%s: %s is now %s (was %s)" % (shown, key, want, current))
    return lines, problems


# --------------------------------------------------------------------------
# What to tell the person, and when
# --------------------------------------------------------------------------

def _left(a):
    """What the account has left, to follow "has": known numbers only."""
    if a["week_room"] is not None:
        return "%d%% of its week left" % pct(a["week_room"])
    if a["room"] is not None:
        return "%d%% of its %s left" % (pct(a["room"]), _window_name(a["window"]))
    return None


def _has(name, a, ready=False):
    left = _left(a)
    if left is None:
        return "%s may have usage left (not read yet)" % name
    return ("%s is ready with %s" if ready else "%s has %s") % (name, left)


def _window_name(name):
    if name == "five_hour":
        return "5-hour window"
    if name in (None, "seven_day"):
        return "week"
    return name.replace("_", " ") + " window"


def _bucket(t, now):
    """A day and half-day label. Notice keys are built from it, so a reset time that
    is only a guess and keeps moving by minutes does not make a new notice each time."""
    t = (t or now).astimezone(cf.LOCAL_TZ)
    return t.strftime("%Y-%m-%d") + ("am" if t.hour < 12 else "pm")


def advice(cfg, snap, st=None):
    """The notices that apply right now, each {"key", "title", "text"}. A notice is
    shown once per key (see deliver), so this can be asked every few minutes."""
    st = st if st is not None else read_state()
    acc, app, now = snap["accounts"], snap["app"], snap["now"]
    notes = []
    if not app or app not in acc:
        return notes
    a = acc[app]
    target = pick_target(snap, exclude=app)
    stops = "it stops chats that are working"
    how = "In Terminal: claude-accounts switch (%s)" % stops    # moves the app itself
    by_app = at_limit(cfg) == "app"
    # Does the StopFailure hook continue a refused chat in Terminal by itself? If
    # not (not installed, script gone, desktop chats left out), nothing takes over.
    problem = handoff_problem(cfg)
    armed = not problem
    paid_next = None
    if armed and not by_app:
        # Name the account the hook itself would hand a refused chat to. It goes by
        # the wrapper's saved limits and the account order, with an account on paid
        # extra usage last, so it can differ from the account with the most room.
        to = hook_target(cfg, app)
        o = acc.get(to) if to else None
        ready = o is not None and not not_ready(snap, to)
        target = to if ready and not o["out"] else None
        # No usage left there either, but it is past its own limit and still answers,
        # on paid extra usage. The hook does not know that account is out (it never
        # refused anything), so the chat is handed to it all the same. Say so.
        if ready and target is None and o["spent"] and o["extra"] and not o["limited"]:
            paid_next = to
    week_tag = cf.iso(a["week_resets"]) or now.strftime("%Y-%m-%d")
    warn_at = settings(cfg).get("warn_at")
    warn_at = sorted({int(v) for v in warn_at if _num(v) and 1 <= v <= 100}) \
        if isinstance(warn_at, list) else list(WARN_AT)
    if a["spent"]:
        refused = a["limited"] or not a["extra"]    # False: it still answers, on paid extra usage
        if refused:
            state = "%s is out of usage%s" % (app, " until %s" % when(a["back"]) if a["back"] else "")
        else:
            state = "%s has used its whole %s%s; chats there now cost extra usage" % (
                app, _window_name(a["window"]), " (resets %s)" % when(a["back"]) if a["back"] else "")
        since = (st.get("out_since") or {}).get(app) if isinstance(st.get("out_since"), dict) else None
        if target and by_app:
            notes.append({"key": "out:%s:%s" % (app, since or _bucket(a["back"], now)), "assistant": True,
                          "title": "Claude: switch the app to %s" % target,
                          "text": "%s. %s. %s" % (state, _has(target, acc[target]), how)})
        elif target:
            # The default: say what happens, open nothing. The hook continues each
            # refused chat in Terminal; signing the app out would stop the others.
            key = "out:%s:%s" % (app, since or _bucket(a["back"], now))
            if not refused:
                # Its own key: when the account does refuse later in the same outage,
                # the notice that chats now move is new and is shown.
                key += ":extra"
                title = "Claude: %s has used its whole %s" % (app, _window_name(a["window"]))
                if armed:
                    tail = ("Nothing moves while extra usage is on: turn it off for %s and chats "
                            "continue on %s in Terminal. Moving the app instead (claude-accounts "
                            "switch) stops chats that are working" % (app, target))
                else:
                    tail = "To move the app: claude-accounts switch (%s)" % stops
            else:
                title = "Claude: %s is out of usage" % app
                if armed:
                    tail = "Chats that hit the limit continue on %s in Terminal" % target
                else:
                    tail = ("Nothing takes over by itself: %s. To move the app: claude-accounts "
                            "switch (%s)" % (problem, stops))
            notes.append({"key": key, "title": title,
                          "text": "%s. %s. %s." % (state, _has(target, acc[target]), tail)})
        elif paid_next and refused:
            notes.append({"key": "out:%s:%s" % (app, since or _bucket(a["back"], now)),
                          "title": "Claude: %s is out of usage" % app,
                          "text": "%s. Chats that hit the limit continue on %s in Terminal, on paid "
                                  "extra usage: %s is past its own limit and still answers." % (
                                      state, paid_next, paid_next)})
        else:
            backs = sorted((v["back"], n) for n, v in acc.items() if v["back"] is not None)
            first = ""
            if backs and not (backs[0][1] == app and a["back"]):   # not what `state` just said
                first = "First back: %s, %s." % (backs[0][1], when(backs[0][0]))
            notes.append({"key": "allout:%s" % _bucket(backs[0][0] if backs else None, now),
                          "title": "Claude: every account is out of usage",
                          "text": ("%s. %s" % (state, first)).strip()})
    else:
        if a["week_util"] is not None:
            used = pct(a["week_util"])
            for threshold in reversed(warn_at):
                if used >= threshold:
                    resets = "Resets %s. " % when(a["week_resets"]) if a["week_resets"] else ""
                    if target and by_app:
                        tail = "%s. %s" % (_has(target, acc[target], ready=True), how)
                    elif target and not armed:
                        tail = "%s. Nothing takes over at 100%%: %s." % (
                            _has(target, acc[target], ready=True), problem)
                    elif target and a["extra"]:
                        tail = ("%s. Extra usage is on for %s: past 100%% it bills instead of "
                                "stopping. Turn it off and chats move to %s in Terminal." % (
                                    _has(target, acc[target], ready=True), app, target))
                    elif target:
                        tail = "%s. When %s runs out, chats continue on %s in Terminal." % (
                            _has(target, acc[target], ready=True), app, target)
                    elif paid_next and not a["extra"]:
                        tail = ("No other account has usage left. When %s runs out, chats continue on "
                                "%s in Terminal, on paid extra usage." % (app, paid_next))
                    else:
                        tail = "No other account has usage left right now."
                    notes.append({"key": "near:%s:%s:%d" % (app, week_tag, threshold),
                                  "title": "Claude: %s is at %d%% of its week" % (app, used),
                                  "text": resets + tail})
                    break
        for name, o in acc.items():
            if name == app or o["out"] or not_ready(snap, name) or o["week_room"] is None:
                continue
            h = hours_until(o["week_resets"], now)
            mine = hours_until(a["week_resets"], now)
            if h is not None and h <= UNUSED_HOURS and o["week_room"] >= UNUSED_ROOM and (
                    mine is None or mine > h):
                use = ("Switch the app to use it. %s" % how if by_app else
                       "To use it in the app: claude-accounts switch (it stops chats that are working).")
                notes.append({"key": "unused:%s:%s" % (name, cf.iso(o["week_resets"])),
                              "title": "Claude: %s has unused usage" % name,
                              "text": "%d%% of its week is left and it resets %s. %s" % (
                                  pct(o["week_room"]), when(o["week_resets"]), use)})
    # Is another account able to take over when this one runs out?
    if a["spent"] or (a["week_util"] is not None and a["week_util"] >= BUSY_AT):
        for name in acc:
            reason = not_ready(snap, name) if name != app else ""
            if reason:
                notes.append({"key": "notready:%s:%s" % (name, now.strftime("%Y-%m-%d")),
                              "title": "Claude: %s cannot take over yet" % name,
                              "text": "%s is %s, and %s is not ready: %s." % (
                                  app, "out of usage" if a["spent"] else
                                  "at %d%% of its week" % pct(a["week_util"]), name, reason)})
    ext = snap["extension"]
    if ext.get("state") == "ok" and ext["account"] != app:
        notes.append({"key": "ext:%s:%s" % (app, st.get("app_changed_at") or ""),
                      "title": "Claude: Chrome extension is still on %s" % ext["account"],
                      "text": "The app is on %s. In Chrome: Claude icon > account menu > sign in "
                              "as %s. Until then browser tasks cannot run." % (
                                  app, acc[app]["email"] or app)})
    return notes


def track_outages(snap):
    """Remember since when each account has been out, so one outage is one notice.
    An account stops being "out" only when a reading shows it has usage again."""
    with edit_state() as st:
        since = st.get("out_since") if isinstance(st.get("out_since"), dict) else {}
        for name, a in snap["accounts"].items():
            if a["spent"]:
                since.setdefault(name, cf.iso(snap["now"]))
            elif a["room"] is not None:
                since.pop(name, None)
        for name in [n for n in since if n not in snap["accounts"]]:
            since.pop(name)
        st["out_since"] = since


def deliver(cfg, notes, quiet_keys=()):
    """Show each notice that was not shown before and remember it. Returns the new ones."""
    new = []
    with edit_state() as st:
        seen = st["notified"]
        for n in notes:
            if n["key"] in seen:
                continue
            seen[n["key"]] = cf.iso(cf.now_local())
            new.append(n)
        for key in sorted(seen, key=lambda k: seen[k] or "")[:-NOTIFIED_KEEP]:
            seen.pop(key, None)
    for n in new:
        cf.log_event("switch_notice", key=n["key"], title=n["title"], text=n["text"])
        if n["key"] not in quiet_keys:
            cf.notify(n["title"], n["text"])
    return new


def assistant_lock():
    """Take the lock a `switch --assist` window holds while it is open. Returns the
    open lock file (keep it; closing it lets go), or None when another window has it."""
    try:
        d = work_dir()
        d.mkdir(parents=True, exist_ok=True)
        f = open(str(d / "assist.lock"), "a+")
    except OSError:
        return None
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def open_assistant(cfg):
    """A Terminal window that walks through the switch of the app and finishes it.
    Only when the config asks for it ("at_limit": "app"): signing the app out stops
    every chat that is working in it, and the StopFailure hook already continues the
    chats that hit the limit. At most one at a time and one in 6 hours. Returns the
    script it opened, or None."""
    if at_limit(cfg) != "app":
        return None
    if os.environ.get("CLAUDE_FAILOVER_NO_NOTIFY") == "1":
        return None
    if settings(cfg).get("open_assistant_at_limit") is False:
        return None
    lock = assistant_lock()
    if lock is None:
        return None                     # a helper window is open already
    lock.close()
    now = cf.now_local()
    with edit_state() as st:
        last = cf.parse_iso(st.get("assistant_at"))
        if last is not None and 0 <= (now - last).total_seconds() < ASSIST_GAP_HOURS * 3600:
            return None
        st["assistant_at"] = cf.iso(now)
    try:
        hdir = cf.handoff_dir()
        hdir.mkdir(parents=True, exist_ok=True)
        path = hdir / ("%s-switch.command" % now.strftime("%Y%m%d-%H%M%S"))
        lines = ["#!/bin/bash",
                 "# claude-failover: the desktop app's account is out of usage.",
                 "export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"]
        conf = config_file(cfg)
        if conf:
            lines.append("export CLAUDE_FAILOVER_CONFIG=%s" % shlex.quote(conf))
        lines += ["cd ~", "exec %s switch --assist" % shlex.quote(str(ACCOUNTS_SCRIPT)), ""]
        with open(str(path), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        os.chmod(str(path), 0o700)
        subprocess.Popen(["/usr/bin/open", "-a", "Terminal", str(path)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return path
    except OSError:
        return None


# --------------------------------------------------------------------------
# Chats and scheduled tasks (desktop_sync does the copying)
# --------------------------------------------------------------------------

def run_sync(cfg, dry_run=False):
    """desktop_sync's report, None when this copy has no desktop sync."""
    try:
        import desktop_sync
    except Exception:
        return None
    try:
        return desktop_sync.sync(cfg, dry_run=dry_run)
    except Exception as e:   # a sync problem must never stop a switch
        return {"errors": ["the sync failed: %s" % why(e)]}


def handover_lines(cfg):
    """Chats that were mid-work or waiting when the app left an account, as short
    lines to show after a switch. desktop_sync owns the chat records: when it offers
    handover_lines(cfg) its list is shown, otherwise there is none."""
    try:
        import desktop_sync
        make = getattr(desktop_sync, "handover_lines", None)
        lines = make(cfg) if callable(make) else []
        return [str(line) for line in lines][:20] if isinstance(lines, (list, tuple)) else []
    except Exception:
        return []


def sync_lines(report, other):
    """(level, text) lines about a sync report: chats, then scheduled tasks on `other`
    (the account that is not signed in, where copies are written)."""
    if report is None:
        return [("warn", "chats and scheduled tasks - not copied (desktop sync is not available)")]
    if report.get("skipped"):
        return [("ok", "chats and scheduled tasks - %s (it does the same work)" % report["skipped"])]
    lines = []
    errors = report.get("errors") or []
    chats = report.get("chats")
    if errors:
        lines.append(("warn", "chats - the sync had %d problem%s (see: claude-accounts sync)" % (
            len(errors), "" if len(errors) == 1 else "s")))
    else:
        waiting = sum(report.get(k) or 0 for k in ("added", "added_needs_restart", "updated", "retired")
                      if _num(report.get(k)))
        if report.get("dry_run") and waiting:
            lines.append(("ok", "chats - %d to copy first (the switch does it)" % waiting))
        else:
            lines.append(("ok", "chats - %s in every account" % (
                "%d" % chats if _num(chats) else "listed")))
    block = report.get("scheduled_tasks")
    if not isinstance(block, dict):
        lines.append(("warn", "scheduled tasks - not checked (this copy has no task sync)"))
        return lines
    if not block.get("on"):
        lines.append(("warn", "scheduled tasks - not copied: task sync is turned off"))
        return lines
    if not block.get("source"):
        lines.append(("warn", "scheduled tasks - not copied: %s" % (
            block.get("note") or "the signed-in account is not known")))
        return lines
    found = False
    for t in block.get("targets") or []:
        if not isinstance(t, dict) or t.get("account") != other:
            continue
        found = True
        changes = t.get("changes") or []
        live = [o for o in t.get("only_there") or [] if isinstance(o, dict) and o.get("live")]
        if t.get("note"):
            lines.append(("warn", "scheduled tasks - %s" % t["note"]))
        elif changes and not t.get("written") and not report.get("dry_run"):
            lines.append(("warn", "scheduled tasks - %d change%s could not be written to %s" % (
                len(changes), "" if len(changes) == 1 else "s", other)))
        elif changes and report.get("dry_run"):
            lines.append(("warn", "scheduled tasks - %s is %d change%s behind %s" % (
                other, len(changes), "" if len(changes) == 1 else "s", block["source"])))
        else:
            n = block.get("tasks")
            lines.append(("ok", "scheduled tasks - %s on %s, the same as on %s" % (
                "%d" % n if _num(n) else "all", other, block["source"])))
        if live:
            names = ", ".join(str(o.get("name") or o.get("id")) for o in live[:4])
            lines.append(("warn", "scheduled tasks - only on %s and still on: %s" % (other, names)))
    if not found:
        lines.append(("warn", "scheduled tasks - %s was not checked" % other))
    return lines


# --------------------------------------------------------------------------
# Finishing a switch (the watcher and `switch` both end here)
# --------------------------------------------------------------------------

def claim_switch(new):
    """Record that the app is now on `new`. Returns the account it was on before,
    "" when this is the first time the app's account is recorded, or None when
    someone else recorded this switch already. Whoever gets a name tells the person."""
    with edit_state() as st:
        old = st.get("app_account")
        if old == new:
            return None
        st["app_account"] = new
        st.pop("pending", None)
        if isinstance(old, str) and old:
            st["app_changed_at"] = cf.iso(cf.now_local())
            return old
        return ""


def finish_switch(cfg, old, new, out, announce=True, sync=True):
    """Everything that follows a switch of the desktop app from `old` to `new`.
    Safe to run twice: each part only changes what is not already right. The config
    is read again first: a `switch` may have waited for hours."""
    try:
        cfg = cf.load_config()
    except SystemExit:
        pass                            # unreadable right now: go on with the copy in memory
    out("  ok  the app is on %s" % new)
    if sync:
        other = old or next((a["name"] for a in cfg.get("accounts") or [] if a["name"] != new), "")
        for level, text in sync_lines(run_sync(cfg), other):
            out("  %s  %s" % ("ok" if level == "ok" else "!!", text))
        todo = handover_lines(cfg)
        if todo:
            out("  ..  chats to pick up on %s:" % new)
            for line in todo:
                out("        %s" % line)
    refresh_usage(cfg, max_age=15)
    snap = snapshot(cfg)
    for apply in (apply_order, apply_followers):
        lines, problems = apply(cfg, snap)
        for line in lines:
            out("  ok  %s" % line)
        for line in problems:
            out("  !!  %s" % line)
    ext = snap["extension"]
    if ext.get("state") == "ok" and ext["account"] == new:
        out("  ok  Chrome extension is on %s" % new)
    elif ext.get("state") in ("ok", "other", "signed_out"):
        was = ext.get("account") or ("another account" if ext["state"] == "other" else "no account")
        email = (snap["accounts"].get(new) or {}).get("email") or new
        out("  !!  Chrome extension is on %s: sign it in as %s (Claude icon in Chrome > account "
            "menu). Browser tasks wait for this." % (was, email))
    if announce:
        cf.log_event("app_switch", frm=old or None, to=new)
        order = (cfg.get("order") or {}).get("headless") or []
        text = "Chats and scheduled tasks came along."
        if order and auto_order_modes(cfg):
            text += " Background jobs use %s first." % cf.included_usage_first(cfg, list(order))[0]
        with edit_state() as st:
            st["notified"]["switched:%s:%s" % (new, st.get("app_changed_at") or "")] = \
                cf.iso(cf.now_local())
        cf.notify("Claude: the app is now on %s" % new, text)
    return snap


# --------------------------------------------------------------------------
# The watcher
# --------------------------------------------------------------------------

def _trim_log():
    """Cut the watcher's log back to its tail, in place (launchd keeps the file open)."""
    path = work_dir() / "agent.log"
    try:
        if path.stat().st_size <= LOG_MAX_BYTES:
            return
        with open(str(path), "r+b") as f:
            f.seek(-LOG_KEEP_BYTES, os.SEEK_END)
            tail = f.read()
            cut = tail.find(b"\n")
            f.seek(0)
            f.write(tail[cut + 1:] if cut >= 0 else tail)
            f.truncate()
    except OSError:
        pass


def tick(cfg, out=None):
    """One pass: finish a switch that happened, read usage that is due, keep the
    account order and the follower settings right, and show the notices that are
    new. Never raises; returns the lines it would log."""
    lines = []

    def say(text):
        lines.append(text)
        if out:
            out(text)

    d = work_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(str(d), 0o700)
    except OSError:
        pass
    try:
        lock = open(str(d / "tick.lock"), "a+")
    except OSError as e:
        say("watch: cannot open its lock (%s)" % why(e))
        return lines
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return lines            # another pass is running
        _trim_log()
        try:
            _tick(cfg, say)
        except Exception as e:      # the watcher must never die on one bad pass
            text = "watch: %s: %s" % (e.__class__.__name__, why(e))
            try:
                with edit_state() as st:
                    known = st.get("problems") if isinstance(st.get("problems"), list) else []
                    st["problems"] = [text]
            except Exception:
                known = []
            if text not in known:   # the same failure every pass is logged once
                say(text)
                cf.log_event("switch_watch_error", error=e.__class__.__name__, text=why(e))
    return lines


def _tick(cfg, say):
    problems = []
    app = app_account(cfg)
    if app:
        old = claim_switch(app)
        if old:
            say("the desktop app moved from %s to %s" % (old, app))
            finish_switch(cfg, old, app, lambda text: say(text.strip()),
                          sync=cf.desktop_sync_on(cfg))
    refresh_usage(cfg)
    for name, rec in sorted(read_state()["usage"].items()):
        if isinstance(rec, dict) and rec.get("ok") is False:
            problems.append("could not read the usage of %s: %s" % (
                name, rec.get("text") or rec.get("error")))
    snap = snapshot(cfg)
    track_outages(snap)
    for apply in (apply_order, apply_followers):
        lines, bad = apply(cfg, snap)
        for line in lines:
            say(line)
        problems += bad
    for note in deliver(cfg, advice(cfg, snap)):
        say("told you: %s - %s" % (note["title"], note["text"]))
        if note.get("assistant") and open_assistant(cfg):
            say("opened the switch helper in Terminal")
    with edit_state() as st:
        before = st.get("problems") if isinstance(st.get("problems"), list) else []
        st["problems"] = problems
        st["last_tick"] = cf.iso(cf.now_local())
    for text in problems:
        if text not in before:      # a lasting problem is logged once, not every pass
            say(text)


def on_limit(cfg):
    """Called by the StopFailure hook right after a chat hit a usage limit: run one
    watcher pass now, in the background, so the notice does not wait for the next
    scheduled pass. Does nothing unless the watcher is turned on."""
    if settings(cfg).get("watch") is not True:
        return False
    env = dict(os.environ)
    conf = config_file(cfg)
    if conf:
        env["CLAUDE_FAILOVER_CONFIG"] = conf
    try:
        subprocess.Popen([str(ACCOUNTS_SCRIPT), "watch", "--quiet"], env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------
# claude-accounts usage
# --------------------------------------------------------------------------

def describe(a):
    """One account's usage in words."""
    if not a["set_up"]:
        return "not set up"
    if a["login_failing"]:
        return "login failing"
    if a["limited"]:
        return "out of usage"
    if a["spent"]:
        return "its whole %s used" % _window_name(a["window"])
    if a["week_util"] is not None:
        text = "%d%% of its week used" % pct(a["week_util"])
        if a["window"] not in (None, "seven_day") and a["util"] is not None and a["util"] >= BUSY_AT:
            text += ", %d%% of its %s" % (pct(a["util"]), _window_name(a["window"]))
        return text
    if a["util"] is not None:
        return "%d%% of its %s used" % (pct(a["util"]), _window_name(a["window"]))
    return "usage not read yet"


def usage_rows(snap):
    rows = []
    for name, a in snap["accounts"].items():
        resets = a["back"] or a["week_resets"] or a["resets"]
        bits = [describe(a)]
        # Past its limit with extra usage on: it is not gone until the reset, it bills.
        paid = a["spent"] and a["extra"] and not a["limited"]
        if resets is not None and paid:
            bits.append("still answers on paid extra usage until %s" % when(resets))
        elif resets is not None:
            bits.append(("back %s" if a["spent"] else "resets %s") % when(resets))
        if paid and resets is not None:
            pass                        # said above
        elif a["extra_in_use"]:
            bits.append("now on extra usage (billed)")
        elif a["extra"]:
            bits.append("extra usage on")
        rows.append((name, bits, name == snap["app"]))
    return rows


def usage_lines(cfg, snap):
    lines = ["Claude usage"]
    width = max([len(n) for n in snap["accounts"]] or [8])
    for name, bits, is_app in usage_rows(snap):
        lines.append("  %-*s  %s%s" % (width, name, ", ".join(bits),
                                      "   <- the desktop app" if is_app else ""))
    read = [a["read_at"] for a in snap["accounts"].values() if a["read_at"] is not None]
    if read:
        lines.append("  read %s" % ago(min(read), snap["now"]))
    # The order jobs are really tried in: an account on paid extra usage goes last.
    order = cf.included_usage_first(cfg, list((cfg.get("order") or {}).get("headless") or []))
    if order:
        lines.append("  background jobs use %s%s" % (
            " -> ".join(order), " (kept in step by the watcher)" if auto_order_modes(cfg) else ""))
    for note in advice(cfg, snap):
        lines.append("  note: %s - %s" % (note["title"].replace("Claude: ", ""), note["text"]))
    return lines


def _json_snapshot(snap):
    def conv(v):
        if isinstance(v, dt.datetime):
            return cf.iso(v)
        if isinstance(v, dt.date):
            return v.isoformat()
        if isinstance(v, dict):
            return {k: conv(x) for k, x in v.items()}
        return v
    return conv(snap)


def cmd_usage(cfg, args):
    if any(a not in ("--fresh", "--json") for a in args):
        print(USAGE)
        return 2
    refresh_usage(cfg, force="--fresh" in args)
    snap = snapshot(cfg)
    if "--json" in args:
        print(json.dumps(_json_snapshot(snap), indent=2))
    else:
        print("\n".join(usage_lines(cfg, snap)))
    return 0


def watcher_health(cfg):
    """A few words on the background job: off, broken, not running, or its interval."""
    minutes, script = installed_job()
    if not minutes:
        return "watcher off (claude-accounts watch install)"
    if not script or not os.path.exists(script):
        return "watcher broken (run: claude-accounts watch install)"
    last = cf.parse_iso(read_state().get("last_tick"))
    if last is not None and (cf.now_local() - last).total_seconds() > 3 * minutes * 60 + 300:
        return "watcher has not finished a pass since %s (see its log: claude-accounts watch status)" % when(last)
    problems = read_state().get("problems")
    if isinstance(problems, list) and problems:
        return "watcher every %d min, %d problem%s (claude-accounts watch status)" % (
            minutes, len(problems), "" if len(problems) == 1 else "s")
    return "watcher every %d min" % minutes


def at_limit_words(cfg):
    """What happens when the app's account runs out, as one phrase for status."""
    if at_limit(cfg) == "app":
        what = ("a notice says to run claude-accounts switch"
                if settings(cfg).get("open_assistant_at_limit") is False
                else "the switch helper opens in Terminal")
        return "%s (switch.at_limit is \"app\"); signing the app out stops chats that are working" % what
    problem = handoff_problem(cfg)
    if problem:
        return "NOTHING takes over: %s" % problem
    text = ("chats continue in Terminal on the other account; the app stays signed in and no "
            "window asks you to sign it out")
    try:
        handoff = cfg.get("handoff") if isinstance(cfg.get("handoff"), dict) else {}
        if not handoff.get("from_terminal", True):
            text += ". Chats started in Terminal stop instead (handoff.from_terminal is off)"
        elif cf.hook_blind_lines(cfg):
            text += ". Not for every account: see claude-accounts hook status"
    except Exception:
        pass
    return text


def at_limit_line(cfg):
    """One line for `claude-accounts status`."""
    return "At a limit - %s" % at_limit_words(cfg)


def status_lines(cfg):
    """Lines for `claude-accounts status`, from saved readings only (no call is
    made). Never raises; [] when there is nothing to say: fewer than two accounts,
    or the watcher is off and no usage was ever read."""
    try:
        if settings(cfg).get("watch") is not True and not read_state()["usage"]:
            return []       # never used here: say nothing
        snap = snapshot(cfg)
        if len(snap["accounts"]) < 2:
            return []
        lines = []
        for name, bits, is_app in usage_rows(snap):
            lines.append("Usage - %s: %s%s" % (name, ", ".join(bits),
                                               " (the desktop app is here)" if is_app else ""))
        lines.append(at_limit_line(cfg))
        health = watcher_health(cfg)
        lines.append("Watcher - %s" % (health[len("watcher "):] if health.startswith("watcher ") else health))
        return lines
    except Exception:
        return []


# --------------------------------------------------------------------------
# claude-accounts switch
# --------------------------------------------------------------------------

def _pop_option(args, name):
    """Take `--name VALUE` or `--name=VALUE` out of args: VALUE, None if absent,
    "" if it has no value."""
    for i, a in enumerate(args):
        if a == name:
            value = args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("--") else ""
            del args[i:i + (2 if value != "" else 1)]
            return value
        if a.startswith(name + "="):
            del args[i]
            return a[len(name) + 1:]
    return None


def recent_scheduled_runs(cfg, account, now, minutes=15):
    """Titles of scheduled-task chats under `account` whose record the app wrote in
    the last few minutes: a run that may still be going."""
    uuid = account_uuids(cfg).get(account)
    if not uuid:
        return []
    root = desktop_dir(cfg) / "claude-code-sessions"
    try:
        folders = [org for acct in root.iterdir() if acct.name.lower() == uuid and acct.is_dir()
                   for org in acct.iterdir() if org.is_dir()]
    except OSError:
        return []
    floor = now.timestamp() - minutes * 60
    titles = []
    for folder in folders:
        try:
            files = [p for p in folder.glob("local_*.json") if p.stat().st_mtime >= floor]
        except OSError:
            continue
        for p in files[:200]:
            data = cf.read_json(p, {})
            if isinstance(data, dict) and data.get("scheduledTaskId") and not data.get("isArchived"):
                titles.append(str(data.get("title") or data.get("scheduledTaskId")))
    return titles


def readiness(cfg, snap, target, auth=None):
    """(level, text) lines about whether `target` can take over the app now.
    level is "ok", "warn" (it works, but read this) or "fail" (fix it first)."""
    a = snap["accounts"][target]
    today = snap["now"].date()
    lines = []
    reason = not_ready(snap, target)
    unsure = ""
    if auth is not None and not reason:
        if auth.get("error"):
            unsure = str(auth["error"])             # the check itself failed: say so, don't guess
        elif not auth.get("loggedIn"):
            reason = "its Claude Code login is signed out (claude-accounts login %s)" % target
        elif a["email"] and (auth.get("email") or "").lower() != a["email"].lower():
            reason = "its folder is signed in as %s, not %s (claude-accounts login %s)" % (
                auth.get("email") or "someone else", a["email"], target)
    if reason:
        lines.append(("fail", "login - %s" % reason))
    elif unsure:
        lines.append(("warn", "login - could not be checked (%s)" % unsure))
    elif a["login_renew"] is not None and today >= a["login_renew"]:
        lines.append(("warn", "login - works, ends by %s: renew with claude-accounts login %s" % (
            a["login_ends"].strftime("%b %-d"), target)))
    else:
        lines.append(("ok", "login - works%s" % (
            " (ends by %s)" % a["login_ends"].strftime("%b %-d") if a["login_ends"] else "")))
    if a["spent"]:
        lines.append(("fail", "usage - %s is out too%s" % (
            target, ", back %s" % when(a["back"]) if a["back"] else "")))
    elif a["room"] is None:
        lines.append(("warn", "usage - not read yet (claude-accounts usage --fresh)"))
    else:
        resets = a["week_resets"] or a["resets"]
        lines.append(("ok", "usage - %s%s" % (_left(a), ", resets %s" % when(resets) if resets else "")))
    return lines


def _show(lines, out):
    marks = {"ok": "ok", "warn": "!!", "fail": "XX"}
    for level, text in lines:
        out("  %s  %s" % (marks.get(level, "!!"), text))


def wait_for_app(cfg, before, seconds, poll=None, keep_waiting=None):
    """Wait until the app is signed in to another account than `before`. Returns its
    id, or None when the time is up or keep_waiting() says to stop."""
    if poll is None:
        try:
            poll = float(os.environ.get("CLAUDE_FAILOVER_SWITCH_POLL") or 2.0)
        except ValueError:
            poll = 2.0
    start = time.monotonic()
    while True:
        uuid = app_account_uuid(cfg)
        if uuid and uuid != before:
            return uuid
        if time.monotonic() - start >= seconds:
            return None
        if keep_waiting is not None and not keep_waiting():
            return None
        time.sleep(max(0.05, poll))


def wait_for_extension(cfg, want, seconds, poll=None):
    """Wait until the Chrome extension is signed in to `want`. True when it is. A
    moment in which its storage cannot be read (Chrome is writing) is waited out."""
    if poll is None:
        try:
            poll = float(os.environ.get("CLAUDE_FAILOVER_SWITCH_POLL") or 2.0) * 2.5
        except ValueError:
            poll = 5.0
    start = time.monotonic()
    while True:
        ext = extension_account(cfg)
        if ext.get("state") == "ok" and ext["account"] == want:
            return True
        if time.monotonic() - start >= seconds:
            return False
        time.sleep(max(0.05, poll))


def assist_needed(cfg, app):
    """False once the app's account is known to have usage again: then the helper
    window that opened at its limit has nothing left to do. Not known = still needed."""
    a = snapshot(cfg, extension={"state": "unknown"})["accounts"].get(app)
    return a is None or a["spent"] or a["room"] is None


def cmd_switch(cfg, args, out=print):
    args = list(args)
    to = _pop_option(args, "--to")
    wait = _pop_option(args, "--wait")
    check, no_wait = "--check" in args, "--no-wait" in args
    assist, force = "--assist" in args, "--force" in args
    if to == "" or wait == "" or any(
            a not in ("--check", "--no-wait", "--assist", "--force") for a in args):
        out(USAGE)
        return 2
    if wait is not None and not wait.isdigit():
        out("--wait takes whole minutes.")
        return 2
    names = [a["name"] for a in cfg.get("accounts") or []]
    if len(names) < 2:
        out("Only one account is configured, so there is nothing to switch to "
            "(claude-accounts add <name>).")
        return 1
    if to is not None and to not in names:
        out("No account named %s. Accounts: %s." % (to, ", ".join(names)))
        return 2
    held = None
    if assist:
        held = assistant_lock()         # one helper window at a time
        if held is None:
            out("Another switch helper window is already open.")
            return 0
    try:
        return _switch(cfg, out, to, wait, check, no_wait, assist, force, names)
    finally:
        if held is not None:
            held.close()


def _switch(cfg, out, to, wait, check, no_wait, assist, force, names):
    before = app_account_uuid(cfg)
    app = name_for_uuid(cfg, before)
    if app and not check:
        # Make sure the app's present account is on record, so the switch that
        # follows is seen as one (also when the watcher is off).
        earlier = claim_switch(app)
        if earlier:
            out("The app moved from %s to %s since the last look. Finishing that first:" % (earlier, app))
            finish_switch(cfg, earlier, app, out)
            out("")
    if not check:
        refresh_usage(cfg, max_age=10)
    snap = snapshot(cfg)
    target = to or pick_target(snap, exclude=app) or next((n for n in names if n != app), None)
    for line in usage_lines(cfg, snap)[:1 + len(names)]:
        out(line)
    out("")
    if app is None:
        out("The desktop app is %s." % (
            "signed in to an account that is not in the config" if before
            else "not signed in to any account yet"))
    if app == target:
        out("The desktop app is already on %s." % target)
        if not check:
            out("Checking that everything followed:")
            other = next((n for n in names if n != app), None)
            finish_switch(cfg, other, app, out, announce=False)
        return 0
    out("Moving the desktop app%s to %s" % (" from %s" % app if app else "", target))
    out("")
    out("Getting %s ready" % target)
    auth = cf.auth_status(cfg, target) if snap["accounts"][target]["set_up"] else {"loggedIn": False}
    lines = readiness(cfg, snap, target, auth=auth)
    lines += sync_lines(run_sync(cfg, dry_run=check), target)
    running = recent_scheduled_runs(cfg, app, snap["now"]) if app else []
    if running:
        lines.append(("warn", "a scheduled task was active in the last 15 minutes (%s): if it is "
                              "still working, let it finish first" % ", ".join(running[:3])))
    ext = snap["extension"]
    if ext.get("state") == "ok" and ext["account"] != target:
        lines.append(("warn", "Chrome extension is on %s: after the app, sign it in as %s too" % (
            ext["account"], target)))
    _show(lines, out)
    failed = [text for level, text in lines if level == "fail"]
    if check:
        out("")
        out("%s can take over." % target if not failed else
            "%s cannot take over yet: fix the XX lines first." % target)
        return 1 if failed else 0
    if failed and not force:
        out("")
        out("%s cannot take over yet: fix the XX lines, then run this again "
            "(--force switches anyway)." % target)
        return 1
    email = snap["accounts"][target]["email"] or target
    if not assist and handoff_armed(cfg):
        out("")
        out("You only need this to move the app itself: a chat that hits its limit already")
        out("continues in Terminal on the other account, with the app left as it is.")
    out("")
    out("Your part, about 30 seconds:")
    out("  1. In the Claude app: your name (bottom left) > Log out")
    out("  2. Sign in as %s" % email)
    out("Chats that are mid-task stop when you log out. They are not lost: open them")
    out("after the switch and say \"continue\".")
    with edit_state() as st:
        st["pending"] = {"to": target, "since": cf.iso(cf.now_local())}
    watching = bool(installed_job()[0])
    later = ("The background watcher finishes the switch when it sees it." if watching
             else "Run `claude-accounts switch` again after you signed in.")
    if no_wait:
        out("")
        out("Not waiting. %s" % later)
        return 0
    minutes = int(wait) if wait is not None else (12 * 60 if assist else 30)
    out("")
    out("Waiting for the app to change account (up to %s). Ctrl-C stops the waiting, "
        "nothing else." % ("%d min" % minutes if minutes < 120 else "%d h" % (minutes // 60)))
    looked = [time.monotonic(), True]

    def still_needed():
        # Opened by the watcher at a limit: stop once the app's account works again.
        if not assist or app is None:
            return True
        if time.monotonic() - looked[0] >= 60:
            looked[0], looked[1] = time.monotonic(), assist_needed(cfg, app)
        return looked[1]

    try:
        uuid = wait_for_app(cfg, before, minutes * 60, keep_waiting=still_needed)
    except KeyboardInterrupt:
        out("")
        out("Stopped waiting. %s" % later)
        return 0
    if uuid is None:
        if assist and app and not assist_needed(cfg, app):
            out("%s has usage again, so there is no need to switch." % app)
            return 0
        out("The app did not change account. Run `claude-accounts switch` again any time.")
        return 1
    new = name_for_uuid(cfg, uuid)
    out("")
    if new is None:
        out("The app is now signed in to an account that is not in the config (%s). "
            "Add it with claude-accounts add, or sign in as %s." % (uuid, email))
        return 1
    if new != target:
        out("The app is now on %s, not %s. Finishing for %s." % (new, target, new))
    out("Finishing")
    claimed = claim_switch(new)
    finish_switch(cfg, app or "", new, out, announce=claimed is not None)
    ext = extension_account(cfg)
    if ext.get("account") != new and extension_installed(cfg):
        out("")
        out("Waiting up to 5 minutes for the Chrome extension to change account...")
        try:
            done = wait_for_extension(cfg, new, 300)
        except KeyboardInterrupt:
            done = False
        if done:
            out("  ok  Chrome extension is on %s" % new)
            lines, problems = apply_followers(cfg, snapshot(cfg))
            for line in lines:
                out("  ok  %s" % line)
            for line in problems:
                out("  !!  %s" % line)
        else:
            out("  !!  Chrome extension not switched yet. %s" % (
                "The watcher picks it up when you do it." if watching
                else "Run `claude-accounts watch` after you do it."))
    out("")
    out("Done. The app is on %s." % new)
    return 0


# --------------------------------------------------------------------------
# claude-accounts watch [install | uninstall | status]
# --------------------------------------------------------------------------

def plist_path():
    folder = os.environ.get("CLAUDE_FAILOVER_LAUNCH_AGENTS_DIR")
    folder = Path(os.path.expanduser(folder)) if folder else (
        Path(os.path.expanduser("~")) / "Library" / "LaunchAgents")
    return folder / (LABEL + ".plist")


def installed_job():
    """(minutes between passes, the script the job runs) from the installed plist,
    or (None, None) when there is no usable one."""
    try:
        with open(str(plist_path()), "rb") as f:
            job = plistlib.load(f)
    except Exception:
        return None, None
    if not isinstance(job, dict):
        return None, None
    secs, argv = job.get("StartInterval"), job.get("ProgramArguments")
    minutes = secs // 60 if isinstance(secs, int) and not isinstance(secs, bool) and secs > 0 else None
    script = argv[0] if isinstance(argv, list) and argv and isinstance(argv[0], str) else None
    return minutes, script


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


def set_setting(cfg, key, value):
    """Set switch.<key> in the config file (and in the config in memory)."""
    def mutate(raw):
        block = raw.get("switch")
        if not isinstance(block, dict):
            block = raw["switch"] = {}
        block[key] = value
    changed, problem = update_config(cfg, mutate)
    if not problem:
        if not isinstance(cfg.get("switch"), dict):
            cfg["switch"] = {}
        cfg["switch"][key] = value
    return changed, problem


def cmd_watch_install(cfg, args):
    args = list(args)
    every = _pop_option(args, "--every")
    if args or every == "":
        print(USAGE)
        return 2
    if every is None:
        minutes = _int(settings(cfg).get("interval_minutes"), DEFAULT_INTERVAL, MIN_INTERVAL, 1440)
    else:
        minutes = int(every) if every.isdigit() else 0
        if minutes < MIN_INTERVAL:
            print("--every takes whole minutes, at least %d." % MIN_INTERVAL)
            return 2
    if not os.access(str(ACCOUNTS_SCRIPT), os.X_OK):
        print("Cannot run %s, so nothing was installed." % ACCOUNTS_SCRIPT)
        return 1
    d = work_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(str(d), 0o700)
    log = str(d / "agent.log")
    env = {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}
    conf = config_file(cfg)
    if conf:
        env["CLAUDE_FAILOVER_CONFIG"] = conf    # launchd starts with almost no environment
    if os.environ.get("CLAUDE_FAILOVER_STATE_DIR"):
        env["CLAUDE_FAILOVER_STATE_DIR"] = os.path.abspath(
            os.path.expanduser(os.environ["CLAUDE_FAILOVER_STATE_DIR"]))
    job = {"Label": LABEL,
           "ProgramArguments": [str(ACCOUNTS_SCRIPT), "watch", "--quiet"],
           "StartInterval": minutes * 60,
           "RunAtLoad": True,
           "StandardOutPath": log,
           "StandardErrorPath": log,
           "EnvironmentVariables": env}
    plist = plist_path()
    try:
        plist.parent.mkdir(parents=True, exist_ok=True)
        _write_bytes(plist, plistlib.dumps(job), 0o644)
    except OSError as e:
        print("Could not write %s: %s" % (tilde(plist), why(e)))
        return 1
    domain = "gui/%d" % os.getuid()
    launchctl("bootout", "%s/%s" % (domain, LABEL))     # an older copy, if loaded; may fail
    rc, text = launchctl("bootstrap", domain, str(plist))
    print("Account watcher every %d min: %s" % (minutes, tilde(plist)))
    print("Log: %s" % tilde(log))
    _, problem = set_setting(cfg, "watch", True)
    if problem:
        print("Could not turn switch.watch on in the config: %s" % problem)
    if rc != 0:
        print("launchctl could not load it (%s). It loads at your next login, or run: "
              "launchctl bootstrap %s %s" % (text or "exit %d" % rc, domain, shlex.quote(str(plist))))
        return 1
    return 0


def cmd_watch_uninstall(cfg):
    launchctl("bootout", "gui/%d/%s" % (os.getuid(), LABEL))    # fails if not loaded: fine
    plist = plist_path()
    if os.path.lexists(str(plist)):
        try:
            os.unlink(str(plist))
        except OSError as e:
            print("Could not remove %s: %s" % (tilde(plist), why(e)))
            return 1
        print("Removed the account watcher: %s" % tilde(plist))
    else:
        print("The account watcher was not installed.")
    _, problem = set_setting(cfg, "watch", False)
    if problem:
        print("Could not turn switch.watch off in the config: %s" % problem)
    return 0


def cmd_watch_status(cfg):
    minutes, script = installed_job()
    plist = plist_path()
    lines, rc = ["Account watcher"], 0
    if minutes and (not script or not os.path.exists(script)):
        lines.append("  background job - BROKEN: it runs %s, which is missing (folder moved?) "
                     "- fix: claude-accounts watch install" % (tilde(script) if script else "nothing"))
        rc = 1
    elif minutes:
        lines.append("  background job - every %d min (%s)" % (minutes, tilde(plist)))
    else:
        lines.append("  background job - not installed (claude-accounts watch install)")
    modes = auto_order_modes(cfg)
    lines.append("  account order - %s" % (
        "kept in step for %s" % " and ".join(
            "background jobs" if m == "headless" else "Terminal chats" for m in modes)
        if modes else "left as written in the config (switch.auto_order is off)"))
    lines.append("  at a limit - %s" % at_limit_words(cfg))
    st = read_state()
    if st.get("app_account"):
        since = cf.parse_iso(st.get("app_changed_at"))
        lines.append("  desktop app - on %s%s" % (
            st["app_account"], " since %s" % when(since) if since else ""))
    order_set = st.get("order_set") if isinstance(st.get("order_set"), dict) else None
    if order_set:
        lines.append("  last order change - %s: %s" % (
            when(cf.parse_iso(order_set.get("at"))), order_set.get("reason")))
    last = cf.parse_iso(st.get("last_tick"))
    lines.append("  last pass - %s" % (
        "%s (%s)" % (when(last), ago(last, cf.now_local())) if last else "none yet"))
    for text in st.get("problems") if isinstance(st.get("problems"), list) else []:
        lines.append("  problem - %s" % text)
    lines.append("  log - %s" % tilde(work_dir() / "agent.log"))
    print("\n".join(lines))
    return rc


def cmd_watch(cfg, args):
    sub = args[0] if args else ""
    if sub == "install":
        return cmd_watch_install(cfg, args[1:])
    if sub in ("uninstall", "status"):
        if len(args) != 1:
            print(USAGE)
            return 2
        return cmd_watch_uninstall(cfg) if sub == "uninstall" else cmd_watch_status(cfg)
    if any(a != "--quiet" for a in args):
        print(USAGE)
        return 2
    lines = tick(cfg)
    if lines:
        stamp = cf.iso(cf.now_local())
        print("\n".join("%s  %s" % (stamp, line) for line in lines))
    elif "--quiet" not in args:
        print("Nothing to do.")
    return 0


def cli(cfg, cmd, argv):
    args = list(argv)
    if args and args[0] in ("help", "-h", "--help"):
        print(USAGE)
        return 0
    if cmd == "usage":
        return cmd_usage(cfg, args)
    if cmd == "switch":
        return cmd_switch(cfg, args)
    if cmd == "watch":
        return cmd_watch(cfg, args)
    print(USAGE)
    return 2
