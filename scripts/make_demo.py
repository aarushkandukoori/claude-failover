#!/usr/bin/python3 -I
"""Make docs/demo.svg, the animated terminal at the top of the README.

Everything the demo shows is real output. This script builds a throwaway
sandbox (its own HOME, config, state folder and desktop app folder, plus the
fake claude binary in scripts/demo_fake_claude.py), runs the real
bin/claude-accounts and bin/claude-failover in it, and draws what they printed:
each command is typed, then its output fades in line by line. It writes
nothing outside the sandbox except docs/demo.svg.

    python3 scripts/make_demo.py          # rewrite docs/demo.svg
    python3 scripts/make_demo.py --keep   # also keep the sandbox to look inside
"""

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "docs" / "demo.svg"
FAKE_CLAUDE = REPO / "scripts" / "demo_fake_claude.py"
PROGRAMS = {"claude": REPO / "bin" / "claude-failover",   # bin/claude is a shim for this one
            "claude-accounts": REPO / "bin" / "claude-accounts"}

# ---- The scenario -----------------------------------------------------------

ZONE = "America/New_York"   # the demo's clock: the output never shows this machine's time zone
ACCOUNTS = [                # (name, email, plan, days until its CLI login ends)
    ("personal", "you@example.com", "max", 26),
    ("work", "you@work.example", "team", 24),
]
SIGNED_IN = "work"          # the account the desktop app is signed into while it runs
CHATS = {                   # desktop app chats each account lists before the sync
    "personal": ["Fix the flaky login test", "Plan the blog redesign", "Explain the billing webhook"],
    "work": ["Review the payments PR", "Speed up the CI cache"],
}
PROMPT = "summarize today's failing CI runs"
ANSWER = """3 runs failed on main today:
- api-tests: test_refund_total fails since tax.py started rounding to 3 decimals
- web-e2e: the login test timed out twice and passed on retry (flaky)
- nightly: the docker build ran out of disk space on the runner"""
STEPS = [                   # (comment, extra environment, command)
    ("# two accounts, both ready", {}, ["claude-accounts", "status"]),
    ("# personal hits its usage limit, so the same run moves to work",
     {"CLAUDE_FAILOVER_VERBOSE": "1"}, ["claude", "-p", PROMPT]),
    ("# personal is skipped until its limit resets", {}, ["claude-accounts", "status"]),
    ("# copy the desktop app chats, so each account lists all of them", {},
     ["claude-accounts", "sync"]),
]
DESC = ("A terminal session. claude-accounts status lists two Claude accounts, personal and work, "
        "both OK. A headless claude -p run hits the usage limit on personal and runs again on "
        "work, which answers. Status then shows personal limited until its reset time, and "
        "claude-accounts sync copies the desktop app chats between the two accounts.")


# ---- The sandbox ------------------------------------------------------------

