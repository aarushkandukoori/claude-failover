"""Tests for desktop_sync.py against temp folders shaped like the desktop app's.

Nothing here touches the real ~/Library, ~/.claude or launchctl: HOME, the app's
data folder, the state folder and the LaunchAgents folder all point into a temp
dir, and launchctl is either switched off or a fake that logs its arguments.

Run:  /usr/bin/python3 -m unittest discover -s tests -p 'test_desktop_sync.py' -v
"""
import contextlib
import datetime as dt
import fcntl
import hashlib
import io
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import claude_failover as cf  # noqa: E402
import desktop_sync as ds  # noqa: E402

# Made-up ids shaped like the app's folder names.
P_ACCT, P_ORG = "aaaaaaaa-0000-4000-8000-000000000001", "aaaaaaaa-0000-4000-8000-0000000000a1"
P_ORG2 = "aaaaaaaa-0000-4000-8000-0000000000a2"
W_ACCT, W_ORG = "bbbbbbbb-0000-4000-8000-000000000002", "bbbbbbbb-0000-4000-8000-0000000000b2"
T_ACCT, T_ORG = "cccccccc-0000-4000-8000-000000000003", "cccccccc-0000-4000-8000-0000000000c3"
CONNECTOR = "dddddddd-0000-4000-8000-000000000004"   # a claude.ai connector id
OTHER_CONNECTOR = "eeeeeeee-0000-4000-8000-000000000005"
BASE_MS = 1760000000000   # a record time in ms

REPORT_KEYS = {"dry_run", "app_running", "active_account", "folders", "skipped_accounts",
               "added", "added_needs_restart", "updated", "retired", "stale_in_running_app",
               "deleted_elsewhere_live", "skipped_no_transcript", "skipped_invalid",
               "skipped_unsafe", "errors", "backup", "inactive_scheduled_tasks", "actions"}

FAKE_LAUNCHCTL = """#!/usr/bin/python3
import json, os, sys
with open(os.environ["FAKE_LAUNCHCTL_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
codes = json.loads(os.environ.get("FAKE_LAUNCHCTL_CODES") or "{}")
sys.exit(codes.get(sys.argv[1], 0))
"""


def snapshot(root):
    """Every path under root (and root itself) -> (type, mode, size, mtime_ns,
    sha256 of a file or target of a link). Any write shows up as a difference."""
    out = {}
    root = str(root)

    def entry(p):
        st = os.lstat(p)
        extra = None
        if stat.S_ISREG(st.st_mode):
            with open(p, "rb") as f:
                extra = hashlib.sha256(f.read()).hexdigest()
        elif stat.S_ISLNK(st.st_mode):
            extra = os.readlink(p)
        return (stat.S_IFMT(st.st_mode), stat.S_IMODE(st.st_mode), st.st_size, st.st_mtime_ns,
                st.st_nlink, extra)

    out["."] = entry(root)
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            out[os.path.relpath(p, root)] = entry(p)
    return out


