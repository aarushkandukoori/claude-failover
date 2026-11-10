"""Tests for claude-failover against a fake claude binary (no real API calls).

Run from the repo root:  /usr/bin/python3 -m unittest discover -s tests -v
"""
import contextlib
import datetime as dt
import io
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import claude_failover as cf  # noqa: E402

FAKE = PROJECT / "tests" / "fake_claude.py"
FAKE_SECURITY = PROJECT / "tests" / "fake_security.py"
WRAPPER = PROJECT / "bin" / "claude-failover"
HOOK = PROJECT / "bin" / "claude-failover-hook"


class Sandbox(unittest.TestCase):
    """Temp config: account A = its own folder, B = the plain default login (no folder).
    HOME is a temp folder too, so nothing reads or writes the real ~/.claude*."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cf-test-"))
        self.default = self.tmp / "default"
        (self.default / "projects").mkdir(parents=True)
        (self.default / "settings.json").write_text('{"model": "fable"}')
        (self.default / "CLAUDE.md").write_text("# rules")
        self.prof = self.tmp / "A"
        self.prof.mkdir()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.cfg_path = self.tmp / "config.json"
        self.cfg_path.write_text(json.dumps({
            "real_claude": str(FAKE),
            "default_config_dir": str(self.default),
            "accounts": [
                {"name": "A", "email": "a@example.com", "config_dir": str(self.prof),
                 "desktop_account_uuid": "uuid-A"},
                {"name": "B", "email": "b@example.com", "config_dir": None,
                 "desktop_account_uuid": "uuid-B"},
            ],
            "order": {"interactive": ["A", "B"], "headless": ["A", "B"]},
            "handoff": {"from_desktop": True, "from_terminal": True},
            "unknown_reset_minutes": 30, "auth_down_recheck_minutes": 30,
            "turned_away_seconds": 90,
            "security": str(FAKE_SECURITY),   # the real keychain is never touched
        }))
        self.state = self.tmp / "state"
        self.handoffs = self.tmp / "handoffs"
        self.plan_path = self.tmp / "plan.json"
        self.log_path = self.tmp / "calls.jsonl"
        self.env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.home),
            "CLAUDE_FAILOVER_CONFIG": str(self.cfg_path),
            "CLAUDE_FAILOVER_STATE_DIR": str(self.state),
            "CLAUDE_FAILOVER_HANDOFF_DIR": str(self.handoffs),
            "FAKE_CLAUDE_PLAN": str(self.plan_path),
            "FAKE_CLAUDE_LOG": str(self.log_path),
            "FAKE_CLAUDE_HOME": str(self.default),
            "FAKE_SECURITY_STORE": str(self.tmp / "keychain.json"),
            "FAKE_SECURITY_LOG": str(self.tmp / "security.jsonl"),
            "CLAUDE_FAILOVER_NO_NOTIFY": "1",
        }
        # the module reads these from os.environ too
        self._saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def plan(self, **behaviour):
        self.plan_path.write_text(json.dumps(behaviour))

    def run_wrapper(self, args, stdin=None, extra_env=None, timeout=60):
        env = dict(self.env)
        env.update(extra_env or {})
        kw = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
        return subprocess.run([str(WRAPPER)] + args, capture_output=True, text=True, env=env,
                              timeout=timeout, cwd=str(self.tmp), **kw)

    def calls(self):
        if not self.log_path.exists():
            return []
        return [json.loads(l) for l in self.log_path.read_text().splitlines()]

    def state_json(self):
        return json.loads((self.state / "accounts.json").read_text())


class ClassifyTests(unittest.TestCase):
    def test_text_limits(self):
        for text in [
            "You've hit your session limit · resets 3:45pm",
            "You’ve hit your weekly limit · resets Mon 12:00am",
            "API Error: You've hit your limit · resets 2:50pm (America/Toronto)",
            "Claude AI usage limit reached|1759770000",
            "5-hour limit reached ∙ resets 12pm",
            "You're out of extra usage · resets 9pm",
            "Fable 5 requires usage credits",
        ]:
            self.assertEqual(cf.classify(1, text, "")["kind"], "limit", text)

    def test_throttle_is_not_our_limit(self):
        v = cf.classify(1, "API Error: Server is temporarily limiting requests (not your usage limit)", "")
        self.assertEqual(v["kind"], "error")

    def test_ok_and_auth(self):
        self.assertEqual(cf.classify(0, "You've hit your session limit", "")["kind"], "ok")
        self.assertEqual(cf.classify(1, "Not logged in · Please run /login", "")["kind"], "auth")
        self.assertEqual(cf.classify(1, "Failed to authenticate. API Error: 401", "")["kind"], "auth")

    def test_limit_text_inside_long_successful_answer_is_ignored(self):
        self.assertEqual(cf.classify(0, "blah\nYou've hit your limit\nblah", "")["kind"], "ok")

    def test_json_and_stream_json(self):
        js = json.dumps({"type": "result", "is_error": True, "api_error_status": 429,
                         "result": "You've hit your session limit · resets 3:45pm"})
        self.assertEqual(cf.classify(1, js, "")["kind"], "limit")
        js2 = json.dumps({"type": "result", "is_error": True, "api_error": "usage_limit_reached",
                          "result": "whatever", "api_error_params": {"rate_limit_info": {"resetsAt": 1791500000}}})
        v = cf.classify(1, js2, "")
        self.assertEqual(v["kind"], "limit")
        self.assertEqual(int(v["resets_at"].timestamp()), 1791500000)
        stream = "\n".join([
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 1791500000}}),
            json.dumps({"type": "result", "is_error": True, "result": "You've hit your session limit"}),
        ])
        self.assertEqual(cf.classify(1, stream, "")["kind"], "limit")

    def test_model_scope(self):
        v = cf.classify(1, "You've hit your Opus limit · resets Mon 12:00am", "")
        self.assertEqual((v["scope"], v["family"]), ("model", "opus"))
        v = cf.classify(1, "You've hit your weekly limit · resets Mon 12:00am", "")
        self.assertEqual(v["scope"], "account")


class ResetParseTests(unittest.TestCase):
    NOW = dt.datetime(2026, 10, 8, 1, 0, tzinfo=cf.LOCAL_TZ)  # Thu 1:00am

    def p(self, text):
        return cf.parse_resets(text, self.NOW)

    def test_clock_today_and_tomorrow(self):
        t = self.p("You've hit your session limit · resets 3:45pm")
        self.assertEqual((t.day, t.hour, t.minute), (8, 15, 45))
        t = self.p("You've hit your session limit · resets 12:30am")
        self.assertEqual((t.day, t.hour, t.minute), (9, 0, 30))

    def test_weekday_month_and_zone(self):
        t = self.p("You've hit your weekly limit · resets Mon 12:00am")
        self.assertEqual((t.weekday(), t.hour), (0, 0))
        self.assertTrue(t > self.NOW)
        t = self.p("resets Oct 10, 3pm")
        self.assertEqual((t.month, t.day, t.hour), (10, 10, 15))
        t = self.p("You've hit your limit · resets 5am (Asia/Tokyo)")
        self.assertIsNotNone(t)
        self.assertEqual(t.astimezone(cf.ZoneInfo("Asia/Tokyo")).hour, 5)

    def test_reset_in_next_year(self):
        now = dt.datetime(2026, 12, 28, 10, 0, tzinfo=cf.LOCAL_TZ)
        t = cf.parse_resets("You've hit your weekly limit \u00b7 resets Jan 2, 2027 at 12am", now)
        self.assertEqual((t.year, t.month, t.day, t.hour), (2027, 1, 2, 0))

    def test_garbage(self):
        self.assertIsNone(self.p("You've hit your limit"))
        self.assertIsNone(self.p("resets whenever"))


class ArgvTests(unittest.TestCase):
    def test_positional_detection(self):
        info = cf.parse_argv(["-p", "do X", "--model", "sonnet", "--allowedTools", "Read", "Edit"])
        self.assertEqual([t for _, t in info["positionals"]], ["do X"])
        self.assertTrue(info["print"])
        self.assertEqual(cf.opt(info, "--allowedTools"), ["Read", "Edit"])
        info = cf.parse_argv(["--model", "opus", "-p", "--output-format", "text"])
        self.assertEqual(info["positionals"], [])
        self.assertEqual(cf.parse_argv(["auth", "status"])["subcommand"], "auth")

    def test_resume_argv(self):
        argv = ["-p", "do X", "--model", "sonnet", "--allowedTools", "Read", "Edit",
                "--session-id", "abc"]
        info = cf.parse_argv(argv)
        out = cf.build_resume_argv(argv, info, "sid1", "CONTINUE")
        self.assertEqual(out[:2], ["-p", "CONTINUE"])
        self.assertNotIn("do X", out)
        self.assertNotIn("abc", out)
        self.assertEqual(out[-2:], ["--resume", "sid1"])
        self.assertIn("--allowedTools", out)
        # the new prompt must still be a positional for claude's parser
        info2 = cf.parse_argv(out)
        self.assertEqual([t for _, t in info2["positionals"]], ["CONTINUE"])


class EnvTests(Sandbox):
    def test_nested_desktop_env_is_cleaned(self):
        cfg = cf.load_config()
        base = {"HOME": "/h", "PATH": "/bin", "CLAUDECODE": "1", "CLAUDE_CODE_SIMPLE": "1",
                "CLAUDE_CODE_ENTRYPOINT": "claude-desktop", "CLAUDE_CODE_OAUTH_TOKEN": "x",
                "ANTHROPIC_API_KEY": "k", "ANTHROPIC_BASE_URL": "u", "CLAUDE_FAILOVER_ACCOUNT": "B",
                "CLAUDE_CONFIG_DIR": "/wrong"}
        env = cf.child_env(cfg, "A", base)
        for k in ("CLAUDECODE", "CLAUDE_CODE_SIMPLE", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY",
                  "ANTHROPIC_BASE_URL", "CLAUDE_FAILOVER_ACCOUNT", "CLAUDE_CODE_ENTRYPOINT"):
            self.assertNotIn(k, env)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.prof))
        env = cf.child_env(cfg, "B", base)
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)

    def test_securestorage_override_for_shared_keychain_login(self):
        cfg = cf.load_config()
        cfg["by_name"]["B"]["config_dir"] = str(self.tmp / "Bdir")
        cfg["by_name"]["B"]["securestorage_dir"] = ""
        env = cf.child_env(cfg, "B", {"CLAUDE_SECURESTORAGE_CONFIG_DIR": "/elsewhere"})
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.tmp / "Bdir"))
        self.assertEqual(env["CLAUDE_SECURESTORAGE_CONFIG_DIR"], "")
        env = cf.child_env(cfg, "A", {"CLAUDE_SECURESTORAGE_CONFIG_DIR": "/elsewhere"})
        self.assertNotIn("CLAUDE_SECURESTORAGE_CONFIG_DIR", env)
        self.assertEqual(cf.default_login_account(cfg), "B")

    def test_host_account_vars_ignored_for_headless_runs(self):
        cfg = cf.load_config()
        host = {"CLAUDE_CODE_ACCOUNT_UUID": "uuid-A", "CLAUDE_CODE_USER_EMAIL": "a@example.com"}
        self.assertEqual(cf.identify_account(cfg, host), "A")
        self.assertEqual(cf.identify_account(cfg, host, trust_host=False), "B")

    def test_dangling_shared_dir_is_flagged_and_repaired(self):
        cfg = cf.load_config()
        cf.ensure_profile_links(cfg, "A")
        self.assertTrue((self.default / "plans" / ".keep").exists())
        subprocess.run(["rm", "-rf", str(self.default / "plans")])
        self.assertTrue(any("plans" in p for p in cf.profile_problems(cfg, "A")))
        cf.ensure_profile_links(cfg, "A")
        self.assertEqual(cf.profile_problems(cfg, "A"), [])

    def test_user_claude_code_settings_kept_when_not_nested(self):
        cfg = cf.load_config()
        env = cf.child_env(cfg, "B", {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "9", "ANTHROPIC_API_KEY": "k"})
        self.assertEqual(env.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS"), "9")
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_profile_links(self):
        cfg = cf.load_config()
        made = cf.ensure_profile_links(cfg, "A")
        self.assertIn("projects", made)
        self.assertTrue((self.prof / "CLAUDE.md").is_symlink())
        self.assertEqual(cf.profile_problems(cfg, "A"), [])
        self.assertEqual(cf.ensure_profile_links(cfg, "A"), [])  # idempotent
        self.assertTrue(cf.shares_projects(cfg, "A") and cf.shares_projects(cfg, "B"))

    def test_status_names_what_is_not_linked(self):
        (self.prof / "projects").mkdir()   # a folder used on its own before
        cfg = cf.load_config()
        self.assertFalse(cf.shares_projects(cfg, "A"))
        auth = {"loggedIn": True, "email": "a@example.com", "subscriptionType": "max"}
        out = io.StringIO()
        with mock.patch.object(cf, "auth_status", return_value=auth), contextlib.redirect_stdout(out):
            cf.accounts_status(cfg)
        text = out.getvalue()
        self.assertIn("PROFILE NOT LINKED", text)
        self.assertIn("already has its own: projects", text)
        self.assertIn("not moved to another account", text)
        self.assertIn("claude-accounts setup", text)


class HeadlessTests(Sandbox):
    def test_print_runs_are_pinned_to_default_permission_mode(self):
        # settings.json may make bypassPermissions the default for chats; a -p job
        # must still deny every tool outside --allowedTools
        self.plan(A="ok")
        self.run_wrapper(["-p", "hi", "--allowedTools", "Read"])
        argv = self.calls()[-1]["argv"]
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "default")
        self.assertIn("hi", argv)

    def test_print_run_keeps_the_mode_it_asked_for(self):
        self.plan(A="ok")
        self.run_wrapper(["-p", "hi", "--dangerously-skip-permissions"])
        self.assertNotIn("--permission-mode", self.calls()[-1]["argv"])
        self.run_wrapper(["-p", "hi", "--permission-mode", "acceptEdits"])
        argv = self.calls()[-1]["argv"]
        self.assertEqual(argv.count("--permission-mode"), 1)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")

    def test_print_run_mirrors_the_mode_in_its_own_settings(self):
        self.plan(A="ok")
        self.run_wrapper(["-p", "hi", "--settings", '{"permissions": {"defaultMode": "auto"}}'])
        argv = self.calls()[-1]["argv"]
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "auto")
        self.run_wrapper(["-p", "hi", "--settings", '{"autoMode": {"allow": ["$defaults"]}}'])
        argv = self.calls()[-1]["argv"]
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "default")
        self.run_wrapper(["-p", "hi", "--settings", "/nonexistent/settings.json"])
        argv = self.calls()[-1]["argv"]
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "default")

    def test_interactive_run_is_not_pinned(self):
        self.plan(A="ok")
        self.run_wrapper(["--version"])
        self.assertNotIn("--permission-mode", self.calls()[-1]["argv"])

    def test_limit_fails_over_and_is_remembered(self):
        self.plan(A="limit", default="ok")
        r = self.run_wrapper(["-p", "hello", "--model", "sonnet"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "OK from default")
        self.assertEqual(r.stderr, "")   # silent on success: callers grep stderr
        calls = self.calls()
        self.assertEqual([c["acct"] for c in calls], ["A", "default"])
        self.assertEqual(calls[0]["config_dir"], str(self.prof))
        self.assertIsNone(calls[1]["config_dir"])
        self.assertIn("hello", calls[1]["argv"])
        st = self.state_json()["accounts"]
        self.assertIn("limited_until", st["A"])
        # next call goes straight to B
        r = self.run_wrapper(["-p", "again"])
        self.assertEqual([c["acct"] for c in self.calls()][-1:], ["default"])
        self.assertEqual(len(self.calls()), 3)

    def test_unmanaged_session_is_not_rerun(self):
        self.plan(A="work_then_limit", default="ok")
        r = self.run_wrapper(["-p", "hi", "--no-session-persistence"])
        self.assertEqual(r.returncode, 1)
        self.assertEqual([c["acct"] for c in self.calls()], ["A"])

    def test_partial_work_then_other_error_reports_the_limit(self):
        self.plan(A="work_then_limit", default="auth")
        r = self.run_wrapper(["-p", "send it"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("hit your session limit", r.stdout)

    def test_a_rerun_passes_the_next_accounts_own_error_through(self):
        # Nothing ran before the move, so the later error is the run's real outcome.
        self.plan(A="limit", default="error")
        r = self.run_wrapper(["-p", "hi"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("something else broke", r.stdout)
        self.assertNotIn("hit your session limit", r.stdout)
        self.assertNotIn("PARTIAL WORK DONE", r.stdout)
        self.assertEqual([c["acct"] for c in self.calls()], ["A", "default"])

    def test_a_refused_login_it_cannot_check_returns_the_login_error(self):
        # No transcript to read, so the run is not moved: the caller gets Claude's own
        # login error, and no other account is tried.
        self.plan(A="auth", default="ok")
        r = self.run_wrapper(["-p", "hi", "--no-session-persistence"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("Not logged in", r.stdout)
        self.assertEqual([c["acct"] for c in self.calls()], ["A"])

    def test_subcommands_use_default_account(self):
        self.plan(A="ok", default="ok")
        self.run_wrapper(["auth", "status"])
        self.assertEqual(self.calls()[-1]["acct"], "default")
        self.run_wrapper(["auth", "status"], extra_env={"CLAUDE_FAILOVER_ACCOUNT": "A"})
        self.assertEqual(self.calls()[-1]["acct"], "A")

    def test_fork_session_failover_resumes_the_fork(self):
        self.plan(A="work_then_limit", default="ok")
        base = "abcabcab-0000-0000-0000-000000000000"
        (self.default / "projects" / "-tmp-proj").mkdir(parents=True, exist_ok=True)
        (self.default / "projects" / "-tmp-proj" / (base + ".jsonl")).write_text("{}\n")
        self.run_wrapper(["-p", "go", "--resume", base, "--fork-session"])
        first, second = self.calls()
        fork = first["argv"][first["argv"].index("--session-id") + 1]
        self.assertNotEqual(fork, base)
        self.assertEqual(second["argv"][-2:], ["--resume", fork])

    def test_success_does_not_clear_saved_limit(self):
        # past its limit an account with extra usage still answers (billed), so a
        # success must not steer work back onto it before the reset
        cfg = cf.load_config()
        until = cf.now_local() + dt.timedelta(hours=5)
        cf.record_verdict(cfg, "A", {"kind": "limit", "text": "x", "scope": "account", "resets_at": until})
        self.plan(A="ok", default="ok")
        self.run_wrapper(["-p", "hi", "--model", "sonnet"], extra_env={"CLAUDE_FAILOVER_ACCOUNT": "A"})
        self.assertIn("limited_until", self.state_json()["accounts"]["A"])
        self.run_wrapper(["-p", "hi"])
        self.assertEqual(self.calls()[-1]["acct"], "default")

    def test_unknown_option_blocks_resume(self):
        self.plan(A="work_then_limit", default="ok")
        r = self.run_wrapper(["-p", "send it", "--brand-new-option", "value"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("hit your session limit", r.stdout)
        self.assertEqual([c["acct"] for c in self.calls()], ["A"])

    def test_attached_short_resume_value(self):
        info = cf.parse_argv(["-p", "-rabc-123", "next step"])
        self.assertEqual(cf.opt(info, "-r"), "abc-123")
        self.assertEqual([t for _, t in info["positionals"]], ["next step"])
        self.plan(A="ok")
        self.run_wrapper(["-p", "-rabc-123", "next step"])
        self.assertNotIn("--session-id", self.calls()[0]["argv"])

    def test_partial_auth_then_error_prints_marker(self):
        self.plan(A="work_then_auth", default="work_then_error")
        r = self.run_wrapper(["-p", "file the report"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("PARTIAL WORK DONE", r.stdout)
        self.assertNotIn("OAuth", r.stdout)
        self.assertNotIn("authenticate", r.stdout.lower())

    def test_sigterm_during_resumed_attempt_returns_signal(self):
        self.plan(A="work_then_limit", default="sleep")
        p = subprocess.Popen([str(WRAPPER), "-p", "hi"], env=dict(self.env), stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, cwd=str(self.tmp))
        deadline = time.time() + 20
        while len(self.calls()) < 2 and time.time() < deadline:
            time.sleep(0.1)
        time.sleep(0.3)
        p.send_signal(signal.SIGTERM)
        out, _ = p.communicate(timeout=20)
        self.assertEqual(p.returncode, 128 + signal.SIGTERM)
        self.assertNotIn(b"hit your session limit", out)

    def test_missing_binary(self):
        cfg = json.loads(self.cfg_path.read_text())
        cfg["real_claude"] = str(self.tmp / "nope" / "claude")
        self.cfg_path.write_text(json.dumps(cfg))
        r = self.run_wrapper(["-p", "hi"])
        self.assertEqual(r.returncode, 127)
        self.assertIn("claude CLI not found", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_bare_keeps_api_key_and_skips_accounts(self):
        self.plan(default="ok")
        r = self.run_wrapper(["--bare", "-p", "hi"], extra_env={"ANTHROPIC_API_KEY": "k"})
        self.assertEqual(r.returncode, 0)
        call = self.calls()[0]
        self.assertIn("ANTHROPIC_API_KEY", call["env"])
        self.assertIsNone(call["config_dir"])
        self.assertFalse((self.state / "accounts.json").exists())

    def test_mcp_add_writes_every_account(self):
        self.plan(A="ok", default="ok")
        r = self.run_wrapper(["mcp", "add", "foo", "--", "npx", "foo"])
        self.assertEqual(r.returncode, 0)
        self.assertEqual(sorted(c["acct"] for c in self.calls()), ["A", "default", "default"])

    def test_bg_job_and_attach_use_same_account(self):
        self.plan(A="ok", default="ok")
        self.run_wrapper(["--bg", "refactor the tests"])
        cfg = cf.load_config()
        cf.record_verdict(cfg, "A", {"kind": "limit", "text": "x", "scope": "account",
                                     "resets_at": cf.now_local() + dt.timedelta(hours=2)})
        self.run_wrapper(["attach", "abc"])
        self.assertEqual([c["acct"] for c in self.calls()], ["A", "A"])

    def test_partial_work_on_both_accounts_prints_marker(self):
        self.plan(A="work_then_limit", default="work_then_error")
        r = self.run_wrapper(["-p", "file the report"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("PARTIAL WORK DONE", r.stdout)

    def test_a_resumed_claude_killed_by_a_signal_reports_the_limit(self):
        # Killed by something other than the caller (a crash, or macOS under memory
        # pressure): a 137 exit code would invite a blind retry of finished work.
        self.plan(A="work_then_limit", default="killed")
        r = self.run_wrapper(["-p", "file the report"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("hit your session limit", r.stdout)
        self.assertEqual([c["acct"] for c in self.calls()], ["A", "default"])

    def test_a_resumed_claude_killed_after_more_work_prints_the_marker(self):
        self.plan(A="work_then_limit", default="work_then_killed")
        r = self.run_wrapper(["-p", "file the report"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("PARTIAL WORK DONE", r.stdout)

    def test_an_account_folder_with_its_own_projects_is_never_moved(self):
        # Its transcripts land in its own projects/ folder, out of the wrapper's
        # sight: it can't tell whether the stopped run did anything, so it must not
        # re-run the task elsewhere.
        (self.prof / "projects").mkdir()
        self.plan(A="work_then_limit", default="ok")
        r = self.run_wrapper(["-p", "send the report"], extra_env={"FAKE_CLAUDE_PER_DIR": "1"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("hit your session limit", r.stdout)
        self.assertEqual([c["acct"] for c in self.calls()], ["A"])
        self.assertTrue(list((self.prof / "projects").glob("*/*.jsonl")))   # its transcript is there

    def test_transcripts_written_through_the_projects_link_are_seen(self):
        self.plan(A="work_then_limit", default="ok")
        r = self.run_wrapper(["-p", "send the report"], extra_env={"FAKE_CLAUDE_PER_DIR": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        first, second = self.calls()
        sid = first["argv"][first["argv"].index("--session-id") + 1]
        self.assertEqual(second["argv"][-2:], ["--resume", sid])   # resumed, never re-run

    def test_mcp_project_scope_runs_once_and_user_scope_everywhere(self):
        self.plan(A="ok", default="ok")
        self.run_wrapper(["mcp", "add", "-s", "project", "foo", "--", "npx", "foo"])
        self.assertEqual(len(self.calls()), 1)
        self.plan(A="ok", default="error")   # the others "already exist": still a success
        r = self.run_wrapper(["mcp", "add", "--scope", "user", "foo", "--", "npx", "foo"])
        self.assertEqual(r.returncode, 0)
        self.assertEqual(len(self.calls()), 4)

    def test_purge_cleans_every_account(self):
        self.plan(A="ok", default="ok")
        self.run_wrapper(["purge", "/tmp/x", "-y"])
        self.assertEqual(len(self.calls()), 3)

    def test_job_commands_find_the_folder_that_holds_the_job(self):
        (self.default / "jobs" / "abc12345").mkdir(parents=True)
        (self.prof / "jobs" / "def67890").mkdir(parents=True)
        self.plan(A="ok", default="ok")
        self.run_wrapper(["attach", "abc1"])
        self.run_wrapper(["logs", "def6"])
        self.assertEqual([c["config_dir"] for c in self.calls()], [None, str(self.prof)])

    def test_verbose_notes(self):
        self.plan(A="limit", default="ok")
        r = self.run_wrapper(["-p", "x"], extra_env={"CLAUDE_FAILOVER_VERBOSE": "1"})
        self.assertEqual(r.returncode, 0)
        self.assertIn("re-running on B", r.stderr)

    def test_work_already_done_resumes_instead_of_rerunning(self):
        self.plan(A="work_then_limit", default="ok")
        r = self.run_wrapper(["-p", "send the emails", "--output-format", "text"])
        self.assertEqual(r.returncode, 0, r.stderr)
        first, second = self.calls()
        sid = first["argv"][first["argv"].index("--session-id") + 1]
        self.assertEqual(second["argv"][-2:], ["--resume", sid])
        self.assertNotIn("send the emails", second["argv"])
        self.assertTrue(any("Continue the original task" in a for a in second["argv"]))
        self.assertIn("--output-format", second["argv"])

    def test_stdin_prompt_is_replayed(self):
        self.plan(A="limit", default="ok")
        r = self.run_wrapper(["-p", "--model", "sonnet"], stdin="PROMPT FROM STDIN")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([c["stdin"] for c in self.calls()], ["PROMPT FROM STDIN"] * 2)

    def test_auth_failure_fails_over(self):
        self.plan(A="auth", default="ok")
        r = self.run_wrapper(["-p", "hi"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("auth_down_until", self.state_json()["accounts"]["A"])

    def test_other_errors_do_not_fail_over(self):
        for behaviour in ("error", "throttle"):
            self.log_path.unlink(missing_ok=True) if hasattr(Path, "unlink") else None
            self.plan(A=behaviour, default="ok")
            r = self.run_wrapper(["-p", "hi"])
            self.assertEqual(r.returncode, 1)
            self.assertEqual([c["acct"] for c in self.calls()][-1], "A")

    def test_all_limited(self):
        self.plan(A="limit", default="weekly_limit")
        r = self.run_wrapper(["-p", "hi"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("weekly limit", r.stdout)
        # both remembered; the next call still tries both (a saved limit can be stale),
        # the one that frees up first (A: 3:45pm) first
        n = len(self.calls())
        self.run_wrapper(["-p", "hi"])
        self.assertEqual([c["acct"] for c in self.calls()[n:]], ["A", "default"])

    def test_model_scoped_limit(self):
        self.plan(A=["opus_limit", "ok", "ok"], default="ok")
        self.run_wrapper(["-p", "hi", "--model", "opus"])
        self.run_wrapper(["-p", "hi", "--model", "sonnet"])   # A still fine for sonnet
        self.run_wrapper(["-p", "hi", "--model", "claude-opus-5-5"])  # A skipped for opus
        self.assertEqual([c["acct"] for c in self.calls()], ["A", "default", "A", "default"])

    def test_switch_model_message_is_model_scoped(self):
        cfg = cf.load_config()
        v = cf.classify(1, "You're out of usage credits. Switch to another model, or manage usage "
                           "credits at claude.ai/settings/usage, to continue.", "")
        self.assertEqual(v["kind"], "limit")
        cf.record_verdict(cfg, "A", v, "fable")
        st = cf.read_state()
        self.assertEqual(cf._block(st, "A", "fable")[0], "limited")
        self.assertEqual(cf._block(st, "A", "sonnet")[0], "ok")

    def test_json_output_limit(self):
        self.plan(A="limit", default="ok")
        r = self.run_wrapper(["-p", "hi", "--output-format", "json"])
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout)["result"], "OK from default")

    def test_pin(self):
        self.plan(A="limit", default="ok")
        r = self.run_wrapper(["-p", "hi"], extra_env={"CLAUDE_FAILOVER_ACCOUNT": "A"})
        self.assertEqual(r.returncode, 1)
        self.assertEqual([c["acct"] for c in self.calls()], ["A"])
        self.assertNotIn("CLAUDE_FAILOVER_ACCOUNT", self.calls()[0]["env"])

    def test_nested_env_from_desktop_session(self):
        self.plan(A="ok")
        self.run_wrapper(["-p", "hi"], extra_env={"CLAUDECODE": "1", "CLAUDE_CODE_SIMPLE": "1",
                                                  "CLAUDE_CODE_ENTRYPOINT": "claude-desktop"})
        env = self.calls()[0]["env"]
        self.assertNotIn("CLAUDE_CODE_SIMPLE", env)
        self.assertIn("CLAUDE_FAILOVER_MANAGED", env)

    def test_sigterm_reaches_claude(self):
        self.plan(A="sleep")
        env = dict(self.env)
        p = subprocess.Popen([str(WRAPPER), "-p", "hi"], env=env, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, cwd=str(self.tmp))
        child = self._wait_for_child()
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=20)
        self.assertEqual(p.returncode, 128 + signal.SIGTERM)
        self.assertFalse(_alive(child))

    def test_sigkill_of_wrapper_kills_orphaned_claude(self):
        self.plan(A="sleep")
        p = subprocess.Popen([str(WRAPPER), "-p", "hi"], env=dict(self.env), stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, cwd=str(self.tmp))
        child = self._wait_for_child()
        p.kill()
        p.wait(timeout=10)
        deadline = time.time() + 20
        while _alive(child) and time.time() < deadline:
            time.sleep(0.2)
        self.assertFalse(_alive(child), "orphaned fake claude still running")

    def _wait_for_child(self):
        deadline = time.time() + 20
        while time.time() < deadline:
            calls = self.calls()
            if calls:
                return calls[-1]["pid"]
            time.sleep(0.1)
        self.fail("fake claude never started")


def _alive(pid):
    r = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    s = r.stdout.strip()
    return bool(s) and not s.startswith("Z")


class HookPayloads:
    """StopFailure payloads, shared by HookTests and DesktopSyncCallSites."""
    LIMIT = "You've hit your session limit · resets 3:45pm"

    def payload(self, **kw):
        sid = kw.pop("session_id", "11111111-2222-3333-4444-555555555555")
        tpath = self.default / "projects" / "-tmp-proj" / (sid + ".jsonl")
        tpath.parent.mkdir(parents=True, exist_ok=True)
        tpath.write_text("{}\n")
        p = {"hook_event_name": "StopFailure", "session_id": sid, "transcript_path": str(tpath),
             "cwd": str(self.tmp), "permission_mode": "bypassPermissions", "error": "rate_limit",
             "error_details": "429", "last_assistant_message": self.LIMIT}
        p.update(kw)
        return json.dumps(p)

    def desktop_env(self, uuid="uuid-A"):
        return {"CLAUDE_CODE_ENTRYPOINT": "claude-desktop", "CLAUDE_CODE_ACCOUNT_UUID": uuid}


class HookTests(HookPayloads, Sandbox):
    def test_desktop_limit_hands_off_to_next_account(self):
        rc = cf.hook_main(self.payload(), env=self.desktop_env(), open_terminal=False, print_run=False)
        self.assertEqual(rc, 0)
        scripts = list(self.handoffs.glob("*.command"))
        self.assertEqual(len(scripts), 1)
        body = scripts[0].read_text()
        self.assertIn("CLAUDE_FAILOVER_ACCOUNT=B", body)
        self.assertIn("--resume 11111111-2222-3333-4444-555555555555 --fork-session", body)
        self.assertIn("--dangerously-skip-permissions", body)
        self.assertIn("cd %s" % self.tmp, body)
        self.assertIn("exec %s --resume" % shlex.quote(str(WRAPPER)), body)   # this repo's wrapper
        self.assertTrue(os.access(scripts[0], os.X_OK))
        self.assertIn("limited_until", self.state_json()["accounts"]["A"])
        # bash can parse it
        self.assertEqual(subprocess.run(["/bin/bash", "-n", str(scripts[0])]).returncode, 0)
        # same session again -> no second window
        cf.hook_main(self.payload(), env=self.desktop_env(), open_terminal=False, print_run=False)
        self.assertEqual(len(list(self.handoffs.glob("*.command"))), 1)

    def test_a_handoff_block_naming_one_kind_keeps_the_other_on(self):
        cfg = json.loads(self.cfg_path.read_text())
        cfg["handoff"] = {"from_terminal": False}
        self.cfg_path.write_text(json.dumps(cfg))
        cf.hook_main(self.payload(), env=self.desktop_env(), open_terminal=False, print_run=False)
        self.assertEqual(len(list(self.handoffs.glob("*.command"))), 1)   # desktop: still on
        cf.hook_main(self.payload(session_id="22222222-2222-3333-4444-555555555555"),
                     env={"CLAUDE_CODE_ENTRYPOINT": "cli"}, open_terminal=False, print_run=False)
        self.assertEqual(len(list(self.handoffs.glob("*.command"))), 1)   # terminal: off

    def test_handoff_switches_model_when_fallback_is_out_of_it(self):
        cfg = cf.load_config()
        cf.record_verdict(cfg, "B", {"kind": "limit", "text": "x", "scope": "model", "family": "fable",
                                     "resets_at": cf.now_local() + dt.timedelta(days=2)})
        sid = "99999999-2222-3333-4444-555555555555"
        tpath = self.default / "projects" / "-tmp-proj" / (sid + ".jsonl")
        tpath.parent.mkdir(parents=True, exist_ok=True)
        tpath.write_text("\n".join([
            json.dumps({"type": "user", "permissionMode": "acceptEdits", "message": {"content": "hi"}}),
            json.dumps({"type": "assistant", "message": {"model": "claude-fable-5-1", "content": []}}),
        ]) + "\n")
        payload = json.dumps({"hook_event_name": "StopFailure", "session_id": sid,
                              "transcript_path": str(tpath), "cwd": str(self.tmp), "error": "rate_limit",
                              "last_assistant_message": self.LIMIT})
        cf.hook_main(payload, env=self.desktop_env(), open_terminal=False, print_run=False)
        body = list(self.handoffs.glob("*.command"))[0].read_text()
        self.assertIn("--model opus", body)
        self.assertIn("--permission-mode acceptEdits", body)
        self.assertIn("CLAUDE_FAILOVER_ACCOUNT=B", body)

    def test_one_handoff_per_limit_window(self):
        cf.hook_main(self.payload(), env=self.desktop_env(), open_terminal=False, print_run=False)
        st = cf.read_state()
        for v in st["handoffs"].values():   # pretend the old 20-minute rule would have expired
            v["at"] = "2026-01-01T00:00:00-05:00"
        cf.atomic_write_json(self.state / "accounts.json", st)
        cf.hook_main(self.payload(), env=self.desktop_env(), open_terminal=False, print_run=False)
        self.assertEqual(len(list(self.handoffs.glob("*.command"))), 1)

    def test_model_only_limit_does_not_hand_off(self):
        msg = "You're out of usage credits. Switch to another model, or manage usage credits, to continue."
        cf.hook_main(self.payload(last_assistant_message=msg), env=self.desktop_env(),
                     open_terminal=False, print_run=False)
        self.assertFalse(self.handoffs.exists() and list(self.handoffs.glob("*")))

    def test_nested_print_run_is_headless(self):
        cf.hook_main(self.payload(), env=self.desktop_env(), open_terminal=False, print_run=True)
        self.assertFalse(self.handoffs.exists() and list(self.handoffs.glob("*")))

    def test_ledger_expires_at_the_source_reset(self):
        cf.hook_main(self.payload(), env=self.desktop_env(), open_terminal=False, print_run=False)
        entry = self.state_json()["handoffs"]["11111111-2222-3333-4444-555555555555"]
        until = cf.parse_iso(entry["until"])
        self.assertEqual((until.hour, until.minute), (15, 45))

    def test_print_run_detected_from_claude_pid_before_fork(self):
        fake = self.tmp / "claude"
        fake.write_text("#!/usr/bin/python3\nimport time\ntime.sleep(30)\n")
        fake.chmod(0o755)
        p1 = subprocess.Popen([str(fake), "-p", "do it"])
        p2 = subprocess.Popen([str(fake), "--input-format", "stream-json", "-p", "x"])
        try:
            time.sleep(0.5)
            self.assertTrue(cf.parent_is_print_run({"CLAUDE_PID": str(p1.pid)}))
            self.assertFalse(cf.parent_is_print_run({"CLAUDE_PID": str(p2.pid)}))
        finally:
            p1.kill(); p2.kill(); p1.wait(); p2.wait()

    def test_ledger_without_stated_reset_lasts_six_hours(self):
        msg = "Your org is out of usage \u00b7 contact your admin"
        cf.hook_main(self.payload(last_assistant_message=msg), env=self.desktop_env(),
                     open_terminal=False, print_run=False)
        entry = self.state_json()["handoffs"]["11111111-2222-3333-4444-555555555555"]
        self.assertGreater(cf.parse_iso(entry["until"]), cf.now_local() + dt.timedelta(hours=5, minutes=50))

    def test_prompt_containing_dash_p_is_not_a_print_run(self):
        fake = self.tmp / "claude"
        fake.write_text("#!/usr/bin/python3\nimport time\ntime.sleep(30)\n")
        fake.chmod(0o755)
        p1 = subprocess.Popen([str(fake), "stage the tests with git add -p and commit"])
        p2 = subprocess.Popen([str(fake), "-p", "explain --input-format stream-json"])
        try:
            time.sleep(0.5)
            self.assertFalse(cf.parent_is_print_run({"CLAUDE_PID": str(p1.pid)}))
            self.assertTrue(cf.parent_is_print_run({"CLAUDE_PID": str(p2.pid)}))
        finally:
            p1.kill(); p2.kill(); p1.wait(); p2.wait()

    def test_headless_runs_only_record(self):
        env = {"CLAUDE_FAILOVER_MANAGED": "1", "CLAUDE_FAILOVER_ACTIVE_ACCOUNT": "B",
               "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}
        cf.hook_main(self.payload(), env=env, open_terminal=False, print_run=False)
        self.assertFalse(self.handoffs.exists() and list(self.handoffs.glob("*")))
        self.assertIn("limited_until", self.state_json()["accounts"]["B"])

    def test_non_limit_errors_ignored(self):
        cf.hook_main(self.payload(last_assistant_message="API Error: Server is temporarily limiting "
                                  "requests (not your usage limit)", error="rate_limit"),
                     env=self.desktop_env(), open_terminal=False, print_run=False)
        self.assertFalse(self.handoffs.exists() and list(self.handoffs.glob("*")))
        self.assertFalse((self.state / "accounts.json").exists()
                         and self.state_json()["accounts"].get("A"))

    def test_no_fallback_left(self):
        cfg = cf.load_config()
        cf.record_verdict(cfg, "B", {"kind": "limit", "text": "x", "scope": "account",
                                     "resets_at": cf.now_local() + dt.timedelta(hours=3)})
        cf.hook_main(self.payload(), env=self.desktop_env(), open_terminal=False, print_run=False)
        self.assertFalse(self.handoffs.exists() and list(self.handoffs.glob("*")))

    def test_subagent_failures_ignored(self):
        cf.hook_main(self.payload(agent_id="sub1"), env=self.desktop_env(), open_terminal=False, print_run=False)
        self.assertFalse(self.handoffs.exists() and list(self.handoffs.glob("*")))

    def test_hook_script_never_crashes(self):
        env = dict(self.env)
        r = subprocess.run([str(HOOK)], input="not json", capture_output=True, text=True, env=env,
                           timeout=30)
        self.assertEqual(r.returncode, 0)

    def test_identify_account(self):
        cfg = cf.load_config()
        self.assertEqual(cf.identify_account(cfg, {"CLAUDE_CODE_USER_EMAIL": "B@example.com"}), "B")
        self.assertEqual(cf.identify_account(cfg, {"CLAUDE_CONFIG_DIR": str(self.prof)}), "A")
        self.assertEqual(cf.identify_account(cfg, {}), "B")
        self.assertEqual(cf.identify_account(cfg, {"CLAUDE_FAILOVER_ACTIVE_ACCOUNT": "A"}), "A")


class LoginBrowser(Sandbox):
    """`login_browser: incognito` keeps a login off the account Chrome is already
    signed in to (otherwise one account's login can end up in another's slot)."""

    def login(self, name, expires=None, **extra):
        cfg = cf.load_config()
        cfg["by_name"][name].update(extra)
        auth = {"loggedIn": True, "email": cfg["by_name"][name]["email"]}
        if expires:
            auth["loginExpiresAt"] = expires
        envs = []
        out = io.StringIO()
        with mock.patch.object(cf, "load_config", return_value=cfg), \
                mock.patch.object(cf.subprocess, "call",
                                  lambda cmd, env=None: envs.append(env) or 0), \
                mock.patch.object(cf, "auth_status", return_value=auth), \
                mock.patch.object(cf, "clear_account"), \
                mock.patch.object(cf, "ensure_profile_links"), \
                contextlib.redirect_stdout(out):
            rc = cf.accounts_main(["login", name])
        return rc, envs[0], out.getvalue()

    def test_incognito_account_gets_the_private_window_browser(self):
        rc, env, out = self.login("B", login_browser="incognito")
        self.assertEqual(rc, 0)
        self.assertEqual(env["BROWSER"], str(PROJECT / "bin" / "open-incognito"))
        self.assertTrue(os.access(env["BROWSER"], os.X_OK))
        self.assertIn("private Chrome window", out)
        # a good login starts the ~30-day clock that status warns from
        logged_in = cf.parse_iso(self.state_json()["accounts"]["B"]["logged_in_at"])
        self.assertEqual(logged_in.date(), cf.now_local().date())
        self.assertIn("claude-accounts status asks you to renew from", out)

    def test_other_accounts_keep_the_default_browser(self):
        rc, env, out = self.login("A")
        self.assertEqual(rc, 0)
        self.assertEqual(env.get("BROWSER"), os.environ.get("BROWSER"))
        self.assertIn("make sure it is signed in", out)

    def test_the_signing_line_comes_before_what_claude_prints(self):
        # Through a pipe Python holds its output back; claude writes straight to it.
        self.plan(A="ok")
        r = subprocess.run([str(PROJECT / "bin" / "claude-accounts"), "login", "A"], capture_output=True,
                           text=True, env=dict(self.env), timeout=60, stdin=subprocess.DEVNULL,
                           cwd=str(self.tmp))
        self.assertIn("Signing A in", r.stdout)
        self.assertLess(r.stdout.index("Signing A in"), r.stdout.index("OK from A"), r.stdout)

    def test_login_keeps_the_end_date_the_cli_reports(self):
        ends = cf.now_local().replace(microsecond=0) + dt.timedelta(days=29)
        rc, env, out = self.login("A", expires=ends.astimezone(dt.timezone.utc).isoformat()
                                  .replace("+00:00", "Z"))
        self.assertEqual(rc, 0)
        day = lambda d: d.strftime("%b %-d")
        self.assertIn("ends by %s (could end from %s): claude-accounts status asks you to renew from %s" % (
            day(ends), day(ends - dt.timedelta(days=3)), day(ends - dt.timedelta(days=8))), out)
        self.assertEqual(cf.parse_iso(self.state_json()["accounts"]["A"]["login_expires_at"]), ends)


class LoginClock(Sandbox):
    """A CLI login lasts ~30 days. Its date is kept so claude-accounts status can
    ask for a re-login from day 25, before headless runs start to fail."""

    def test_clear_keeps_the_login_and_spare_dates(self):
        cf.record_login("A")
        with cf.edit_state() as st:
            st["accounts"]["A"].update(spare_token_saved=cf.iso(cf.now_local()),
                                       login_expires_at="2026-12-01T13:59:00+00:00",
                                       limited_until="2099-01-01T00:00:00-05:00")
        kept = ["logged_in_at", "login_expires_at", "spare_token_saved"]
        cf.clear_account("A")
        self.assertEqual(sorted(self.state_json()["accounts"]["A"]), kept)
        cf.clear_account()
        self.assertEqual(sorted(self.state_json()["accounts"]["A"]), kept)

    def test_logged_in_command_dates_an_outside_login(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cf.accounts_main(["logged-in", "A", "2026-11-02"]), 0)
            self.assertEqual(cf.accounts_main(["logged-in", "A", "Nov 2"]), 2)
        self.assertTrue(self.state_json()["accounts"]["A"]["logged_in_at"].startswith("2026-11-02T00:00:00"))
        self.assertIn("renew from Nov 27 (expires around Dec 2)", out.getvalue())

    def test_status_says_renew_from_day_25(self):
        cfg = cf.load_config()
        auth = {"loggedIn": True, "email": "a@example.com", "subscriptionType": "max"}

        def status_text():
            out = io.StringIO()
            with mock.patch.object(cf, "auth_status", return_value=auth), contextlib.redirect_stdout(out):
                cf.accounts_status(cfg)
            return out.getvalue()
        cf.record_login("A", cf.now_local() - dt.timedelta(days=24))
        self.assertIn("renew from", status_text())
        self.assertNotIn("LOGIN EXPIRES", status_text())
        cf.record_login("A", cf.now_local() - dt.timedelta(days=25))
        self.assertIn("LOGIN EXPIRES AROUND", status_text())
        self.assertIn("renew now: claude-accounts login A", status_text())

    # Claude Code 2.1.318+ says when the stored login ends (claude auth status
    # loginExpiresAt, -p JSON credential_expires_at). The wrapper keeps the latest
    # date it sees and asks for a re-login 5 days before the earliest possible end
    # (the CLI's date can be up to 3 days late). Without it, the guess stays.

    def test_cli_timestamps_parse(self):
        self.assertEqual(cf.parse_iso("2026-12-01T13:59:00.473Z"),
                         dt.datetime(2026, 12, 1, 13, 59, 0, 473000, tzinfo=dt.timezone.utc))
        self.assertEqual(cf.parse_iso("2026-11-05T21:25:03.123456Z").microsecond, 123456)
        self.assertEqual(cf.parse_iso("2026-11-02T00:00:00-05:00").utcoffset(), dt.timedelta(hours=-5))
        self.assertIsNone(cf.parse_iso("Dec 1"))

    def test_login_window_prefers_a_cli_date_that_belongs_to_this_login(self):
        rec = {"logged_in_at": "2026-11-02T00:00:00-05:00", "login_expires_at": "2026-12-01T13:59:00+00:00"}
        win = cf.login_window(rec)
        self.assertEqual((win["source"], win["ends"], win["earliest"], win["renew"]),
                         ("cli", dt.date(2026, 12, 1), dt.date(2026, 11, 28), dt.date(2026, 11, 23)))
        rec["login_expires_at"] = "2026-10-30T00:00:00+00:00"   # from the previous login
        win = cf.login_window(rec)
        self.assertEqual((win["source"], win["ends"], win["earliest"], win["renew"]),
                         ("guess", dt.date(2026, 12, 2), None, dt.date(2026, 11, 27)))
        self.assertEqual(cf.login_window({"login_expires_at": "2026-12-01T13:59:00+00:00"})["source"], "cli")
        self.assertIsNone(cf.login_window({}))

    def test_status_uses_the_cli_end_date_when_it_has_one(self):
        cfg = cf.load_config()
        cf.record_login("A", cf.now_local() - dt.timedelta(days=2))
        day = lambda d: d.strftime("%b %-d")

        def status(days_to_end, as_json=False):
            ends = cf.now_local().replace(microsecond=0) + dt.timedelta(days=days_to_end)
            auth = {"loggedIn": True, "email": "a@example.com", "subscriptionType": "max",
                    "loginExpiresAt": ends.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
            out = io.StringIO()
            with mock.patch.object(cf, "auth_status", return_value=auth), contextlib.redirect_stdout(out):
                cf.accounts_status(cfg, as_json=as_json)
            return out.getvalue(), ends
        text, ends = status(12)   # can end from day 9; the ask starts on day 4
        self.assertIn("ends by %s (could end from %s; CLI date); renew from %s" % (
            day(ends), day(ends - dt.timedelta(days=3)), day(ends - dt.timedelta(days=8))), text)
        self.assertNotIn("LOGIN ENDS", text)
        self.assertNotIn("expires around", text)
        self.assertEqual(cf.parse_iso(self.state_json()["accounts"]["A"]["login_expires_at"]), ends)
        text, ends = status(8)    # today is the day the ask starts
        self.assertIn("LOGIN ENDS BY %s (could end from %s) - renew now: claude-accounts login A" % (
            day(ends), day(ends - dt.timedelta(days=3))), text)
        text, ends = status(8, as_json=True)
        row = [r for r in json.loads(text)["accounts"] if r["account"] == "A"][0]
        self.assertEqual((row["login_date_source"], row["login_ends"], row["login_ends_earliest"],
                          row["login_renew_from"]),
                         ("cli", ends.date().isoformat(), (ends - dt.timedelta(days=3)).date().isoformat(),
                          (ends - dt.timedelta(days=8)).date().isoformat()))

    def test_a_new_login_drops_the_old_end_date(self):
        cf.record_login_expiry("A", "2026-12-01T13:59:00.473Z")
        self.assertEqual(self.state_json()["accounts"]["A"]["login_expires_at"], "2026-12-01T13:59:00+00:00")
        cf.record_login("A")
        self.assertNotIn("login_expires_at", self.state_json()["accounts"]["A"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cf.accounts_main(["logged-in", "A", "2026-11-02"]), 0)
        self.assertIn("replaces the guess with the CLI's own end date", out.getvalue())

    def test_headless_json_runs_record_the_end_date_text_runs_do_not(self):
        self.plan(A="ok")
        env = {"FAKE_CLAUDE_EXPIRES": "2026-12-01T13:59:00.473Z"}
        r = self.run_wrapper(["-p", "hi"], extra_env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("login_expires_at", self.state_json()["accounts"]["A"])
        r = self.run_wrapper(["-p", "hi", "--output-format", "json"], extra_env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.state_json()["accounts"]["A"]["login_expires_at"], "2026-12-01T13:59:00+00:00")
        self.assertEqual(json.loads(r.stdout)["result"], "OK from A")   # the output is untouched


class SpareToken(Sandbox):
    """A one-year spare token (claude setup-token) covers headless runs whose login
    is refused. It never replaces a working login and never leaves the keychain
    except into claude's environment."""

    def save_spare(self, name="A", in_keychain=True):
        # Made up at runtime and nothing like a real token's format.
        self.token = "-".join(["fake", "spare", uuid.uuid4().hex[:12]])
        if in_keychain:
            (self.tmp / "keychain.json").write_text(
                json.dumps({"%s/%s" % (cf.SPARE_TOKEN_SERVICE, name): self.token}))
        with cf.edit_state() as st:
            st["accounts"].setdefault(name, {})["spare_token_saved"] = cf.iso(cf.now_local())

    def keychain_calls(self):
        p = self.tmp / "security.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def assert_token_nowhere(self, *texts):
        for t in texts:
            self.assertNotIn(self.token, t)
        for p in list(self.state.iterdir()) + [self.log_path]:
            if p.is_file():
                self.assertNotIn(self.token, p.read_text(errors="replace"), p.name)

    def test_refused_login_retries_on_the_spare_token(self):
        self.save_spare("A")
        self.plan(A="auth_unless_token", default="ok")
        r = self.run_wrapper(["-p", "hello"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "OK from A via token")
        calls = self.calls()
        self.assertEqual([c["acct"] for c in calls], ["A", "A"])
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", calls[0]["env"])
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", calls[1]["env"])
        self.assertIn("hello", calls[1]["argv"])
        a = self.state_json()["accounts"]["A"]
        self.assertIn("auth_down_until", a)   # the login is still down (status still asks)...
        self.assertIn("spare_token_ok", a)    # ...but the run went through on the spare
        self.assert_token_nowhere(r.stdout, r.stderr)

    def test_a_token_run_never_dates_the_login(self):
        # The credential_expires_at of a token run is the token's year, not the login's.
        self.save_spare("A")
        self.plan(A="auth_unless_token", default="ok")
        r = self.run_wrapper(["-p", "hi", "--output-format", "json"],
                             extra_env={"FAKE_CLAUDE_EXPIRES": "2026-12-01T13:59:00Z",
                                        "FAKE_CLAUDE_TOKEN_EXPIRES": "2027-11-05T00:00:00Z"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["result"], "OK from A via token")
        a = self.state_json()["accounts"]["A"]
        self.assertEqual(a["login_expires_at"], "2026-12-01T13:59:00+00:00")   # from the login attempt
        self.assertIn("spare_token_ok", a)
        self.assert_token_nowhere(r.stdout, r.stderr)

    def test_login_known_down_goes_straight_to_the_spare(self):
        self.save_spare("A")
        cf.record_verdict(cf.load_config(), "A", {"kind": "auth", "text": "Not logged in"})
        self.plan(A="auth_unless_token")
        r = self.run_wrapper(["-p", "hi"], extra_env={"CLAUDE_FAILOVER_ACCOUNT": "A"})
        self.assertEqual(r.stdout.strip(), "OK from A via token")
        self.assertEqual(len(self.calls()), 1)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", self.calls()[0]["env"])

    def test_usage_limit_does_not_try_the_spare(self):
        self.save_spare("A")
        self.plan(A="limit", default="ok")
        r = self.run_wrapper(["-p", "hi"])
        self.assertEqual(r.stdout.strip(), "OK from default")
        self.assertEqual([c["acct"] for c in self.calls()], ["A", "default"])
        self.assertEqual(self.keychain_calls(), [])   # a limit is the account's: token not even read

    def test_no_spare_saved_never_touches_the_keychain(self):
        self.plan(A="auth", default="ok")
        r = self.run_wrapper(["-p", "hi"])
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.keychain_calls(), [])

    def test_refused_spare_is_recorded_apart_and_redacted(self):
        self.save_spare("A")
        self.plan(A="token_echo_auth")
        r = self.run_wrapper(["-p", "hi"], extra_env={"CLAUDE_FAILOVER_ACCOUNT": "A"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("[spare token] was rejected", r.stdout)
        a = self.state_json()["accounts"]["A"]
        self.assertIn("auth_down_until", a)
        self.assertIn("[spare token]", a["spare_token_text"])
        self.assertTrue(cf.spare_token_failing(a))
        self.assert_token_nowhere(r.stdout, r.stderr)

    def test_refusal_in_the_same_second_as_the_save_counts(self):
        t = "2026-11-03T10:00:00-05:00"
        self.assertTrue(cf.spare_token_failing({"spare_token_saved": t, "spare_token_failed": t}))
        self.assertFalse(cf.spare_token_failing({"spare_token_saved": t, "spare_token_failed": t,
                                                 "spare_token_ok": "2026-11-03T10:00:05-05:00"}))
        self.assertFalse(cf.spare_token_failing({"spare_token_saved": t}))

    def test_unreadable_spare_falls_back_to_the_login(self):
        self.save_spare("A", in_keychain=False)
        cf.record_verdict(cf.load_config(), "A", {"kind": "auth", "text": "Not logged in"})
        self.plan(A="auth")
        r = self.run_wrapper(["-p", "hi"], extra_env={"CLAUDE_FAILOVER_ACCOUNT": "A"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("Please run /login", r.stdout)   # the caller still gets a real error
        self.assertEqual(len(self.calls()), 1)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", self.calls()[0]["env"])

    def test_auth_status_never_sees_the_spare(self):
        # The WRONG ACCOUNT check reads the email from `claude auth status`. With a
        # token in its environment, auth status reports no email at all.
        self.save_spare("A")
        self.plan(A="ok")
        cf.auth_status(cf.load_config(), "A")
        self.assertEqual(self.calls()[-1]["argv"][:2], ["auth", "status"])
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", self.calls()[-1]["env"])
        self.assertEqual(self.keychain_calls(), [])

    def test_token_command_saves_tests_and_never_prints_it(self):
        token = "-".join(["fake", "spare", uuid.uuid4().hex[:12]])
        cmds = []

        def fake_call(cmd, env=None):
            cmds.append((cmd, env))
            if "add-generic-password" in cmd:   # what pasting at security's hidden prompt does
                (self.tmp / "keychain.json").write_text(
                    json.dumps({"%s/%s" % (cf.SPARE_TOKEN_SERVICE, "B"): token}))
            return 0
        self.plan(default="auth_unless_token")
        cfg = cf.load_config()
        cfg["by_name"]["B"]["login_browser"] = "incognito"
        out = io.StringIO()
        with mock.patch.object(cf, "load_config", return_value=cfg), \
                mock.patch.object(cf.subprocess, "call", fake_call), \
                contextlib.redirect_stdout(out):
            rc = cf.accounts_main(["token", "B"])
        self.assertEqual(rc, 0, out.getvalue())
        (setup, setup_env), (add, _) = cmds
        self.assertEqual(setup[1:], ["setup-token"])
        self.assertEqual(setup_env["BROWSER"], str(PROJECT / "bin" / "open-incognito"))
        self.assertEqual(add[-1], "-w")   # security prompts for it: never in any argv
        self.assertNotIn(token, " ".join(add))
        b = self.state_json()["accounts"]["B"]
        self.assertIn("spare_token_saved", b)
        self.assertIn("spare_token_ok", b)
        self.assertIn("The spare token works", out.getvalue())
        self.assertNotIn(token, out.getvalue())
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", self.calls()[-1]["env"])   # the check used it

    def test_test_command_checks_login_and_spare(self):
        self.save_spare("A")
        self.plan(A="ok")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cf.accounts_main(["test", "A"])
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("A (spare token) OK", out.getvalue())
        self.assertEqual(["CLAUDE_CODE_OAUTH_TOKEN" in c["env"] for c in self.calls()], [False, True])
        self.assert_token_nowhere(out.getvalue())



# --------------------------------------------------------------------------
# Setting the tool up: config discovery, the real claude, the shim, init / add / hook
# --------------------------------------------------------------------------

def _executable(path, text="#!/bin/sh\nexit 0\n"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


class CleanEnv(unittest.TestCase):
    """A temp HOME, no claude-failover variables, and a temp stand-in for this
    repo's folder (so a config.json in a developer's own clone can't leak in).
    Only temp places are searched for claude: the test Mac may have a real one."""
    KEYS = ("HOME", "PATH", "CLAUDE_FAILOVER_CONFIG", "CLAUDE_FAILOVER_STATE_DIR",
            "CLAUDE_FAILOVER_HANDOFF_DIR", "CLAUDE_FAILOVER_REAL_CLAUDE", "CLAUDE_FAILOVER_NO_NOTIFY")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cf-test-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.repo = self.tmp / "repo"
        (self.repo / "bin").mkdir(parents=True)
        self._saved = {k: os.environ.get(k) for k in self.KEYS}
        for k in self.KEYS:
            os.environ.pop(k, None)
        os.environ.update({"HOME": str(self.home), "PATH": "/usr/bin:/bin",
                           "CLAUDE_FAILOVER_NO_NOTIFY": "1"})
        for name, value in (("PROJECT", self.repo),
                            ("REAL_CLAUDE_PLACES", ("~/.local/bin/claude", "~/.claude/local/claude"))):
            patcher = mock.patch.object(cf, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.user_cfg = self.home / ".config" / "claude-failover" / "config.json"

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def write_config(self, path, cfg):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg))
        return path

    def run_cmd(self, argv):
        """claude-accounts <argv> in this process -> (exit code, what it printed)."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cf.accounts_main(argv)
        return rc, out.getvalue()


class ConfigTests(CleanEnv):
    def test_discovery_order(self):
        self.assertIsNone(cf.config_path())
        with self.assertRaises(SystemExit) as e:
            cf.load_config()
        self.assertEqual(str(e.exception), "claude-failover: no config found - run: claude-accounts init")
        self.write_config(self.user_cfg, {"accounts": []})
        self.assertEqual(cf.config_path(), self.user_cfg)     # the usual place
        legacy = self.write_config(self.repo / "config.json", {"accounts": []})
        self.assertEqual(cf.config_path(), legacy)            # a config in the repo wins over ~/.config
        custom = self.tmp / "elsewhere" / "cfg.json"
        os.environ["CLAUDE_FAILOVER_CONFIG"] = str(custom)
        self.assertIsNone(cf.config_path())                   # named but missing: no fallback
        self.write_config(custom, {"accounts": []})
        self.assertEqual(cf.config_path(), custom)            # the variable wins over both
        self.assertEqual(cf.load_config()["_config_path"], str(custom))

    def test_tilde_paths_are_expanded(self):
        self.write_config(self.user_cfg, {
            "real_claude": "~/tools/claude", "default_config_dir": "~/.claude",
            "projects_dir": "~/.claude/projects", "state_dir": "~/fo-state", "handoff_dir": "here",
            "accounts": [{"name": "work", "config_dir": "~/.claude-work"},
                         {"name": "plain", "config_dir": None}]})
        cfg = cf.load_config()
        home = str(self.home)
        self.assertEqual(cfg["real_claude"], home + "/tools/claude")
        self.assertEqual(cfg["default_config_dir"], home + "/.claude")
        self.assertEqual(cfg["projects_dir"], home + "/.claude/projects")
        self.assertEqual(cfg["by_name"]["work"]["config_dir"], home + "/.claude-work")
        self.assertIsNone(cfg["by_name"]["plain"]["config_dir"])
        self.assertEqual(cfg["order"], {"interactive": [], "headless": []})
        self.assertEqual(cf.state_dir(), self.home / "fo-state")
        self.assertEqual(cf.handoff_dir(), self.user_cfg.parent / "here")   # relative: next to the config
        os.environ["CLAUDE_FAILOVER_STATE_DIR"] = str(self.tmp / "env-state")
        os.environ["CLAUDE_FAILOVER_HANDOFF_DIR"] = str(self.tmp / "env-handoffs")
        self.assertEqual(cf.state_dir(), self.tmp / "env-state")            # the variables always win
        self.assertEqual(cf.handoff_dir(), self.tmp / "env-handoffs")

    def test_state_and_handoffs_default_to_the_config_folder(self):
        self.write_config(self.user_cfg, {"accounts": []})
        self.assertEqual(cf.load_config()["default_config_dir"], str(self.home / ".claude"))
        self.assertEqual(cf.state_dir(), self.user_cfg.parent / "state")
        self.assertEqual(cf.handoff_dir(), self.user_cfg.parent / "handoffs")
        self.write_config(self.repo / "config.json", {"accounts": []})
        self.assertEqual(cf.state_dir(), self.repo / "state")        # the original layout is unchanged
        self.assertEqual(cf.handoff_dir(), self.repo / "handoffs")

    def test_another_clones_init_does_not_hide_the_repo_config(self):
        # An install that keeps config.json in its repo folder, then `claude-accounts
        # init` (or ./install.sh) in another clone on the same Mac writes ~/.config.
        repo_cfg = self.write_config(self.repo / "config.json", {
            "accounts": [{"name": "A", "config_dir": None}]})
        self.write_config(self.user_cfg, {"accounts": []})
        self.assertEqual(cf.config_path(), repo_cfg)
        self.assertIn("A", cf.load_config()["by_name"])
        self.assertEqual(cf.state_dir(), self.repo / "state")       # limits and login dates stay put
        self.assertEqual(cf.handoff_dir(), self.repo / "handoffs")

    def test_the_example_config_leaves_real_claude_to_detection(self):
        example = json.loads((PROJECT / "config.example.json").read_text())
        self.assertNotIn("real_claude", example)

    def test_a_broken_config_is_one_clear_line(self):
        self.user_cfg.parent.mkdir(parents=True)
        for text, words in (("{not json", "cannot read"), ("[1, 2]", "JSON object"),
                            ('{"accounts": [{"email": "x@example.com"}]}', '"accounts"'),
                            ('{"accounts": [], "order": ["a"]}', '"order"')):
            self.user_cfg.write_text(text)
            with self.assertRaises(SystemExit) as e:
                cf.load_config()
            self.assertIn(words, str(e.exception))

    def test_real_claude_is_found_in_the_usual_places(self):
        own = _executable(self.repo / "bin" / "claude")
        links = self.tmp / "links"
        links.mkdir()
        (links / "claude").symlink_to(own)                    # our own shim, linked onto PATH
        clone = _executable(self.tmp / "clone" / "claude",    # another copy's shim
                            '#!/bin/sh\nexec "$(dirname "$0")/claude-failover" "$@"\n')
        on_path = _executable(self.tmp / "path" / "claude")
        os.environ["PATH"] = os.pathsep.join([str(links), str(clone.parent), str(on_path.parent)])
        self.assertEqual(cf.find_real_claude(), str(on_path))
        local = _executable(self.home / ".local" / "bin" / "claude")
        self.assertEqual(cf.find_real_claude(), str(local))   # the native installer's place first
        os.environ["CLAUDE_FAILOVER_REAL_CLAUDE"] = str(own)  # never our own shim, even when named
        self.assertEqual(cf.find_real_claude(), str(local))
        named = _executable(self.tmp / "named" / "claude")
        os.environ["CLAUDE_FAILOVER_REAL_CLAUDE"] = str(named)
        self.assertEqual(cf.find_real_claude(), str(named))
        self.write_config(self.user_cfg, {"accounts": []})    # no real_claude in the config
        self.assertEqual(cf.load_config()["real_claude"], str(named))

    def test_config_naming_our_own_shim_is_refused(self):
        _executable(self.repo / "bin" / "claude")
        link = self.tmp / "claude-link"
        link.symlink_to(self.repo / "bin" / "claude")
        for real in (str(self.repo / "bin" / "claude"), str(link)):
            self.write_config(self.user_cfg, {"real_claude": real, "accounts": []})
            with self.assertRaises(SystemExit) as e:
                cf.load_config()
            self.assertIn("run itself forever", str(e.exception))

    def test_a_copy_of_a_wrapper_shim_as_real_claude_is_refused(self):
        # Outside this repo's bin/, but it would start a wrapper all the same, which
        # would run it again: an endless loop (with -p, a growing tree of processes).
        # Only load_config runs here, so even a broken check can't start that loop.
        clone = _executable(self.tmp / "clone" / "claude",
                            '#!/bin/sh\nexec /elsewhere/claude-failover/bin/claude-failover "$@"\n')
        self.write_config(self.user_cfg, {"real_claude": str(clone), "accounts": []})
        with self.assertRaises(SystemExit) as e:
            cf.load_config()
        self.assertIn("run itself forever", str(e.exception))
        plain = _executable(self.tmp / "plain" / "claude")
        self.write_config(self.user_cfg, {"real_claude": str(plain), "accounts": []})
        self.assertEqual(cf.load_config()["real_claude"], str(plain))

    def test_a_missing_pinned_claude_names_the_one_found(self):
        local = _executable(self.home / ".local" / "bin" / "claude")
        gone = str(self.tmp / "gone" / "claude")
        self.write_config(self.user_cfg, {"real_claude": gone, "accounts": []})
        cfg = cf.load_config()
        self.assertEqual(cfg["real_claude"], gone)                  # a pin is never swapped silently
        line = cf.claude_missing(cfg)
        for words in ("not found at %s" % gone, str(self.user_cfg), str(local), "remove real_claude"):
            self.assertIn(words, line)
        self.assertEqual(cf.effective_real_claude(), gone)          # so install.sh can say it is gone
        self.user_cfg.unlink()
        self.assertEqual(cf.effective_real_claude(), str(local))    # no config: what detection finds

    def test_no_claude_anywhere_is_reported(self):
        self.write_config(self.user_cfg, {"accounts": []})
        cfg = cf.load_config()
        self.assertEqual(cfg["real_claude"], "")
        self.assertIn("install Claude Code", cf.claude_missing(cfg))


class EntryPointTests(unittest.TestCase):
    """The real bin/ scripts, run the way a user runs them, with a temp HOME."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cf-test-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "CLAUDE_FAILOVER_NO_NOTIFY": "1"}

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def run_bin(self, name, args, **env):
        return subprocess.run([str(PROJECT / "bin" / name)] + args, capture_output=True, text=True,
                              env=dict(self.env, **env), timeout=30, stdin=subprocess.DEVNULL,
                              cwd=str(self.tmp))

    def test_no_config_is_one_friendly_line(self):
        missing = str(self.tmp / "missing.json")
        for name, args in (("claude-failover", ["-p", "hi"]), ("claude-accounts", ["status"]),
                           ("claude-accounts", ["add", "work"]), ("claude", ["--version"])):
            r = self.run_bin(name, args, CLAUDE_FAILOVER_CONFIG=missing)
            self.assertEqual((r.returncode, r.stderr, r.stdout), (1, cf.NO_CONFIG + "\n", ""), name)

    @unittest.skipIf((PROJECT / "config.json").exists(), "this clone keeps a config.json in the repo")
    def test_no_config_with_an_empty_home(self):
        r = self.run_bin("claude-accounts", [])
        self.assertEqual((r.returncode, r.stderr), (1, cf.NO_CONFIG + "\n"))

    def test_module_imports_without_a_config(self):
        r = subprocess.run(["/usr/bin/python3", "-I", "-c", "import sys; sys.path.insert(0, %r); "
                            "import claude_failover" % str(PROJECT)], capture_output=True, text=True,
                           env=dict(self.env, CLAUDE_FAILOVER_CONFIG=str(self.tmp / "missing.json")),
                           timeout=30)
        self.assertEqual((r.returncode, r.stderr), (0, ""))

    def test_init_works_without_a_config(self):
        target = self.tmp / "c" / "config.json"
        r = self.run_bin("claude-accounts", ["init"], CLAUDE_FAILOVER_CONFIG=str(target),
                         CLAUDE_FAILOVER_REAL_CLAUDE=str(FAKE))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("real_claude", json.loads(target.read_text()))   # looked up on every run
        self.assertIn("Real claude binary: %s" % FAKE, r.stdout)

    def test_no_accounts_runs_claude_unchanged(self):
        # bin/ can be on PATH before the first account is added: every claude call,
        # scripts included, must still work as plain claude, with no note on a pipe.
        cfg = self.tmp / "config.json"
        cfg.write_text(json.dumps({"real_claude": str(FAKE), "accounts": []}))
        plan, log = self.tmp / "plan.json", self.tmp / "calls.jsonl"
        plan.write_text(json.dumps({"default": "ok"}))
        env = {"CLAUDE_FAILOVER_CONFIG": str(cfg), "FAKE_CLAUDE_PLAN": str(plan),
               "FAKE_CLAUDE_LOG": str(log), "FAKE_CLAUDE_HOME": str(self.tmp / "dot-claude")}
        for name in ("claude-failover", "claude"):
            r = self.run_bin(name, ["-p", "hi"], **env)
            self.assertEqual((r.returncode, r.stdout.strip(), r.stderr), (0, "OK from default", ""), name)
        calls = [json.loads(l) for l in log.read_text().splitlines()]
        self.assertEqual([c["argv"] for c in calls], [["-p", "hi"]] * 2)   # nothing added
        self.assertEqual([k for c in calls for k in c["env"] if k.startswith("CLAUDE_FAILOVER_")], [])

    def test_own_shim_as_real_claude_is_refused(self):
        cfg = self.tmp / "config.json"
        cfg.write_text(json.dumps({"real_claude": str(PROJECT / "bin" / "claude"),
                                   "accounts": [{"name": "A", "config_dir": None}]}))
        r = self.run_bin("claude-failover", ["-p", "hi"], CLAUDE_FAILOVER_CONFIG=str(cfg))
        self.assertEqual(r.returncode, 1)
        self.assertIn("run itself forever", r.stderr)
        self.assertNotIn("Traceback", r.stderr)


class TwoClonesTest(unittest.TestCase):
    """Two clones on one Mac: install.sh in a fresh clone writes a starter config to
    ~/.config. An install that keeps its config.json in its own repo folder (the
    original layout) must go on using that one."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cf-test-"))
        self.home = self.tmp / "home"
        self.home.mkdir()

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def clone(self, name):
        d = self.tmp / name
        shutil.copytree(str(PROJECT / "bin"), str(d / "bin"))
        for f in ("claude_failover.py", "desktop_sync.py", "install.sh"):
            shutil.copy2(str(PROJECT / f), str(d / f))
        return d

    def test_install_in_another_clone_leaves_this_one_alone(self):
        a, b = self.clone("A"), self.clone("B")
        (a / "config.json").write_text(json.dumps({
            "real_claude": str(FAKE), "security": str(FAKE_SECURITY),
            "accounts": [{"name": "solo", "config_dir": None}],
            "order": {"interactive": ["solo"], "headless": ["solo"]}}))
        plan = self.tmp / "plan.json"
        plan.write_text(json.dumps({"default": "ok"}))
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "CLAUDE_FAILOVER_NO_NOTIFY": "1",
               "CLAUDE_FAILOVER_REAL_CLAUDE": str(FAKE), "FAKE_CLAUDE_PLAN": str(plan),
               "FAKE_CLAUDE_LOG": str(self.tmp / "calls.jsonl"),
               "FAKE_CLAUDE_HOME": str(self.tmp / "dot-claude")}

        def run(*argv):
            return subprocess.run([str(x) for x in argv], capture_output=True, text=True, env=env,
                                  timeout=120, stdin=subprocess.DEVNULL, cwd=str(self.tmp))
        r = run(b / "install.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.home / ".config" / "claude-failover" / "config.json").is_file())
        r = run(a / "bin" / "claude-failover", "--version")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "OK from default"), r.stderr)
        r = run(a / "bin" / "claude-accounts", "status", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        status = json.loads(r.stdout)
        self.assertEqual([x["account"] for x in status["accounts"]], ["solo"])
        self.assertEqual(os.path.realpath(status["config"]), os.path.realpath(str(a / "config.json")))


class ShimTests(Sandbox):
    def test_claude_shim_runs_the_wrapper_through_symlinks(self):
        a, b = self.tmp / "links" / "a", self.tmp / "links" / "b"
        a.mkdir(parents=True)
        b.mkdir()
        (b / "claude").symlink_to(PROJECT / "bin" / "claude")       # absolute link
        (a / "claude").symlink_to(Path("..") / "b" / "claude")      # relative to its own folder
        self.plan(A="ok")
        for exe, cwd in ((a / "claude", self.tmp), ("./claude", a), (PROJECT / "bin" / "claude", self.tmp)):
            r = subprocess.run([str(exe), "--version"], capture_output=True, text=True, env=dict(self.env),
                               timeout=30, stdin=subprocess.DEVNULL, cwd=str(cwd))
            self.assertEqual((r.returncode, r.stdout.strip()), (0, "OK from A"), r.stderr)
        self.assertEqual([c["argv"] for c in self.calls()], [["--version"]] * 3)


class InitAddTests(CleanEnv):
    def setUp(self):
        super().setUp()
        os.environ["CLAUDE_FAILOVER_REAL_CLAUDE"] = str(FAKE)

    def config(self):
        return json.loads(self.user_cfg.read_text())

    def test_init_writes_a_private_starter_config(self):
        rc, out = self.run_cmd(["init"])
        self.assertEqual(rc, 0, out)
        self.assertIn("Real claude binary: %s" % FAKE, out)
        self.assertEqual(self.config(), {
            "default_config_dir": "~/.claude", "accounts": [],
            "order": {"interactive": [], "headless": []},
            "handoff": {"from_desktop": True, "from_terminal": True},
            "handoff_model_fallbacks": ["opus", "sonnet"],
            "desktop_sync": {"enabled": False, "interval_minutes": 10}})
        self.assertEqual(stat.S_IMODE(self.user_cfg.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.user_cfg.parent.stat().st_mode), 0o700)
        for step in ("claude-accounts add", "claude-accounts login", "claude-accounts hook install",
                     "claude-accounts sync install"):
            self.assertIn(step, out)
        self.assertEqual(cf.config_path(), self.user_cfg)      # found with no variable set

    def test_init_without_claude_leaves_real_claude_out(self):
        del os.environ["CLAUDE_FAILOVER_REAL_CLAUDE"]
        rc, out = self.run_cmd(["init"])
        self.assertEqual(rc, 0, out)
        self.assertNotIn("real_claude", self.config())
        self.assertIn("No claude binary found", out)

    def test_real_claude_is_looked_up_on_every_run(self):
        # init does not pin the binary it found: when Claude Code moves (Homebrew to
        # the native installer, say), the next run finds it in its new place.
        del os.environ["CLAUDE_FAILOVER_REAL_CLAUDE"]
        local = _executable(self.home / ".local" / "bin" / "claude")
        self.assertEqual(self.run_cmd(["init"])[0], 0)
        self.assertNotIn("real_claude", self.config())
        self.assertEqual(cf.load_config()["real_claude"], str(local))
        local.unlink()
        moved = _executable(self.home / ".claude" / "local" / "claude")
        self.assertEqual(cf.load_config()["real_claude"], str(moved))

    def test_init_refuses_an_existing_config_unless_forced(self):
        self.run_cmd(["init"])
        self.user_cfg.write_text('{"accounts": [], "mine": true}')
        rc, out = self.run_cmd(["init"])
        self.assertEqual(rc, 1)
        self.assertIn("already exists", out)
        self.assertEqual(self.config(), {"accounts": [], "mine": True})
        rc, out = self.run_cmd(["init", "--force"])
        self.assertEqual(rc, 0, out)
        backups = list(self.user_cfg.parent.glob("config.json.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertRegex(backups[0].name, r"^config\.json\.bak-\d{8}-\d{6}$")
        self.assertEqual(json.loads(backups[0].read_text()), {"accounts": [], "mine": True})
        self.assertNotIn("mine", self.config())
        self.assertEqual(stat.S_IMODE(self.user_cfg.stat().st_mode), 0o600)

    def test_init_respects_the_variable_and_a_config_in_the_repo(self):
        repo_cfg = self.repo / "config.json"
        repo_cfg.write_text('{"accounts": [], "mine": true}')
        self.assertEqual(self.run_cmd(["init"])[0], 1)          # that config is in use: don't hide it
        rc, out = self.run_cmd(["init", "--force"])               # start over: in place, after a backup
        self.assertEqual(rc, 0, out)
        self.assertNotIn("mine", json.loads(repo_cfg.read_text()))
        backups = list(self.repo.glob("config.json.bak-*"))
        self.assertEqual([json.loads(b.read_text()) for b in backups], [{"accounts": [], "mine": True}])
        self.assertFalse(self.user_cfg.exists())                  # no second config elsewhere
        self.assertEqual(cf.config_path(), repo_cfg)
        repo_cfg.unlink()
        custom = self.tmp / "custom" / "fo.json"
        os.environ["CLAUDE_FAILOVER_CONFIG"] = str(custom)
        rc, out = self.run_cmd(["init"])
        self.assertEqual(rc, 0, out)
        self.assertTrue(custom.is_file())
        self.assertFalse(self.user_cfg.exists())

    def test_add_appends_the_account_and_links_its_folder(self):
        self.run_cmd(["init"])
        cfg = self.config()
        cfg["my_own_key"] = {"kept": True}
        self.user_cfg.write_text(json.dumps(cfg))
        rc, out = self.run_cmd(["add", "work", "--email", "you@work.example"])
        self.assertEqual(rc, 0, out)
        self.assertTrue(out.rstrip().endswith("next: claude-accounts login work"), out)
        rc, out = self.run_cmd(["add", "personal", "--config-dir=%s" % (self.tmp / "p")])
        self.assertEqual(rc, 0, out)
        cfg = self.config()
        self.assertEqual(cfg["accounts"], [
            {"name": "work", "email": "you@work.example", "config_dir": "~/.claude-work"},
            {"name": "personal", "config_dir": str(self.tmp / "p")}])
        self.assertEqual(cfg["order"], {"interactive": ["work", "personal"],
                                        "headless": ["work", "personal"]})
        self.assertEqual(cfg["my_own_key"], {"kept": True})
        self.assertEqual(cfg["default_config_dir"], "~/.claude")    # kept as written
        self.assertEqual(stat.S_IMODE(self.user_cfg.stat().st_mode), 0o600)
        folder = self.home / ".claude-work"
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertTrue((folder / "projects").is_symlink())
        self.assertEqual(os.path.realpath(folder / "projects"), os.path.realpath(self.home / ".claude" / "projects"))
        self.assertTrue((self.tmp / "p" / "projects").is_symlink())

    def test_add_refuses_bad_names_duplicates_and_shared_folders(self):
        self.run_cmd(["init"])
        self.assertEqual(self.run_cmd(["add", "work"])[0], 0)
        before = self.user_cfg.read_text()
        for args, want in ((["work"], 1), (["bad name"], 2), (["x" * 33], 2), (["a/b"], 2),
                           (["work\n"], 2), ([], 2), (["two", "names"], 2), (["new", "--email"], 2),
                           (["other", "--config-dir", "~/.claude-work"], 1),
                           (["other", "--config-dir", "~/.claude"], 1)):
            rc, out = self.run_cmd(["add"] + args)
            self.assertEqual(rc, want, (args, out))
        self.assertEqual(self.user_cfg.read_text(), before)

    def test_no_accounts_yet(self):
        self.run_cmd(["init"])
        rc, out = self.run_cmd(["status"])
        self.assertEqual(rc, 0)
        self.assertIn("no accounts yet", out)
        # The wrapper then runs the real claude unchanged (plain environment, no
        # failover). The one note goes only to a terminal, never into a script's stderr.
        execs = []

        def fake_execve(path, args, env):
            execs.append((path, args, env))
            raise SystemExit(0)   # a real execve never returns
        for tty in (False, True):
            err = io.StringIO()
            with mock.patch.object(cf.os, "execve", fake_execve), \
                    mock.patch.object(cf.os, "isatty", lambda fd: tty), \
                    contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
                cf.main(["-p", "hi"])
            path, args, env = execs[-1]
            self.assertEqual((path, args), (str(FAKE), [str(FAKE), "-p", "hi"]))
            self.assertEqual([k for k in env if k.startswith("CLAUDE_FAILOVER_")], [])
            self.assertEqual("claude-accounts add" in err.getvalue(), tty, err.getvalue())

    def test_add_into_a_folder_that_cannot_be_made_adds_nothing(self):
        self.run_cmd(["init"])
        before = self.user_cfg.read_text()
        (self.home / "blocker").write_text("a file, not a folder")
        rc, out = self.run_cmd(["add", "work", "--config-dir", str(self.home / "blocker" / "work")])
        self.assertEqual(rc, 1, out)
        self.assertIn("Nothing was added", out)
        self.assertEqual(self.user_cfg.read_text(), before)     # not half added
        self.assertEqual(self.run_cmd(["add", "work"])[0], 0)   # so the name is still free

    def test_setup_goes_on_after_a_folder_it_cannot_make(self):
        (self.home / "blocker").write_text("x")
        good = self.tmp / "good"
        self.write_config(self.user_cfg, {"accounts": [
            {"name": "bad", "config_dir": str(self.home / "blocker" / "bad")},
            {"name": "good", "config_dir": str(good)}]})
        rc, out = self.run_cmd(["setup"])
        self.assertEqual(rc, 1, out)
        self.assertIn("bad: cannot create", out)
        self.assertTrue((good / "projects").is_symlink())

    def test_login_stops_when_its_folder_cannot_be_made(self):
        (self.home / "blocker").write_text("x")
        self.write_config(self.user_cfg, {"accounts": [
            {"name": "bad", "config_dir": str(self.home / "blocker" / "bad")}]})
        calls = []
        with mock.patch.object(cf.subprocess, "call", lambda *a, **k: calls.append(a) or 0):
            rc, out = self.run_cmd(["login", "bad"])
        self.assertEqual(rc, 1, out)
        self.assertIn("Cannot create", out)
        self.assertEqual(calls, [])   # claude never ran

    def test_add_into_a_folder_with_its_own_settings_and_projects_says_so(self):
        # A folder used with CLAUDE_CONFIG_DIR before: linking never replaces its own
        # settings.json (so its chats would skip the hook) or projects/ (so the
        # wrapper could not see its transcripts).
        self.run_cmd(["init"])
        own = self.home / ".claude-work"
        (own / "projects").mkdir(parents=True)
        (own / "settings.json").write_text('{"model": "sonnet"}')
        rc, out = self.run_cmd(["add", "work", "--email", "you@work.example"])
        self.assertEqual(rc, 1, out)
        for words in ("already has its own: settings.json, projects", "not handed off",
                      "not moved to another account", "claude-accounts setup"):
            self.assertIn(words, out)
        self.assertEqual([a["name"] for a in self.config()["accounts"]], ["work"])   # still added
        rc, out = self.run_cmd(["setup"])
        self.assertEqual(rc, 1, out)
        self.assertNotIn("already linked", out)
        rc, out = self.run_cmd(["hook", "install"])
        self.assertEqual(rc, 0, out)
        self.assertIn("warning - work: ~/.claude-work/settings.json is not a link", out)
        rc, out = self.run_cmd(["hook", "status"])
        self.assertEqual(rc, 1, out)
        self.assertIn("work: ~/.claude-work/settings.json", out)

    def test_mark_and_log_take_whole_numbers_only(self):
        self.run_cmd(["init"])
        self.run_cmd(["add", "work"])
        for args in (["mark", "work", "abc"], ["mark", "work", "0"], ["log", "abc"], ["log", "-3"],
                     ["log", "0"]):
            rc, out = self.run_cmd(args)
            self.assertEqual(rc, 2, args)
            self.assertIn("usage: claude-accounts %s" % args[0], out)
        rc, out = self.run_cmd(["mark", "work", "5"])
        self.assertEqual(rc, 0, out)
        self.assertIn("work marked limited for 5 min", out)
        self.assertEqual(self.run_cmd(["log", "1"])[0], 0)

    def test_usage_lists_every_command(self):
        rc, out = self.run_cmd(["no-such-command"])    # works with no config at all
        self.assertEqual(rc, 2)
        for cmd in ("status [--json]", "init [--force]", "add <name> [--email E] [--config-dir DIR]",
                    "setup", "login <account>", "logged-in <account> [YYYY-MM-DD]", "token <account>",
                    "test [account]", "clear [account]", "mark <account> [minutes]", "log [n]", "argspec",
                    "hook install|uninstall|status", "sync [--dry-run] [--quiet] [--json]",
                    "sync install [--every MINUTES]", "sync uninstall", "sync status"):
            self.assertIn(cmd, out)


class ArgspecTests(CleanEnv):
    """claude-accounts argspec reads option definitions out of the claude binary
    (over 300 MB) in pieces, never the whole file at once."""

    def fake_binary(self, path, n=60):
        noise = bytes(range(128, 256))   # not text: the binary is mostly not
        parts = []
        for i in range(n):
            parts += [noise, b'.option("--opt-%d <value>")' % i, b"\x00\xff", b'new Ab("--flag-%d")' % i]
        parts.append(b'.option("-r, --resume [id]")')
        return self._write(path, b"".join(parts))

    def _write(self, path, data):
        path = _executable(path)
        path.write_bytes(data)
        return path

    def test_the_option_table_is_built_from_the_binary(self):
        real = self.fake_binary(self.tmp / "tools" / "claude")
        self.write_config(self.user_cfg, {"real_claude": str(real), "accounts": []})
        rc, out = self.run_cmd(["argspec"])
        self.assertEqual(rc, 0, out)
        spec = json.loads((self.repo / "argspec.json").read_text())
        self.assertIn("--opt-59", spec["one"])
        self.assertIn("--flag-0", spec["flag"])
        self.assertEqual([n for n in spec["optional"] if n in ("-r", "--resume")], ["--resume", "-r"])

    def test_pieces_find_exactly_what_a_whole_read_finds(self):
        real = self.fake_binary(self.tmp / "tools" / "claude")
        whole = {m.group(1) for m in cf.OPTION_DEF_RE.finditer(real.read_bytes().decode("latin-1"))}
        self.assertEqual(len(whole), 121)
        for chunk in (7, 16, 50, 64, 1000):   # boundaries fall inside definitions
            self.assertEqual(set(cf.option_specs(str(real), chunk_size=chunk, overlap=40)), whole, chunk)
        # One definition cut in two by a piece boundary is still found whole.
        across = self._write(self.tmp / "across", b"x" * 50 + b'.option("--across <v>")' + b"y" * 50)
        self.assertEqual(set(cf.option_specs(str(across), chunk_size=60)), {"--across <v>"})

    def test_an_empty_binary_is_one_clear_line(self):
        real = self._write(self.tmp / "tools" / "claude", b"")
        self.write_config(self.user_cfg, {"real_claude": str(real), "accounts": []})
        rc, out = self.run_cmd(["argspec"])
        self.assertEqual(rc, 1)
        self.assertIn("only 0 options found", out)
        self.assertFalse((self.repo / "argspec.json").exists())


class HookCommandTests(Sandbox):
    """claude-accounts hook install|uninstall|status on the shared settings.json."""

    def settings(self):
        return json.loads((self.default / "settings.json").read_text())

    def backups(self):
        return sorted(self.state.glob("settings.json.bak-*"))

    def hook(self, sub):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cf.accounts_main(["hook", sub])
        return rc, out.getvalue()

    def ours(self):
        return {"type": "command", "command": shlex.quote(str(cf.HOOK_SCRIPT)), "timeout": 30}

    def test_the_command_is_this_repos_hook(self):
        self.assertEqual(cf.HOOK_SCRIPT, PROJECT / "bin" / "claude-failover-hook")
        self.assertTrue(cf.HOOK_SCRIPT.is_absolute())

    def test_install_is_idempotent_and_keeps_everything_else(self):
        other = {"matcher": "", "hooks": [{"type": "command", "command": "/usr/local/bin/other-hook"}]}
        (self.default / "settings.json").write_text(json.dumps(
            {"model": "fable", "env": {"X": "1"}, "hooks": {"PreToolUse": [other], "StopFailure": [other]}}))
        original = (self.default / "settings.json").read_text()
        rc, out = self.hook("install")
        self.assertEqual(rc, 0, out)
        s = self.settings()
        self.assertEqual((s["model"], s["env"], s["hooks"]["PreToolUse"]), ("fable", {"X": "1"}, [other]))
        self.assertEqual(s["hooks"]["StopFailure"],
                         [other, {"matcher": "rate_limit", "hooks": [self.ours()]}])
        self.assertIs(s["autoContinueAtUsageLimit"], False)
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(self.backups()[0].read_text(), original)
        self.assertEqual(stat.S_IMODE(self.backups()[0].stat().st_mode), 0o600)
        after = (self.default / "settings.json").read_text()
        rc, out = self.hook("install")
        self.assertEqual(rc, 0, out)
        self.assertIn("already", out)
        self.assertEqual((self.default / "settings.json").read_text(), after)   # nothing to change...
        self.assertEqual(len(self.backups()), 1)                                # ...so no new backup

    def test_an_entry_from_another_copy_counts_as_installed(self):
        other = _executable(self.tmp / "other" / "bin" / "claude-failover-hook")
        (self.default / "settings.json").write_text(json.dumps({"hooks": {"StopFailure": [
            {"matcher": "rate_limit",
             "hooks": [{"type": "command", "command": str(other)}]}]}}))
        rc, out = self.hook("install")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.settings()["hooks"]["StopFailure"]), 1)
        self.assertIn("another copy", out)
        self.assertIs(self.settings()["autoContinueAtUsageLimit"], False)
        rc, out = self.hook("status")
        self.assertEqual(rc, 0, out)
        self.assertIn("(another copy of claude-failover)", out)

    def test_a_hook_whose_copy_was_moved_is_flagged_and_repaired(self):
        gone = str(self.tmp / "moved-away" / "bin" / "claude-failover-hook")
        (self.default / "settings.json").write_text(json.dumps({"hooks": {"StopFailure": [
            {"matcher": "rate_limit", "hooks": [{"type": "command", "command": gone, "timeout": 30}]}]}}))
        rc, out = self.hook("status")
        self.assertEqual(rc, 1, out)
        self.assertIn("MISSING (moved or deleted copy) - fix: claude-accounts hook install", out)
        rc, out = self.hook("install")
        self.assertEqual(rc, 0, out)
        self.assertIn("which is missing", out)
        self.assertEqual(self.settings()["hooks"]["StopFailure"],
                         [{"matcher": "rate_limit", "hooks": [self.ours()]}])
        rc, out = self.hook("status")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("MISSING", out)

    def test_a_folder_name_with_an_apostrophe_is_still_this_copy(self):
        # shlex.quote writes "Bob's" in pieces: '...Bob'"'"'s Code/...'
        script = _executable(self.tmp / "Bob's Code" / "claude-failover" / "bin" / "claude-failover-hook")
        with mock.patch.object(cf, "HOOK_SCRIPT", script):
            self.assertEqual(self.hook("install")[0], 0)
            rc, out = self.hook("status")
            self.assertEqual(rc, 0, out)
            self.assertNotIn("another copy", out)
            rc, out = self.hook("install")
            self.assertIn("already", out)
            self.assertNotIn("uninstall", out)   # no advice that would only loop

    def test_an_account_folder_with_its_own_settings_does_not_run_the_hook(self):
        (self.prof / "settings.json").write_text('{"model": "sonnet"}')   # account A's own copy
        rc, out = self.hook("install")
        self.assertEqual(rc, 0, out)
        self.assertIn("warning - A: %s is not a link" % (self.prof / "settings.json"), out)
        rc, out = self.hook("status")
        self.assertEqual(rc, 1, out)
        self.assertIn("A: ", out)
        (self.prof / "settings.json").unlink()
        cf.ensure_profile_links(cf.load_config(), "A")    # what `claude-accounts setup` does
        self.assertEqual(self.hook("status")[0], 0)

    def test_install_creates_a_missing_settings_file(self):
        (self.default / "settings.json").unlink()
        rc, out = self.hook("install")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.backups(), [])     # nothing to back up
        self.assertEqual(self.settings()["hooks"]["StopFailure"],
                         [{"matcher": "rate_limit", "hooks": [self.ours()]}])
        self.assertEqual(stat.S_IMODE((self.default / "settings.json").stat().st_mode), 0o600)
        self.assertTrue((self.prof / "settings.json").is_symlink())   # account A's folder sees it too

    def test_invalid_json_is_never_overwritten(self):
        (self.default / "settings.json").write_text('{"model": "fable",}')
        for sub in ("install", "uninstall", "status"):
            rc, out = self.hook(sub)
            self.assertEqual(rc, 1, sub)
            self.assertIn("not valid JSON", out)
        self.assertEqual((self.default / "settings.json").read_text(), '{"model": "fable",}')
        self.assertEqual(self.backups(), [])

    def test_a_symlinked_settings_file_stays_a_link(self):
        real = self.tmp / "dotfiles" / "settings.json"
        real.parent.mkdir()
        real.write_text('{"model": "fable"}')
        (self.default / "settings.json").unlink()
        (self.default / "settings.json").symlink_to(real)
        self.assertEqual(self.hook("install")[0], 0)
        self.assertTrue((self.default / "settings.json").is_symlink())
        self.assertIn("StopFailure", json.loads(real.read_text())["hooks"])
        self.assertEqual(self.backups()[0].read_text(), '{"model": "fable"}')
        self.assertEqual(self.hook("uninstall")[0], 0)
        self.assertTrue((self.default / "settings.json").is_symlink())
        self.assertNotIn("hooks", json.loads(real.read_text()))

    def test_uninstall_removes_only_ours(self):
        theirs = {"type": "command", "command": "/usr/local/bin/other-hook"}
        (self.default / "settings.json").write_text(json.dumps({
            "autoContinueAtUsageLimit": False,
            "hooks": {"StopFailure": [{"matcher": "rate_limit", "hooks": [self.ours()]},
                                      {"matcher": "rate_limit", "hooks": [theirs, self.ours()]}],
                      "Stop": [{"hooks": [theirs]}]}}))
        rc, out = self.hook("uninstall")
        self.assertEqual(rc, 0, out)
        s = self.settings()
        self.assertEqual(s["hooks"], {"StopFailure": [{"matcher": "rate_limit", "hooks": [theirs]}],
                                      "Stop": [{"hooks": [theirs]}]})
        self.assertIs(s["autoContinueAtUsageLimit"], False)       # left as it was...
        self.assertIn("autoContinueAtUsageLimit is unchanged", out)   # ...and it says so
        self.assertEqual(len(self.backups()), 1)
        rc, out = self.hook("uninstall")
        self.assertEqual(rc, 0)
        self.assertIn("nothing changed", out)
        self.assertEqual(len(self.backups()), 1)

    def test_uninstall_drops_the_lists_it_empties(self):
        self.hook("install")
        self.hook("uninstall")
        s = self.settings()
        self.assertNotIn("hooks", s)
        self.assertEqual(s["model"], "fable")
        self.assertIs(s["autoContinueAtUsageLimit"], False)

    def test_status(self):
        rc, out = self.hook("status")
        self.assertEqual(rc, 1)
        self.assertIn("not installed", out)
        self.hook("install")
        rc, out = self.hook("status")
        self.assertEqual(rc, 0, out)
        self.assertIn("hook: installed", out)
        self.assertIn(str(cf.HOOK_SCRIPT), out)
        self.assertEqual(self.hook("bogus")[0], 2)


class DesktopSyncCallSites(HookPayloads, Sandbox):
    """claude_failover calls desktop_sync in three places. A fake module stands in
    for it, so these tests never depend on the real one."""

    def setUp(self):
        super().setUp()
        self.seen = []
        fake = types.ModuleType("desktop_sync")
        fake.cli = lambda cfg, argv: self.seen.append(("cli", sorted(cfg["by_name"]), argv)) or 7
        fake.sync_quietly = lambda cfg: self.seen.append(("sync_quietly", sorted(cfg["by_name"])))
        fake.status_line = lambda cfg: "Desktop chats - 12 listed under every account"
        self.fake = fake
        patcher = mock.patch.dict(sys.modules, {"desktop_sync": fake})
        patcher.start()
        self.addCleanup(patcher.stop)

    def enable_sync(self):
        cfg = json.loads(self.cfg_path.read_text())
        cfg["desktop_sync"] = {"enabled": True, "interval_minutes": 10}
        self.cfg_path.write_text(json.dumps(cfg))

    def test_sync_command_hands_over_to_desktop_sync(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cf.accounts_main(["sync", "--dry-run", "--json"])
        self.assertEqual(rc, 7)
        self.assertEqual(self.seen, [("cli", ["A", "B"], ["--dry-run", "--json"])])

    def status_text(self):
        out = io.StringIO()
        with mock.patch.object(cf, "auth_status", return_value={"loggedIn": False}), \
                contextlib.redirect_stdout(out):
            self.assertEqual(cf.accounts_status(cf.load_config()), 0)
        return out.getvalue()

    def test_status_shows_the_sync_line_only_when_enabled(self):
        self.assertNotIn("Desktop chats", self.status_text())
        self.enable_sync()
        text = self.status_text()
        self.assertTrue(text.rstrip().endswith("Desktop chats - 12 listed under every account"), text)
        self.fake.status_line = lambda cfg: None
        self.assertNotIn("Desktop chats", self.status_text())

        def broken(cfg):
            raise RuntimeError("broken")
        self.fake.status_line = broken
        self.assertNotIn("Desktop chats", self.status_text())   # status still works
        with mock.patch.dict(sys.modules, {"desktop_sync": None}):   # module not there at all
            self.assertNotIn("Desktop chats", self.status_text())

    def hand_off(self, session_id, popen_error=None):
        popen = mock.Mock(side_effect=popen_error)
        with mock.patch.object(cf.subprocess, "Popen", popen):
            rc = cf.hook_main(self.payload(session_id=session_id), env=self.desktop_env(),
                              open_terminal=True, print_run=False)
        self.assertEqual(rc, 0)
        return popen

    def test_hook_syncs_after_a_handoff_when_enabled(self):
        # Session ids differ in their first 8 characters: a handoff script is
        # named <second>-<id[:8]>.command.
        popen = self.hand_off("10000001-0000-0000-0000-000000000000")
        self.assertEqual(popen.call_count, 1)        # Terminal was opened
        self.assertEqual(self.seen, [])              # but sync is off
        self.enable_sync()
        self.hand_off("10000002-0000-0000-0000-000000000000")
        self.assertEqual(self.seen, [("sync_quietly", ["A", "B"])])
        self.hand_off("10000003-0000-0000-0000-000000000000", popen_error=OSError("no Terminal"))
        self.assertEqual(len(self.seen), 1)          # no handoff, so no sync

        def broken(cfg):
            raise RuntimeError("broken")
        self.fake.sync_quietly = broken
        self.hand_off("10000004-0000-0000-0000-000000000000")   # the hook still ends cleanly
        with mock.patch.dict(sys.modules, {"desktop_sync": None}):
            self.hand_off("10000005-0000-0000-0000-000000000000")
        self.assertEqual(len(list(self.handoffs.glob("*.command"))), 5)

    def test_a_plain_true_is_off_and_never_a_crash(self):
        # "desktop_sync": true is a natural way to write a switch. Like
        # desktop_sync.settings(), only {"enabled": true} turns sync on.
        cfg = json.loads(self.cfg_path.read_text())
        cfg["desktop_sync"] = True
        self.cfg_path.write_text(json.dumps(cfg))
        asked = []
        self.fake.status_line = lambda cfg: asked.append(1) or "Desktop chats - x"
        self.assertNotIn("Desktop chats", self.status_text())   # status_text checks exit code 0
        self.assertEqual(asked, [])
        self.hand_off("10000006-0000-0000-0000-000000000000")
        self.assertEqual(self.seen, [])


class GitignoreTests(unittest.TestCase):
    """Per-machine files never reach a commit, temp files of an interrupted write included."""

    def test_private_and_temp_files_are_ignored(self):
        git = shutil.which("git")
        if git is None:
            self.skipTest("git is not installed")
        inside = subprocess.run([git, "-C", str(PROJECT), "rev-parse", "--is-inside-work-tree"],
                                capture_output=True, text=True)
        if inside.stdout.strip() != "true":
            self.skipTest("not a git checkout")

        def check(path):
            return subprocess.run([git, "-C", str(PROJECT), "check-ignore", "-q", "--no-index", path],
                                  capture_output=True).returncode
        for path in ("config.json", "config.json.k3j4h5l2", "config.json.bak-20261109-230000",
                     "argspec.json", "argspec.json.x1y2z3w4", "state/accounts.json", "handoffs/x"):
            self.assertEqual(check(path), 0, path)
        for path in ("config.example.json", "tests/fake_claude.py", "README.md"):
            self.assertEqual(check(path), 1, path)


class DocLinkTests(unittest.TestCase):

    def test_code_comments_name_doc_sections_that_exist(self):
        # A comment such as "(docs/ARCHITECTURE.md, Login clock and spare token)" must
        # name a real heading of that file, so a reader can find the section.
        pat = re.compile(r"\((README(?:\.md)?|docs/[A-Z-]+\.md)[:,] ([^)\n]+)\)")
        for src in ("claude_failover.py", "desktop_sync.py"):
            for m in pat.finditer((PROJECT / src).read_text()):
                doc = "README.md" if m.group(1).startswith("README") else m.group(1)
                heads = {line.lstrip("#").strip().lower()
                         for line in (PROJECT / doc).read_text().splitlines()
                         if line.startswith("#")}
                self.assertIn(m.group(2).strip().lower(), heads, "%s: %s" % (src, m.group(0)))


if __name__ == "__main__":
    unittest.main()
