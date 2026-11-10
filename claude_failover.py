"""claude-failover: run Claude Code on whichever of your Claude accounts still has usage.

Each account gets its own config folder (Claude Code's CLAUDE_CONFIG_DIR
setting), so each one keeps its own login. Everything in it except the login and
.claude.json is a symlink to ~/.claude, so memory, CLAUDE.md, settings, plugins
and transcripts are shared by every account.

Entry points:
  bin/claude-failover       drop-in replacement for the `claude` binary.
      -p runs: try accounts in order; if one is refused by a usage limit,
               remember when it resets and move the work to the next account
               (a fresh re-run if no tool ran yet, otherwise --resume of the same
               session so nothing is done twice).
      interactive: pick the first account that is not limited, then exec claude.
  bin/claude            tiny shim that runs bin/claude-failover, for PATH.
  bin/claude-failover-hook  StopFailure hook (`claude-accounts hook install`).
               When a desktop or Terminal chat hits a usage limit it records the
               limit and continues the same conversation in a new Terminal window
               on the next account.
  bin/claude-accounts       init / add / status / login / hook / sync and more.

Desktop sync (desktop_sync.py, optional): the Claude desktop app keeps its list
of Code chats per account, while the transcripts themselves are shared. After
switching the app to another account its sidebar looks empty. `claude-accounts
sync` copies the chat records between accounts so every account lists every chat.

Config: $CLAUDE_FAILOVER_CONFIG, else config.json next to this file when there is
one (the original place), else ~/.config/claude-failover/config.json, where
`claude-accounts init` writes a starter one.

Everything is stdlib Python 3.9. Logins stay inside the macOS keychain and only
the real claude binary touches them. The one secret this code reads is an
account's optional spare token (`claude-accounts token`, its own keychain item):
fetched at run time for a headless run whose login was refused, handed to claude
in its environment, and never written to a file, a log or the caller's output.
"""

import datetime as dt
import fcntl
import json
import os
import re
import select
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - python < 3.9
    ZoneInfo = None

PROJECT = Path(__file__).resolve().parent
WRAPPER = PROJECT / "bin" / "claude-failover"
HOOK_SCRIPT = PROJECT / "bin" / "claude-failover-hook"
LOCAL_TZ = dt.datetime.now().astimezone().tzinfo

# A CLI login lasts about 30 days (observed, not documented) and Claude only
# warns about it inside an interactive chat, which a headless job never is. So
# `claude-accounts login` records the date, and `claude-accounts status` (and any
# monitor that reads state/accounts.json) asks for a re-login from day 25.
LOGIN_LIFETIME_DAYS = 30
LOGIN_RENEW_DAY = 25
# Claude Code 2.1.318+ says when the stored login ends (`claude auth status`
# loginExpiresAt, -p JSON results credential_expires_at). The wrapper keeps the
# latest date it sees as login_expires_at and status uses it instead of the
# guess above. The CLI says the real end can be up to 3 days before that date,
# so the ask starts LOGIN_RENEW_LEAD_DAYS before the earliest day (the same
# 5-day lead the guess has).
LOGIN_EXPIRY_SLACK_DAYS = 3
LOGIN_RENEW_LEAD_DAYS = LOGIN_LIFETIME_DAYS - LOGIN_RENEW_DAY
# Spare token: a one-year token from `claude setup-token`, kept in its own
# keychain item (service below, account = the account name).
SPARE_TOKEN_SERVICE = "claude-failover-spare-token"
SPARE_TOKEN_DAYS = 365
SPARE_RENEW_DAY = 335


# --------------------------------------------------------------------------
# Config + state
# --------------------------------------------------------------------------

NO_CONFIG = "claude-failover: no config found - run: claude-accounts init"
# Where the real claude usually lives: the native installer, the older local
# install, Homebrew on Apple silicon, Homebrew on Intel. Checked in this order.
REAL_CLAUDE_PLACES = ("~/.local/bin/claude", "~/.claude/local/claude",
                      "/opt/homebrew/bin/claude", "/usr/local/bin/claude")


def user_config_file():
    """Where `claude-accounts init` writes the config: $CLAUDE_FAILOVER_CONFIG,
    else ~/.config/claude-failover/config.json."""
    env = os.environ.get("CLAUDE_FAILOVER_CONFIG")
    if env:
        # Absolute, so the state/ folder next to it doesn't move with the cwd.
        return Path(os.path.abspath(os.path.expanduser(env)))
    return Path(os.path.expanduser("~")) / ".config" / "claude-failover" / "config.json"


def config_path():
    """The config file in use, or None. $CLAUDE_FAILOVER_CONFIG when it is set (a
    missing file then means no config). Otherwise config.json in this repo when
    there is one, else ~/.config/claude-failover/config.json.

    The repo file comes first. It is gitignored, so only an install that keeps its
    config in its own folder (the original layout) has one. That install must keep
    working when another clone's `claude-accounts init` writes ~/.config."""
    if os.environ.get("CLAUDE_FAILOVER_CONFIG"):
        candidates = [user_config_file()]
    else:
        candidates = [PROJECT / "config.json", user_config_file()]
    for p in candidates:
        if p.is_file():
            return p
    return None


def expand(p):
    """'~/x' -> '<home>/x'. None, '' and absolute paths come back unchanged."""
    return os.path.expanduser(p) if isinstance(p, str) and p else p


def in_own_bin(path):
    """True if `path` resolves into this repo's bin/ folder. Running that as the
    real claude would start this wrapper again, forever."""
    own = os.path.realpath(str(PROJECT / "bin")) + os.sep
    return os.path.realpath(path).startswith(own)


def is_wrapper(path):
    """True if `path` is this wrapper or another copy of it (a second clone's
    `claude` shim on PATH). Two clones that each picked the other's shim would
    start each other forever. The real claude never names claude-failover."""
    if in_own_bin(path):
        return True
    try:
        with open(path, "rb") as f:
            head = f.read(4096)   # a shim is tiny; the real binary is huge, so only peek
    except OSError:
        return False
    return b"claude-failover" in head or b"claude_failover" in head


def find_real_claude():
    """The real claude binary: $CLAUDE_FAILOVER_REAL_CLAUDE, the usual install
    places, then each folder on PATH. Our own `claude` shim (and any other copy of
    this wrapper) is skipped. None if nothing is found."""
    places = [os.environ.get("CLAUDE_FAILOVER_REAL_CLAUDE") or ""]
    places += list(REAL_CLAUDE_PLACES)
    places += [os.path.join(d, "claude") for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    for p in places:
        p = expand(p)
        if p and os.path.isfile(p) and os.access(p, os.X_OK) and not is_wrapper(p):
            return os.path.abspath(p)
    return None


def load_config():
    """Read the config (see config_path) and fill in defaults. Paths may start
    with ~. Exits with one clear line, never a traceback, when there is no
    usable config."""
    path = config_path()
    if path is None:
        raise SystemExit(NO_CONFIG)
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        raise SystemExit("claude-failover: cannot read %s: %s" % (path, e))
    if not isinstance(cfg, dict):
        raise SystemExit("claude-failover: %s must hold a JSON object" % path)
    cfg["_config_path"] = str(path)
    for key in ("default_config_dir", "projects_dir", "state_dir", "handoff_dir"):
        if key in cfg:
            cfg[key] = expand(cfg[key])
    cfg["default_config_dir"] = cfg.get("default_config_dir") or expand("~/.claude")
    real = expand(cfg.get("real_claude"))
    if real and is_wrapper(real):
        # is_wrapper, not just in_own_bin: a copy of a wrapper shim outside this
        # folder would start this wrapper again just the same.
        raise SystemExit("claude-failover: real_claude in %s is %s, inside this tool's own "
                         "bin/ folder or another copy of claude-failover (it would run itself "
                         "forever). Set it to the real claude binary, or remove it to "
                         "auto-detect." % (path, real))
    cfg["real_claude"] = real or find_real_claude() or ""   # "" = not found (main says so)
    cfg["accounts"] = cfg.get("accounts") or []
    if not isinstance(cfg["accounts"], list) or not all(
            isinstance(a, dict) and isinstance(a.get("name"), str) and a["name"]
            for a in cfg["accounts"]):
        raise SystemExit('claude-failover: "accounts" in %s must be a list of {"name": ...} '
                         'entries' % path)
    if not isinstance(cfg.get("order") or {}, dict):
        raise SystemExit('claude-failover: "order" in %s must look like '
                         '{"interactive": [...], "headless": [...]}' % path)
    for a in cfg["accounts"]:
        for key in ("config_dir", "securestorage_dir"):
            if key in a:   # None and "" stay as they are ("" = reuse the plain keychain login)
                a[key] = expand(a[key])
    cfg["order"] = cfg.get("order") or {}
    cfg["order"].setdefault("interactive", [])
    cfg["order"].setdefault("headless", [])
    cfg.setdefault("unknown_reset_minutes", 30)
    cfg.setdefault("auth_down_recheck_minutes", 30)
    cfg.setdefault("turned_away_seconds", 90)
    cfg.setdefault("handoff", {"from_desktop": True, "from_terminal": True})
    cfg.setdefault("security", "/usr/bin/security")   # macOS keychain CLI (tests use a fake)
    cfg["by_name"] = {a["name"]: a for a in cfg["accounts"]}
    return cfg


def default_login_account(cfg):
    """The account behind the plain keychain item 'Claude Code-credentials' (what
    the real claude uses when no CLAUDE_CONFIG_DIR is set). None with no accounts."""
    name = cfg.get("default_login_account")
    if name in cfg["by_name"]:
        return name
    for a in cfg["accounts"]:
        if not a.get("config_dir") or a.get("securestorage_dir") == "":
            return a["name"]
    return cfg["accounts"][0]["name"] if cfg["accounts"] else None


def desktop_sync_on(cfg):
    """True if the config turns desktop sync on. Same rule as
    desktop_sync.settings(): only a {"enabled": ...} block counts, so a
    hand-written "desktop_sync": true is off here too, never a crash."""
    block = cfg.get("desktop_sync")
    return isinstance(block, dict) and bool(block.get("enabled"))


def state_dir():
    """Where accounts.json and events.jsonl live: $CLAUDE_FAILOVER_STATE_DIR, else
    the config's state_dir, else state/ next to the config file."""
    return _data_dir("CLAUDE_FAILOVER_STATE_DIR", "state_dir", "state")


def handoff_dir():
    """Where the hook writes its Terminal scripts: $CLAUDE_FAILOVER_HANDOFF_DIR,
    else the config's handoff_dir, else handoffs/ next to the config file."""
    return _data_dir("CLAUDE_FAILOVER_HANDOFF_DIR", "handoff_dir", "handoffs")


def _data_dir(env_key, cfg_key, default_name):
    # Looked up on every call (the config is a small file): state is read from
    # places that have no config at hand, and the answer can never go stale.
    if os.environ.get(env_key):
        return Path(os.path.expanduser(os.environ[env_key]))
    path = config_path()
    raw = read_json(path, {}) if path else {}
    value = raw.get(cfg_key) if isinstance(raw, dict) else None
    base = (path or user_config_file()).parent
    if isinstance(value, str) and value:
        return base / os.path.expanduser(value)   # a relative path counts from the config's folder
    return base / default_name


def now_local():
    return dt.datetime.now(LOCAL_TZ)


def iso(t):
    return t.isoformat(timespec="seconds") if t else None


def parse_iso(s):
    if not isinstance(s, str) or not s:
        return None
    # The CLI writes instants as 2026-12-01T13:59:00.473Z; Python 3.9's parser
    # takes neither the Z nor a fraction that isn't 3 or 6 digits. The fraction
    # is kept (transcript timestamps are compared to the second's microseconds).
    s = re.sub(r"\.(\d+)(?=Z$|[+-]\d\d:?\d\d$|$)",
               lambda m: "." + (m.group(1) + "000000")[:6], s.strip())
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=LOCAL_TZ)


def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def atomic_write_json(path, obj, mode=None, ensure_ascii=True):
    """Write JSON to a temp file, then rename it over `path`: a crash never leaves
    half a file. The new file is 0600 (mkstemp) unless `mode` says otherwise."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, ensure_ascii=ensure_ascii)
            f.write("\n")
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, str(path))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def save_user_json(path, obj, mode=None):
    """Save a JSON file the user owns (the config, settings.json) atomically,
    keeping non-ASCII text readable. A symlink is followed: its target is
    replaced and the link stays. mode=None keeps the file's current permissions
    (0600 for a new file). Returns the path actually written."""
    target = Path(os.path.realpath(path))
    if mode is None:
        try:
            mode = stat.S_IMODE(target.stat().st_mode)
        except OSError:
            mode = 0o600
    atomic_write_json(target, obj, mode=mode, ensure_ascii=False)
    return target


def backup_copy(src, folder):
    """Copy a file we are about to change to folder/<name>.bak-<YYYYmmdd-HHMMSS>
    (never over an earlier copy). A symlink's target is what gets copied. The copy
    is 0600: settings.json can hold API keys in its env block."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    data = Path(src).read_bytes()
    base = "%s.bak-%s" % (Path(src).name, now_local().strftime("%Y%m%d-%H%M%S"))
    dest, n = folder / base, 2
    while dest.exists():
        dest, n = folder / ("%s-%d" % (base, n)), n + 1
    # Created 0600 from the start (O_EXCL: never over another file).
    fd = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return dest


def read_state():
    st = read_json(state_dir() / "accounts.json", {})
    st.setdefault("accounts", {})
    st.setdefault("handoffs", {})
    return st