class Base(unittest.TestCase):
    """Two accounts by default, each with its chat folder named in the config."""
    ACCOUNTS = (("personal", P_ACCT, P_ORG), ("work", W_ACCT, W_ORG))

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ds-test-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.desktop = self.tmp / "desktop"
        self.sessions = self.desktop / "claude-code-sessions"
        self.shared = self.tmp / "dot-claude"
        self.project = self.shared / "projects" / "-tmp-proj"
        self.project.mkdir(parents=True)
        self.state = self.tmp / "state"
        self.agents = self.tmp / "LaunchAgents"
        self.cfg_path = self.tmp / "config.json"
        self._saved_env = dict(os.environ)
        os.environ.update({
            "HOME": str(self.home),
            "CLAUDE_FAILOVER_CONFIG": str(self.cfg_path),
            "CLAUDE_FAILOVER_STATE_DIR": str(self.state),
            "CLAUDE_FAILOVER_DESKTOP_DIR": str(self.desktop),
            "CLAUDE_FAILOVER_APP_RUNNING": "0",
            "CLAUDE_FAILOVER_LAUNCHCTL": "0",
            "CLAUDE_FAILOVER_LAUNCH_AGENTS_DIR": str(self.agents),
            "CLAUDE_FAILOVER_NO_NOTIFY": "1",
        })
        self.dirs = {}
        accounts = []
        for name, acct, org in self.ACCOUNTS:
            d = self.sessions / acct / org
            d.mkdir(parents=True)
            self.dirs[name] = d
            accounts.append({"name": name, "email": "%s@example.com" % name, "config_dir": None,
                             "desktop_account_uuid": acct, "desktop_org_uuid": org})
        self.cfg = {"default_config_dir": str(self.shared), "accounts": accounts,
                    "desktop_sync": {"enabled": True, "interval_minutes": 10},
                    "_config_path": str(self.cfg_path)}

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    # -- fixtures --

    def record(self, account, sid, title="chat", activity=BASE_MS, mtime=None, transcript=True,
               **fields):
        data = {"sessionId": "local_" + sid, "cliSessionId": "cli-" + sid, "cwd": "/tmp/proj",
                "title": title, "model": "opus", "permissionMode": "default", "isArchived": False,
                "createdAt": BASE_MS - 1000, "lastActivityAt": activity,
                "lastFocusedAt": activity}
        data.update(fields)
        data = {k: v for k, v in data.items() if v is not None}
        path = self.dirs[account] / ("local_%s.json" % sid)
        path.write_text(json.dumps(data))
        if mtime is not None:
            os.utime(str(path), (mtime, mtime))
        if transcript:
            self.transcript(data.get("cliSessionId") or sid)
        return path

    def transcript(self, cli_id):
        (self.project / (cli_id + ".jsonl")).write_text('{"type":"user"}\n')

    def path(self, account, sid):
        return self.dirs[account] / ("local_%s.json" % sid)

    def load(self, account, sid):
        return json.loads(self.path(account, sid).read_text())

    def has(self, account, sid):
        return self.path(account, sid).exists()

    def sync(self, dry_run=False):
        return ds.sync(self.cfg, dry_run=dry_run)

    def sign_in(self, account_uuid, running=True):
        (self.desktop / "config.json").write_text(json.dumps({"lastKnownAccountUuid": account_uuid}))
        os.environ["CLAUDE_FAILOVER_APP_RUNNING"] = "1" if running else "0"

    def run_cli(self, *args, cfg=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ds.cli(self.cfg if cfg is None else cfg, list(args))
        return rc, out.getvalue()

    def trash(self, account_uuid):
        d = self.state / "desktop-sync" / "trash" / account_uuid
        return sorted(d.iterdir()) if d.is_dir() else []

    def no_temp_files(self):
        for d in self.dirs.values():
            self.assertEqual([p.name for p in d.iterdir() if p.name.startswith(ds.TEMP_PREFIX)], [])


class UnionTest(Base):

    def test_adds_both_ways(self):
        self.record("personal", "s1", title="from personal")
        self.record("work", "s2", title="from work")
        r = self.sync()
        self.assertEqual(r["errors"], [])
        self.assertEqual((r["added"], r["updated"], r["retired"]), (2, 0, 0))
        self.assertEqual(self.load("work", "s1"), self.load("personal", "s1"))
        self.assertEqual(self.load("personal", "s2"), self.load("work", "s2"))
        self.assertEqual(r["chats"], 2)
        self.assertEqual([f["chats"] for f in r["folders"]], [1, 1])   # before the copies
        self.assertEqual(sorted((a["action"], a["session"], a["from"], a["to"]) for a in r["actions"]),
                         [("add", "local_s1", "personal", "work"), ("add", "local_s2", "work", "personal")])
        # A copy has its source's file time (never newer) and is private.
        for src, dst, sid in (("personal", "work", "s1"), ("work", "personal", "s2")):
            self.assertEqual(os.stat(str(self.path(dst, sid))).st_mtime_ns,
                             os.stat(str(self.path(src, sid))).st_mtime_ns)
            self.assertEqual(stat.S_IMODE(os.stat(str(self.path(dst, sid))).st_mode), 0o600)
        self.no_temp_files()

    def test_second_run_does_nothing(self):
        now = time.time()
        self.record("personal", "s1")
        self.record("personal", "s2", title="renamed", mtime=now - 10)
        self.record("work", "s2", title="old name", mtime=now - 500)
        self.record("work", "s3", activity=BASE_MS + 99)
        first = self.sync()
        self.assertEqual((first["added"], first["updated"]), (2, 1))
        before = snapshot(self.sessions)
        second = self.sync()
        self.assertEqual(snapshot(self.sessions), before)
        for key in ds.COUNTS:
            self.assertEqual(second[key], 0, key)
        self.assertEqual((second["actions"], second["errors"], second["backup"]), ([], [], None))
        rc, out = self.run_cli()
        self.assertIn("  in sync - nothing to copy", out.splitlines())
        self.assertEqual(rc, 0)

    def test_event_log_only_gets_runs_that_changed_something(self):
        events = self.state / "events.jsonl"
        self.record("personal", "s1")
        self.sync()
        lines = events.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        event = json.loads(lines[0])
        self.assertEqual((event["event"], event["added"], event["errors"]), ("desktop_sync", 1, 0))
        self.sync()   # nothing to do: last.json is updated, the event log is not
        self.assertEqual(len(events.read_text().splitlines()), 1)
        last = json.loads((self.state / "desktop-sync" / "last.json").read_text())
        self.assertIn("at", last)
        self.assertNotIn("actions", last["report"])
        self.assertEqual(last["report"]["added"], 0)

    def test_lone_surrogate_title_is_copied(self):
        # Half an emoji in a title: valid JSON, but it has no UTF-8 form.
        path = self.dirs["personal"] / "local_s1.json"
        path.write_text('{"sessionId":"local_s1","cliSessionId":"cli-s1","title":"bad \\ud83d end",'
                        '"lastActivityAt":%d}' % BASE_MS)
        self.transcript("cli-s1")
        r = self.sync()
        self.assertEqual((r["added"], r["errors"]), (1, []))
        self.assertEqual(self.load("work", "s1"), self.load("personal", "s1"))

    def test_actions_list_is_capped(self):
        for sid in ("s1", "s2", "s3"):
            self.record("personal", sid)
        with mock.patch.object(ds, "MAX_ACTIONS", 2):
            r = self.sync(dry_run=True)
        self.assertEqual((r["added"], len(r["actions"]), r["actions_truncated"]), (3, 2, True))


class FreshnessTest(Base):

    def test_newer_turns_beat_a_newer_file_time(self):
        now = time.time()
        src = self.record("personal", "s1", title="newer turns", activity=BASE_MS + 5000,
                          mtime=now - 1000)
        old = self.record("work", "s1", title="clicked later", activity=BASE_MS, mtime=now)
        old_bytes, src_bytes = old.read_bytes(), src.read_bytes()
        r = self.sync()
        self.assertEqual(r["updated"], 1)
        self.assertEqual(self.load("work", "s1")["title"], "newer turns")
        self.assertEqual(src.read_bytes(), src_bytes)
        self.assertEqual(os.stat(str(old)).st_mtime_ns, os.stat(str(src)).st_mtime_ns)
        self.assertEqual(r["actions"][0]["from"], "personal")
        # The replaced copy is in the trash, as it was.
        trashed = self.trash(W_ACCT)
        self.assertEqual(len(trashed), 1)
        self.assertRegex(trashed[0].name, r"^local_s1\.json\.\d+$")
        self.assertEqual(trashed[0].read_bytes(), old_bytes)
        self.assertEqual(stat.S_IMODE(trashed[0].stat().st_mode), 0o600)

    def test_file_time_breaks_a_tie(self):
        now = time.time()
        self.record("personal", "s1", title="old name", mtime=now - 1000)
        self.record("work", "s1", title="new name", isArchived=True, mtime=now)
        r = self.sync()
        self.assertEqual(r["updated"], 1)
        got = self.load("personal", "s1")
        self.assertEqual((got["title"], got["isArchived"]), ("new name", True))

    def test_file_times_within_a_second_count_as_equal(self):
        now = time.time()
        self.record("personal", "s1", title="a", mtime=now - 0.5)
        self.record("work", "s1", title="b", mtime=now)
        r = self.sync()
        self.assertEqual((r["updated"], r["added"]), (0, 0))
        self.assertEqual(self.load("personal", "s1")["title"], "a")

    def test_same_content_is_not_rewritten(self):
        now = time.time()
        self.record("personal", "s1", mtime=now)
        old = self.record("work", "s1", mtime=now - 100)
        before = snapshot(self.dirs["work"])
        r = self.sync()
        self.assertEqual(r["updated"], 0)
        self.assertEqual(snapshot(self.dirs["work"]), before)
        self.assertTrue(old.exists())

    def test_viewing_a_stale_copy_does_not_revert_a_rename(self):
        # The app rewrites a record (a new lastFocusedAt) whenever a chat is opened,
        # so a copy that was only looked at gets the newest file time. It must not
        # win over a rename and an archive made on the other account.
        now = time.time()
        self.record("personal", "s1", title="old name", mtime=now - 1000)
        self.assertEqual(self.sync()["added"], 1)
        renamed = self.record("personal", "s1", title="new name", isArchived=True, mtime=now - 500)
        looked_at = self.load("work", "s1")
        looked_at["lastFocusedAt"] = BASE_MS + 999
        self.path("work", "s1").write_text(json.dumps(looked_at))
        os.utime(str(self.path("work", "s1")), (now, now))
        renamed_bytes = renamed.read_bytes()
        self.sign_in(W_ACCT, running=True)   # the app moved on to work and opened the chat there
        r = self.sync()
        self.assertEqual((r["updated"], r["stale_in_running_app"]), (0, 1))
        self.assertEqual(renamed.read_bytes(), renamed_bytes)
        os.environ["CLAUDE_FAILOVER_APP_RUNNING"] = "0"   # the app quit: work catches up
        r = self.sync()
        self.assertEqual(r["updated"], 1)
        got = self.load("work", "s1")
        self.assertEqual((got["title"], got["isArchived"]), ("new name", True))
        self.assertEqual(renamed.read_bytes(), renamed_bytes)
        self.assertEqual(self.trash(P_ACCT), [])

    def test_focus_only_difference_is_not_copied(self):
        now = time.time()
        self.record("personal", "s1", mtime=now - 1000)
        self.record("work", "s1", mtime=now, lastFocusedAt=BASE_MS + 999)
        before = snapshot(self.sessions)
        r = self.sync()
        self.assertEqual((r["updated"], r["added"], r["errors"]), (0, 0, []))
        self.assertEqual(snapshot(self.sessions), before)
        self.assertEqual(self.trash(P_ACCT), [])


class LiveAppTest(Base):

    def test_running_app_folder_gets_adds_but_no_updates(self):
        self.sign_in(W_ACCT, running=True)
        self.record("personal", "s1")                                     # new for work
        self.record("personal", "s2", title="newer", activity=BASE_MS + 5000)
        stale = self.record("work", "s2", title="older", activity=BASE_MS)
        self.record("work", "s3")                                         # new for personal
        self.record("personal", "s4", title="older", activity=BASE_MS)
        self.record("work", "s4", title="newer", activity=BASE_MS + 5000)
        stale_bytes = stale.read_bytes()
        r = self.sync()
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["active_account"], "work")
        self.assertEqual([(f["account"], f["live"]) for f in r["folders"]],
                         [("personal", False), ("work", True)])
        self.assertEqual((r["added"], r["added_needs_restart"]), (2, 1))
        self.assertEqual((r["updated"], r["stale_in_running_app"]), (1, 1))
        self.assertTrue(self.has("work", "s1"))
        self.assertTrue(self.has("personal", "s3"))
        self.assertEqual(stale.read_bytes(), stale_bytes)               # the app holds it
        self.assertEqual(self.load("personal", "s4")["title"], "newer")  # not live: updated
        rc, out = self.run_cli()
        self.assertIn("Desktop chats (app running, signed in as work)", out)
        self.assertIn("  work      4 chats  (signed in)", out.splitlines())   # after the first run
        self.assertIn("  waiting - 1 chat to update once the Claude app quits or switches accounts",
                      out.splitlines())
        # Once the app quits, the waiting update goes through.
        os.environ["CLAUDE_FAILOVER_APP_RUNNING"] = "0"
        r = self.sync()
        self.assertEqual((r["updated"], r["stale_in_running_app"]), (1, 0))
        self.assertEqual(self.load("work", "s2")["title"], "newer")

    def test_add_into_running_app_folder_says_restart(self):
        self.sign_in(W_ACCT, running=True)
        self.record("personal", "s1")
        rc, out = self.run_cli()
        self.assertIn("  personal -> work: 1 added - they appear after you quit and reopen the "
                      "Claude app", out.splitlines())

    def test_unknown_signed_in_account_counts_every_folder_as_live(self):
        os.environ["CLAUDE_FAILOVER_APP_RUNNING"] = "1"   # and no config.json in the app folder
        self.record("personal", "s1", title="newer", activity=BASE_MS + 5)
        self.record("work", "s1", title="older")
        self.record("personal", "s2")
        r = self.sync()
        self.assertIsNone(r["active_account"])
        self.assertTrue(all(f["live"] for f in r["folders"]))
        self.assertEqual((r["added"], r["added_needs_restart"], r["updated"],
                          r["stale_in_running_app"]), (1, 1, 0, 1))
        self.assertEqual(self.load("work", "s1")["title"], "older")
        self.assertIn("signed-in account unknown", ds.report_lines(r)[0])