def demo_id(*parts):
    """A random-looking UUID that is the same on every run (uuid5 of a name)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "claude-failover-demo/" + "/".join(parts)))


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def build_sandbox(home, now):
    """A full install under `home`: config, two signed-in accounts, a fake claude and
    a desktop app folder with a few chats per account. Returns the environment the
    commands run with. Every path the tool reads points into the sandbox."""
    fake = home / ".local" / "bin" / "claude"   # where Claude Code's installer puts it
    fake.parent.mkdir(parents=True)
    shutil.copyfile(str(FAKE_CLAUDE), str(fake))
    fake.chmod(0o755)
    desktop = home / "Library" / "Application Support" / "Claude"
    cwd = home / "code" / "shop"                # the folder the desktop chats ran in
    transcripts = home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    transcripts.mkdir(parents=True)
    names = [a[0] for a in ACCOUNTS]
    config = {"real_claude": str(fake), "default_config_dir": "~/.claude",
              "security": "/usr/bin/false",     # a keychain tool that finds nothing
              "accounts": [], "order": {"interactive": names, "headless": names}}
    plan = {}
    for i, (name, email, sub, days) in enumerate(ACCOUNTS):
        account_id, org_id = demo_id(name, "account"), demo_id(name, "org")
        # The account's own config folder. Its .claude.json names the login: that is
        # how the fake claude and desktop sync tell the accounts apart.
        write_json(home / (".claude-" + name) / ".claude.json", {"oauthAccount": {
            "accountUuid": account_id, "organizationUuid": org_id, "emailAddress": email}})
        config["accounts"].append({"name": name, "email": email, "config_dir": "~/.claude-" + name})
        ends = (now + dt.timedelta(days=days)).astimezone(dt.timezone.utc)
        plan[email] = {"plan": sub, "login_expires_at": ends.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                       "reply": ANSWER, "exit_code": 0}
        # One desktop app record per chat. The conversation itself is a transcript in
        # the shared ~/.claude/projects, as with the real app.
        folder = desktop / "claude-code-sessions" / account_id / org_id
        for n, title in enumerate(CHATS[name]):
            sid, cli_id = demo_id(name, title, "desktop"), demo_id(name, title)
            at = int((now - dt.timedelta(hours=3 * n + i + 1)).timestamp() * 1000)
            write_json(folder / ("local_%s.json" % sid), {
                "sessionId": "local_" + sid, "cliSessionId": cli_id, "cwd": str(cwd),
                "title": title, "model": "opus", "permissionMode": "default", "isArchived": False,
                "createdAt": at - 600000, "lastActivityAt": at, "lastTurnEdgeAt": at,
                "latestUserFrameAt": at, "lastFocusedAt": at})
            line = {"type": "user", "sessionId": cli_id, "message": {"role": "user", "content": title}}
            (transcripts / (cli_id + ".jsonl")).write_text(json.dumps(line) + "\n", encoding="utf-8")
    # The first account is out of usage until the next full hour 2 to 3 hours from
    # now, a realistic reset time, worded the way Claude Code words it.
    reset = (now + dt.timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
    reset += dt.timedelta(hours=1)
    plan[ACCOUNTS[0][1]].update(exit_code=1, reply="You've hit your session limit · resets %s (%s)"
                                % (reset.strftime("%-I%p").lower(), ZONE))
    write_json(desktop / "config.json", {"lastKnownAccountUuid": demo_id(SIGNED_IN, "account")})
    tool = home / ".config" / "claude-failover"
    write_json(tool / "config.json", config)
    write_json(home / ".demo-plan.json", plan)
    return {
        "HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
        "TZ": ZONE,
        "CLAUDE_FAILOVER_CONFIG": str(tool / "config.json"),
        "CLAUDE_FAILOVER_STATE_DIR": str(tool / "state"),
        "CLAUDE_FAILOVER_HANDOFF_DIR": str(tool / "handoffs"),
        "CLAUDE_FAILOVER_REAL_CLAUDE": str(fake),
        "CLAUDE_FAILOVER_DESKTOP_DIR": str(desktop),
        "CLAUDE_FAILOVER_LAUNCH_AGENTS_DIR": str(home / "Library" / "LaunchAgents"),
        "CLAUDE_FAILOVER_LAUNCHCTL": "0",      # never load a LaunchAgent
        "CLAUDE_FAILOVER_NO_NOTIFY": "1",      # no macOS notifications
        "CLAUDE_FAILOVER_APP_RUNNING": "1",    # the desktop app counts as running
        "DEMO_CLAUDE_PLAN": str(home / ".demo-plan.json"),
    }


def run(env, home, extra, cmd):
    """Run one demo command in the sandbox. Returns the lines it printed, stdout and
    stderr in the order they were written, as a terminal shows them."""
    argv = ["/usr/bin/python3", "-I", "-B", str(PROGRAMS[cmd[0]])] + cmd[1:]   # -B: no .pyc in the repo
    try:
        proc = subprocess.run(argv, env=dict(env, **extra), cwd=str(home), stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
    except subprocess.TimeoutExpired:
        raise SystemExit("make_demo: `%s` did not finish within 2 minutes" % " ".join(cmd))
    text = proc.stdout.decode("utf-8", "replace")
    if proc.returncode != 0:
        raise SystemExit("make_demo: `%s` exited %d:\n%s" % (" ".join(cmd), proc.returncode, text))
    # Terminal colors and other control characters are not allowed in XML.
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|[\x00-\x08\x0b-\x1f\x7f]", "", text.expandtabs())
    # Sandbox paths print as ~/... already (HOME is the sandbox). Shorten any that
    # print in full, then refuse to draw a path of this machine.
    for path, short in ((os.path.realpath(str(home)), "~"), (str(home), "~"),
                        (str(REPO), "~/claude-failover")):
        text = text.replace(path, short)
    for local in (os.path.expanduser("~"), "/private/", "/var/folders/", "/tmp/"):
        if local in text:
            raise SystemExit("make_demo: `%s` printed a local path:\n%s" % (" ".join(cmd), text))
    return [line.rstrip() for line in text.rstrip().splitlines()]


# ---- The terminal -----------------------------------------------------------

WIDTH, FONT, CHAR, ROW = 860, 13, 7.8, 19   # px. CHAR is 0.6 em, the advance of common monospace fonts
LEFT, TOP, BOTTOM = 20, 52, 14              # text inset, top of the first row, space under the last
COLS = int((WIDTH - 2 * LEFT) / CHAR) - 1   # 104 characters per row
BG, BAR, EDGE, CURSOR = "#0d1117", "#161b22", "#30363d", "#9da7b3"
NOTE = "[claude-failover]"                  # how the wrapper's own notes start: drawn in amber
NBSP = "\u00a0"                             # SVG would merge runs of plain spaces and break the columns
STYLE = (
    "text{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:%dpx}"
    ".out{fill:#c9d1d9}.cmd{fill:#f0f6fc}.ps{fill:#7ee787}.comment{fill:#8b949e}.note{fill:#e3b341}"
    ".title{fill:#8b949e;font-size:12px}"
    # Each row fades in at its own animation-delay. "both" keeps it hidden until then
    # and holds the last frame. Without animations (or with reduced motion) all shows.
    ".f{animation:fade .2s ease-out both}.cover{opacity:0}"
    "@keyframes fade{from{opacity:0}to{opacity:1}}@keyframes hide{from{opacity:1}to{opacity:0}}"
    "@keyframes blink{50%%{opacity:0}}"
    "@media (prefers-reduced-motion:reduce){.f,.cover,.cursor{animation:none}}" % FONT)


def shown(extra, cmd):
    """The command as typed at the prompt, quoted the way a shell needs it."""
    def quote(arg):
        if re.fullmatch(r"[\w@%+=:,./-]+", arg):
            return arg
        return '"%s"' % re.sub(r'(["\\$`])', r"\\\1", arg)
    return " ".join(["%s=%s" % kv for kv in extra.items()] + [quote(a) for a in cmd])


def wrap(line):
    """Rows for one line of output. A line too long for a row breaks at a space and
    goes on 2 columns deeper. An arrow ("a -> b") starts the new row when it can."""
    if len(line) <= COLS:
        return [line]
    indent = " " * (len(line) - len(line.lstrip()) + 2)
    head, arrow, tail = line.rpartition(" -> ")
    if arrow and len(head) <= COLS and len(indent) + 3 + len(tail) <= COLS:
        return [head, indent + "-> " + tail]
    return textwrap.wrap(line, COLS, subsequent_indent=indent, break_on_hyphens=False)


def timeline(runs):
    """Every row of the terminal, top to bottom, with its timing in seconds:
    (kind, text, appears at, typing starts at, typing takes). Also the total."""
    rows, t = [], 0.3
    for comment, command, lines in runs:
        if len(command) + 2 > COLS:
            raise SystemExit("make_demo: command too long for one row: %s" % command)
        if rows:
            rows.append(("blank", "", t, 0, 0))
        rows.append(("comment", comment, t, 0, 0))
        t += 0.3
        typing = min(0.035 * len(command), 1.6)
        rows.append(("command", command, t, t + 0.35, typing))
        t += 0.35 + typing + 0.45               # type it, press return, first output
        previous = None
        for line in lines:
            kind = "note" if line.startswith(NOTE) else "out"
            if kind == "note":
                t += 0.5                        # the first account's attempt
            elif previous == "note":
                t += 0.8                        # the run again on the next account
            previous = kind
            for part in wrap(line):
                rows.append((kind, part, t, 0, 0))
                t += 0.06
        t += 0.65
    rows.append(("prompt", "", t, 0, 0))
    return rows, t + 0.2


def text(cls, x, y, content, delay=None):
    """One row of text. textLength pins it to the grid whatever the font's width."""
    style = ' style="animation-delay:%.2fs"' % delay if delay is not None else ""
    return '<text class="%s" x="%.1f" y="%d" textLength="%.1f" lengthAdjust="spacing"%s>%s</text>' % (
        cls, x, y, len(content) * CHAR, style, escape(content).replace(" ", NBSP))