@contextmanager
def edit_state():
    """Lock, load, let the caller change it, save. Scheduled jobs run in parallel."""
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    with open(d / ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        st = read_state()
        yield st
        atomic_write_json(d / "accounts.json", st)


def log_event(event, **fields):
    rec = {"t": iso(now_local()), "event": event}
    rec.update(fields)
    try:
        d = state_dir()
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def note(msg, headless=False):
    """One line on stderr (never stdout: callers parse stdout).

    Headless runs stay silent unless CLAUDE_FAILOVER_VERBOSE=1: scripts often
    grep stderr for "limit" / "OAuth" and would misread a note about an account
    that was skipped. The details are always in state/events.jsonl."""
    if headless and os.environ.get("CLAUDE_FAILOVER_VERBOSE") != "1":
        return
    try:
        sys.stderr.write("[claude-failover] %s\n" % msg)
        sys.stderr.flush()
    except OSError:
        pass


# --------------------------------------------------------------------------
# Reading Claude's output: was this a usage limit, a lost login, or fine?
# --------------------------------------------------------------------------

# Prefixes Claude Code itself uses to classify usage-limit messages (the GSr/RTt
# list in the 2.1.25x-2.1.29x binaries) plus the older wordings. Anchored at the
# start of a line on purpose: "Server is temporarily limiting requests (not your
# usage limit)" is a server throttle, not our limit, and must not match.
APOS = "['’]"
LIMIT_RE = re.compile(
    r"^\s*(?:API Error:\s*)?(?:"
    r"You" + APOS + r"ve hit your"
    r"|You" + APOS + r"ve reached your"
    r"|You" + APOS + r"re out of (?:usage credits|extra usage)"
    r"|Your org is out of usage"
    r"|Fable(?: \d+(?:\.\d+)?)? requires usage credits"
    r"|Claude AI usage limit reached"
    r"|Usage limit reached"
    r"|\d+-hour limit reached"
    r"|(?:Session|Weekly) limit reached"
    r"|Your seat type doesn" + APOS + r"t include"
    r"|Your usage allocation has been disabled"
    r"|Your group" + APOS + r"s usage limit is set to \$0"
    r")",
    re.I | re.M,
)
NOT_OUR_LIMIT_RE = re.compile(r"not your usage limit", re.I)
AUTH_RE = re.compile(
    r"^\s*(?:API Error:\s*)?(?:"
    r"Not logged in"
    r"|Invalid API key"
    r"|Failed to authenticate"
    r"|OAuth (?:token|session) (?:has )?(?:expired|been revoked)"
    r"|.*Please run /login"
    r"|OAuth authentication is currently not supported"
    r")",
    re.I | re.M,
)
SCOPE_RE = re.compile(r"hit your (.+?) limit", re.I)
# Messages that mean "this model is out, others still work" (e.g. Fable needs
# usage credits once its included allowance is gone).
MODEL_ONLY_RE = re.compile(r"Switch to another model|requires usage credits", re.I)
MODEL_FAMILIES = ("opus", "sonnet", "haiku", "fable")


def _result_from_json(stdout):
    """--output-format json / stream-json: return (result_obj, rate_limit_info)."""
    text = stdout.strip()
    if not text.startswith("{") and not text.startswith("["):
        return None, None
    result, rli = None, None
    objs = []
    try:
        obj = json.loads(text)
        objs = obj if isinstance(obj, list) else [obj]
    except ValueError:
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    objs.append(json.loads(line))
                except ValueError:
                    pass
    for o in objs:
        if not isinstance(o, dict):
            continue
        if o.get("type") == "result":
            result = o
        if o.get("type") == "rate_limit_event":
            info = o.get("rate_limit_info") or {}
            if info.get("status") == "rejected":
                rli = info
    if result is not None and rli is None:
        params = result.get("api_error_params") or {}
        if isinstance(params.get("rate_limit_info"), dict):
            rli = params["rate_limit_info"]
    return result, rli


def classify(rc, stdout, stderr):
    """Return {"kind": ok|limit|auth|error, "text": the message line, ...}."""
    if rc == 0:
        return {"kind": "ok"}
    out = (stdout or "").strip()
    result, rli = _result_from_json(out)
    reset_epoch = None
    if rli and rli.get("resetsAt"):
        reset_epoch = rli.get("resetsAt")
    if result is not None:
        msg = str(result.get("result") or "")
        if result.get("api_error") == "usage_limit_reached" or (
                LIMIT_RE.search(msg) and not NOT_OUR_LIMIT_RE.search(msg)):
            return _limit_verdict(msg, reset_epoch)
        if AUTH_RE.search(msg):
            return {"kind": "auth", "text": first_line(msg)}
        if rli:
            return _limit_verdict(msg or "usage limit (rate_limit_event rejected)", reset_epoch)
        # fall through: maybe the message went to stderr
    # Plain text: an error run prints only the error, so look at the edges.
    lines = [l for l in (out + "\n" + (stderr or "")).splitlines() if l.strip()]
    edge = lines[:4] + lines[-4:]
    for line in edge:
        if LIMIT_RE.search(line) and not NOT_OUR_LIMIT_RE.search(line):
            return _limit_verdict(line.strip(), reset_epoch)
    for line in edge:
        if AUTH_RE.search(line):
            return {"kind": "auth", "text": line.strip()}
    return {"kind": "error", "text": first_line(out or stderr or "")}


def first_line(s):
    for line in (s or "").splitlines():
        if line.strip():
            return line.strip()[:300]
    return ""


def _limit_verdict(text, reset_epoch=None):
    v = {"kind": "limit", "text": first_line(text) or text[:300], "scope": "account",
         "family": None, "resets_at": None}
    m = SCOPE_RE.search(text)
    if m:
        word = m.group(1).strip().lower()
        for fam in MODEL_FAMILIES:
            if word.startswith(fam):
                v["scope"], v["family"] = "model", fam
    if reset_epoch:
        try:
            v["resets_at"] = dt.datetime.fromtimestamp(float(reset_epoch), LOCAL_TZ)
        except (TypeError, ValueError, OverflowError):
            pass
    if v["resets_at"] is None:
        v["resets_at"] = parse_resets(text)
    return v


MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
RESETS_RE = re.compile(r"resets\s+(?:at\s+|on\s+)?(.+?)(?:\s+[·∙|]|\s+-\s|$)", re.I | re.M)
WHEN_RE = re.compile(
    r"^(?:(?P<wd>mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s+)?"
    r"(?:(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?"
    r"(?:,?\s*(?P<yr>\d{4}))?,?\s*(?:at\s+)?)?"
    r"(?:(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)?)?$",
    re.I,
)


def parse_resets(text, now=None):
    """'... · resets 3:45pm (America/New_York)' -> aware datetime, else None."""
    m = RESETS_RE.search(text or "")
    if not m:
        return None
    when = m.group(1).strip().rstrip(".")
    tz = LOCAL_TZ
    tzm = re.search(r"\(([A-Za-z_]+/[A-Za-z_/+-]+|UTC)\)\s*$", when)
    if tzm:
        if ZoneInfo is not None:
            try:
                tz = ZoneInfo(tzm.group(1))
            except Exception:
                tz = LOCAL_TZ
        when = when[:tzm.start()].strip()
    w = WHEN_RE.match(when)
    if not w or not (w.group("h") or w.group("mon") or w.group("wd")):
        return None
    now = (now or now_local()).astimezone(tz)
    hour = int(w.group("h") or 0)
    minute = int(w.group("m") or 0)
    ap = (w.group("ap") or "").lower()
    if ap == "pm" and hour < 12:
        hour += 12
    if ap == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    if w.group("mon"):
        month = MONTHS.index(w.group("mon").lower()[:3]) + 1
        try:
            t = now.replace(year=int(w.group("yr") or now.year), month=month,
                            day=int(w.group("day")), hour=hour, minute=minute,
                            second=0, microsecond=0)
        except ValueError:
            return None
        if not w.group("yr") and t < now - dt.timedelta(days=200):
            t = t.replace(year=t.year + 1)
    else:
        t = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if w.group("wd"):
            target = WEEKDAYS.index(w.group("wd").lower()[:3])
            t += dt.timedelta(days=(target - t.weekday()) % 7)
            if t <= now:
                t += dt.timedelta(days=7)
        elif t < now - dt.timedelta(minutes=5):
            t += dt.timedelta(days=1)
    # Never trust a parse that lands absurdly far away.
    if t > now + dt.timedelta(days=8):
        return None
    return t.astimezone(LOCAL_TZ)


# --------------------------------------------------------------------------
# Accounts: which ones are set up, limited, or logged out
# --------------------------------------------------------------------------

def account_is_set_up(cfg, name):
    acct = cfg["by_name"][name]
    return not acct.get("config_dir") or Path(acct["config_dir"]).is_dir()


def account_block(st, name, now=None):
    """('ok', None) or ('limited'|'auth_down', until) for one account and model family."""
    return _block(st, name, None, now)


def _block(st, name, family, now=None):
    now = now or now_local()
    a = st["accounts"].get(name, {})
    until = parse_iso(a.get("limited_until"))
    if until and until > now:
        return "limited", until
    if family:
        mu = parse_iso((a.get("model_limits") or {}).get(family))
        if mu and mu > now:
            return "limited", mu
    ad = parse_iso(a.get("auth_down_until"))
    if ad and ad > now:
        return "auth_down", ad
    return "ok", None


def plan_accounts(cfg, mode, family=None, pinned=None):
    """Return (usable_now, blocked_sorted_by_when_they_free_up)."""
    if pinned:
        if pinned not in cfg["by_name"]:
            raise SystemExit("claude-failover: unknown account %r (CLAUDE_FAILOVER_ACCOUNT)" % pinned)
        return [pinned], []
    order = [n for n in cfg["order"].get(mode, []) if n in cfg["by_name"]]
    order += [a["name"] for a in cfg["accounts"] if a["name"] not in order]
    st = read_state()
    usable, blocked = [], []
    for name in order:
        if not account_is_set_up(cfg, name):
            continue
        kind, until = _block(st, name, family)
        if kind == "ok":
            usable.append(name)
        else:
            blocked.append((until, name))
    blocked.sort(key=lambda x: x[0])
    return usable, [n for _, n in blocked]


def record_verdict(cfg, name, verdict, family=None, spare=False, login_expires_at=None):
    """Remember a limit / lost login / success for an account. spare=True means the
    run used the account's spare token: then a success or a refused token says
    nothing about the login, so it is kept apart (a usage limit is the account's).
    login_expires_at: the credential_expires_at a -p JSON result reported, kept as
    the login's end date (on a spare-token run it would be the token's: ignored)."""
    now = now_local()
    with edit_state() as st:
        a = st["accounts"].setdefault(name, {})
        if login_expires_at and not spare and parse_iso(login_expires_at) is not None:
            a["login_expires_at"] = iso(parse_iso(login_expires_at))
        if spare and verdict["kind"] == "ok":
            a["spare_token_ok"] = iso(now)
            return
        if spare and verdict["kind"] == "auth":
            a["spare_token_failed"] = iso(now)
            a["spare_token_text"] = verdict.get("text")
            return
        if verdict["kind"] == "ok":
            a["last_ok"] = iso(now)
            a.pop("auth_down_until", None)
            a.pop("auth_text", None)
            # A success does NOT clear a saved limit: past its limit an account
            # with extra usage turned on still answers, but bills it as paid
            # overage. Limits expire at their reset time; a stale or wrong record
            # can't stop headless runs because blocked accounts are still tried
            # when nothing else is usable.
            return
        if verdict["kind"] == "limit":
            until = verdict.get("resets_at")
            if until is None or until <= now:
                until = now + dt.timedelta(minutes=cfg["unknown_reset_minutes"])
            fam = verdict.get("family")
            if not fam and family and MODEL_ONLY_RE.search(verdict.get("text") or ""):
                fam = family   # "...Switch to another model..." = only this model is out
            if fam:
                a.setdefault("model_limits", {})[fam] = iso(until)
            else:
                a["limited_until"] = iso(until)
            a["limit_text"] = verdict.get("text")
            a["limit_seen"] = iso(now)
        elif verdict["kind"] == "auth":
            a["auth_down_until"] = iso(now + dt.timedelta(minutes=cfg["auth_down_recheck_minutes"]))
            a["auth_text"] = verdict.get("text")


def clear_account(name=None):
    """Forget saved limits and login failures. Keeps the dates the login and the
    spare token were made: the early warning counts from them."""
    keep = ("logged_in_at", "login_expires_at", "spare_token_saved")
    with edit_state() as st:
        names = [name] if name else list(st["accounts"].keys())
        for n in names:
            old = st["accounts"].pop(n, None) or {}
            kept = {k: old[k] for k in keep if k in old}
            if kept:
                st["accounts"][n] = kept


def record_login(name, when=None):
    """Start an account's login clock (claude-accounts login / logged-in). A new
    login is a new credential, so the end date seen for the old one is dropped."""
    with edit_state() as st:
        a = st["accounts"].setdefault(name, {})
        a["logged_in_at"] = iso(when or now_local())
        a.pop("login_expires_at", None)


def record_login_expiry(name, stamp):
    """Keep the date the CLI says the stored login ends (claude auth status
    loginExpiresAt, -p JSON credential_expires_at), normalised to seconds."""
    t = parse_iso(stamp)
    if t is None:
        return
    with edit_state() as st:
        st["accounts"].setdefault(name, {})["login_expires_at"] = iso(t)


def note_login_end(name, info):
    """Record the end date from a `claude auth status` answer, if it names one.
    Never raises: bookkeeping must not break a status read or a login."""
    try:
        if isinstance(info, dict) and info.get("loggedIn"):
            record_login_expiry(name, info.get("loginExpiresAt"))
    except Exception:
        pass


def credential_expiry(stdout):
    """credential_expires_at from a -p JSON / stream-json result, else None."""
    result, _ = _result_from_json(stdout or "")
    if result is not None:
        v = result.get("credential_expires_at")
        if isinstance(v, str) and parse_iso(v) is not None:
            return v
    return None


def login_window(rec):
    """When to renew an account's CLI login, from its state record, as a dict:
    ends      the day the login ends: the CLI's date, else logged_in_at + 30 days
    earliest  the first day it can end (the CLI's date - 3 days; None for the guess)
    renew     the day claude-accounts status starts asking (5 days before earliest / ends)
    source    "cli" or "guess". None when no date is known at all.
    A CLI date from before the login itself belongs to a previous login: ignored."""
    logged_in = parse_iso(rec.get("logged_in_at"))
    ends_at = parse_iso(rec.get("login_expires_at"))
    if ends_at is not None and (logged_in is None or ends_at > logged_in):
        ends = ends_at.astimezone(LOCAL_TZ).date()
        earliest = ends - dt.timedelta(days=LOGIN_EXPIRY_SLACK_DAYS)
        return {"ends": ends, "earliest": earliest, "source": "cli",
                "renew": earliest - dt.timedelta(days=LOGIN_RENEW_LEAD_DAYS)}
    renew, ends = renewal_dates(rec.get("logged_in_at"), LOGIN_RENEW_DAY, LOGIN_LIFETIME_DAYS)
    if ends is None:
        return None
    return {"ends": ends, "earliest": None, "renew": renew, "source": "guess"}


def renewal_dates(stamp, renew_day, lifetime_days):
    """(renew-from date, expected expiry date) for a login or token made at `stamp`."""
    t = parse_iso(stamp)
    if t is None:
        return None, None
    d = t.astimezone(LOCAL_TZ).date()
    return d + dt.timedelta(days=renew_day), d + dt.timedelta(days=lifetime_days)


def spare_token_failing(rec):
    """True if the account's spare token was refused after it last worked or was saved
    (the same second counts: times are kept to the second)."""
    failed = parse_iso(rec.get("spare_token_failed"))
    good = [t for t in (parse_iso(rec.get("spare_token_ok")), parse_iso(rec.get("spare_token_saved"))) if t]
    return failed is not None and (not good or failed >= max(good))


def read_spare_token(cfg, name):
    """The account's spare token from the macOS keychain, or None. Read only when a
    run needs it; the caller must never write it anywhere."""
    try:
        r = subprocess.run([cfg["security"], "find-generic-password", "-s", SPARE_TOKEN_SERVICE,
                            "-a", name, "-w"], capture_output=True, text=True, timeout=15,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    token = r.stdout.strip()
    return token if r.returncode == 0 and token else None


def headless_attempts(st, names):
    """[(account, spare)] in the order a -p run tries them. Each account runs on its
    own login. One with a spare token saved also gets a second try on that token
    when its login is refused, and goes straight to the token while the login is
    known to be down (re-checked every auth_down_recheck_minutes)."""
    out = []
    for name in names:
        if not st["accounts"].get(name, {}).get("spare_token_saved"):
            out.append((name, False))
        elif _block(st, name, None)[0] == "auth_down":
            out += [(name, True), (name, False)]
        else:
            out += [(name, False), (name, True)]
    return out


def attempt_label(name, spare):
    return name + " (spare token)" if spare else name


# Everything an account folder shares with ~/.claude. The login (keychain item)
# and .claude.json (account metadata, caches) stay per account.
SHARED_DIRS = ["projects", "plugins", "skills", "agents", "commands", "output-styles",
               "file-history", "todos", "plans"]
SHARED = [
    "CLAUDE.md", "settings.json", "projects", "plugins", "skills", "agents", "commands",
    "output-styles", "hooks", "keybindings.json", "file-history", "todos", "plans",
    "history.jsonl", "scheduled-tasks",
]
# Top-level ~/.claude.json keys that describe the account or cache server data;
# everything else (onboarding done, folder trust, migrations) is copied once.
ACCOUNT_KEYS_PREFIXES = ("cached", "oauthAccount", "userID", "clientDataCache", "groveConfig",
                         "passesEligibility", "modelAccess", "additionalModel",
                         "orgModelDefault", "autoCompactWindows", "penguinMode",
                         "claudeAiMcpEverConnected", "s1mAccess", "subscriptionNotice",
                         "replBridge")


def make_account_folder(path):
    """Create an account folder (0700) if it is missing. None when it exists
    afterwards, else why it could not be made (for one clear line, never a
    traceback)."""
    try:
        Path(path).mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as e:
        return e.strerror or str(e)
    return None


def ensure_profile_links(cfg, name, seed=False):
    """Make an account folder share ~/.claude. Never replaces a real file or folder.
    seed=True also creates the folder; callers check it exists afterwards
    (make_account_folder says why it could not be made)."""
    acct = cfg["by_name"][name]
    if not acct.get("config_dir"):
        return []
    base = Path(cfg["default_config_dir"])
    prof = Path(acct["config_dir"])
    made = []
    if seed and make_account_folder(prof):
        return made
    if not prof.is_dir():
        return made
    # Every shared folder must exist in ~/.claude before it is linked, and must
    # never be empty: Claude's own cleanup deletes empty folders under ~/.claude,
    # which would leave the links dangling (it happened to plans/ and todos/).
    for item in SHARED_DIRS:
        try:
            (base / item).mkdir(parents=True, exist_ok=True)
            keep = base / item / ".keep"
            if not keep.exists():
                keep.write_text("kept by claude-failover: shared by ~/.claude-* account folders\n")
        except OSError:
            pass
    for item in SHARED:
        src, dst = base / item, prof / item
        if src.exists() and not dst.exists() and not dst.is_symlink():
            try:
                dst.symlink_to(src)
                made.append(item)
            except OSError:
                pass
    gj = prof / ".claude.json"
    if seed and not gj.exists():
        src = read_json(Path(os.path.expanduser("~")) / ".claude.json", {})
        if src:
            keep = {k: v for k, v in src.items() if not k.startswith(ACCOUNT_KEYS_PREFIXES)}
            try:
                atomic_write_json(gj, keep)
                os.chmod(gj, 0o600)
                made.append(".claude.json (seeded)")
            except OSError:
                pass   # only a head start: claude makes its own on first run
    return made


def shares_projects(cfg, name):
    """True if the account's transcripts land in the shared projects folder, the
    only place the wrapper looks. An account folder that kept its own projects/
    hides them: then the wrapper cannot tell whether a stopped run did anything."""
    acct = cfg["by_name"][name]
    if not acct.get("config_dir"):
        return True
    own = Path(acct["config_dir"]) / "projects"
    return os.path.realpath(own) == os.path.realpath(projects_dir(cfg))


def profile_problems(cfg, name):
    """Shared items that are real copies instead of links (would split memory)."""
    acct = cfg["by_name"][name]
    if not acct.get("config_dir"):
        return []
    base, prof = Path(cfg["default_config_dir"]), Path(acct["config_dir"])
    bad = []
    for item in SHARED:
        dst = prof / item
        if dst.exists() and not dst.is_symlink():
            bad.append(item)
        elif dst.is_symlink() and os.path.realpath(dst) != os.path.realpath(base / item):
            bad.append(item)
        elif dst.is_symlink() and not dst.exists():
            bad.append(item + " (link to a missing folder)")
    return bad


def unlinked_lines(cfg, name):
    """What `add`, `setup` and `status` print about shared items an account folder
    holds as its own copies: what that breaks and how to fix it. [] if none."""
    bad = profile_problems(cfg, name)
    if not bad:
        return []
    folder = tilde(cfg["by_name"][name]["config_dir"])
    base = tilde(cfg["default_config_dir"])
    gone = " (link to a missing folder)"
    own = [b for b in bad if not b.endswith(gone)]
    dangling = [b[:-len(gone)] for b in bad if b.endswith(gone)]
    lines = []
    if own:
        lines.append("not linked to %s, because %s already has its own: %s"
                     % (base, folder, ", ".join(own)))
        if "settings.json" in own:
            lines.append("its chats do not run the StopFailure hook in %s/settings.json, so "
                         "they are not handed off" % base)
        if "projects" in own:
            lines.append("the wrapper cannot see its transcripts, so its -p runs are not moved "
                         "to another account after a limit")
        lines.append("fix: merge what you want to keep into %s, move these out of %s, then "
                     "run: claude-accounts setup" % (base, folder))
    if dangling:
        lines.append("links to missing folders: %s - fix: claude-accounts setup" % ", ".join(dangling))
    return lines


# --------------------------------------------------------------------------
# Environment for the real claude
# --------------------------------------------------------------------------

# Login / provider selectors: always removed so the account we pick is the one used.
AUTH_ENV = {
    "CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR",
    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "CLAUDE_CODE_OAUTH_SCOPES",
    "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_SUBSCRIPTION_TYPE", "CLAUDE_CODE_RATE_LIMIT_TIER",
    "CLAUDE_CODE_ACCOUNT_UUID", "CLAUDE_CODE_ORGANIZATION_UUID", "CLAUDE_CODE_USER_EMAIL",
}
# Set by a parent Claude session (desktop app or CLI) for its own tools. A nested
# claude that inherits them thinks it is desktop-hosted (or even bare mode, which
# ignores the login). Removed when we are running inside a Claude session.
HOST_PREFIXES = ("CLAUDE_CODE_", "CLAUDE_AGENT_SDK_")
HOST_EXACT = {
    "CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_PREVIEW_CLASSIFIER_FLOOR",
    "AI_AGENT", "MCP_CONNECTION_NONBLOCKING", "MCP_SERVER_CONNECTION_BATCH_SIZE",
    "ANTHROPIC_BASE_URL",
}


def child_env(cfg, name, base=None, managed=False):
    env = dict(os.environ if base is None else base)
    nested = "CLAUDECODE" in env or "CLAUDE_CODE_ENTRYPOINT" in env
    for k in list(env):
        if k in AUTH_ENV:
            del env[k]
        elif nested and (k.startswith(HOST_PREFIXES) or k in HOST_EXACT):
            del env[k]
    env.pop("CLAUDE_FAILOVER_ACCOUNT", None)   # a pin applies to this call only
    acct = cfg["by_name"][name]
    if acct.get("config_dir"):
        env["CLAUDE_CONFIG_DIR"] = acct["config_dir"]   # exact absolute path: keychain item = sha256 of it
    if "securestorage_dir" in acct and acct["securestorage_dir"] is not None:
        # "" = use the plain keychain item 'Claude Code-credentials' (and ~/.claude for
        # its lock) while settings/.claude.json live in config_dir. Claude Code's own
        # override (uw()/FF() in the binary); it lets an account keep the plain login
        # without sharing ~/.claude.json, which the desktop app rewrites with its own
        # account.
        env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] = acct["securestorage_dir"]
    env["CLAUDE_FAILOVER_ACTIVE_ACCOUNT"] = name
    if managed:
        env["CLAUDE_FAILOVER_MANAGED"] = "1"
    else:
        env.pop("CLAUDE_FAILOVER_MANAGED", None)
    return env


# --------------------------------------------------------------------------
# Command line: enough of commander's rules to find the prompt and session flags
# --------------------------------------------------------------------------

def _load_argspec():
    """Option arities extracted from the claude binary (argspec.json, regenerate with
    `claude-accounts argspec` after a CLI update). Unknown options make the wrapper
    refuse to build a resume command rather than guess."""
    spec = read_json(PROJECT / "argspec.json", {})
    one = set(spec.get("one", [])) | {
        "--agent", "--agents", "--append-system-prompt", "--append-system-prompt-file",
        "--autocompact", "--debug-file", "--effort", "--environment", "--fallback-model",
        "--input-format", "--json-schema", "--max-budget-usd", "--max-turns", "--model",
        "-n", "--name", "--output-format", "--permission-mode", "--permission-prompt-tool",
        "--plugin-dir", "--plugin-url", "--remote-control-session-name-prefix",
        "--session-id", "--setting-sources", "--settings", "--system-prompt",
        "--system-prompt-file", "--permission-prompts", "--system-prompt-snapshot"}
    optional = set(spec.get("optional", [])) | {
        "-r", "--resume", "-d", "--debug", "--cloud", "--from-pr", "--prompt-suggestions",
        "--remote-control", "--teleport", "-w", "--worktree"}
    variadic = set(spec.get("variadic", [])) | {
        "--add-dir", "--allowedTools", "--allowed-tools", "--disallowedTools",
        "--disallowed-tools", "--betas", "--file", "--mcp-config", "--tools"}
    flags = set(spec.get("flag", [])) | {
        "-p", "--print", "-c", "--continue", "--fork-session", "--no-session-persistence",
        "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--verbose",
        "--strict-mcp-config", "--bare", "--chrome", "--no-chrome", "--bg", "--background",
        "--include-partial-messages", "--replay-user-messages", "-h", "--help", "-v", "--version"}
    one -= optional | variadic
    return one, optional, variadic, flags


ONE, OPTIONAL, VARIADIC, FLAGS = _load_argspec()
SUBCOMMANDS = {
    "agents", "attach", "auth", "auto-mode", "doctor", "gateway", "import", "install",
    "logs", "mcp", "plugin", "plugins", "project", "purge", "respawn", "rm", "setup-token",
    "stop", "kill", "ultrareview", "update", "upgrade", "help",
}
# Background-job commands: they must reach the account folder the job was started in.
JOB_SUBCOMMANDS = {"agents", "attach", "logs", "stop", "kill", "rm", "respawn"}

# `claude-accounts argspec` finds option definitions such as .option("-r, --resume [id]")
# in the claude binary.
OPTION_DEF_RE = re.compile(
    r'(?:new [A-Za-z_$][\w$]{0,3}\(|\.option\(|\.requiredOption\(|'
    r'\.addOption\(new [A-Za-z_$][\w$]{0,3}\()"((?:-[A-Za-z], )?--[a-z][a-zA-Z0-9-]*'
    r'(?: <[^>"]+>| \[[^\]"]+\])?)"')
# The binary is over 300 MB, so it is read in pieces instead of all at once. Each
# piece also holds the last ARGSPEC_OVERLAP bytes of the one before, so a
# definition cut in two at a piece boundary is still seen whole (a definition is
# a few dozen bytes, far shorter than the overlap).
ARGSPEC_CHUNK = 8 * 1024 * 1024
ARGSPEC_OVERLAP = 4096


def option_specs(path, chunk_size=ARGSPEC_CHUNK, overlap=ARGSPEC_OVERLAP):
    """Every option definition string ("-r, --resume [id]") in a binary, read
    piece by piece. One inside an overlap can come out twice: callers collect
    them in sets. Raises OSError when the file can't be read."""
    with open(path, "rb") as f:
        tail = b""
        while True:
            block = f.read(chunk_size)
            if not block:
                return
            data = tail + block
            # latin-1 maps every byte to one character, so nothing fails to decode
            # and the pattern sees the same text as on the whole file.
            for m in OPTION_DEF_RE.finditer(data.decode("latin-1")):
                yield m.group(1)
            tail = data[-overlap:]


def parse_argv(argv):
    """Split argv into options and positionals the way claude's parser would."""
    info = {"print": False, "positionals": [], "opts": {}, "subcommand": None, "unknown": []}
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            info["positionals"] += [(j, argv[j]) for j in range(i + 1, len(argv))]
            break
        if tok.startswith("-") and len(tok) > 1:
            name, eq, val = tok.partition("=")
            if (not eq and not tok.startswith("--") and len(tok) > 2
                    and tok[:2] in (ONE | OPTIONAL)):
                name, eq, val = tok[:2], "=", tok[2:]   # commander: -rVALUE == -r VALUE
            if name in ("-p", "--print"):
                info["print"] = True
            if name not in ONE | OPTIONAL | VARIADIC | FLAGS:
                info["unknown"].append(name)
            if eq:
                info["opts"][name] = val
            elif name in ONE:
                if i + 1 < len(argv):
                    info["opts"][name] = argv[i + 1]
                    i += 1
            elif name in OPTIONAL:
                if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                    info["opts"][name] = argv[i + 1]
                    i += 1
                else:
                    info["opts"][name] = True
            elif name in VARIADIC:
                vals = []
                while i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                    vals.append(argv[i + 1])
                    i += 1
                info["opts"][name] = vals
            else:
                info["opts"][name] = True
        else:
            if not info["positionals"] and info["subcommand"] is None and tok in SUBCOMMANDS:
                info["subcommand"] = tok
                break
            info["positionals"].append((i, tok))
        i += 1
    return info


def opt(info, *names):
    for n in names:
        if n in info["opts"]:
            return info["opts"][n]
    return None


def model_family(cfg, info):
    model = opt(info, "--model")
    if not isinstance(model, str) or not model:
        settings = read_json(Path(cfg["default_config_dir"]) / "settings.json", {})
        model = settings.get("model") or ""
    model = model.lower()
    for fam in MODEL_FAMILIES:
        if fam in model:
            return fam
    return None


# --------------------------------------------------------------------------
# Transcripts: did the stopped run already do something?
# --------------------------------------------------------------------------

def projects_dir(cfg):
    return Path(cfg.get("projects_dir") or Path(cfg["default_config_dir"]) / "projects")


def find_transcript(cfg, session_id):
    if not session_id:
        return None
    root = projects_dir(cfg)
    name = session_id + ".jsonl"
    try:
        for entry in os.scandir(root):
            p = Path(entry.path) / name
            if p.is_file():
                return p
    except OSError:
        return None
    return None


def transcript_has_tool_use(path, since=None):
    """True if an assistant message in the transcript called a tool (after `since`)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"tool_use"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("type") != "assistant":
                    continue
                if since is not None:
                    ts = parse_iso((rec.get("timestamp") or "").replace("Z", "+00:00"))
                    if ts and ts < since:
                        continue
                content = (rec.get("message") or {}).get("content") or []
                if any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content):
                    return True
    except OSError:
        return False
    return False


def transcript_tail_fields(path, max_bytes=2_000_000):
    """Last permissionMode and assistant model recorded in a transcript."""
    mode = model = None
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return None, None
    for line in data.splitlines():
        if '"permissionMode"' in line or '"model"' in line:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("permissionMode"):
                mode = rec["permissionMode"]
            m = (rec.get("message") or {}).get("model") if rec.get("type") == "assistant" else None
            if m and not m.startswith("<"):
                model = m
    return mode, model


def family_of(model):
    model = (model or "").lower()
    for fam in MODEL_FAMILIES:
        if fam in model:
            return fam
    return None


# --------------------------------------------------------------------------
# Running the real claude (headless)
# --------------------------------------------------------------------------

_CURRENT = {"child": None, "watchdog": None, "stop_signal": None}

# Tiny watchdog: if the wrapper itself is SIGKILLed (subprocess.run timeout kills
# only the pid it started), it takes the orphaned claude down with it.
WATCHDOG_SRC = r"""
import os, select, signal, subprocess, sys, time
wpid, cpid = int(sys.argv[1]), int(sys.argv[2])
kq = select.kqueue()
try:
    kq.control([select.kevent(wpid, select.KQ_FILTER_PROC, select.KQ_EV_ADD, select.KQ_NOTE_EXIT)], 0)
    kq.control(None, 1, None)
except OSError:
    pass
def alive_orphan():
    try:
        out = subprocess.run(["/bin/ps", "-o", "ppid=", "-p", str(cpid)], capture_output=True, text=True).stdout.strip()
    except Exception:
        return False
    return out == "1"
if alive_orphan():
    os.kill(cpid, signal.SIGTERM)
    for _ in range(50):
        time.sleep(0.2)
        if not alive_orphan():
            sys.exit(0)
    try:
        os.kill(cpid, signal.SIGKILL)
    except OSError:
        pass
"""


def _forward(sig, _frame):
    _CURRENT["stop_signal"] = sig
    child = _CURRENT["child"]
    if child is not None and child.poll() is None:
        try:
            child.send_signal(sig)
        except OSError:
            pass


def install_signal_forwarding():
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _forward)


def slurp_stdin(idle_seconds=3.0):
    """Read piped stdin once so it can be replayed on a second account."""
    try:
        fd = sys.stdin.fileno()
        st = os.fstat(fd)
    except (OSError, ValueError, AttributeError):
        return None
    if os.isatty(fd):
        return None
    if stat.S_ISREG(st.st_mode):
        with os.fdopen(os.dup(fd), "rb") as f:
            return f.read()
    chunks = []
    while True:
        try:
            ready, _, _ = select.select([fd], [], [], idle_seconds)
        except (OSError, ValueError):
            break
        if not ready:
            if chunks:
                continue  # data started arriving: read to EOF like claude does
            break  # nobody is writing and the pipe never closes: no stdin
        data = os.read(fd, 65536)
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks)


def run_once(cfg, argv, env, stdin_bytes):
    """Run the real claude, capture everything. Returns (rc, out, err, seconds)."""
    start = time.monotonic()
    stdin_arg = subprocess.PIPE if stdin_bytes is not None else None
    child = subprocess.Popen([cfg["real_claude"]] + argv, env=env, stdin=stdin_arg,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _CURRENT["child"] = child
    watchdog = None
    try:
        watchdog = subprocess.Popen(
            ["/usr/bin/python3", "-I", "-c", WATCHDOG_SRC, str(os.getpid()), str(child.pid)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True)
    except OSError:
        watchdog = None
    try:
        out, err = child.communicate(stdin_bytes)
    finally:
        _CURRENT["child"] = None
        if watchdog is not None:
            try:
                watchdog.kill()
                watchdog.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
    return child.returncode, out, err, time.monotonic() - start


def continuation_prompt(prev, verdict, nxt):
    why = "a usage limit" if verdict["kind"] == "limit" else "a lost login"
    return (
        "[claude-failover] The previous attempt at this task stopped because Claude account "
        "'%s' hit %s (%s). You are now running on account '%s'. Continue the original task "
        "from exactly where it stopped. Do not repeat any action that already completed - check "
        "the tool results above - and give the final output in the format the original task asked for."
        % (prev, why, verdict.get("text") or "", nxt)
    )


def add_opts(argv, extra):
    """Append options, but before a `--` terminator so they stay options."""
    if "--" in argv:
        i = argv.index("--")
        return argv[:i] + extra + argv[i:]
    return argv + extra


def build_resume_argv(argv, info, session_id, prompt):
    """Same options as the caller, minus its prompt and session flags, plus --resume."""
    drop = {i for i, _ in info["positionals"]}
    out, i = [], 0
    while i < len(argv):
        tok = argv[i]
        name = tok.partition("=")[0]
        if i in drop:
            i += 1
            continue
        if name in ("--session-id", "-r", "--resume", "-c", "--continue", "--fork-session"):
            has_inline = "=" in tok
            takes_value = name in ("--session-id",) or (
                name in ("-r", "--resume") and i + 1 < len(argv) and not argv[i + 1].startswith("-"))
            i += 1 if (has_inline or not takes_value) else 2
            continue
        out.append(tok)
        i += 1
    # Put the prompt right after -p so no variadic option can swallow it.
    out = [t for t in out if t != "--"]
    for j, tok in enumerate(out):
        if tok in ("-p", "--print"):
            return out[:j + 1] + [prompt] + out[j + 1:] + ["--resume", session_id]
    return ["-p", prompt] + out + ["--resume", session_id]


PERMISSION_MODES = {"default", "acceptEdits", "plan", "auto", "bypassPermissions", "dontAsk"}


def headless_mode(info):
    """The --permission-mode to pin on a -p run that names none (None = leave it).
    settings.json may make bypassPermissions the default for chats. A -p run that
    reads that file would inherit it, and in bypass mode --allowedTools limits
    nothing. So every -p run gets an explicit mode: the one its own --settings
    JSON asks for, else default, where every tool outside --allowedTools is
    denied (what scheduled jobs count on)."""
    if opt(info, "--permission-mode") is not None or opt(info, "--dangerously-skip-permissions") is not None:
        return None
    mode, raw = "default", opt(info, "--settings")
    if isinstance(raw, str) and raw.strip():
        try:
            text = raw if raw.lstrip().startswith("{") else Path(os.path.expanduser(raw)).read_text()
            want = (json.loads(text).get("permissions") or {}).get("defaultMode")
            if want in PERMISSION_MODES:
                mode = want
        except (OSError, ValueError, AttributeError):
            pass
    return mode


def run_headless(cfg, argv, pinned):
    info = parse_argv(argv)
    pin = headless_mode(info)
    if pin:
        argv = add_opts(argv, ["--permission-mode", pin])
        info = parse_argv(argv)
    family = model_family(cfg, info)
    stdin_bytes = slurp_stdin()
    usable, blocked = plan_accounts(cfg, "headless", family, pinned)
    # Known-limited accounts are skipped. If every account is limited, still try them
    # all, soonest reset first: a saved limit can be stale or wrong, and a refused
    # request costs a couple of seconds and no usage.
    attempts = headless_attempts(read_state(), usable or blocked)
    if not attempts:
        note("no Claude account is set up (see claude-accounts status)", headless=True)
        return 1
    has = lambda *f: any(opt(info, x) is not None for x in f)
    caller_sid = opt(info, "--session-id")
    caller_resume = opt(info, "-r", "--resume")
    fresh = not has("--session-id", "-r", "--resume", "-c", "--continue",
                    "--no-session-persistence", "--fork-session")
    install_signal_forwarding()
    started = dt.datetime.now(dt.timezone.utc)

    cur_argv, cur_stdin = list(argv), stdin_bytes
    session_id = caller_sid if isinstance(caller_sid, str) else (
        caller_resume if isinstance(caller_resume, str) else None)
    # We can only move work safely when we know which transcript to look at.
    if fresh or (has("--fork-session") and not isinstance(caller_sid, str)
                 and isinstance(caller_resume, str)):
        session_id = str(uuid.uuid4())
        cur_argv = add_opts(argv, ["--session-id", session_id])
    managed = session_id is not None and not has("--no-session-persistence")

    rc, out, err = 1, b"", b""
    verdict = {"kind": "error"}
    first_partial = None   # (rc, out, err, kind, account) of an attempt that stopped after doing work
    moved_to = []
    while attempts:
        name, spare = attempts.pop(0)
        here = attempt_label(name, spare)
        if _CURRENT["stop_signal"] is not None:
            break   # the caller asked us to stop between attempts
        ensure_profile_links(cfg, name)
        env = child_env(cfg, name, managed=True)
        token = read_spare_token(cfg, name) if spare else None
        if spare and not token:
            log_event("spare_token_unreadable", account=name)
            continue   # keychain item gone or locked: go on to the next attempt
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        attempt_started = dt.datetime.now(dt.timezone.utc)
        try:
            rc, out, err, secs = run_once(cfg, cur_argv, env, cur_stdin)
        except OSError as e:
            note("cannot run %s: %s" % (cfg["real_claude"], e))
            return 127
        if token:   # claude doesn't print it; this makes sure it never reaches a log
            out = out.replace(token.encode(), b"[spare token]")
            err = err.replace(token.encode(), b"[spare token]")
        out_text = out.decode("utf-8", "replace")
        verdict = classify(rc, out_text, err.decode("utf-8", "replace"))
        try:
            record_verdict(cfg, name, verdict, family, spare,
                           login_expires_at=None if spare else credential_expiry(out_text))
            log_event("run", account=name, kind=verdict["kind"], rc=rc, seconds=round(secs, 1),
                      cwd=os.getcwd(), text=verdict.get("text"),
                      **({"spare_token": True} if spare else {}))
            if verdict["kind"] == "auth":
                warn_logged_out(cfg, name, verdict, spare)
        except Exception as e:  # bookkeeping must never change what the caller sees
            note("state not saved: %s" % e, headless=True)
        if verdict["kind"] in ("ok", "error") or _CURRENT["stop_signal"] is not None:
            break
        if verdict["kind"] == "limit":
            # A usage limit belongs to the account, so its spare token would hit it too.
            attempts = [a for a in attempts if a[0] != name]
        if not attempts:
            note("%s: %s - no other account available" % (here, verdict.get("text")), headless=True)
            break
        nxt = attempt_label(*attempts[0])
        if not shares_projects(cfg, name):
            # Its transcripts are in its own projects/ folder, out of our sight (and
            # out of reach of a --resume on the next account).
            note("%s: its projects folder is not the shared one - cannot tell whether work "
                 "already ran - not moving it" % here, headless=True)
            break
        # Move the work: resume the same session if anything already happened in it
        # (so no action runs twice); re-run only when we can see nothing ran.
        transcript = find_transcript(cfg, session_id) if managed else None
        if transcript is None:
            if not (fresh and secs <= cfg["turned_away_seconds"]):
                note("%s: cannot tell whether work already ran - not moving it" % here, headless=True)
                break
            action = "rerun"
        elif transcript_has_tool_use(transcript, since=None if fresh else started):
            action = "resume"
        elif fresh:
            action = "rerun"
        else:
            action = "resume"
        if action == "resume" and info["unknown"]:
            # Options this wrapper doesn't know could take a value; rebuilding the
            # command could garble it. Safer to stop than to guess.
            note("%s: unknown option(s) %s - not resuming on another account"
                 % (here, " ".join(info["unknown"])), headless=True)
            break
        if action == "resume":
            first_partial = first_partial or (rc, out, err, verdict["kind"], here)
            moved_to.append(nxt)
            prompt = continuation_prompt(here, verdict, nxt)
            cur_argv = build_resume_argv(argv, info, session_id, prompt)
            # Piped stdin was the first turn's input; it is already in the transcript.
            cur_stdin = None if stdin_bytes is None else b""
        else:
            cur_argv, cur_stdin = list(argv), stdin_bytes
            session_id = str(uuid.uuid4())
            cur_argv = add_opts(argv, ["--session-id", session_id])
        note("%s: %s -> %s on %s" % (here, verdict.get("text"),
                                     "resuming the session" if action == "resume" else "re-running", nxt),
             headless=True)
        log_event("failover", frm=here, to=nxt, how=action, session=session_id)
    sig = _CURRENT["stop_signal"]
    # Work already ran on an earlier account and a later account then failed for a
    # different reason. Never let that later error (e.g. "OAuth expired") speak for
    # the run: scripts read auth wording as "nothing happened, retry", which
    # would redo the work. Report the original usage limit, or a plain marker.
    # That includes a later claude killed by a signal the caller did not send (a
    # crash, or macOS under memory pressure): its 128+n exit would invite a retry.
    if (rc != 0 and first_partial is not None and sig is None
            and verdict["kind"] != "limit"):
        p_rc, p_out, p_err, p_kind, p_acct = first_partial
        transcript = find_transcript(cfg, session_id) if managed else None
        later_worked = transcript is not None and transcript_has_tool_use(
            transcript, since=attempt_started)
        if p_kind == "limit" and not later_worked:
            rc, out, err = p_rc, p_out, p_err   # the move never got going: report the limit
        else:
            rc, err = 1, b""
            out = ("claude-failover: PARTIAL WORK DONE - this task started on %s and was moved "
                   "to %s, which stopped before finishing. Check what already happened before "
                   "running it again.\n" % (p_acct, ", ".join(moved_to) or "another account")).encode()
    try:
        sys.stdout.buffer.write(out)
        sys.stdout.buffer.flush()
    except (OSError, ValueError):
        pass
    try:
        sys.stderr.buffer.write(err)
        sys.stderr.buffer.flush()
    except (OSError, ValueError):
        pass
    if sig is not None:
        return 128 + int(sig)
    return rc if rc >= 0 else 128 - rc


def warn_logged_out(cfg, name, verdict, spare=False):
    """A lost login (or a refused spare token) can be silent to headless callers
    when a spare token or another account covers it, so tell the user directly
    with a macOS notification - at most every 6 hours per account."""
    key = "spare_warned" if spare else "logout_warned"
    with edit_state() as st:
        a = st["accounts"].setdefault(name, {})
        last = parse_iso(a.get(key))
        if last and now_local() - last < dt.timedelta(hours=6):
            return
        a[key] = iso(now_local())
        has_spare = bool(a.get("spare_token_saved"))
    if spare:
        notify("Claude: %s spare token refused" % name,
               "Make a new one: claude-accounts token %s" % name)
    else:
        notify("Claude: %s is logged out" % name,
               "Fix: claude-accounts login %s%s" % (
                   name, " (headless runs use its spare token meanwhile)" if has_spare else ""))


# --------------------------------------------------------------------------
# Interactive + passthrough
# --------------------------------------------------------------------------

def pick_interactive(cfg, info, pinned):
    family = model_family(cfg, info)
    usable, blocked = plan_accounts(cfg, "interactive", family, pinned)
    if usable:
        name = usable[0]
        first = cfg["order"]["interactive"][0] if cfg["order"].get("interactive") else name
        if name != first and not pinned:
            st = read_state()
            kind, until = _block(st, first, family)
            if kind != "ok":
                note("using %s (%s is %s until %s)" % (name, first, kind.replace("_", " "),
                                                      until.strftime("%a %-I:%M%p") if until else "?"))
        return name
    if blocked:
        st = read_state()
        _, until = _block(st, blocked[0], family)
        note("every account is limited; trying %s (frees up %s)" % (
            blocked[0], until.strftime("%a %-I:%M%p") if until else "soon"))
        return blocked[0]
    raise SystemExit("claude-failover: no Claude account is set up (run claude-accounts status)")


def plain_env(base=None):
    """Caller's environment minus only our own variables (for --bare: no account
    switching applies, and it needs the caller's ANTHROPIC_API_KEY)."""
    env = dict(os.environ if base is None else base)
    for k in list(env):
        if k.startswith("CLAUDE_FAILOVER_"):
            del env[k]
    return env


# Config that lives in ONE .claude.json: every account folder has its own, and
# the desktop app reads the plain ~/.claude.json. These commands run once per
# target (`claude mcp add/remove` with local/user scope, `claude purge`).
MCP_WRITE = {"add", "add-json", "remove"}


def config_targets(cfg):
    """[(label, env)] for every account folder plus the desktop's plain ~/.claude."""
    targets = [(a["name"], child_env(cfg, a["name"])) for a in cfg["accounts"]
               if account_is_set_up(cfg, a["name"])]
    plain = child_env(cfg, default_login_account(cfg))
    for k in ("CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR"):
        plain.pop(k, None)
    targets.append(("desktop app (~/.claude.json)", plain))
    return targets


def run_everywhere(cfg, argv, what):
    """Run the same command for every config target. Success if any target took it
    (the others then just report 'already exists' / 'not found')."""
    rcs = []
    for label, env in config_targets(cfg):
        note("%s -> %s" % (what, label))
        try:
            rcs.append(subprocess.call([cfg["real_claude"]] + argv, env=env))
        except OSError as e:
            note("cannot run %s: %s" % (cfg["real_claude"], e))
            return 127
    return 0 if 0 in rcs else (rcs[-1] if rcs else 1)


def mcp_scope(argv):
    for i, t in enumerate(argv):
        if t in ("-s", "--scope") and i + 1 < len(argv):
            return argv[i + 1]
        if t.startswith("--scope="):
            return t.split("=", 1)[1]
        if t.startswith("-s") and len(t) > 2 and not t.startswith("--"):
            return t[2:]
    return "local"


def job_folders(cfg):
    """[(env, folder)] where background jobs can live: each account folder, plus
    the plain ~/.claude (jobs started before the account folders existed)."""
    out = [(child_env(cfg, a["name"]), Path(a.get("config_dir") or cfg["default_config_dir"]))
           for a in cfg["accounts"] if account_is_set_up(cfg, a["name"])]
    plain = child_env(cfg, default_login_account(cfg))
    for k in ("CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR"):
        plain.pop(k, None)
    out.append((plain, Path(cfg["default_config_dir"])))
    return out


def job_env(cfg, args, pinned):
    """Environment for attach/logs/stop/rm/... : the folder that holds that job."""
    if pinned:
        return child_env(cfg, pinned)
    ids = [a for a in args if not a.startswith("-")]
    newest, newest_t = None, -1.0
    for env, folder in job_folders(cfg):
        jobs = folder / "jobs"
        try:
            entries = [e for e in jobs.iterdir() if e.is_dir()]
        except OSError:
            continue
        for e in entries:
            if any(e.name.startswith(i) for i in ids):
                return env
            try:
                t = (e / "state.json").stat().st_mtime
            except OSError:
                t = 0
            if t > newest_t:
                newest, newest_t = env, t
    if not ids and newest is not None:
        return newest   # `claude agents` / `logs` with no id: the folder with the latest job
    st = read_state()
    name = st.get("bg_account") or pick_interactive(cfg, {"opts": {}}, None)
    return child_env(cfg, name)


def claude_missing(cfg):
    """The error line when the real claude can't be run, else None."""
    if not cfg["real_claude"]:
        return ("claude CLI not found - install Claude Code, or set real_claude in %s"
                % cfg["_config_path"])
    if not os.access(cfg["real_claude"], os.X_OK):
        line = "claude CLI not found at %s (real_claude in %s)" % (cfg["real_claude"],
                                                                   cfg["_config_path"])
        # Claude Code moved (say from Homebrew to the native installer): say where.
        found = find_real_claude()
        if found and found != cfg["real_claude"]:
            line += " - Claude Code is at %s now: remove real_claude from the config to use it" % found
        return line
    return None


def effective_real_claude():
    """For install.sh: the claude binary the wrapper will run (the config's
    real_claude, else the one detection finds), or "" when there is none or the
    config can't be used. A configured path that is gone comes back as it is, so
    install.sh can say so."""
    if config_path() is None:
        return find_real_claude() or ""
    try:
        return load_config()["real_claude"]
    except SystemExit:
        return ""


def main(argv):
    cfg = load_config()
    missing = claude_missing(cfg)
    if missing:
        note(missing)
        return 127
    if not cfg["accounts"]:
        # No accounts yet (bin/ can be on PATH before the first `claude-accounts
        # add`). Do no harm: run the real claude exactly as a plain `claude` would.
        if os.isatty(2):
            note("no accounts in %s yet, so no failover - add one: claude-accounts add <name>"
                 % tilde(cfg["_config_path"]))
        os.execve(cfg["real_claude"], [cfg["real_claude"]] + argv, plain_env())
    pinned = os.environ.get("CLAUDE_FAILOVER_ACCOUNT") or None
    if os.environ.get("CLAUDE_FAILOVER", "").lower() in ("off", "0", "no"):
        names = [n for n in cfg["order"]["interactive"] if account_is_set_up(cfg, n)]
        name = pinned or (names[0] if names else cfg["accounts"][0]["name"])
        os.execve(cfg["real_claude"], [cfg["real_claude"]] + argv, child_env(cfg, name))
    info = parse_argv(argv)
    streaming_input = opt(info, "--input-format") == "stream-json"
    sub = info["subcommand"]
    if opt(info, "--bare") is not None:
        # --bare never reads a subscription login; pass straight through.
        os.execve(cfg["real_claude"], [cfg["real_claude"]] + argv, plain_env())
    if sub == "mcp" and not pinned and len(argv) > argv.index("mcp") + 1 \
            and argv[argv.index("mcp") + 1] in MCP_WRITE \
            and mcp_scope(argv[argv.index("mcp") + 2:]) != "project":
        return run_everywhere(cfg, argv, "mcp")   # project scope = ./.mcp.json: once is enough
    if sub == "purge" and not pinned:
        return run_everywhere(cfg, argv, "purge")
    bg = opt(info, "--bg", "--background") is not None
    if sub in JOB_SUBCOMMANDS or bg:
        # Background jobs live in the account folder that started them; keep the
        # launch and attach/stop/logs on the same account.
        if bg:
            name = pick_interactive(cfg, info, pinned)
            try:
                with edit_state() as st2:
                    st2["bg_account"] = name
            except Exception:
                pass
            ensure_profile_links(cfg, name)
            os.execve(cfg["real_claude"], [cfg["real_claude"]] + argv, child_env(cfg, name))
        os.execve(cfg["real_claude"], [cfg["real_claude"]] + argv,
                  job_env(cfg, argv[argv.index(sub) + 1:], pinned))
    if sub:
        # auth / update / purge ... act on one predictable account: the default
        # login (config default_login_account) unless CLAUDE_FAILOVER_ACCOUNT says otherwise.
        name = pinned or default_login_account(cfg)
        if sub in ("auth", "mcp", "setup-token", "purge") and os.isatty(2):
            note("'%s' runs on account %s (set CLAUDE_FAILOVER_ACCOUNT to change; "
                 "to log in use: claude-accounts login <account>)" % (sub, name))
        os.execve(cfg["real_claude"], [cfg["real_claude"]] + argv, child_env(cfg, name))
    # A stream-json caller that writes ONE message and closes stdin (it says so
    # with CLAUDE_FAILOVER_ONESHOT_STDIN=1) is a plain headless run: its stdin can
    # be buffered and replayed on the next account. Real streaming sessions (SDK
    # hosts) stay on the interactive path.
    oneshot = os.environ.get("CLAUDE_FAILOVER_ONESHOT_STDIN") == "1"
    if info["print"] and (not streaming_input or oneshot):
        return run_headless(cfg, argv, pinned)
    name = pick_interactive(cfg, info, pinned)
    ensure_profile_links(cfg, name)
    os.execve(cfg["real_claude"], [cfg["real_claude"]] + argv, child_env(cfg, name))
    return 0  # not reached


# --------------------------------------------------------------------------
# StopFailure hook: a chat hit its limit -> continue it on the next account
# --------------------------------------------------------------------------

def identify_account(cfg, env, trust_host=True):
    """Which account a Claude process ran on. trust_host=False ignores the desktop
    app's account variables (a `claude -p` started from a desktop Bash tool inherits
    them but actually runs on the keychain login)."""
    name = env.get("CLAUDE_FAILOVER_ACTIVE_ACCOUNT")
    if name in cfg["by_name"]:
        return name
    uid = env.get("CLAUDE_CODE_ACCOUNT_UUID") if trust_host else None
    email = (env.get("CLAUDE_CODE_USER_EMAIL") or "").lower() if trust_host else ""
    for a in cfg["accounts"]:
        if uid and uid == a.get("desktop_account_uuid"):
            return a["name"]
        if email and email == (a.get("email") or "").lower():
            return a["name"]
    cdir = env.get("CLAUDE_CONFIG_DIR")
    for a in cfg["accounts"]:
        if cdir and a.get("config_dir") and os.path.normpath(cdir) == os.path.normpath(a["config_dir"]):
            return a["name"]
    if not cdir or os.path.normpath(cdir) == os.path.normpath(cfg["default_config_dir"]):
        return default_login_account(cfg)
    return None


def has_controlling_terminal():
    try:
        fd = os.open("/dev/tty", os.O_RDONLY | os.O_NOCTTY)
    except OSError:
        return False
    os.close(fd)
    return True


def proc_argv(pid):
    """Exact argv of a process (macOS sysctl KERN_PROCARGS2), or None. ps joins
    arguments with spaces, so a prompt containing ' -p ' would look like a flag."""
    try:
        import ctypes
        import ctypes.util
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        argmax = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(argmax))
        if libc.sysctl((ctypes.c_int * 2)(1, 8), 2, ctypes.byref(argmax), ctypes.byref(size),
                       None, 0) != 0:   # CTL_KERN, KERN_ARGMAX
            return None
        buf = ctypes.create_string_buffer(argmax.value)
        size = ctypes.c_size_t(argmax.value)
        if libc.sysctl((ctypes.c_int * 3)(1, 49, int(pid)), 3, buf, ctypes.byref(size),
                       None, 0) != 0:   # CTL_KERN, KERN_PROCARGS2
            return None
        data = buf.raw[:size.value]
        argc = int.from_bytes(data[:4], sys.byteorder)
        rest = data[4:]
        i = rest.find(b"\0")             # executable path, then NUL padding
        while i < len(rest) and rest[i:i + 1] == b"\0":
            i += 1
        args = rest[i:].split(b"\0")[:argc]
        return [a.decode("utf-8", "replace") for a in args]
    except Exception:
        return None