class LetterCaseTest(Base):

    def test_an_id_typed_in_capitals_still_marks_the_running_apps_folder(self):
        # On the Mac's usual file system "BBBB" opens the folder "bbbb". The running
        # app's folder must still be recognised, or it would get updates and
        # deletions the app then writes back over.
        probe = self.tmp / "CaseProbe"
        probe.write_text("x")
        if not (self.tmp / "caseprobe").exists():
            self.skipTest("this file system tells letter case apart")
        self.cfg["accounts"][1].update(desktop_account_uuid=W_ACCT.upper(),
                                       desktop_org_uuid=W_ORG.upper())
        self.sign_in(W_ACCT, running=True)
        self.record("personal", "s1", title="newer", activity=BASE_MS + 5000)
        self.record("work", "s1", title="older")
        (self.dirs["personal"] / "deleted_s2").write_text("1760000000123")
        self.record("work", "s2")
        before = snapshot(self.dirs["work"])
        r = self.sync()
        self.assertEqual(r["active_account"], "work")
        self.assertEqual([(f["account"], f["account_uuid"], f["org_uuid"], f["live"]) for f in r["folders"]],
                         [("personal", P_ACCT, P_ORG, False), ("work", W_ACCT, W_ORG, True)])
        self.assertEqual((r["updated"], r["retired"], r["stale_in_running_app"],
                          r["deleted_elsewhere_live"]), (0, 0, 1, 1))
        self.assertEqual(snapshot(self.dirs["work"]), before)
        trash = self.state / "desktop-sync" / "trash"
        self.assertNotIn(W_ACCT.upper(), os.listdir(str(trash)) if trash.is_dir() else [])


class SizeLimitTest(Base):

    def test_a_copy_that_would_be_over_the_limit_is_not_written(self):
        # The target keeps its own account fields, so a copy can come out bigger
        # than its source. The app skips a file over the limit: the chat would vanish.
        with mock.patch.object(ds, "MAX_RECORD_BYTES", 4096):
            src = self.record("personal", "s1", title="newer", activity=BASE_MS + 50, notes="x" * 3600)
            self.record("work", "s1", title="older", remoteMcpServersConfig={"big": "y" * 1000})
            self.assertLess(src.stat().st_size, 4096)
            before = snapshot(self.dirs["work"])
            r = self.sync()
        self.assertEqual(r["updated"], 0)
        self.assertEqual(len(r["errors"]), 1)
        self.assertIn("local_s1.json would be over 10 MiB in work", r["errors"][0])
        self.assertEqual(snapshot(self.dirs["work"]), before)
        self.assertEqual(self.trash(W_ACCT), [])

    def test_a_lone_surrogate_escapes_only_itself(self):
        # Escaping every non-ASCII character would make CJK text twice as big.
        cjk = "漢字" * 500
        path = self.dirs["personal"] / "local_s1.json"
        path.write_bytes(('{"sessionId":"local_s1","cliSessionId":"cli-s1","title":"%s \\ud83d end",'
                          '"lastActivityAt":%d}' % (cjk, BASE_MS)).encode("utf-8"))
        self.transcript("cli-s1")
        r = self.sync()
        self.assertEqual((r["added"], r["errors"]), (1, []))
        written = self.path("work", "s1").read_bytes()
        self.assertIn(cjk.encode("utf-8"), written)
        self.assertIn(b"\\ud83d", written)
        self.assertLess(len(written), path.stat().st_size + 100)
        self.assertEqual(self.load("work", "s1"), self.load("personal", "s1"))