def rect(x, y, width, fill, extra=""):
    return '<rect x="%.1f" y="%d" width="%.1f" height="%d" fill="%s"%s/>' % (x, y, width, ROW, fill, extra)


def render(rows):
    height = TOP + len(rows) * ROW + BOTTOM
    x_cmd = LEFT + 2 * CHAR                     # after the "$ " prompt
    frames, body = [], []
    for i, (kind, content, start, type_at, typing) in enumerate(rows):
        top, base = TOP + i * ROW, TOP + i * ROW + 14   # the row's top and its text baseline
        if kind == "command":
            # The whole command sits under a cover (a cursor block, then a bar in the
            # background color) that steps right one character at a time.
            n = len(content)
            frames.append("@keyframes type%d{from{transform:translateX(0)}to{transform:translateX(%.1fpx)}}"
                          % (i, n * CHAR))
            body += ['<g class="f" style="animation-delay:%.2fs">' % start,
                     text("ps", LEFT, base, "$"), text("cmd", x_cmd, base, content),
                     '<g class="cover" style="animation:type%d %.2fs steps(%d,end) %.2fs both,'
                     'hide .01s %.2fs both">' % (i, typing, n, type_at, type_at + typing + 0.2),
                     rect(x_cmd, top, CHAR, CURSOR), rect(x_cmd + CHAR, top, n * CHAR, BG), "</g></g>"]
        elif kind == "prompt":                  # the last, empty prompt: the cursor blinks, then stays
            body += ['<g class="f" style="animation-delay:%.2fs">' % start, text("ps", LEFT, base, "$"),
                     rect(x_cmd, top, CHAR, CURSOR,
                          ' class="cursor" style="animation:blink 1s step-end %.2fs 3"' % start), "</g>"]
        elif content:
            body.append(text(kind + " f", LEFT, base, content, start))
    head = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" '
        'role="img" aria-labelledby="title desc">' % (WIDTH, height, WIDTH, height),
        '<title id="title">claude-failover demo</title>',
        '<desc id="desc">%s</desc>' % escape(DESC),
        "<style>%s%s</style>" % (STYLE, "".join(frames)),
        # A cover steps past the end of its command, so the screen clips it.
        '<clipPath id="screen"><rect x="1" y="37" width="%d" height="%d"/></clipPath>'
        % (WIDTH - 2, height - 38),
        '<rect width="%d" height="%d" rx="10" fill="%s"/>' % (WIDTH, height, BG),
        '<path d="M0 37V10A10 10 0 0 1 10 0H%dA10 10 0 0 1 %d 10V37Z" fill="%s"/>'
        % (WIDTH - 10, WIDTH, BAR),
        '<path d="M0 36.5H%d" stroke="%s"/>' % (WIDTH, EDGE),
        '<circle cx="20" cy="18.5" r="6" fill="#ff5f57"/><circle cx="40" cy="18.5" r="6" '
        'fill="#febc2e"/><circle cx="60" cy="18.5" r="6" fill="#28c840"/>',
        '<text class="title" x="%d" y="23" text-anchor="middle">claude-failover</text>' % (WIDTH // 2),
        '<g clip-path="url(#screen)">']
    tail = ["</g>", '<rect x=".5" y=".5" width="%d" height="%d" rx="9.5" fill="none" stroke="%s"/>'
            % (WIDTH - 1, height - 1, EDGE), "</svg>"]
    return "\n".join(head + body + tail) + "\n"