def _ppid(pid):
    try:
        out = subprocess.run(["/bin/ps", "-o", "ppid=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        return int(out) if out.isdigit() else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def parent_is_print_run(env=None):
    """True if the claude process that fired this hook is a `claude -p` run. Such a
    run may have inherited desktop variables from a desktop Bash tool while really
    running on the keychain login. Desktop chats run with --input-format
    stream-json and no -p. Must be called BEFORE the hook forks: after setsid the
    parent is launchd. Claude puts the firing process id in CLAUDE_PID."""
    env = os.environ if env is None else env
    starts = []
    if (env.get("CLAUDE_PID") or "").isdigit():
        starts.append(int(env["CLAUDE_PID"]))
    starts.append(os.getppid())
    for pid in starts:
        for _ in range(3):
            argv = proc_argv(pid)
            if argv and "claude" in os.path.basename(argv[0]).lower() + " " + " ".join(argv[:2]).lower() \
                    and os.path.basename(argv[0]) not in ("sh", "bash", "zsh"):
                info = parse_argv(argv[1:])
                return bool(info["print"]) and opt(info, "--input-format") != "stream-json"
            nxt = _ppid(pid)
            if not nxt or nxt <= 1:
                break
            pid = nxt
    return False


def session_kind(env, print_run=None):
    """'desktop', 'terminal', or 'headless' for the Claude session that fired the hook."""
    entry = env.get("CLAUDE_CODE_ENTRYPOINT", "")
    if env.get("CLAUDE_FAILOVER_MANAGED") == "1":
        return "headless"
    if print_run if print_run is not None else parent_is_print_run():
        return "headless"
    if entry == "claude-desktop":
        return "desktop"
    if entry.startswith("sdk") or entry in ("mcp", "remote", "github-action"):
        return "headless"
    if entry == "cli" or has_controlling_terminal():
        return "terminal"
    return "headless"


PERMISSION_FLAGS = {
    "bypassPermissions": ["--dangerously-skip-permissions",
                          "--settings", '{"skipDangerousModePermissionPrompt": true}'],
    "acceptEdits": ["--permission-mode", "acceptEdits"],
    "plan": ["--permission-mode", "plan"],
    "auto": ["--permission-mode", "auto"],
    "dontAsk": ["--permission-mode", "dontAsk"],
}


def notify(title, message):
    if os.environ.get("CLAUDE_FAILOVER_NO_NOTIFY") == "1":
        return
    script = 'display notification %s with title %s' % (
        json.dumps(message), json.dumps(title))
    try:
        subprocess.run(["/usr/bin/osascript", "-e", script], timeout=10,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        pass


def write_handoff_script(cfg, session_id, cwd, frm, to, permission_mode, verdict, kind,
                         model=None):
    hdir = handoff_dir()
    hdir.mkdir(parents=True, exist_ok=True)
    stamp = now_local().strftime("%Y%m%d-%H%M%S")
    path = hdir / ("%s-%s.command" % (stamp, session_id[:8]))
    where = "the Claude desktop app" if kind == "desktop" else "another Terminal session"
    prompt = (
        "[claude-failover] This conversation moved here from %s because account '%s' hit its "
        "usage limit (%s). You are now on account '%s' in a Terminal window. Pick up the task "
        "exactly where it stopped. Do not redo steps that already finished (check the tool "
        "results above). Desktop-only tools (browser pane, computer use, ccd_* tools) are not "
        "available here." % (where, frm, verdict.get("text") or "usage limit", to)
    )
    cmd = [str(WRAPPER), "--resume", session_id, "--fork-session"]
    cmd += PERMISSION_FLAGS.get(permission_mode or "", [])
    if model:
        cmd += ["--model", model]
    cmd.append(prompt)
    body = "\n".join([
        "#!/bin/bash",
        "# claude-failover handoff %s: %s hit its usage limit, continuing on %s." % (stamp, frm, to),
        "export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "cd %s 2>/dev/null || cd ~" % shlex.quote(cwd or os.path.expanduser("~")),
        "printf '\\n  %s is out of usage. Continuing this conversation on %s...\\n\\n'" % (frm, to),
        "CLAUDE_FAILOVER_ACCOUNT=%s exec %s" % (shlex.quote(to), " ".join(shlex.quote(c) for c in cmd)),
        "",
    ])
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    os.chmod(path, 0o700)
    return path


def choose_handoff_target(cfg, frm, family):
    """Next chat account that can run this conversation, and a model to switch to
    if that account is out of the conversation's model (None = keep the model)."""
    usable, _ = plan_accounts(cfg, "interactive", family)
    usable = [n for n in usable if n != frm]
    if usable:
        return usable[0], None
    st = read_state()
    for name in plan_accounts(cfg, "interactive", None)[0]:
        if name == frm:
            continue
        for alias in cfg.get("handoff_model_fallbacks", ["opus", "sonnet"]):
            if _block(st, name, family_of(alias))[0] == "ok":
                return name, alias
    return None, None


def hook_main(stdin_text=None, env=None, open_terminal=True, print_run=None):
    env = dict(os.environ if env is None else env)
    try:
        payload = json.loads(stdin_text if stdin_text is not None else sys.stdin.read() or "{}")
    except ValueError:
        return 0
    if payload.get("hook_event_name", "StopFailure") != "StopFailure":
        return 0
    if payload.get("agent_id"):
        return 0  # a subagent's failure; the main turn reports its own
    msg = str(payload.get("last_assistant_message") or "")
    details = str(payload.get("error_details") or "")
    verdict = classify(1, msg, details)
    if verdict["kind"] not in ("limit",):
        log_event("hook_ignored", error=payload.get("error"), text=first_line(msg))
        return 0
    try:
        cfg = load_config()
    except SystemExit:
        return 0   # no usable config: nothing to record or hand the chat to
    if not cfg["accounts"]:
        return 0
    kind = session_kind(env, print_run)
    frm = identify_account(cfg, env, trust_host=(kind != "headless"))
    transcript = payload.get("transcript_path")
    perm, model = payload.get("permission_mode"), None
    if transcript and os.path.exists(transcript):
        t_perm, model = transcript_tail_fields(transcript)
        perm = perm or t_perm
    family = family_of(model) or model_family(cfg, {"opts": {}})
    log_event("hook_limit", account=frm, session_kind=kind, session=payload.get("session_id"),
              model=model, text=verdict.get("text"))
    if frm:
        record_verdict(cfg, frm, verdict, family)
    if kind == "headless":
        return 0  # the wrapper (or the caller) handles -p runs
    # Both kinds default to on: a config that names only one of them keeps the other.
    handoff = cfg["handoff"] if isinstance(cfg.get("handoff"), dict) else {}
    if (kind == "desktop" and not handoff.get("from_desktop", True)) or (
            kind == "terminal" and not handoff.get("from_terminal", True)):
        return 0
    session_id = payload.get("session_id")
    if not session_id or (transcript and not os.path.exists(transcript)):
        return 0
    # Out of one model only: the same account can go on with another model, so
    # don't fork the chat to another account - just say so.
    model_only = verdict.get("family") or MODEL_ONLY_RE.search(verdict.get("text") or "")
    if frm and model_only:
        st = read_state()
        alt = [a for a in cfg.get("handoff_model_fallbacks", ["opus", "sonnet"])
               if family_of(a) != family and _block(st, frm, family_of(a))[0] == "ok"]
        if alt:
            notify("Claude: %s is out of %s" % (frm, family or "this model"),
                   "Switch this chat's model to %s to keep going on the same account." % alt[0].title())
            log_event("handoff_skipped", reason="model_only", session=session_id, suggest=alt[0])
            return 0
    with edit_state() as st:
        prev = st["handoffs"].get(session_id)
        prev_until = parse_iso(prev.get("until") if isinstance(prev, dict) else prev)
        if prev_until and now_local() < prev_until:
            already = prev.get("at") if isinstance(prev, dict) else prev
            notify("Claude: still out of usage",
                   "This chat already continued in Terminal (%s)." % (already or "earlier"))
            return 0
    to, use_model = choose_handoff_target(cfg, frm, family)
    if not to:
        st = read_state()
        parts = []
        for n in [a["name"] for a in cfg["accounts"]]:
            k, until = _block(st, n, family)
            if until:
                parts.append("%s back %s" % (n, until.strftime("%a %-I:%M%p")))
        notify("Claude: every account is out of usage", "; ".join(parts) or "No fallback account available")
        log_event("handoff_skipped", reason="no_account", session=session_id)
        return 0
    with edit_state() as st:
        # Hand a chat off once per limit window: until the source account resets,
        # later failures there only get a notification. Unknown reset: 6 hours.
        src_until = verdict.get("resets_at")   # only a reset Claude actually stated
        until = src_until if src_until and src_until > now_local() else \
            now_local() + dt.timedelta(hours=6)
        st["handoffs"][session_id] = {"at": iso(now_local()), "until": iso(until), "to": to}
        if len(st["handoffs"]) > 200:
            def key(k):
                v = st["handoffs"][k]
                return (v.get("at") if isinstance(v, dict) else v) or ""
            for k in sorted(st["handoffs"], key=key)[:-200]:
                st["handoffs"].pop(k, None)
    path = write_handoff_script(cfg, session_id, payload.get("cwd"), frm or "this account", to,
                                perm, verdict, kind, use_model)
    log_event("handoff", frm=frm, to=to, model=use_model, session=session_id, script=str(path),
              session_kind=kind)
    if open_terminal:
        try:
            subprocess.Popen(["/usr/bin/open", "-a", "Terminal", str(path)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            opened = True
        except OSError:
            opened = False
        notify("Claude: %s is out of usage" % (frm or "this account"),
               "Continuing the conversation on %s in Terminal." % to)
        if opened and desktop_sync_on(cfg):
            # The desktop app lists chats per account. Copying the records now
            # means this chat is already in the list when the app is switched
            # to the account that has usage left.
            try:
                import desktop_sync
                desktop_sync.sync_quietly(cfg)
            except Exception:
                pass   # the hook must never fail; the next sync catches up
    return 0


# --------------------------------------------------------------------------
# claude-accounts: status / setup / login / clear / test / mark
# --------------------------------------------------------------------------

def auth_status(cfg, name, timeout=30):
    """`claude auth status --json` for one account (reads no secrets itself)."""
    try:
        r = subprocess.run([cfg["real_claude"], "auth", "status", "--json"],
                           env=child_env(cfg, name), capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
        data = json.loads(r.stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"loggedIn": False, "error": "could not run claude auth status"}
    return data


def probe_account(cfg, name, token=None):
    """One tiny haiku call on an account, on its spare token if one is given.
    Records the outcome; returns (verdict, ran_on_paid_extra_usage)."""
    env = child_env(cfg, name, managed=True)
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    ensure_profile_links(cfg, name)
    overage, expiry = False, None
    try:
        r = subprocess.run([cfg["real_claude"], "-p", "Reply with exactly: OK", "--model", "haiku",
                            "--output-format", "stream-json", "--verbose"],
                           env=env, capture_output=True, text=True, timeout=180,
                           stdin=subprocess.DEVNULL)
        out, err = r.stdout, r.stderr
        if token:
            out, err = out.replace(token, "[spare token]"), err.replace(token, "[spare token]")
        for line in out.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "rate_limit_event" and \
                    (ev.get("rate_limit_info") or {}).get("isUsingOverage"):
                overage = True
            if ev.get("type") == "result" and isinstance(ev.get("credential_expires_at"), str):
                expiry = ev["credential_expires_at"]
        verdict = classify(r.returncode, out, err)
    except subprocess.TimeoutExpired:
        verdict = {"kind": "error", "text": "timed out"}
    if verdict["kind"] in ("ok", "limit", "auth"):
        record_verdict(cfg, name, verdict, spare=bool(token),
                       login_expires_at=None if token else expiry)
    if overage:
        try:
            with edit_state() as st:
                st["accounts"].setdefault(name, {})["overage_seen"] = iso(now_local())
        except Exception:
            pass
    return verdict, overage


def _fmt(t):
    return t.strftime("%a %b %-d %-I:%M%p") if t else "-"


def _day(d):
    return d.strftime("%b %-d") if d else "-"


def accounts_status(cfg, as_json=False):
    st = read_state()
    rows = []
    for a in cfg["accounts"]:
        name = a["name"]
        set_up = account_is_set_up(cfg, name)
        auth = auth_status(cfg, name) if set_up else {"loggedIn": False}
        if set_up:
            note_login_end(name, auth)   # the CLI's end date, if it reports one
        kind, until = account_block(st, name)
        rec = read_state()["accounts"].get(name, {})
        row = {
            "account": name,
            "expected_email": a.get("email"),
            "config_dir": a.get("config_dir") or cfg["default_config_dir"],
            "set_up": set_up,
            "logged_in": bool(auth.get("loggedIn")),
            "email": auth.get("email"),
            "plan": auth.get("subscriptionType"),
            # No email in the config = nothing to compare against.
            "email_ok": not a.get("email") or
                        (auth.get("email") or "").lower() == a["email"].lower(),
            "state": kind,
            "until": iso(until),
            "model_limits": {k: v for k, v in (rec.get("model_limits") or {}).items()
                             if (parse_iso(v) or now_local()) > now_local()},
            "last_limit": rec.get("limit_text"),
            "last_ok": rec.get("last_ok"),
            "overage_seen": rec.get("overage_seen"),
            "profile_problems": profile_problems(cfg, name) if set_up else [],
            "logged_in_at": rec.get("logged_in_at"),
            "login_expires_at": rec.get("login_expires_at"),
            "spare_token_saved": rec.get("spare_token_saved"),
            "spare_token_failing": spare_token_failing(rec),
        }
        win = login_window(rec)
        row.update({
            "login_ends": win["ends"].isoformat() if win else None,
            "login_ends_earliest": win["earliest"].isoformat() if win and win["earliest"] else None,
            "login_renew_from": win["renew"].isoformat() if win else None,
            "login_date_source": win["source"] if win else None,
        })
        if not set_up:
            row["health"] = "not set up"
        elif not row["logged_in"]:
            row["health"] = "logged out"
        elif not row["email_ok"]:
            row["health"] = "WRONG ACCOUNT"
        elif row["profile_problems"]:
            row["health"] = "profile not linked"
        elif kind == "limited" and parse_iso(rec.get("auth_down_until")) and \
                parse_iso(rec.get("auth_down_until")) > now_local():
            row["health"] = "limited + login failing"
        elif kind == "limited":
            row["health"] = "limited"
        elif kind == "auth_down":
            row["health"] = "login failing"
        else:
            row["health"] = "ok"
        rows.append(row)
    if as_json:
        print(json.dumps({"accounts": rows, "order": cfg["order"],
                          "config": cfg["_config_path"]}, indent=2))
        return 0
    print("Claude accounts (headless order: %s; chat order: %s)" % (
        " -> ".join(cfg["order"]["headless"]), " -> ".join(cfg["order"]["interactive"])))
    for r in rows:
        line = "  %-12s %-18s" % (r["account"], r["health"].upper())
        if r["logged_in"]:
            line += " %s (%s)" % (r["email"], r["plan"] or "?")
        if r["state"] != "ok":
            line += "  until %s" % _fmt(parse_iso(r["until"]))
        print(line)
        if r["last_limit"] and r["state"] == "limited":
            print("               last limit: %s" % r["last_limit"])
        if r.get("overage_seen"):
            print("               extra usage (billed) was in use at %s" % r["overage_seen"][:16])
        for fam, t in r["model_limits"].items():
            print("               %s limit until %s" % (fam, _fmt(parse_iso(t))))
        if r["health"] == "WRONG ACCOUNT":
            print("               expected %s - log in again with: claude-accounts login %s"
                  % (r["expected_email"], r["account"]))
        if r["health"] in ("logged out", "not set up") or "login failing" in r["health"]:
            print("               fix: claude-accounts login %s" % r["account"])
        if r["profile_problems"]:
            for line in unlinked_lines(cfg, r["account"]):
                print("               " + line)
        today = now_local().date()
        win = login_window({"logged_in_at": r["logged_in_at"], "login_expires_at": r["login_expires_at"]})
        if not r["logged_in"]:
            pass
        elif win is None:
            print("               login date unknown (logins last ~%d days); if you know it: "
                  "claude-accounts logged-in %s YYYY-MM-DD" % (LOGIN_LIFETIME_DAYS, r["account"]))
        elif win["source"] == "cli" and today >= win["renew"]:
            print("               LOGIN ENDS BY %s (could end from %s) - renew now: claude-accounts login %s"
                  % (_day(win["ends"]), _day(win["earliest"]), r["account"]))
        elif win["source"] == "cli":
            since = ("login from %s, " % _day(parse_iso(r["logged_in_at"]))) if r["logged_in_at"] else "login "
            print("               %sends by %s (could end from %s; CLI date); renew from %s" % (
                since, _day(win["ends"]), _day(win["earliest"]), _day(win["renew"])))
        elif today >= win["renew"]:
            print("               LOGIN EXPIRES AROUND %s - renew now: claude-accounts login %s"
                  % (_day(win["ends"]), r["account"]))
        else:
            print("               login from %s; renew from %s (expires around %s)" % (
                _day(parse_iso(r["logged_in_at"])), _day(win["renew"]), _day(win["ends"])))
        if r["spare_token_saved"]:
            renew, expires = renewal_dates(r["spare_token_saved"], SPARE_RENEW_DAY, SPARE_TOKEN_DAYS)
            if r["spare_token_failing"]:
                how = "REFUSED - make a new one: claude-accounts token %s" % r["account"]
            elif today >= renew:
                how = "EXPIRES AROUND %s - renew: claude-accounts token %s" % (
                    expires.strftime("%b %-d %Y"), r["account"])
            else:
                how = "lasts to around %s" % expires.strftime("%b %-d %Y")
            print("               spare token for headless runs: %s" % how)
    if not rows:
        print("  no accounts yet - add one: claude-accounts add <name> --email you@example.com")
    if desktop_sync_on(cfg):
        try:
            import desktop_sync
            line = desktop_sync.status_line(cfg)
        except Exception:   # also ImportError: status must work without it
            line = None
        if isinstance(line, str) and line:
            print(line)
    return 0


# --------------------------------------------------------------------------
# claude-accounts: init / add / hook (setting the tool up)
# --------------------------------------------------------------------------

ACCOUNT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def tilde(path):
    """'<home>/x' -> '~/x', so a written config names no user folder."""
    home = os.path.expanduser("~")
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def whole_number(text):
    """'15' -> 15. None for anything that is not a whole number of at least 1
    (the caller prints its usage line, never a traceback)."""
    if not isinstance(text, str) or not re.fullmatch(r"[0-9]+", text):
        return None
    return int(text) if int(text) >= 1 else None


def pop_option(args, name):
    """Take `--name VALUE` or `--name=VALUE` out of args. Returns VALUE, None if
    the option is absent, or "" if it has no value."""
    for i, a in enumerate(args):
        if a == name:
            del args[i]
            return args.pop(i) if i < len(args) else ""
        if a.startswith(name + "="):
            del args[i]
            return a[len(name) + 1:]
    return None


def accounts_init(rest):
    """Write a starter config. An existing one is only replaced with --force: then
    the config in use is rewritten in place (wherever it is), after a copy to
    <file>.bak-<time>. Writing a new file elsewhere would leave the old one in use,
    or quietly move the state folder that sits next to the config."""
    force = "--force" in rest
    current = config_path()
    if current is not None and not force:
        print("A config already exists: %s\nEdit it, or start over with: claude-accounts init --force"
              % current)
        return 1
    target = current if current is not None else user_config_file()
    # No real_claude: it is detected on every run, so a Claude Code that moves
    # (Homebrew to the native installer, say) is still found.
    real = find_real_claude()
    cfg = {
        "default_config_dir": "~/.claude",
        "accounts": [],
        "order": {"interactive": [], "headless": []},
        "handoff": {"from_desktop": True, "from_terminal": True},
        "handoff_model_fallbacks": ["opus", "sonnet"],
        "desktop_sync": {"enabled": False, "interval_minutes": 10},
    }
    if not target.parent.exists():
        target.parent.mkdir(parents=True)
        os.chmod(target.parent, 0o700)   # the config names your accounts: keep it private
    if target.exists():
        print("Old config kept as %s" % backup_copy(target, target.parent))
    save_user_json(target, cfg, mode=0o600)
    print("Wrote %s" % target)
    if real:
        print("Real claude binary: %s (looked up on every run)" % real)
    else:
        print("No claude binary found yet. Install Claude Code (it is looked up on every run),"
              " or set real_claude in the config.")
    print("Next steps:\n"
          "  claude-accounts add <name> --email you@example.com   once per Claude account\n"
          "  claude-accounts login <name>                          sign each account in\n"
          "  claude-accounts hook install                          move a chat that hits a limit"
          " to the next account\n"
          "  claude-accounts sync install                          optional: list desktop app chats"
          " under every account")
    return 0


def accounts_add(cfg, rest):
    """Add an account to the config (end of both orders) and create its folder,
    linked to the shared ~/.claude. Keys this tool doesn't know are kept."""
    args = list(rest)
    email = pop_option(args, "--email")
    config_dir = pop_option(args, "--config-dir")
    if len(args) != 1 or args[0].startswith("-") or email == "" or config_dir == "":
        print("usage: claude-accounts add <name> [--email you@example.com] [--config-dir DIR]")
        return 2
    name = args[0]
    if not ACCOUNT_NAME_RE.fullmatch(name):
        print("Bad account name %r: use 1-32 letters, digits, - or _ (e.g. work)." % name)
        return 2
    if name in cfg["by_name"]:
        print("There is already an account named %s in %s." % (name, cfg["_config_path"]))
        return 1
    if config_dir and not config_dir.startswith("~"):
        config_dir = os.path.abspath(config_dir)   # every caller must see the same path
    config_dir = config_dir or "~/.claude-%s" % name
    # One folder = one login. A folder shared with another account (or the shared
    # ~/.claude itself) would make both "accounts" the same one.
    taken = {os.path.realpath(a["config_dir"]) for a in cfg["accounts"] if a.get("config_dir")}
    taken.add(os.path.realpath(cfg["default_config_dir"]))
    if os.path.realpath(expand(config_dir)) in taken:
        print("%s is already in use (by another account, or it is the shared %s). "
              "Pick another --config-dir." % (config_dir, cfg["default_config_dir"]))
        return 1
    # The folder first: an account saved without one would be half added (and a
    # second `add` would then say it already exists).
    err = make_account_folder(expand(config_dir))
    if err:
        print("Cannot create %s: %s. Nothing was added." % (config_dir, err))
        return 1
    path = Path(cfg["_config_path"])
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)   # the file as written: no defaults filled in, ~ kept
    acct = {"name": name}
    if email:
        acct["email"] = email
    acct["config_dir"] = config_dir
    raw["accounts"] = (raw.get("accounts") or []) + [acct]
    raw["order"] = raw.get("order") or {}
    for mode in ("interactive", "headless"):
        names = raw["order"].get(mode) or []
        raw["order"][mode] = names + ([name] if name not in names else [])
    save_user_json(path, raw, mode=0o600)
    cfg = load_config()
    made = ensure_profile_links(cfg, name, seed=True)
    print("Added %s to %s (folder %s)." % (name, path, tilde(cfg["by_name"][name]["config_dir"])))
    if made:
        print("Linked from %s: %s" % (tilde(cfg["default_config_dir"]), ", ".join(made)))
    # A folder used with CLAUDE_CONFIG_DIR before can hold its own settings.json
    # or projects/. Linking never replaces those, so say what that breaks.
    problems = unlinked_lines(cfg, name)
    for line in problems:
        print("  " + line)
    print("next: claude-accounts login %s" % name)
    return 1 if problems else 0


def settings_file(cfg):
    """settings.json of the shared config folder: every account folder links to it."""
    return Path(cfg["default_config_dir"]) / "settings.json"


def read_settings(path):
    """settings.json as a dict ({} when there is no file), or None (after saying
    why) when it can't be used safely."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        print("cannot read %s: %s" % (path, e))
        return None
    try:
        data = json.loads(text)
    except ValueError as e:
        print("%s is not valid JSON (%s). Fix it first; nothing was changed." % (path, e))
        return None
    if not isinstance(data, dict):
        print("%s does not hold a JSON object. Fix it first; nothing was changed." % path)
        return None
    return data


def hook_script_of(cmd):
    """The program a hook command runs: its first word, unquoted the way the
    shell does it (a path like "Bob's Code" is quoted in pieces). None if it
    can't be split."""
    try:
        words = shlex.split(cmd) if isinstance(cmd, str) else []
    except ValueError:
        return None
    return os.path.expanduser(words[0]) if words else None


def is_this_hook(script):
    """True if `script` is this copy's claude-failover-hook."""
    return bool(script) and os.path.realpath(script) == os.path.realpath(str(HOOK_SCRIPT))


def is_our_hook(h):
    """A hooks entry that runs claude-failover-hook (from this or any other copy)."""
    script = hook_script_of(h.get("command") if isinstance(h, dict) else None)
    return script is not None and script.endswith("/claude-failover-hook")


def hook_blind_lines(cfg):
    """One line per set-up account whose folder has its own settings.json (or
    none), so the hook in the shared settings.json never runs for its chats."""
    shared_file = str(settings_file(cfg))
    lines = []
    for a in cfg["accounts"]:
        cdir = a.get("config_dir")
        if not cdir or not os.path.isdir(cdir):
            continue   # the plain ~/.claude login, or not set up yet
        own = os.path.join(cdir, "settings.json")
        if os.path.realpath(own) == os.path.realpath(shared_file):
            continue
        if os.path.lexists(own):
            fix = "merge it into %s, move it away, then run: claude-accounts setup" % tilde(shared_file)
        else:
            fix = "claude-accounts setup"
        lines.append("%s: %s is not a link to %s, so this account's chats do not run the hook "
                     "- fix: %s" % (a["name"], tilde(own), tilde(shared_file), fix))
    return lines


def stop_failure_entries(settings):
    hooks = settings.get("hooks")
    entries = hooks.get("StopFailure") if isinstance(hooks, dict) else None
    return entries if isinstance(entries, list) else []


def our_hooks(settings):
    """The StopFailure hook entries ({"type", "command", ...}) that run claude-failover-hook."""
    return [h for e in stop_failure_entries(settings) if isinstance(e, dict)
            for h in (e.get("hooks") if isinstance(e.get("hooks"), list) else []) if is_our_hook(h)]


def our_hook_commands(settings):
    return [h["command"] for h in our_hooks(settings)]


def hook_install(cfg):
    path = settings_file(cfg)
    settings = read_settings(path)
    if settings is None:
        return 1
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict) or not isinstance(hooks.get("StopFailure", []), list):
        print("\"hooks\" in %s has a shape this tool doesn't expect. Add the hook by hand; "
              "nothing was changed." % path)
        return 1
    found = our_hook_commands(settings)
    changed = False
    replaced = []
    # An entry whose script is gone (that copy was moved or deleted) can never run:
    # point it at this copy instead.
    for h in our_hooks(settings):
        if not os.path.exists(hook_script_of(h["command"])):
            replaced.append(h["command"])
            h["command"] = shlex.quote(str(HOOK_SCRIPT))
            changed = True
    if not found:
        settings["hooks"] = hooks
        hooks.setdefault("StopFailure", []).append({
            "matcher": "rate_limit",
            "hooks": [{"type": "command", "command": shlex.quote(str(HOOK_SCRIPT)), "timeout": 30}]})
        changed = True
    # Otherwise a stopped chat would also restart by itself at the reset time and
    # redo work that already continued on the other account.
    if settings.get("autoContinueAtUsageLimit") is not False:
        settings["autoContinueAtUsageLimit"] = False
        changed = True
    if changed:
        if os.path.exists(path):
            print("Backup: %s" % backup_copy(path, state_dir()))
        save_user_json(path, settings)
        for a in cfg["accounts"]:   # a new settings.json gets linked into every account folder
            ensure_profile_links(cfg, a["name"])
    if replaced:
        print("The hook in %s ran %s, which is missing (moved or deleted copy). It now runs %s."
              % (path, replaced[0], shlex.quote(str(HOOK_SCRIPT))))
    elif found:
        print("The hook is already in %s: %s" % (path, found[0]))
        if not is_this_hook(hook_script_of(found[0])):
            print("That is another copy of claude-failover. To use this one: claude-accounts hook "
                  "uninstall, then claude-accounts hook install.")
    else:
        print("Installed the StopFailure hook in %s: a chat that hits a usage limit continues "
              "on the next account in Terminal." % path)
    if changed:
        print("autoContinueAtUsageLimit is false, so a stopped chat does not also restart "
              "itself at the reset time and redo the work.")
    for line in hook_blind_lines(cfg):
        print("warning - " + line)
    return 0


def hook_uninstall(cfg):
    path = settings_file(cfg)
    settings = read_settings(path)
    if settings is None:
        return 1
    kept, removed = [], 0
    for entry in stop_failure_entries(settings):
        hooks = entry.get("hooks") if isinstance(entry, dict) else None
        ours = [h for h in hooks if is_our_hook(h)] if isinstance(hooks, list) else []
        if not ours:
            kept.append(entry)
            continue
        removed += len(ours)
        others = [h for h in hooks if not is_our_hook(h)]
        if others:   # the entry also runs someone else's hook: keep that part
            entry["hooks"] = others
            kept.append(entry)
    if not removed:
        print("The claude-failover hook is not in %s; nothing changed." % path)
        return 0
    if kept:
        settings["hooks"]["StopFailure"] = kept
    else:
        del settings["hooks"]["StopFailure"]
        if not settings["hooks"]:
            del settings["hooks"]
    print("Backup: %s" % backup_copy(path, state_dir()))
    save_user_json(path, settings)
    print("Removed the claude-failover hook from %s." % path)
    print("autoContinueAtUsageLimit is unchanged (%s). Set it yourself if you want stopped "
          "chats to restart at the reset time." % json.dumps(settings.get("autoContinueAtUsageLimit")))
    return 0


def hook_status(cfg):
    path = settings_file(cfg)
    settings = read_settings(path)
    if settings is None:
        return 1
    found = our_hook_commands(settings)
    if not found:
        print("hook: not installed in %s (install: claude-accounts hook install)" % path)
        return 1
    rc = 0
    for c in found:
        script = hook_script_of(c)
        if not os.path.exists(script):
            # The repo was moved or deleted: the hook can never run.
            other = "  MISSING (moved or deleted copy) - fix: claude-accounts hook install"
            rc = 1
        elif is_this_hook(script):
            other = ""
        else:
            other = "  (another copy of claude-failover)"
        print("hook: installed in %s\n  command: %s%s" % (path, c, other))
    print("  autoContinueAtUsageLimit: %s" % json.dumps(settings.get("autoContinueAtUsageLimit")))
    for line in hook_blind_lines(cfg):
        print("  " + line)
        rc = 1
    return rc


ACCOUNTS_USAGE = """usage: claude-accounts <command>
  status [--json]                      accounts, limits, login dates
  init [--force]                       write a starter config
  add <name> [--email E] [--config-dir DIR]   add an account and create its folder
  setup                                create and link every account folder
  login <account>                      sign an account in (opens a browser)
  logged-in <account> [YYYY-MM-DD]     date a login made some other way
  token <account>                      save a one-year spare token in the keychain
  test [account]                       one tiny call per account (and spare token)
  clear [account]                      forget saved limits
  mark <account> [minutes]             mark an account limited by hand
  log [n]                              the last n events
  argspec                              rebuild argspec.json from the installed claude
  hook install|uninstall|status        the StopFailure hook in settings.json
  sync [--dry-run] [--quiet] [--json]  list desktop app chats under every account
  sync install [--every MINUTES] | sync uninstall | sync status"""
ACCOUNTS_COMMANDS = {"status", "init", "add", "setup", "login", "logged-in", "token", "test",
                     "clear", "mark", "log", "argspec", "hook", "sync"}
NEEDS_CLAUDE = {"status", "login", "token", "test", "argspec"}


def accounts_main(argv):
    cmd = argv[0] if argv else "status"
    rest = argv[1:]
    if cmd in ("help", "-h", "--help"):
        print(ACCOUNTS_USAGE)
        return 0
    if cmd not in ACCOUNTS_COMMANDS:
        print(ACCOUNTS_USAGE)
        return 2
    if cmd == "init":   # the one command that works without a config
        return accounts_init(rest)
    cfg = load_config()
    if cmd in NEEDS_CLAUDE and claude_missing(cfg):
        print(claude_missing(cfg))
        return 1
    if cmd == "status":
        return accounts_status(cfg, as_json="--json" in rest)
    if cmd == "add":
        return accounts_add(cfg, rest)
    if cmd == "hook":
        sub = rest[0] if rest else ""
        if sub == "install":
            return hook_install(cfg)
        if sub == "uninstall":
            return hook_uninstall(cfg)
        if sub == "status":
            return hook_status(cfg)
        print("usage: claude-accounts hook install|uninstall|status")
        return 2
    if cmd == "sync":
        try:
            import desktop_sync
        except ImportError as e:
            print("desktop sync is not available (%s)" % e)
            return 1
        return desktop_sync.cli(cfg, rest)
    if cmd == "setup":
        worst = 0
        for a in cfg["accounts"]:
            if not a.get("config_dir"):
                continue   # the plain ~/.claude login has no folder of its own
            folder = tilde(a["config_dir"])
            err = make_account_folder(a["config_dir"])
            if err:
                print("%s: cannot create %s: %s" % (a["name"], folder, err))
                worst = 1
                continue   # the other accounts still get set up
            made = ensure_profile_links(cfg, a["name"], seed=True)
            problems = unlinked_lines(cfg, a["name"])
            if made:
                print("%s: %s linked %s" % (a["name"], folder, ", ".join(made)))
            elif not problems:
                print("%s: %s (already linked)" % (a["name"], folder))
            else:
                print("%s: %s" % (a["name"], folder))
            for line in problems:
                print("  " + line)
            worst = worst or (1 if problems else 0)
        return worst
    if cmd == "login":
        if not rest or rest[0] not in cfg["by_name"]:
            print("usage: claude-accounts login <%s>" % "|".join(cfg["by_name"]))
            return 2
        name = rest[0]
        acct = cfg["by_name"][name]
        want = acct.get("email") or ""   # no email in the config: any account is accepted
        who = want or "the account you want for %s" % name
        err = make_account_folder(acct["config_dir"]) if acct.get("config_dir") else None
        if err:
            print("Cannot create %s: %s" % (tilde(acct["config_dir"]), err))
            return 1
        ensure_profile_links(cfg, name, seed=True)
        env = child_env(cfg, name)
        if acct.get("login_browser") == "incognito":
            # If Chrome's normal profile is signed in to claude.ai as another account,
            # approving there would save that account's login in this slot. A private
            # window has no session, so the account you sign in with is the right one.
            env["BROWSER"] = str(PROJECT / "bin" / "open-incognito")
            print("Signing %s in. A private Chrome window opens: sign in as %s, then click"
                  " Authorize." % (name, who))
        else:
            print("Signing %s in. A browser window opens: make sure it is signed in to claude.ai as %s,"
                  " then click Authorize." % (name, who))
        sys.stdout.flush()   # this line first, before what claude prints (also through a pipe)
        rc = subprocess.call([cfg["real_claude"], "auth", "login", "--claudeai"]
                             + (["--email", want] if want else []), env=env)
        info = auth_status(cfg, name)
        got = (info.get("email") or "").lower()
        if info.get("loggedIn") and (not want or got == want.lower()):
            clear_account(name)
            record_login(name)
            note_login_end(name, info)
            win = login_window(read_state()["accounts"].get(name, {}))
            if win and win["source"] == "cli":
                print("Done. %s is logged in (%s plan). The CLI says this login ends by %s (could end "
                      "from %s): claude-accounts status asks you to renew from %s." % (
                          got, info.get("subscriptionType"), _day(win["ends"]),
                          _day(win["earliest"]), _day(win["renew"])))
            else:
                print("Done. %s is logged in (%s plan). Logins last about %d days: claude-accounts "
                      "status asks you to renew from %s (expires around %s)." % (
                          got, info.get("subscriptionType"), LOGIN_LIFETIME_DAYS,
                          _day(win["renew"]), _day(win["ends"])))
            return 0
        print("Login did not end up on %s (got: %s). Run it again with the browser signed in to "
              "the right claude.ai account." % (who, got or "not logged in"))
        return rc or 1
    if cmd == "logged-in":
        # Date a login made outside `claude-accounts login` (e.g. /login inside a chat).
        if not rest or rest[0] not in cfg["by_name"]:
            print("usage: claude-accounts logged-in <%s> [YYYY-MM-DD]" % "|".join(cfg["by_name"]))
            return 2
        try:
            when = (dt.datetime.strptime(rest[1], "%Y-%m-%d").replace(tzinfo=LOCAL_TZ)
                    if len(rest) > 1 else now_local())
        except ValueError:
            print("the date must look like 2026-11-02")
            return 2
        record_login(rest[0], when)
        renew, expires = renewal_dates(iso(when), LOGIN_RENEW_DAY, LOGIN_LIFETIME_DAYS)
        print("%s: login dated %s; renew from %s (expires around %s). The next claude-accounts "
              "status replaces the guess with the CLI's own end date." % (
                  rest[0], when.strftime("%b %-d"), renew.strftime("%b %-d"), expires.strftime("%b %-d")))
        return 0
    if cmd == "token":
        # A one-year spare token for headless runs whose login is refused (README: Spare token).
        if not rest or rest[0] not in cfg["by_name"]:
            print("usage: claude-accounts token <%s>" % "|".join(cfg["by_name"]))
            return 2
        name = rest[0]
        acct = cfg["by_name"][name]
        env = child_env(cfg, name)
        if acct.get("login_browser") == "incognito":
            env["BROWSER"] = str(PROJECT / "bin" / "open-incognito")
        print("Step 1 of 2: a browser window opens. Check it is signed in to claude.ai as %s "
              "(nothing can check the token's account later), then click Authorize."
              % (acct.get("email") or "the account you want for %s" % name))
        sys.stdout.flush()   # keep the steps in order when the output goes through a pipe
        rc = subprocess.call([cfg["real_claude"], "setup-token"], env=env)
        if rc != 0:
            print("setup-token did not finish; nothing was saved.")
            return rc
        print("Step 2 of 2: copy the token printed above and paste it at both prompts below "
              "(nothing shows while you paste). It goes straight into the macOS keychain.")
        sys.stdout.flush()
        rc = subprocess.call([cfg["security"], "add-generic-password", "-U", "-s", SPARE_TOKEN_SERVICE,
                              "-a", name, "-l", "claude-failover spare token (%s)" % name, "-w"])
        if rc != 0:
            print("Not saved (security exited %d)." % rc)
            return rc
        with edit_state() as st:
            st["accounts"].setdefault(name, {})["spare_token_saved"] = iso(now_local())
        token = read_spare_token(cfg, name)
        verdict = probe_account(cfg, name, token)[0] if token else {
            "kind": "error", "text": "could not read it back from the keychain"}
        if verdict["kind"] != "ok":
            print("Saved, but a test call on it failed (%s: %s). Run claude-accounts token %s again."
                  % (verdict["kind"], verdict.get("text") or "", name))
            return 1
        renew, expires = renewal_dates(iso(now_local()), SPARE_RENEW_DAY, SPARE_TOKEN_DAYS)
        print("Done. The spare token works. Headless runs use it only when %s's login is refused. "
              "It lasts about a year: claude-accounts status asks you to renew it from %s. Now "
              "clear this window (Cmd+K) so the token isn't left on screen."
              % (name, renew.strftime("%b %-d %Y")))
        return 0
    if cmd == "clear":
        clear_account(rest[0] if rest else None)
        print("Cleared saved limits%s." % (" for " + rest[0] if rest else ""))
        return 0
    if cmd == "mark":
        minutes = whole_number(rest[1]) if len(rest) > 1 else 60
        if len(rest) < 1 or rest[0] not in cfg["by_name"] or minutes is None:
            print("usage: claude-accounts mark <account> [minutes]")
            return 2
        record_verdict(cfg, rest[0], {"kind": "limit", "text": "marked by hand", "scope": "account",
                                      "resets_at": now_local() + dt.timedelta(minutes=minutes)})
        print("%s marked limited for %d min." % (rest[0], minutes))
        return 0
    if cmd == "test":
        names = rest or [a["name"] for a in cfg["accounts"]]
        st = read_state()
        worst = 0
        for name in names:
            has_spare = bool(st["accounts"].get(name, {}).get("spare_token_saved"))
            for spare in ([False, True] if has_spare else [False]):
                label = attempt_label(name, spare)
                token = read_spare_token(cfg, name) if spare else None
                if spare and not token:
                    print("%-12s ERROR not found in the keychain - fix: claude-accounts token %s"
                          % (label, name))
                    worst = 1
                    continue
                verdict, overage = probe_account(cfg, name, token)
                extra = "  ** past its plan limit: this ran on PAID extra usage **" if overage else ""
                print("%-12s %s %s%s" % (label, verdict["kind"].upper(), verdict.get("text") or "", extra))
                worst = worst or (0 if verdict["kind"] == "ok" else 1)
        return worst
    if cmd == "argspec":
        # Rebuild argspec.json (option arities) from the installed claude binary.
        real = os.path.realpath(cfg["real_claude"])
        kinds = {}
        try:
            for spec in option_specs(real):
                kind = "flag"
                if "<" in spec:
                    kind = "variadic" if "..." in spec else "one"
                elif "[" in spec:
                    kind = "variadic" if "..." in spec else "optional"
                names = re.findall(r"--[a-z][a-zA-Z0-9-]*", spec)[:1] + re.findall(r"^(-[A-Za-z]),", spec)
                for n in names:
                    kinds.setdefault(n, set()).add(kind)   # a set: a spec seen twice counts once
        except OSError as e:
            print("cannot read %s: %s" % (real, e))
            return 1
        # Same name defined differently by subcommands: prefer value-taking kinds,
        # and the main command's known forms.
        prefer = {"--debug": "optional", "-d": "optional", "--mcp-config": "variadic",
                  "--add-dir": "variadic", "--remote": "optional"}
        out = {"one": [], "optional": [], "variadic": [], "flag": []}
        for n, ks in kinds.items():
            k = prefer.get(n) or next((x for x in ("variadic", "one", "optional", "flag") if x in ks))
            out[k].append(n)
        for k in out:
            out[k].sort()
        if len(kinds) < 50:
            print("only %d options found - binary format changed? argspec.json left as is" % len(kinds))
            return 1
        atomic_write_json(PROJECT / "argspec.json", out)
        print("argspec.json: %s" % ", ".join("%d %s" % (len(v), k) for k, v in out.items()))
        return 0
    if cmd == "log":
        n = whole_number(rest[0]) if rest else 20
        if n is None:
            print("usage: claude-accounts log [n]")
            return 2
        try:
            lines = (state_dir() / "events.jsonl").read_text(encoding="utf-8").splitlines()[-n:]
        except OSError:
            lines = []
        print("\n".join(lines) or "(no events yet)")
        return 0
    print(ACCOUNTS_USAGE)
    return 2