class ThreeAccounts(Base):
    ACCOUNTS = (("personal", P_ACCT, P_ORG), ("work", W_ACCT, W_ORG), ("third", T_ACCT, T_ORG))

    def test_one_chat_reaches_every_account(self):
        self.record("personal", "s1")
        r = self.sync()
        self.assertEqual(r["added"], 2)
        self.assertTrue(self.has("work", "s1") and self.has("third", "s1"))

    def test_deletion_travels_and_is_never_undone(self):
        tomb = self.dirs["personal"] / "deleted_s1"
        tomb.write_bytes(b"1760000000123")
        old = self.record("work", "s1")
        old_bytes = old.read_bytes()
        r = self.sync()
        self.assertEqual((r["retired"], r["added"], r["errors"]), (1, 0, []))
        self.assertFalse(old.exists())
        self.assertEqual((self.dirs["work"] / "deleted_s1").read_bytes(), b"1760000000123")
        trashed = self.trash(W_ACCT)
        self.assertEqual([p.read_bytes() for p in trashed], [old_bytes])
        self.assertEqual(r["actions"], [{"action": "retire", "session": "local_s1", "title": "chat",
                                         "from": "personal", "to": "work"}])
        # A folder that never had the chat gets nothing, not even a tombstone.
        self.assertEqual(sorted(p.name for p in self.dirs["third"].iterdir()), [])
        again = self.sync()
        self.assertEqual((again["retired"], again["added"]), (0, 0))
        # An old copy turning up later is retired too, never copied back.
        self.record("third", "s1", activity=BASE_MS + 10 ** 6)
        r = self.sync()
        self.assertEqual((r["retired"], r["added"]), (1, 0))
        for account in ("personal", "work", "third"):
            self.assertFalse(self.has(account, "s1"), account)

    def test_running_app_folder_keeps_a_deleted_chat_until_it_quits(self):
        self.sign_in(W_ACCT, running=True)
        (self.dirs["personal"] / "deleted_s1").write_text("1760000000123")
        self.record("work", "s1")
        r = self.sync()
        self.assertEqual((r["retired"], r["deleted_elsewhere_live"]), (0, 1))
        self.assertTrue(self.has("work", "s1"))
        self.assertFalse((self.dirs["work"] / "deleted_s1").exists())
        self.assertEqual(self.trash(W_ACCT), [])
        os.environ["CLAUDE_FAILOVER_APP_RUNNING"] = "0"
        r = self.sync()
        self.assertEqual((r["retired"], r["deleted_elsewhere_live"]), (1, 0))
        self.assertFalse(self.has("work", "s1"))


class SkipTest(Base):

    def test_no_transcript_means_no_copy(self):
        self.record("personal", "s1", transcript=False)
        self.record("personal", "s2", cliSessionId=None)          # transcript s2.jsonl
        self.record("personal", "s3", cliSessionId=None, transcript=False)
        r = self.sync()
        self.assertEqual((r["added"], r["skipped_no_transcript"]), (1, 2))
        self.assertTrue(self.has("work", "s2"))
        self.assertFalse(self.has("work", "s1") or self.has("work", "s3"))
        rc, out = self.run_cli("--dry-run")
        self.assertIn("  skipped - 2 with no transcript on disk", out.splitlines())

    def test_odd_files_are_skipped_and_never_overwritten(self):
        for sid in ("s1", "s2", "s3", "s4", "s5", "s6"):
            self.record("personal", sid, title="valid", activity=BASE_MS + 10 ** 6)
        work = self.dirs["work"]
        (work / "local_s1.json").write_text("{not json")
        (work / "local_s2.json").write_text(json.dumps({"sessionId": "local_other"}))
        elsewhere = self.tmp / "elsewhere.json"
        elsewhere.write_text(json.dumps({"sessionId": "local_s3", "lastActivityAt": 1}))
        os.symlink(str(elsewhere), str(work / "local_s3.json"))
        (work / "local_s4.json").write_text(json.dumps({"sessionId": "local_s4", "lastActivityAt": 1}))
        os.link(str(work / "local_s4.json"), str(self.tmp / "hardlink.json"))
        with open(str(work / "local_s5.json"), "wb") as f:
            f.truncate(ds.MAX_RECORD_BYTES + 1)   # sparse: no 10 MiB written
        (work / "local_s6.json").write_text("[1, 2]")
        (work / "local_a.b.json").write_text("{}")                    # not a chat id
        (work / "local_s7.json").write_text('{"sessionId": "local_s7", "x": NaN}')
        before_work = snapshot(work)
        before_target = elsewhere.read_bytes()
        r = self.sync()
        self.assertEqual(r["skipped_unsafe"], 3)        # symlink, hard link, too big
        self.assertEqual(r["skipped_invalid"], 5)       # bad JSON, wrong id, list, bad name, NaN
        self.assertEqual((r["added"], r["updated"], r["errors"]), (0, 0, []))
        self.assertEqual(snapshot(work), before_work)   # not one byte, link or time changed
        self.assertEqual(elsewhere.read_bytes(), before_target)
        self.assertFalse(self.has("personal", "s7"))    # an invalid file is never a source


