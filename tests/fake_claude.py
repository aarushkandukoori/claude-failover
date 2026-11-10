#!/usr/bin/python3 -I
"""Stand-in for the claude binary in tests. Behaviour per account comes from
$FAKE_CLAUDE_PLAN (JSON {account: behaviour}); every call is appended to
$FAKE_CLAUDE_LOG so tests can see argv, stdin and the account used.

Transcripts go to $FAKE_CLAUDE_HOME/projects. With FAKE_CLAUDE_PER_DIR=1 an account
folder's transcripts go to $CLAUDE_CONFIG_DIR/projects instead, as with the real
CLI: through the link to the shared folder, or into a folder of its own."""
import json, os, signal, sys, time, uuid

argv = sys.argv[1:]
token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
cdir = os.environ.get("CLAUDE_CONFIG_DIR")
acct = os.path.basename(cdir) if cdir else "default"
plan = json.loads(open(os.environ["FAKE_CLAUDE_PLAN"]).read())
behaviour = plan.get(acct, "ok")
if isinstance(behaviour, list):  # consume one behaviour per call
    counter = os.environ["FAKE_CLAUDE_PLAN"] + "." + acct + ".n"
    n = int(open(counter).read()) if os.path.exists(counter) else 0
    open(counter, "w").write(str(n + 1))
    behaviour = behaviour[min(n, len(behaviour) - 1)]
stdin = "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()
env_keys = sorted(k for k in os.environ if k.startswith(("CLAUDE", "ANTHROPIC")))
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
    f.write(json.dumps({"acct": acct, "argv": argv, "stdin": stdin, "env": env_keys,
                        "config_dir": cdir, "pid": os.getpid()}) + "\n")

def val(flag):
    if flag in argv:
        i = argv.index(flag)
        return argv[i + 1] if i + 1 < len(argv) else None
    return None

sid = val("--session-id") or val("--resume") or str(uuid.uuid4())
home = cdir if cdir and os.environ.get("FAKE_CLAUDE_PER_DIR") == "1" else os.environ["FAKE_CLAUDE_HOME"]
projects = os.path.join(home, "projects", "-tmp-proj")
os.makedirs(projects, exist_ok=True)
tpath = os.path.join(projects, sid + ".jsonl")
if "--resume" in argv and not os.path.exists(os.path.join(projects, val("--resume") + ".jsonl")):
    print("No conversation found with session ID: %s" % sid)
    sys.exit(1)
with open(tpath, "a") as f:
    f.write(json.dumps({"type": "user", "timestamp": "2026-10-08T00:00:00Z",
                        "message": {"role": "user", "content": "prompt"}}) + "\n")
json_out = val("--output-format") in ("json", "stream-json")

LIMIT = "You've hit your session limit · resets 3:45pm (America/New_York)"
# Like the real CLI (2.1.318+), a JSON result names the day the credential ends:
# the stored login's date, or the token's when CLAUDE_CODE_OAUTH_TOKEN is set.
expires = os.environ.get("FAKE_CLAUDE_TOKEN_EXPIRES" if token else "FAKE_CLAUDE_EXPIRES")
def finish(text, rc, is_limit=False):
    if json_out:
        print(json.dumps({"type": "result", "subtype": "success", "is_error": rc != 0,
                          "result": text, "session_id": sid,
                          **({"api_error_status": 429} if is_limit else {}),
                          **({"credential_expires_at": expires} if expires else {})}))
    else:
        print(text)
    sys.exit(rc)

if behaviour == "ok":
    finish("OK from %s" % acct, 0)
if behaviour == "limit":
    finish(LIMIT, 1, True)
if behaviour == "weekly_limit":
    finish("You've hit your weekly limit · resets Mon 12:00am", 1, True)
if behaviour == "opus_limit":
    finish("You've hit your Opus limit · resets Mon 12:00am", 1, True)
if behaviour == "work_then_limit":
    with open(tpath, "a") as f:
        f.write(json.dumps({"type": "assistant", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "message": {"role": "assistant", "content": [
                                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]}}) + "\n")
    finish(LIMIT, 1, True)
if behaviour in ("work_then_error", "work_then_auth"):
    with open(tpath, "a") as fh:
        fh.write(json.dumps({"type": "assistant", "timestamp": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                             "message": {"role": "assistant", "content": [
                                 {"type": "tool_use", "id": "t2", "name": "Bash", "input": {}}]}}) + "\n")
    if behaviour == "work_then_error":
        finish("Error: Reached max turns (3)", 1)
    finish("OAuth token has expired. Please run /login", 1)
if behaviour == "auth":
    finish("Not logged in · Please run /login", 1)
if behaviour == "auth_unless_token":   # login expired, spare token fine
    if token:
        finish("OK from %s via token" % acct, 0)
    finish("Failed to authenticate: OAuth session expired and could not be refreshed", 1)
if behaviour == "token_echo_auth":     # a refused token whose error repeats it
    finish("Failed to authenticate: token %s was rejected" % token, 1)
if behaviour == "throttle":
    finish("API Error: Server is temporarily limiting requests (not your usage limit)", 1)
if behaviour == "error":
    finish("Error: something else broke", 1)
if behaviour in ("killed", "work_then_killed"):   # dies from a signal nobody sent (a crash)
    if behaviour == "work_then_killed":
        with open(tpath, "a") as fh:
            fh.write(json.dumps({"type": "assistant", "timestamp": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                                 "message": {"role": "assistant", "content": [
                                     {"type": "tool_use", "id": "t3", "name": "Bash", "input": {}}]}}) + "\n")
    os.kill(os.getpid(), signal.SIGKILL)
if behaviour == "sleep":
    time.sleep(120)
    finish("woke", 0)
finish("unknown behaviour %s" % behaviour, 3)