def check(svg, rows):
    """The file must parse as XML and draw every row."""
    root = ET.fromstring(svg.encode("utf-8"))
    drawn = {"".join(t.itertext()).replace(NBSP, " ")
             for t in root.iter("{http://www.w3.org/2000/svg}text")}
    missing = [row[1] for row in rows if row[1] and row[1] not in drawn]
    if missing:
        raise SystemExit("make_demo: rows missing from the SVG: %r" % missing)


def main(argv):
    if any(a != "--keep" for a in argv):
        raise SystemExit(__doc__)
    root = Path(tempfile.mkdtemp(prefix="claude-failover-demo-"))
    try:
        home = root / "home"
        env = build_sandbox(home, dt.datetime.now(ZoneInfo(ZONE)))
        runs = [(comment, shown(extra, cmd), run(env, home, extra, cmd))
                for comment, extra, cmd in STEPS]
    finally:
        if "--keep" in argv:
            print("sandbox kept: %s" % root)
        else:
            shutil.rmtree(str(root), ignore_errors=True)
    rows, seconds = timeline(runs)
    svg = render(rows)
    check(svg, rows)
    OUT.write_text(svg, encoding="utf-8")
    for kind, content, *_ in rows:
        print(("$ " if kind == "command" else "") + content)
    print("\nwrote %s: %d rows, %.1f s of animation, %.1f KB"
          % (OUT.relative_to(REPO), len(rows), seconds, len(svg.encode("utf-8")) / 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