class AccountBoundTest(Base):
    ACCOUNTS = (("personal", P_ACCT, P_ORG), ("work", W_ACCT, W_ORG),
                ("personal-team", P_ACCT, P_ORG2))   # same account, second organization

    BOUND = {"bridgeSessionIds": ["bridge-1"], "remoteMcpServersConfig": {"x": {"url": "u"}},
             "mcpConnectorsAsked": [CONNECTOR], "mcpConnectorsAskedOut": [CONNECTOR],
             "mcpToolsAskedOff": ["search"]}

    def test_stripped_only_across_accounts(self):
        tools = {CONNECTOR + ":search": True, "local:files": True}
        self.record("personal", "s1", enabledMcpTools=tools, **self.BOUND)
        self.record("personal", "s2", enabledMcpTools={CONNECTOR + ":search": True})
        r = self.sync()
        self.assertEqual((r["added"], r["errors"]), (4, []))
        src, other_account = self.load("personal", "s1"), self.load("work", "s1")
        for key in self.BOUND:
            self.assertNotIn(key, other_account)
        self.assertEqual(other_account["enabledMcpTools"], {"local:files": True})
        expected = {k: v for k, v in src.items() if k not in self.BOUND and k != "enabledMcpTools"}
        self.assertEqual({k: v for k, v in other_account.items() if k != "enabledMcpTools"}, expected)
        self.assertNotIn("enabledMcpTools", self.load("work", "s2"))
        # The same account in another organization folder gets everything.
        self.assertEqual(self.load("personal-team", "s1"), src)

    def test_update_keeps_the_targets_own_account_fields(self):
        self.record("personal", "s1", title="newer", activity=BASE_MS + 50,
                    bridgeSessionIds=["p-bridge"],
                    enabledMcpTools={CONNECTOR + ":a": True, "local:x": True})
        self.record("work", "s1", title="older", activity=BASE_MS, bridgeSessionIds=["w-bridge"],
                    mcpToolsAskedOff=["w"],
                    enabledMcpTools={OTHER_CONNECTOR + ":b": True, "local:x": False})
        r = self.sync()
        self.assertEqual((r["updated"], r["added"]), (1, 1))   # work updated, personal-team added
        got = self.load("work", "s1")
        self.assertEqual(got["title"], "newer")
        self.assertEqual(got["bridgeSessionIds"], ["w-bridge"])
        self.assertEqual(got["mcpToolsAskedOff"], ["w"])
        self.assertEqual(got["enabledMcpTools"], {"local:x": True, OTHER_CONNECTOR + ":b": True})
        again = self.sync()
        self.assertEqual((again["updated"], again["added"]), (0, 0))

    # Set by the app when it quits during a turn, so it resumes that turn at its next
    # launch. A copy in another folder would make another account resume it again.
    MARKERS = {"interruptedByQuitAt": BASE_MS + 60000, "interruptedAwaitingPerson": False,
               "interruptedUnseenResume": True, "interruptedByQuitFor": "",
               "remoteControlLiveAtUpdateQuit": {"accountId": "x"}, "armedWorkAtQuit": {"turn": 1}}

    def test_quit_resume_markers_never_travel(self):
        self.record("personal", "s1", **self.MARKERS)
        r = self.sync()
        self.assertEqual((r["added"], r["errors"]), (2, []))
        for account in ("work", "personal-team"):   # another account, and its own other org
            got = self.load(account, "s1")
            self.assertEqual([k for k in self.MARKERS if k in got], [], account)
        self.assertEqual(self.load("personal", "s1")["interruptedByQuitAt"], BASE_MS + 60000)
        again = self.sync()   # copies that differ only in markers say the same thing
        self.assertEqual((again["updated"], again["added"]), (0, 0))

    def test_an_update_keeps_the_targets_own_markers(self):
        self.record("personal", "s1", title="newer", activity=BASE_MS + 50,
                    interruptedByQuitAt=BASE_MS + 1)
        self.record("work", "s1", title="older", interruptedByQuitAt=BASE_MS + 2)
        self.record("personal-team", "s1", title="older", interruptedByQuitAt=BASE_MS + 3)
        r = self.sync()
        self.assertEqual((r["updated"], r["errors"]), (2, []))
        for account, mark in (("work", BASE_MS + 2), ("personal-team", BASE_MS + 3)):
            got = self.load(account, "s1")
            self.assertEqual((got["title"], got["interruptedByQuitAt"]), ("newer", mark))


class UntouchedFilesTest(Base):
    OTHER = {"archived-sessions.idx", "scheduled-tasks.json", "backlog", "waiting-input",
             "item.json", "w.json", "notes.txt", "local_notes.txt"}

    def test_other_app_files_stay_byte_identical(self):
        for d in self.dirs.values():
            (d / "archived-sessions.idx").write_text('{"v":1,"archived":["local_s1"]}')
            (d / "scheduled-tasks.json").write_text(json.dumps(
                {"scheduledTasks": [{"id": "t", "enabled": True}], "recordedSkips": {}}))
            (d / "backlog").mkdir()
            (d / "backlog" / "item.json").write_text("{}")
            (d / "waiting-input").mkdir()
            (d / "waiting-input" / "w.json").write_text("{}")
            (d / "notes.txt").write_text("keep")
            (d / "local_notes.txt").write_text("keep too")
        now = time.time()
        self.record("personal", "s1")                                      # add
        self.record("personal", "s2", title="new", mtime=now)              # update
        self.record("work", "s2", title="old", mtime=now - 100)
        (self.dirs["work"] / "deleted_s3").write_text("1760000000123")     # retire
        self.record("personal", "s3")

        def others():
            return {k: v for k, v in snapshot(self.sessions).items()
                    if os.path.basename(k) in self.OTHER}

        before = others()
        self.assertEqual(len(before), 16)
        r = self.sync()
        self.assertEqual((r["added"], r["updated"], r["retired"]), (1, 1, 1))
        self.assertEqual(others(), before)


class DryRunTest(Base):

    def test_dry_run_writes_nothing(self):
        now = time.time()
        self.record("personal", "s1")
        self.record("personal", "s2", title="new", activity=BASE_MS + 10, mtime=now - 100)
        self.record("work", "s2", title="old", mtime=now - 50)
        (self.dirs["personal"] / "deleted_s3").write_text("1760000000999")
        self.record("work", "s3")
        before = snapshot(self.tmp)
        dry = self.sync(dry_run=True)
        self.assertEqual(snapshot(self.tmp), before)
        self.assertFalse(self.state.exists())
        self.assertTrue(dry["dry_run"])
        self.assertEqual((dry["added"], dry["updated"], dry["retired"], dry["backup"]),
                         (1, 1, 1, None))
        rc, out = self.run_cli("--dry-run")
        self.assertIn("Desktop chats (app not running) - dry run, nothing written", out)
        self.assertIn("  personal -> work: 1 to add, 1 to update, 1 deleted to move to the trash",
                      out.splitlines())
        self.assertEqual(snapshot(self.tmp), before)
        real = self.sync()
        self.assertEqual((real["added"], real["updated"], real["retired"]), (1, 1, 1))
        self.assertEqual(dry["actions"], real["actions"])


class BackupTest(Base):

    def test_first_write_makes_one_backup(self):
        s1 = self.record("personal", "s1")
        self.record("work", "s2")
        (self.dirs["work"] / "deleted_s9").write_text("1760000000123")
        (self.dirs["personal"] / "scheduled-tasks.json").write_text("{}")   # not a chat file
        r = self.sync()
        backups = self.state / "desktop-sync" / "backups"
        tarballs = sorted(backups.glob("*.tar.gz"))
        self.assertEqual(len(tarballs), 1)
        self.assertEqual(r["backup"], str(tarballs[0]))
        self.assertRegex(tarballs[0].name, r"^\d{8}-\d{6}\.tar\.gz$")
        with tarfile.open(str(tarballs[0])) as tar:
            names = sorted(tar.getnames())
            self.assertEqual(tar.extractfile("%s/%s/local_s1.json" % (P_ACCT, P_ORG)).read(),
                             s1.read_bytes())
        self.assertEqual(names, sorted(["%s/%s/local_s1.json" % (P_ACCT, P_ORG),
                                        "%s/%s/local_s2.json" % (W_ACCT, W_ORG),
                                        "%s/%s/deleted_s9" % (W_ACCT, W_ORG)]))
        self.assertEqual(stat.S_IMODE(tarballs[0].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(backups.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.state / "desktop-sync").stat().st_mode), 0o700)
        self.record("personal", "s3")
        r = self.sync()
        self.assertEqual((r["added"], r["backup"]), (1, None))
        self.assertEqual(len(list(backups.glob("*.tar.gz"))), 1)

    def test_no_backup_until_something_is_written(self):
        now = time.time()
        self.record("personal", "s1", mtime=now)
        self.record("work", "s1", mtime=now)
        r = self.sync()
        self.assertEqual((r["added"], r["updated"], r["backup"]), (0, 0, None))
        self.assertEqual(list((self.state / "desktop-sync").glob("backups/*.tar.gz")), [])


class TrashTest(Base):

    def test_keeps_the_two_newest_copies_and_drops_old_ones(self):
        now = int(time.time())
        d = self.state / "desktop-sync" / "trash" / W_ACCT
        d.mkdir(parents=True)
        names = ["local_x.json.%d" % (now - 300), "local_x.json.%d" % (now - 200),
                 "local_x.json.%d-2" % (now - 200), "local_y.json.%d" % (now - 31 * 86400),
                 "local_z.json.%d" % (now - 29 * 86400), "notes.txt"]
        for n in names:
            (d / n).write_text(n)
        self.sync()   # any real run prunes the trash
        self.assertEqual(sorted(p.name for p in d.iterdir()),
                         sorted(["local_x.json.%d" % (now - 200), "local_x.json.%d-2" % (now - 200),
                                 "local_z.json.%d" % (now - 29 * 86400), "notes.txt"]))

    def test_repeated_updates_keep_two_copies(self):
        self.record("work", "s1", title="v0", activity=BASE_MS)
        for n in (1, 2, 3):
            self.record("personal", "s1", title="v%d" % n, activity=BASE_MS + n)
            self.assertEqual(self.sync()["updated"], 1)
        trashed = self.trash(W_ACCT)
        self.assertEqual(len(trashed), 2)
        titles = sorted(json.loads(p.read_text())["title"] for p in trashed)
        self.assertEqual(titles, ["v1", "v2"])


class LockTest(Base):

    def test_busy_lock_skips_the_run(self):
        self.record("personal", "s1")
        lock_dir = self.state / "desktop-sync"
        lock_dir.mkdir(parents=True)
        with open(str(lock_dir / ".lock"), "a+") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            self.assertEqual(self.sync(), {"skipped": "another sync is running"})
            rc, out = self.run_cli()
            self.assertEqual((rc, out.strip()), (0, "Desktop chats - skipped, another sync is running"))
            rc, out = self.run_cli("--quiet")
            self.assertEqual((rc, out), (0, ""))
            self.assertFalse(self.has("work", "s1"))
        self.assertEqual(self.sync()["added"], 1)


class ResolveTest(Base):

    def test_folders_from_claude_json_and_busiest_org_never_shared_claude_json(self):
        root = self.sessions
        # a: its own .claude.json names account and org. A busier org folder loses.
        a_dir = self.tmp / "cfg-a"
        a_dir.mkdir()
        (a_dir / ".claude.json").write_text(json.dumps(
            {"oauthAccount": {"accountUuid": "acct-a", "organizationUuid": "org-a"}}))
        (root / "acct-a" / "org-a").mkdir(parents=True)
        (root / "acct-a" / "org-busy").mkdir()
        (root / "acct-a" / "org-busy" / "local_x.json").write_text("{}")
        # b: only desktop_account_uuid. The org folder with the most records wins.
        (root / "acct-b" / "org-empty").mkdir(parents=True)
        (root / "acct-b" / "org-real").mkdir()
        for sid in ("x", "y"):
            (root / "acct-b" / "org-real" / ("local_%s.json" % sid)).write_text("{}")
        # c: no folder of its own. The shared ~/.claude.json (here naming b) is not used.
        (self.home / ".claude.json").write_text(json.dumps(
            {"oauthAccount": {"accountUuid": "acct-b", "organizationUuid": "org-real"}}))
        # d: an own folder whose .claude.json is only a link to the shared one.
        d_dir = self.tmp / "cfg-d"
        d_dir.mkdir()
        os.symlink(str(self.home / ".claude.json"), str(d_dir / ".claude.json"))
        # e: two empty org folders, f: one empty org folder.
        (root / "acct-e" / "org-1").mkdir(parents=True)
        (root / "acct-e" / "org-2").mkdir()
        (root / "acct-f" / "org-only").mkdir(parents=True)
        cfg = {"default_config_dir": str(self.shared), "accounts": [
            {"name": "a", "config_dir": str(a_dir)},
            {"name": "b", "desktop_account_uuid": "acct-b"},
            {"name": "c", "config_dir": None},
            {"name": "d", "config_dir": str(d_dir)},
            {"name": "e", "desktop_account_uuid": "acct-e"},
            {"name": "f", "desktop_account_uuid": "acct-f"},
            {"name": "g", "desktop_account_uuid": "acct-b", "desktop_org_uuid": "org-real"},
            {"name": "h", "desktop_account_uuid": "acct-missing"},
            {"name": "i", "desktop_account_uuid": "../acct-b"},
            {"name": "j", "desktop_account_uuid": "acct-f", "desktop_org_uuid": "org-missing"},
        ]}
        folders, skipped = ds.resolve_folders(cfg, self.desktop)
        self.assertEqual([(f["account"], f["account_uuid"], f["org_uuid"]) for f in folders],
                         [("a", "acct-a", "org-a"), ("b", "acct-b", "org-real"),
                          ("f", "acct-f", "org-only")])
        reasons = {s["account"]: s["reason"] for s in skipped}
        self.assertEqual(sorted(reasons), ["c", "d", "e", "g", "h", "i", "j"])
        self.assertIn("same desktop folder as b", reasons["g"])
        self.assertIn("desktop_org_uuid", reasons["e"])

    def test_fewer_than_two_folders_does_nothing(self):
        self.cfg["accounts"][1]["desktop_account_uuid"] = "acct-missing"
        self.record("personal", "s1")
        before = snapshot(self.sessions)
        r = self.sync()
        self.assertEqual((r["added"], len(r["folders"]), len(r["skipped_accounts"])), (0, 1, 1))
        self.assertIn("need at least 2", r["note"])
        self.assertEqual(snapshot(self.sessions), before)
        rc, out = self.run_cli()
        self.assertIn("  nothing to do - found chat folders for 1 account, need at least 2",
                      out.splitlines())
        self.assertTrue(any(l.startswith("  skipped work - ") for l in out.splitlines()))
        self.assertEqual(rc, 0)

    def test_no_desktop_app_folder(self):
        shutil.rmtree(str(self.desktop))
        r = self.sync()
        self.assertIn("no desktop app chat folders", r["note"])
        self.assertEqual(r["errors"], [])


class LaunchAgentTest(Base):

    def setUp(self):
        super().setUp()
        fake = self.tmp / "fake_launchctl"
        fake.write_text(FAKE_LAUNCHCTL)
        fake.chmod(0o755)
        self.calls = self.tmp / "launchctl.jsonl"
        os.environ["CLAUDE_FAILOVER_LAUNCHCTL"] = str(fake)
        os.environ["FAKE_LAUNCHCTL_LOG"] = str(self.calls)
        os.environ["FAKE_LAUNCHCTL_CODES"] = json.dumps({"bootout": 3})   # not loaded yet
        raw = {"default_config_dir": str(self.shared), "accounts": self.cfg["accounts"],
               "note": "café ☕", "desktop_sync": {"enabled": False, "interval_minutes": 10}}
        self.cfg_path.write_text(json.dumps(raw, indent=4, ensure_ascii=False), encoding="utf-8")
        os.chmod(str(self.cfg_path), 0o640)
        self.plist = self.agents / (ds.LABEL + ".plist")

    def launchctl_calls(self):
        if not self.calls.exists():
            return []
        return [json.loads(l) for l in self.calls.read_text().splitlines()]

    def test_install_status_uninstall_round_trip(self):
        original = self.cfg_path.read_bytes()
        cfg = cf.load_config()
        self.assertIsNone(ds.status_line(cfg))   # not enabled yet
        rc, out = self.run_cli("install", "--every", "5", cfg=cfg)
        self.assertEqual(rc, 0, out)
        with open(str(self.plist), "rb") as f:
            job = plistlib.load(f)
        log = str(self.state / "desktop-sync" / "agent.log")
        self.assertEqual(job, {
            "Label": ds.LABEL,
            "ProgramArguments": [str(PROJECT / "bin" / "claude-accounts"), "sync", "--quiet"],
            "StartInterval": 300, "RunAtLoad": True,
            "StandardOutPath": log, "StandardErrorPath": log,
            "EnvironmentVariables": {"CLAUDE_FAILOVER_CONFIG": str(self.cfg_path),
                                     "CLAUDE_FAILOVER_STATE_DIR": str(self.state)}})
        uid = os.getuid()
        self.assertEqual(self.launchctl_calls(), [
            ["bootout", "gui/%d/%s" % (uid, ds.LABEL)],
            ["bootstrap", "gui/%d" % uid, str(self.plist)]])
        # The config: enabled, everything else as it was, the old file kept.
        now = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(now["desktop_sync"], {"enabled": True, "interval_minutes": 10})
        self.assertEqual(now["note"], "café ☕")
        self.assertEqual(now["accounts"], self.cfg["accounts"])
        self.assertEqual(stat.S_IMODE(self.cfg_path.stat().st_mode), 0o640)
        baks = sorted(self.tmp.glob("config.json.bak-*"))
        self.assertEqual([b.read_bytes() for b in baks], [original])
        self.assertEqual(stat.S_IMODE(baks[0].stat().st_mode), 0o600)
        self.assertIn("Set desktop_sync.enabled to true", out)
        # status, and the status line once the config says enabled.
        cfg = cf.load_config()
        rc, out = self.run_cli("status", cfg=cfg)
        self.assertEqual(rc, 0)
        self.assertIn("  background job - every 5 min", out)
        self.assertIn("  sync at handoff - on", out.splitlines())
        self.assertIn("  last sync - never", out.splitlines())
        self.assertEqual(ds.status_line(cfg), "Desktop chats - not synced yet (claude-accounts "
                                              "sync), auto-sync every 5 min")
        # uninstall: bootout, only our plist removed, enabled false with another backup.
        other = self.agents / "com.example.other.plist"
        other.write_text("keep")
        rc, out = self.run_cli("uninstall", cfg=cfg)
        self.assertEqual(rc, 0, out)
        self.assertFalse(self.plist.exists())
        self.assertTrue(other.exists())
        self.assertEqual(self.launchctl_calls()[-1], ["bootout", "gui/%d/%s" % (uid, ds.LABEL)])
        self.assertIs(json.loads(self.cfg_path.read_text())["desktop_sync"]["enabled"], False)
        self.assertEqual(len(list(self.tmp.glob("config.json.bak-*"))), 2)
        rc, out = self.run_cli("status", cfg=cf.load_config())
        self.assertIn("  background job - not installed (claude-accounts sync install)",
                      out.splitlines())

    def test_install_uses_the_config_interval_and_says_when_launchctl_fails(self):
        os.environ["FAKE_LAUNCHCTL_CODES"] = json.dumps({"bootstrap": 5})
        rc, out = self.run_cli("install", cfg=cf.load_config())
        self.assertEqual(rc, 1)
        self.assertIn("launchctl could not load it", out)
        with open(str(self.plist), "rb") as f:
            self.assertEqual(plistlib.load(f)["StartInterval"], 600)

    def test_bad_arguments(self):
        cfg = cf.load_config()
        for args in (["install", "--every", "1"], ["install", "--every", "x"],
                     ["install", "--every"], ["install", "now"], ["status", "x"], ["bogus"]):
            rc, out = self.run_cli(*args, cfg=cfg)
            self.assertEqual(rc, 2, args)
        self.assertFalse(self.plist.exists())
        self.assertEqual(self.launchctl_calls(), [])
        rc, out = self.run_cli("help", cfg=cfg)
        self.assertEqual(rc, 0)
        self.assertIn("usage: claude-accounts sync", out)

    def test_a_job_whose_copy_was_moved_is_reported(self):
        self.agents.mkdir(parents=True, exist_ok=True)
        cfg = cf.load_config()
        cfg["desktop_sync"] = {"enabled": True}

        def install(script):
            with open(str(self.plist), "wb") as f:
                plistlib.dump({"Label": ds.LABEL, "StartInterval": 600,
                               "ProgramArguments": [str(script), "sync", "--quiet"]}, f)
        install(self.tmp / "moved-away" / "bin" / "claude-accounts")   # the folder was renamed
        rc, out = self.run_cli("status", cfg=cfg)
        self.assertEqual(rc, 1)
        self.assertIn("  background job - BROKEN: it runs %s, which is missing (folder moved?) - fix: "
                      "claude-accounts sync install" % (self.tmp / "moved-away" / "bin" / "claude-accounts"),
                      out.splitlines())
        self.assertIn("auto-sync broken (run: claude-accounts sync install)", ds.status_line(cfg))
        other = self.tmp / "other" / "bin" / "claude-accounts"   # another copy that exists
        other.parent.mkdir(parents=True)
        other.write_text("#!/bin/sh\n")
        install(other)
        rc, out = self.run_cli("status", cfg=cfg)
        self.assertEqual(rc, 0)
        self.assertIn("from another copy: %s" % other, out)
        self.assertIn("auto-sync every 10 min", ds.status_line(cfg))

    def test_without_a_config_file_prints_what_to_add(self):
        os.environ["CLAUDE_FAILOVER_CONFIG"] = str(self.tmp / "missing.json")
        cfg = dict(self.cfg)
        del cfg["_config_path"]
        rc, out = self.run_cli("install", cfg=cfg)
        self.assertEqual(rc, 0)
        self.assertIn('Add this to your config: "desktop_sync": {"enabled": true}', out)
        with open(str(self.plist), "rb") as f:
            self.assertNotIn("CLAUDE_FAILOVER_CONFIG", plistlib.load(f)["EnvironmentVariables"])


class StatusLineTest(Base):

    def test_lines(self):
        self.assertEqual(ds.status_line({"desktop_sync": {"enabled": False}}), None)
        self.assertEqual(ds.status_line({}), None)
        self.assertEqual(ds.status_line(self.cfg),
                         "Desktop chats - not synced yet (claude-accounts sync), auto-sync off")
        self.record("personal", "s1")
        self.record("work", "s2")
        self.sync()
        self.assertEqual(ds.status_line(self.cfg), "Desktop chats - 2 accounts in sync (2 chats), "
                                                   "last sync just now, auto-sync off")
        last = self.state / "desktop-sync" / "last.json"
        data = json.loads(last.read_text())
        data["report"]["stale_in_running_app"] = 3
        data["at"] = cf.iso(cf.now_local() - dt.timedelta(minutes=4, seconds=5))
        last.write_text(json.dumps(data))
        self.assertEqual(ds.status_line(self.cfg),
                         "Desktop chats - 2 accounts, 2 chats, 3 changes waiting for the app to "
                         "quit, last sync 4 min ago, auto-sync off")
        data["report"]["errors"] = ["boom"]
        last.write_text(json.dumps(data))
        self.assertIn("had 1 error", ds.status_line(self.cfg))
        last.write_text("not json")
        self.assertIn("not synced yet", ds.status_line(self.cfg))

    def test_never_raises(self):
        with mock.patch.object(ds, "read_last", side_effect=RuntimeError("boom")):
            self.assertIsNone(ds.status_line(self.cfg))


class CliTest(Base):

    def test_text_output(self):
        self.record("personal", "s1")
        self.record("work", "s2")
        self.record("work", "s3", transcript=False)
        rc, out = self.run_cli()
        lines = out.splitlines()
        self.assertEqual(rc, 0)
        self.assertEqual(lines[0], "Desktop chats (app not running)")
        self.assertEqual(lines[1:3], ["  personal  1 chat", "  work      2 chats"])
        self.assertIn("  personal -> work: 1 added", lines)
        self.assertIn("  work -> personal: 1 added", lines)
        self.assertIn("  skipped - 1 with no transcript on disk", lines)
        self.assertTrue(any(l.startswith("  backup - the chat folders as they were: ")
                            for l in lines))

    def test_json_output(self):
        self.record("personal", "s1")
        rc, out = self.run_cli("--json")
        r = json.loads(out)
        self.assertEqual(rc, 0)
        self.assertTrue(REPORT_KEYS <= set(r), REPORT_KEYS - set(r))
        self.assertEqual(set(r["folders"][0]), {"account", "account_uuid", "org_uuid", "path",
                                                "chats", "live"})
        self.assertEqual(set(r["actions"][0]), {"action", "session", "title", "from", "to"})

    def test_quiet_prints_nothing_without_errors(self):
        self.record("personal", "s1")
        rc, out = self.run_cli("--quiet")
        self.assertEqual((rc, out), (0, ""))
        self.assertTrue(self.has("work", "s1"))

    @unittest.skipIf(os.geteuid() == 0, "root ignores folder permissions")
    def test_errors_exit_1_and_quiet_prints_them(self):
        self.record("personal", "s1")
        os.chmod(str(self.dirs["work"]), 0o500)   # the app folder can't be written
        try:
            rc, out = self.run_cli("--quiet")
        finally:
            os.chmod(str(self.dirs["work"]), 0o755)
        self.assertEqual(rc, 1)
        self.assertIn("  error - could not add local_s1.json to work", out)
        self.assertTrue(out.startswith("claude-accounts sync at "))
        self.no_temp_files()


class ScheduledTasksTest(Base):

    def test_heads_up_for_tasks_of_accounts_not_signed_in(self):
        tasks = self.dirs["personal"] / "scheduled-tasks.json"
        tasks.write_text(json.dumps({"scheduledTasks": [
            {"id": "t1", "displayName": "Weekly report", "enabled": True, "cronExpression": "0 9 * * 1"},
            {"id": "t2", "displayName": "Paused", "enabled": False, "fireAt": BASE_MS},
            {"id": "t3", "name": "nightly-cleanup", "enabled": True},
            {"id": "t4-only-id", "enabled": True},
        ], "recordedSkips": {"t1": []}}))
        (self.dirs["work"] / "scheduled-tasks.json").write_text(json.dumps(
            {"scheduledTasks": [{"id": "w1", "displayName": "Work task", "enabled": True}]}))
        before = tasks.read_bytes()
        self.sign_in(W_ACCT, running=False)
        r = self.sync()
        self.assertEqual(r["inactive_scheduled_tasks"], [
            {"account": "personal", "task": "Weekly report"},
            {"account": "personal", "task": "nightly-cleanup"},
            {"account": "personal", "task": "t4-only-id"}])
        rc, out = self.run_cli()
        self.assertIn("  heads-up - 3 scheduled tasks only run while the app is signed in as "
                      "personal: Weekly report, nightly-cleanup, t4-only-id", out.splitlines())
        self.assertEqual(tasks.read_bytes(), before)


class AppDetectionTest(unittest.TestCase):

    def setUp(self):
        self._saved = os.environ.pop("CLAUDE_FAILOVER_APP_RUNNING", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["CLAUDE_FAILOVER_APP_RUNNING"] = self._saved

    def ps(self, text, rc=0):
        return subprocess.CompletedProcess(["/bin/ps"], rc, stdout=text.encode())

    def test_the_app_executable_counts(self):
        out = "/usr/sbin/cfprefsd\n/Applications/Claude.app/Contents/MacOS/Claude\n"
        with mock.patch.object(ds.subprocess, "run", return_value=self.ps(out)):
            self.assertTrue(ds.app_running())

    def test_helpers_alone_do_not_count(self):
        out = ("/Applications/Claude.app/Contents/Frameworks/Claude Helper.app/Contents/MacOS/"
               "Claude Helper\n/usr/bin/claude\n")
        with mock.patch.object(ds.subprocess, "run", return_value=self.ps(out)):
            self.assertFalse(ds.app_running())

    def test_unknown_counts_as_running(self):
        with mock.patch.object(ds.subprocess, "run", side_effect=OSError("no ps")):
            self.assertTrue(ds.app_running())
        with mock.patch.object(ds.subprocess, "run", return_value=self.ps("", rc=1)):
            self.assertTrue(ds.app_running())

    def test_environment_decides_in_tests(self):
        os.environ["CLAUDE_FAILOVER_APP_RUNNING"] = "0"
        with mock.patch.object(ds.subprocess, "run", side_effect=AssertionError("ps was called")):
            self.assertFalse(ds.app_running())
        os.environ.pop("CLAUDE_FAILOVER_APP_RUNNING")


class HookTest(Base):

    def test_sync_quietly_never_raises(self):
        with mock.patch.object(ds, "sync", side_effect=RuntimeError("boom")):
            self.assertIsNone(ds.sync_quietly(self.cfg))
        self.record("personal", "s1")
        ds.sync_quietly(self.cfg)
        self.assertTrue(self.has("work", "s1"))


if __name__ == "__main__":
    unittest.main()
