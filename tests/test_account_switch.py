"""Tests for account_switch.py against temp folders shaped like the real ones.

Nothing here touches the real ~/Library, ~/.claude, Chrome profile or launchctl,
and no claude is ever started: HOME, the config, the state folder, the desktop
app's data folder, the Chrome folder and the LaunchAgents folder all point into a
temp dir, usage readings are written by hand or come from a fake reader, and
notifications are caught.

Run:  /usr/bin/python3 -m unittest discover -s tests -p 'test_account_switch.py' -v
"""
import contextlib
import datetime as dt
import io
import json
import os
import plistlib
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import claude_failover as cf  # noqa: E402
import account_switch as asw  # noqa: E402

P_ACCT = "aaaaaaaa-0000-4000-8000-000000000001"   # "personal"
W_ACCT = "bbbbbbbb-0000-4000-8000-000000000002"   # "work"
X_ACCT = "cccccccc-0000-4000-8000-000000000003"   # an account that is not in the config

WEEK = {"status": "allowed_warning", "resetsAt": 1795924800, "rateLimitType": "seven_day",
        "utilization": 0.83, "overageStatus": "allowed", "isUsingOverage": False,
        "surpassedThreshold": 0.75, "overageEnabled": True,
        "unifiedWindows": {"seven_day": {"utilization": 0.83, "resetsAt": 1795924800,
                                         "status": "allowed_warning"}}}
IDLE = {"status": "allowed", "resetsAt": 1795672800, "rateLimitType": "seven_day",
        "overageStatus": "rejected", "overageDisabledReason": "org_level_disabled",
        "isUsingOverage": False,
        "unifiedWindows": {"seven_day": {"utilization": 0, "resetsAt": 1795672800,
                                         "status": "allowed"}}}


def vint(n):
    out = bytearray()
    while True:
        byte, n = n & 0x7f, n >> 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def table_block(entries, restart_every=16):
    """A LevelDB table block: keys prefix-compressed, a restart point every few keys."""
    out, restarts, prev = bytearray(), [], b""
    for i, (key, value) in enumerate(entries):
        shared = 0
        if i % restart_every == 0:
            restarts.append(len(out))
        else:
            while shared < min(len(prev), len(key)) and prev[shared] == key[shared]:
                shared += 1
        out += vint(shared) + vint(len(key) - shared) + vint(len(value)) + key[shared:] + value
        prev = key
    for r in restarts or [0]:
        out += r.to_bytes(4, "little")
    out += len(restarts or [0]).to_bytes(4, "little")
    return bytes(out)


def snappy_literals(data):
    """A valid Snappy stream that holds the data as literal runs."""
    out = bytearray(vint(len(data)))
    for i in range(0, len(data), 60):
        chunk = data[i:i + 60]
        out.append((len(chunk) - 1) << 2)
        out += chunk
    return bytes(out)


def make_table(versions, compress=False, blocks=1, restart_every=16):
    """A table file from [(user key, sequence, kind, value)]; kind 1 = value, 0 = removal."""
    entries = sorted(((k + ((seq << 8) | kind).to_bytes(8, "little"), v) for k, seq, kind, v in versions),
                     key=lambda e: (e[0][:-8], -int.from_bytes(e[0][-8:], "little")))
    out, index = bytearray(), []
    per = max(1, -(-len(entries) // blocks))
    for i in range(0, len(entries), per):
        part = entries[i:i + per]
        raw = table_block(part, restart_every)
        body = snappy_literals(raw) if compress else raw
        index.append((part[-1][0], vint(len(out)) + vint(len(body))))
        out += body + (b"\x01" if compress else b"\x00") + b"\x00" * 4
    meta = table_block([])
    meta_at = len(out)
    out += meta + b"\x00" * 5
    idx = table_block(index, 1)
    idx_at = len(out)
    out += idx + b"\x00" * 5
    footer = vint(meta_at) + vint(len(meta)) + vint(idx_at) + vint(len(idx))
    return bytes(out) + footer + b"\x00" * (40 - len(footer)) + asw.TABLE_MAGIC


def frame(records):
    """LevelDB's log format: records cut into fragments at 32 KB blocks."""
    out = bytearray()
    for rec in records:
        first = True
        while True:
            left = 32768 - len(out) % 32768
            if left < 7:
                out += b"\x00" * left
                continue
            piece, rec = rec[:left - 7], rec[left - 7:]
            kind = (1 if not rec else 2) if first else (4 if not rec else 3)
            out += b"\x00" * 4 + len(piece).to_bytes(2, "little") + bytes([kind]) + piece
            first = False
            if not rec:
                break
    return bytes(out)


def batch(sequence, ops):
    """A write batch from [(kind, key, value)]."""
    rec = sequence.to_bytes(8, "little") + len(ops).to_bytes(4, "little")
    for kind, key, value in ops:
        rec += bytes([kind]) + vint(len(key)) + key
        if kind == 1:
            rec += vint(len(value)) + value
    return rec


def make_manifest(tables, log_number, dropped=()):
    name = b"leveldb.BytewiseComparator"
    rec = vint(1) + vint(len(name)) + name + vint(2) + vint(log_number) + vint(9) + vint(0)
    rec += vint(3) + vint(999) + vint(4) + vint(5000)
    rec += vint(5) + vint(1) + vint(3) + b"abc"
    for n in dropped:
        rec += vint(7) + vint(0) + vint(n) + vint(10) + vint(1) + b"a" + vint(1) + b"z"
    edit2 = b"".join(vint(6) + vint(0) + vint(n) for n in dropped)
    for n in tables:
        edit2 += vint(7) + vint(1) + vint(n) + vint(4096) + vint(1) + b"a" + vint(1) + b"z"
    return frame([rec, edit2])


KEY = b"accountUuid"


def version(seq, what):
    """(key, sequence, kind, value) for the account key: an id, None (removed) or raw bytes."""
    if what is None:
        return (KEY, seq, 0, b"")
    return (KEY, seq, 1, what if isinstance(what, bytes) else json.dumps(what).encode())


class Base(unittest.TestCase):
    """Two accounts, "personal" and "work"; the desktop app starts on personal."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="asw-test-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.desktop = self.tmp / "desktop"
        self.desktop.mkdir()
        self.chrome = self.tmp / "chrome"
        self.state = self.tmp / "state"
        self.agents = self.tmp / "LaunchAgents"
        self.cfg_path = self.tmp / "config.json"
        self._saved_env = dict(os.environ)
        os.environ.update({
            "HOME": str(self.home),
            "CLAUDE_FAILOVER_CONFIG": str(self.cfg_path),
            "CLAUDE_FAILOVER_STATE_DIR": str(self.state),
            "CLAUDE_FAILOVER_DESKTOP_DIR": str(self.desktop),
            "CLAUDE_FAILOVER_CHROME_DIR": str(self.chrome),
            "CLAUDE_FAILOVER_APP_RUNNING": "0",
            "CLAUDE_FAILOVER_LAUNCHCTL": "0",
            "CLAUDE_FAILOVER_LAUNCH_AGENTS_DIR": str(self.agents),
            "CLAUDE_FAILOVER_NO_NOTIFY": "1",
            "CLAUDE_FAILOVER_MEMGUARD": "0",
            "CLAUDE_FAILOVER_SWITCH_POLL": "0.02",
        })
        self.raw = {
            "real_claude": "/bin/echo",
            "default_config_dir": str(self.tmp / "dot-claude"),
            "accounts": [
                {"name": "personal", "email": "personal@example.com", "desktop_account_uuid": P_ACCT},
                {"name": "work", "email": "work@example.com", "desktop_account_uuid": W_ACCT},
            ],
            "order": {"interactive": ["personal", "work"], "headless": ["personal", "work"]},
            "desktop_sync": {"enabled": False},
            "keep_me": {"untouched": [1, 2, 3]},
        }
        self.save_cfg()
        self.sign_in(P_ACCT)
        self.now = cf.now_local()
        self.sent = []
        patcher = mock.patch.object(cf, "notify", side_effect=lambda t, m: self.sent.append((t, m)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    # -- fixtures --

    def save_cfg(self, **switch):
        if switch:
            self.raw["switch"] = dict(self.raw.get("switch") or {}, **switch)
        self.cfg_path.write_text(json.dumps(self.raw, indent=2) + "\n")

    def cfg(self):
        return cf.load_config()

    def on_disk(self):
        return json.loads(self.cfg_path.read_text())

    def sign_in(self, account_uuid):
        (self.desktop / "config.json").write_text(json.dumps({"lastKnownAccountUuid": account_uuid}))

    def usage(self, name, util, reset_h=72.0, age_min=0.0, **more):
        """Write a usage reading by hand: util used, its week resetting in reset_h hours."""
        now = cf.now_local()
        rec = {"at": cf.iso(now - dt.timedelta(minutes=age_min)), "ok": True, "util": util,
               "window": "seven_day", "status": "allowed"}
        if reset_h is not None:
            resets = cf.iso(now + dt.timedelta(hours=reset_h))
            rec.update({"resets_at": resets, "week_util": util, "week_resets_at": resets})
        rec.update(more)
        with asw.edit_state() as st:
            st["usage"][name] = rec
        return rec

    def limit(self, name, hours=10.0):
        with cf.edit_state() as st:
            st["accounts"].setdefault(name, {})["limited_until"] = cf.iso(
                cf.now_local() + dt.timedelta(hours=hours))

    def ext_folder(self, profile="Default"):
        d = self.chrome / profile / "Local Extension Settings" / asw.EXTENSION_ID
        d.mkdir(parents=True, exist_ok=True)
        return d

    def ext(self, tables=None, logs=None, profile="Default", live=None, log_number=None,
            compress=False, manifest=True, restart_every=16):
        """Write the extension's storage folder as real LevelDB files. tables and logs
        map a file number to the versions of the account key in that file:
        [(sequence, account id | None for a removal | raw value bytes)]. Every table
        also holds neighbouring keys that must never be mistaken for the key."""
        d = self.ext_folder(profile)
        for old in d.iterdir():
            old.unlink()
        tables, logs = tables or {}, logs or {}
        noise = [(b"accountEmail", 1, 1, b'"p@example.com"'),
                 (b"accountUuidOld", 2, 1, json.dumps(X_ACCT).encode()),
                 (b"profile", 3, 1, json.dumps({"accountUuid": X_ACCT}).encode()),
                 (b"zeta", 4, 1, b"true")]
        for number, versions in tables.items():
            (d / ("%06d.ldb" % number)).write_bytes(make_table(
                noise + [version(seq, what) for seq, what in versions],
                compress=compress, restart_every=restart_every))
        for number, versions in logs.items():
            self.ext_log(number, versions, profile)
        if manifest:
            if log_number is None:
                log_number = min(logs) if logs else (max(tables) + 1 if tables else 1)
            (d / "MANIFEST-000002").write_bytes(make_manifest(
                sorted(tables) if live is None else live, log_number))
            (d / "CURRENT").write_text("MANIFEST-000002\n")
        return d

    def ext_log(self, number, versions, profile="Default"):
        """A write-ahead log: one batch per version, each next to an unrelated write."""
        records = []
        for seq, what in versions:
            _, _, kind, value = version(seq, what)
            records.append(batch(seq - 1, [(1, b"profile", json.dumps({"accountUuid": X_ACCT}).encode()),
                                           (kind, KEY, value)]))
        path = self.ext_folder(profile) / ("%06d.log" % number)
        path.write_bytes(frame(records))
        return path

    def run_cli(self, cmd, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = asw.cli(self.cfg(), cmd, list(args))
        return rc, out.getvalue()

    def snap(self, **kw):
        return asw.snapshot(self.cfg(), **kw)


class ParseRateInfoTest(unittest.TestCase):
    def test_week_window_with_extra_usage(self):
        r = asw.parse_rate_info(WEEK)
        self.assertEqual((r["util"], r["window"], r["week_util"]), (0.83, "seven_day", 0.83))
        self.assertTrue(r["extra"])
        self.assertFalse(r["extra_in_use"])
        self.assertEqual(cf.parse_iso(r["week_resets_at"]).timestamp(), 1795924800)

    def test_unused_account(self):
        r = asw.parse_rate_info(IDLE)
        self.assertEqual((r["util"], r["week_util"], r["extra"]), (0.0, 0.0, False))

    def test_rejected_means_full(self):
        info = {"status": "rejected", "rateLimitType": "seven_day", "resetsAt": 1795924800,
                "isUsingOverage": False}
        r = asw.parse_rate_info(info)
        self.assertEqual((r["util"], r["window"]), (1.0, "seven_day"))
        self.assertEqual(cf.parse_iso(r["resets_at"]).timestamp(), 1795924800)

    def test_fullest_window_wins(self):
        info = {"status": "allowed", "unifiedWindows": {
            "five_hour": {"utilization": 0.95, "resetsAt": 1795000000, "status": "allowed_warning"},
            "seven_day": {"utilization": 0.4, "resetsAt": 1795924800, "status": "allowed"}}}
        r = asw.parse_rate_info(info)
        self.assertEqual((r["util"], r["window"], r["week_util"]), (0.95, "five_hour", 0.4))

    def test_refused_window_beats_a_fuller_looking_one(self):
        info = {"status": "allowed", "unifiedWindows": {
            "five_hour": {"utilization": 0.2, "resetsAt": 1795000000, "status": "rejected"},
            "seven_day": {"utilization": 0.9, "resetsAt": 1795924800, "status": "allowed"}}}
        r = asw.parse_rate_info(info)
        self.assertEqual((r["util"], r["window"]), (1.0, "five_hour"))

    def test_extra_usage_in_use(self):
        info = dict(WEEK, isUsingOverage=True)
        self.assertTrue(asw.parse_rate_info(info)["extra_in_use"])

    def test_nothing_usable(self):
        self.assertIsNone(asw.parse_rate_info(None))
        self.assertIsNone(asw.parse_rate_info("x"))
        self.assertIsNone(asw.parse_rate_info({"status": "allowed"}))
        self.assertIsNone(asw.parse_rate_info({"status": "allowed", "rateLimitType": "seven_day",
                                               "utilization": True}))


class WhoIsSignedInTest(Base):
    def test_app_account_by_id_any_letter_case(self):
        self.sign_in(W_ACCT.upper())
        self.assertEqual(asw.app_account(self.cfg()), "work")

    def test_unknown_or_missing(self):
        self.sign_in(X_ACCT)
        self.assertIsNone(asw.app_account(self.cfg()))
        (self.desktop / "config.json").unlink()
        self.assertIsNone(asw.app_account(self.cfg()))
        (self.desktop / "config.json").write_text("not json")
        self.assertIsNone(asw.app_account(self.cfg()))

    def test_id_from_the_accounts_own_claude_json(self):
        own = self.tmp / "work-dir"
        own.mkdir()
        (own / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": W_ACCT}}))
        self.raw["accounts"][1] = {"name": "work", "config_dir": str(own)}
        self.save_cfg()
        self.assertEqual(asw.account_uuids(self.cfg())["work"], W_ACCT)

    def test_two_folders_on_one_login_name_neither(self):
        self.raw["accounts"][1]["desktop_account_uuid"] = P_ACCT
        self.save_cfg()
        self.assertIsNone(asw.app_account(self.cfg()))


class ExtensionTest(Base):
    def answer(self):
        return asw.extension_account(self.cfg())

    def test_unknown_without_storage(self):
        self.assertEqual(self.answer(), {"state": "unknown"})

    def test_value_in_a_table(self):
        self.ext(tables={5: [(10, P_ACCT)]})
        self.assertEqual(self.answer()["account"], "personal")

    def test_prefix_compressed_key_is_found(self):
        # "accountEmail" sits right before the key, so only "Uuid..." is stored for it.
        d = self.ext(tables={5: [(10, W_ACCT)]})
        self.assertNotIn(b"accountUuid", (d / "000005.ldb").read_bytes().split(b"accountEmail")[1][:40])
        self.assertEqual(self.answer()["account"], "work")

    def test_snappy_compressed_table(self):
        self.ext(tables={5: [(10, W_ACCT)]}, compress=True)
        self.assertEqual(self.answer()["account"], "work")

    def test_snappy_copies(self):
        # "abc" as a literal, then a copy of 9 bytes from 3 back (it overlaps itself)
        self.assertEqual(asw._snappy(bytes([12, 2 << 2]) + b"abc" + bytes([(5 << 2) | 1, 3])),
                         b"abcabcabcabc")
        long = bytes(range(256)) * 2
        packed = asw.__dict__["_snappy"](snappy_literals(long))
        self.assertEqual(packed, long)
        with self.assertRaises(asw._Unreadable):
            asw._snappy(bytes([12, 2 << 2]) + b"abc" + bytes([(5 << 2) | 1, 9]))   # copy from before the start

    def test_highest_sequence_wins_not_the_file_number(self):
        self.ext(tables={5: [(30, W_ACCT)], 9: [(20, P_ACCT)]})
        self.assertEqual(self.answer()["account"], "work")

    def test_log_is_newer_than_the_table(self):
        self.ext(tables={5: [(10, P_ACCT)]}, logs={7: [(20, P_ACCT), (30, W_ACCT)]})
        self.assertEqual(self.answer()["account"], "work")

    def test_removed_in_the_log(self):
        self.ext(tables={5: [(10, P_ACCT)]}, logs={7: [(20, W_ACCT), (30, None)]})
        self.assertEqual(self.answer(), {"state": "signed_out"})

    def test_removed_in_a_newer_table(self):
        # Signed out, then Chrome restarted: the removal now lives in a table file.
        self.ext(tables={5: [(10, P_ACCT)], 8: [(40, None)]}, logs={9: []})
        self.assertEqual(self.answer(), {"state": "signed_out"})

    def test_never_signed_in(self):
        self.ext(tables={5: []}, logs={7: []})
        self.assertEqual(self.answer(), {"state": "signed_out"})

    def test_account_outside_the_config(self):
        self.ext(logs={7: [(20, X_ACCT)]})
        self.assertEqual(self.answer(), {"state": "other", "uuid": X_ACCT})

    def test_neighbouring_keys_and_json_values_are_not_the_key(self):
        # Every fixture table holds accountUuidOld and a JSON value with "accountUuid"
        # in it, both naming an account outside the config. Neither may be read.
        self.ext(tables={5: [(10, P_ACCT)]})
        self.assertEqual(self.answer()["account"], "personal")
        self.ext(tables={5: []})
        self.assertEqual(self.answer(), {"state": "signed_out"})

    def test_files_the_database_dropped_are_ignored(self):
        # 000009.ldb and 000003.log are leftovers the MANIFEST no longer lists.
        self.ext(tables={5: [(10, P_ACCT)], 9: [(99, W_ACCT)]}, logs={3: [(98, W_ACCT)], 7: []},
                 live=[5], log_number=7)
        self.assertEqual(self.answer()["account"], "personal")

    def test_a_write_cut_by_a_block_edge(self):
        d = self.ext(tables={5: [(10, P_ACCT)]}, logs={7: []})
        filler = batch(20, [(1, b"filler", b"x" * (32768 - 7 - 7 - 20 - 12 - 1 - 1 - 6 - 3))])
        ours = batch(30, [(1, KEY, json.dumps(W_ACCT).encode())])
        data = frame([filler, ours])
        self.assertEqual(data[32768 + 6], 4)        # our record continues in the second block
        self.assertNotIn(KEY, data[:32768])
        (d / "000007.log").write_bytes(data)
        self.assertEqual(self.answer()["account"], "work")

    def test_a_torn_tail_is_not_an_error(self):
        d = self.ext(tables={5: [(10, P_ACCT)]}, logs={7: [(20, W_ACCT)]})
        log = d / "000007.log"
        log.write_bytes(log.read_bytes() + frame([batch(30, [(1, KEY, json.dumps(P_ACCT).encode())])])[:-9])
        self.assertEqual(self.answer()["account"], "work")

    def test_anything_odd_is_unknown(self):
        self.ext(tables={5: [(10, P_ACCT)]}, manifest=False)
        self.assertEqual(self.answer(), {"state": "unknown"})
        d = self.ext(tables={5: [(10, P_ACCT)]})
        (d / "000005.ldb").write_bytes(b"not a table at all" * 10)
        self.assertEqual(self.answer(), {"state": "unknown"})
        d = self.ext(tables={5: [(10, P_ACCT)], 6: [(11, P_ACCT)]})
        (d / "000006.ldb").unlink()                 # mid-compaction: a listed table is gone
        self.assertEqual(self.answer(), {"state": "unknown"})
        self.ext(tables={5: [(10, b"12345")]})      # a value that is not an account id
        self.assertEqual(self.answer(), {"state": "unknown"})
        self.ext(tables={5: [(10, b"null")]})
        self.assertEqual(self.answer(), {"state": "signed_out"})
        d = self.ext(tables={5: [(10, P_ACCT)]})
        table = bytearray((d / "000005.ldb").read_bytes())
        table[len(table) // 3] ^= 0xff
        (d / "000005.ldb").write_bytes(bytes(table[:60]) + bytes(table[-48:]))
        self.assertEqual(self.answer(), {"state": "unknown"})

    def test_two_profiles_must_agree(self):
        self.ext(tables={5: [(10, P_ACCT)]}, profile="Default")
        self.ext(tables={5: [(10, P_ACCT)]}, profile="Profile 1")
        self.assertEqual(self.answer()["account"], "personal")
        self.assertTrue(asw.extension_installed(self.cfg()))
        self.ext(tables={5: [(10, W_ACCT)]}, profile="Profile 1")
        self.assertEqual(self.answer(), {"state": "unknown"})      # two answers: no guess
        self.save_cfg(chrome_profile="Profile 1")
        self.assertEqual(self.answer()["account"], "work")
        self.save_cfg(chrome_profile=None)
        # One profile that cannot be read, or is signed out, leaves it open which is in use.
        self.ext(tables={5: [(10, P_ACCT)]}, profile="Profile 1", manifest=False)
        self.assertEqual(self.answer(), {"state": "unknown"})
        self.ext(tables={5: [(10, None)]}, profile="Profile 1")
        self.assertEqual(self.answer(), {"state": "unknown"})
        self.save_cfg(chrome_profile="Default")
        self.assertEqual(self.answer()["account"], "personal")
        self.save_cfg(chrome_profile=None)
        self.ext(tables={5: [(10, None)]}, profile="Default")
        self.assertEqual(self.answer(), {"state": "signed_out"})   # both say so
        self.assertFalse(asw.extension_installed(dict(self.cfg(), switch={"chrome_extension_id": "x" * 32})))

    def test_a_database_that_changes_while_it_is_read_is_unknown(self):
        # Chrome flushed in between: the MANIFEST read first no longer matches.
        d = self.ext(tables={5: [(10, P_ACCT)]}, logs={7: [(20, W_ACCT)]})
        real = asw._live_files
        answers = [real(d), ({5, 8}, 9)]
        with mock.patch.object(asw, "_live_files", side_effect=lambda folder: answers.pop(0)):
            self.assertEqual(self.answer(), {"state": "unknown"})
        self.assertEqual(self.answer()["account"], "work")

    def test_a_damaged_record_in_the_middle_of_a_log_is_unknown(self):
        d = self.ext(tables={5: [(10, P_ACCT)]}, logs={7: []})
        good = batch(20, [(1, KEY, json.dumps(W_ACCT).encode())])
        filler = batch(30, [(1, b"filler", b"x" * 40000)])
        data = bytearray(frame([good, filler, batch(40, [(1, KEY, json.dumps(P_ACCT).encode())])]))
        data[len(frame([good])) + 6] = 9            # a fragment type that does not exist
        (d / "000007.log").write_bytes(bytes(data))
        self.assertEqual(self.answer(), {"state": "unknown"})     # not "work": later writes are lost

    def test_the_real_reader_matches_a_plain_scan(self):
        # get by key agrees with reading every entry of every block
        versions = [(b"k%03d" % i, i + 1, 1, b"v%d" % i) for i in range(200)]
        data = make_table(versions, blocks=7, restart_every=4, compress=True)
        for i in (0, 57, 199):
            self.assertEqual(asw._table_versions(data, b"k%03d" % i), [(i + 1, 1, b"v%d" % i)])
        self.assertEqual(asw._table_versions(data, b"k999"), [])
        self.assertEqual(asw._table_versions(data, b"a"), [])


class ReadingTest(Base):
    def test_probe_due(self):
        now = cf.now_local()
        fresh = self.usage("work", 0.2, age_min=5)
        old = self.usage("work", 0.2, age_min=61)
        self.assertEqual(asw.probe_due(None, now, 60), "never read")
        self.assertIsNone(asw.probe_due(fresh, now, 60))
        self.assertIsNotNone(asw.probe_due(old, now, 60))

    def test_an_account_close_to_its_limit_is_read_more_often(self):
        now = cf.now_local()
        self.assertIsNotNone(asw.probe_due(self.usage("work", 0.8, age_min=25), now, 60))
        self.assertIsNone(asw.probe_due(self.usage("work", 0.5, age_min=25), now, 60))

    def test_full_is_not_read_again_before_its_reset(self):
        now = cf.now_local()
        rec = self.usage("work", 1.0, reset_h=30, age_min=600)
        self.assertIsNone(asw.probe_due(rec, now, 60))
        gone = self.usage("work", 1.0, reset_h=-1, age_min=600)
        self.assertEqual(asw.probe_due(gone, now, 60), "its window has reset")

    def test_a_reading_from_before_a_limit_is_worth_nothing(self):
        # 60% used an hour ago, then the wrapper saw the account hit its limit, and
        # that (guessed) limit has run out since. The old 40% must not come back.
        now = cf.now_local()
        rec = self.usage("work", 0.6, age_min=50)
        with cf.edit_state() as st:
            st["accounts"]["work"] = {"limit_seen": cf.iso(now - dt.timedelta(minutes=40)),
                                      "limited_until": cf.iso(now - dt.timedelta(minutes=10))}
        seen = now - dt.timedelta(minutes=40)
        self.assertEqual(asw._reading(rec, now, seen)[3], False)
        self.assertEqual(asw.probe_due(rec, now, 60, seen), "a limit was seen since the reading")
        w = self.snap()["accounts"]["work"]
        self.assertEqual((w["room"], w["spent"], w["util"]), (None, False, None))
        self.assertTrue(asw.assist_needed(self.cfg(), "work"))      # not known = still needed
        read = mock.Mock(return_value={"at": cf.iso(now), "ok": True, "util": 1.0})
        self.usage("personal", 0.1)
        self.assertEqual(asw.refresh_usage(self.cfg(), read=read), ["work"])

    def test_refresh_reads_what_is_due_and_keeps_it(self):
        self.usage("personal", 0.5, age_min=5)
        seen = []

        def read(cfg, name, now):
            seen.append(name)
            return {"at": cf.iso(now), "ok": True, "util": 0.1}

        self.assertEqual(asw.refresh_usage(self.cfg(), read=read), ["work"])
        self.assertEqual(seen, ["work"])
        self.assertEqual(asw.read_state()["usage"]["work"]["util"], 0.1)
        self.assertEqual(asw.read_state()["usage"]["personal"]["util"], 0.5)

    def test_refresh_never_reads_an_account_that_is_out(self):
        self.usage("personal", 1.0, reset_h=40, age_min=500)   # full until its reset
        self.limit("work")                                     # limited, says the wrapper
        read = mock.Mock(return_value={"at": cf.iso(cf.now_local()), "ok": True, "util": 0.1})
        self.assertEqual(asw.refresh_usage(self.cfg(), force=True, read=read), [])
        read.assert_not_called()

    def test_force_and_max_age(self):
        self.usage("personal", 0.5, age_min=20)
        self.usage("work", 0.1, age_min=3)
        read = mock.Mock(side_effect=lambda cfg, name, now: {"at": cf.iso(now), "ok": True, "util": 0.3})
        self.assertEqual(asw.refresh_usage(self.cfg(), max_age=10, read=read), ["personal"])
        self.assertEqual(asw.refresh_usage(self.cfg(), force=True, read=read), ["personal", "work"])

    def test_tight_memory_puts_a_read_off_but_not_forever(self):
        self.usage("personal", 0.5, age_min=90)
        self.usage("work", 0.1, age_min=200)
        read = mock.Mock(side_effect=lambda cfg, name, now: {"at": cf.iso(now), "ok": True, "util": 0.3})
        with mock.patch.object(asw, "memory_tight", return_value=True):
            self.assertEqual(asw.refresh_usage(self.cfg(), read=read), ["work"])
            # close to its limit: not put off for long
            self.usage("personal", 0.9, age_min=45)
            self.usage("work", 0.1, age_min=45)
            self.assertEqual(asw.refresh_usage(self.cfg(), read=read), ["personal"])
            # a reading that no longer counts (a limit was seen since) is read anyway
            self.usage("personal", 0.5, age_min=90)
            self.usage("work", 0.1, age_min=30)
            with cf.edit_state() as st:
                st["accounts"]["work"] = {"limit_seen": cf.iso(cf.now_local() - dt.timedelta(minutes=5))}
            self.assertEqual(asw.refresh_usage(self.cfg(), read=read), ["work"])

    def test_probe_turns_a_call_into_a_record(self):
        ok = ({"kind": "ok"}, WEEK, "2026-12-06T17:12:46.773Z")
        with mock.patch.object(asw, "run_probe", return_value=ok):
            rec = asw.probe(self.cfg(), "personal")
        self.assertEqual((rec["ok"], rec["util"], rec["extra"]), (True, 0.83, True))
        self.assertIsNotNone(cf.read_state()["accounts"]["personal"].get("last_ok"))
        until = cf.now_local() + dt.timedelta(hours=5)
        limit = ({"kind": "limit", "text": "weekly limit", "resets_at": until}, None, None)
        with mock.patch.object(asw, "run_probe", return_value=limit):
            rec = asw.probe(self.cfg(), "work")
        self.assertEqual((rec["util"], rec["resets_at"]), (1.0, cf.iso(until)))
        self.assertIsNotNone(cf.read_state()["accounts"]["work"].get("limited_until"))

    def test_a_call_that_finds_a_limit_keeps_the_real_reset_and_its_own_reading(self):
        # The refusal names no reset time; the limits report does (four days away).
        resets = int((cf.now_local() + dt.timedelta(days=4)).timestamp())
        info = {"status": "rejected", "rateLimitType": "seven_day", "resetsAt": resets,
                "unifiedWindows": {"seven_day": {"utilization": 1.0, "resetsAt": resets,
                                                 "status": "rejected"}}}
        started = cf.now_local() - dt.timedelta(seconds=120)       # the call took two minutes
        with mock.patch.object(asw, "run_probe",
                               return_value=({"kind": "limit", "text": "weekly limit"}, info, None)):
            rec = asw.probe(self.cfg(), "work", now=started)
        with asw.edit_state() as st:
            st["usage"]["work"] = rec
        crec = cf.read_state()["accounts"]["work"]
        self.assertEqual(cf.parse_iso(crec["limited_until"]).timestamp(), resets)   # not a 30-min guess
        self.assertGreaterEqual(cf.parse_iso(rec["at"]), cf.parse_iso(crec["limit_seen"]))
        w = self.snap()["accounts"]["work"]
        self.assertEqual((w["spent"], w["room"]), (True, 0.0))
        self.assertEqual(w["back"].timestamp(), resets)
        # and once the wrapper's own record is gone, the reading still says "out until then"
        with cf.edit_state() as st:
            st["accounts"]["work"].pop("limited_until")
        w = self.snap()["accounts"]["work"]
        self.assertEqual((w["spent"], w["back"].timestamp()), (True, resets))
        read = mock.Mock(return_value={"at": cf.iso(cf.now_local()), "ok": True, "util": 0.1})
        self.assertEqual(asw.refresh_usage(self.cfg(), force=True, read=read), ["personal"])
        self.assertEqual([c[0][1] for c in read.call_args_list], ["personal"])   # never "work"
        bad = ({"kind": "error", "text": "timed out"}, None, None)
        with mock.patch.object(asw, "run_probe", return_value=bad):
            rec = asw.probe(self.cfg(), "work")
        self.assertEqual((rec["ok"], rec["error"]), (False, "error"))
        self.assertNotIn("util", rec)


class SnapshotTest(Base):
    def test_room_and_reset(self):
        self.usage("personal", 0.87, reset_h=140, extra=True)
        self.usage("work", 0.0, reset_h=70)
        s = self.snap()
        self.assertEqual(s["app"], "personal")
        p, w = s["accounts"]["personal"], s["accounts"]["work"]
        self.assertAlmostEqual(p["room"], 0.13)
        self.assertEqual((w["room"], w["out"], w["spent"], p["extra"]), (1.0, False, False, True))
        self.assertIsNotNone(w["week_resets"])

    def test_old_reading_is_unknown(self):
        self.usage("work", 0.2, age_min=asw.STALE_MINUTES + 5)
        self.assertIsNone(self.snap()["accounts"]["work"]["room"])

    def test_full_stays_known_until_its_reset(self):
        self.usage("work", 1.0, reset_h=20, age_min=900)
        w = self.snap()["accounts"]["work"]
        self.assertEqual((w["room"], w["spent"], w["out"]), (0.0, True, True))
        self.assertIsNotNone(w["back"])
        self.usage("work", 1.0, reset_h=-0.5, age_min=900)       # the week has reset since
        w = self.snap()["accounts"]["work"]
        self.assertEqual((w["room"], w["spent"]), (None, False))

    def test_a_limit_the_wrapper_saw(self):
        self.limit("work", hours=6)
        w = self.snap()["accounts"]["work"]
        self.assertEqual((w["room"], w["limited"], w["spent"], w["out"]), (0.0, True, True, True))
        self.assertAlmostEqual(asw.hours_until(w["back"], cf.now_local()), 6, delta=0.01)

    def test_login_failing_is_out_but_not_spent(self):
        self.usage("work", 0.1)
        with cf.edit_state() as st:
            st["accounts"].setdefault("work", {})["auth_down_until"] = cf.iso(
                cf.now_local() + dt.timedelta(minutes=20))
        s = self.snap()
        w = s["accounts"]["work"]
        self.assertEqual((w["usable"], w["out"], w["spent"]), (False, True, False))
        self.assertIn("login is failing", asw.not_ready(s, "work"))
        self.assertIsNone(asw.pick_target(s, exclude="personal"))

    def test_pick_target_takes_the_most_room(self):
        self.raw["accounts"].append({"name": "third", "desktop_account_uuid": X_ACCT})
        self.save_cfg()
        self.usage("personal", 0.9)
        self.usage("work", 0.6)
        self.usage("third", 0.2)
        self.assertEqual(asw.pick_target(self.snap(), exclude="personal"), "third")


class DesiredOrderTest(Base):
    def order(self, current=("personal", "work")):
        return asw.desired_order(self.snap(), list(current))

    def test_spare_the_apps_account(self):
        self.usage("personal", 0.87, reset_h=140)
        self.usage("work", 0.0, reset_h=70)
        order, reason = self.order()
        self.assertEqual(order, ["work", "personal"])
        self.assertIn("the desktop app is on personal", reason)

    def test_other_account_out_means_the_apps_account(self):
        self.usage("personal", 0.3)
        self.limit("work")
        order, reason = self.order(("work", "personal"))
        self.assertEqual(order, ["personal", "work"])
        self.assertIn("no other account", reason)

    def test_unknown_usage_changes_nothing(self):
        self.usage("personal", 0.3)
        order, reason = self.order()
        self.assertIsNone(order)
        self.assertIn("work", reason)

    def test_week_about_to_reset_goes_first_even_for_the_apps_account(self):
        self.usage("personal", 0.4, reset_h=5)
        self.usage("work", 0.0, reset_h=100)
        order, reason = self.order(("work", "personal"))
        self.assertEqual(order, ["personal", "work"])
        self.assertIn("is unused", reason)

    def test_nearly_empty_expiring_account_is_not_pulled_forward(self):
        self.usage("personal", 0.95, reset_h=5)
        self.usage("work", 0.0, reset_h=100)
        self.assertEqual(self.order()[0], ["work", "personal"])

    def test_app_not_known_means_soonest_reset_first(self):
        self.sign_in(X_ACCT)
        self.usage("personal", 0.1, reset_h=100)
        self.usage("work", 0.1, reset_h=40)
        self.assertEqual(self.order()[0], ["work", "personal"])

    def test_both_out_soonest_back_first(self):
        self.limit("personal", hours=50)
        self.limit("work", hours=5)
        order, reason = self.order()
        self.assertEqual((order, reason), (["work", "personal"], "every account is out"))

    def test_one_account(self):
        self.raw["accounts"] = self.raw["accounts"][:1]
        self.save_cfg()
        self.assertIsNone(self.order(("personal",))[0])


class ApplyOrderTest(Base):
    def setUp(self):
        super().setUp()
        self.usage("personal", 0.87, reset_h=140)
        self.usage("work", 0.0, reset_h=70)

    def test_off_by_default(self):
        self.assertEqual(asw.apply_order(self.cfg(), self.snap()), ([], []))
        self.assertEqual(self.on_disk()["order"]["headless"], ["personal", "work"])

    def test_rewrites_only_the_order(self):
        self.save_cfg(auto_order=True)
        before = self.on_disk()
        cfg = self.cfg()
        lines, problems = asw.apply_order(cfg, self.snap())
        self.assertEqual((len(lines), problems), (2, []))
        after = self.on_disk()
        self.assertEqual(after["order"], {"interactive": ["work", "personal"],
                                          "headless": ["work", "personal"]})
        self.assertEqual({k: v for k, v in after.items() if k != "order"},
                         {k: v for k, v in before.items() if k != "order"})
        self.assertEqual(cfg["order"]["headless"], ["work", "personal"])      # in memory too
        self.assertEqual(asw.apply_order(self.cfg(), self.snap()), ([], []))  # nothing left to do
        backups = list((self.state / "switch" / "config-backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text()), before)
        self.assertEqual(cf.load_config()["order"]["headless"], ["work", "personal"])

    def test_one_mode_only(self):
        self.save_cfg(auto_order={"headless": True})
        asw.apply_order(self.cfg(), self.snap())
        self.assertEqual(self.on_disk()["order"], {"interactive": ["personal", "work"],
                                                   "headless": ["work", "personal"]})

    def test_unknown_usage_leaves_the_file_alone(self):
        self.save_cfg(auto_order=True)
        with asw.edit_state() as st:
            st["usage"].pop("work")
        text = self.cfg_path.read_text()
        self.assertEqual(asw.apply_order(self.cfg(), self.snap()), ([], []))
        self.assertEqual(self.cfg_path.read_text(), text)

    def test_keeps_the_files_indent_and_mode(self):
        self.raw["switch"] = {"auto_order": True}
        self.cfg_path.write_text(json.dumps(self.raw, indent=4) + "\n")
        os.chmod(str(self.cfg_path), 0o600)
        asw.apply_order(self.cfg(), self.snap())
        text = self.cfg_path.read_text()
        self.assertIn('\n    "order": {\n        "interactive": [', text)
        self.assertEqual(os.stat(str(self.cfg_path)).st_mode & 0o777, 0o600)

    def test_old_copies_are_pruned(self):
        self.save_cfg(auto_order=True)
        for i in range(asw.BACKUPS_KEEP + 4):
            changed, problem = asw.update_config(self.cfg(), lambda raw: raw.__setitem__("n", i))
            self.assertEqual((changed, problem), (True, None))
        self.assertEqual(len(list((self.state / "switch" / "config-backups").iterdir())),
                         asw.BACKUPS_KEEP)

    def test_unreadable_config_is_reported_not_raised(self):
        self.save_cfg(auto_order=True)
        cfg, snap = self.cfg(), self.snap()
        self.cfg_path.write_text("{ not json")
        lines, problems = asw.apply_order(cfg, snap)
        self.assertEqual(lines, [])
        self.assertIn("not changed", problems[0])
        self.assertEqual(self.cfg_path.read_text(), "{ not json")

    def test_read_only_config_is_left_alone(self):
        self.save_cfg(auto_order=True)
        os.chmod(str(self.cfg_path), 0o444)
        text = self.cfg_path.read_text()
        lines, problems = asw.apply_order(self.cfg(), self.snap())
        self.assertEqual(lines, [])
        self.assertIn("read-only", problems[0])
        self.assertEqual(self.cfg_path.read_text(), text)

    def test_changes_made_to_the_file_meanwhile_are_respected(self):
        self.save_cfg(auto_order=True)
        cfg, snap = self.cfg(), self.snap()                 # a long-running process's copy
        self.raw["accounts"].append({"name": "third", "desktop_account_uuid": X_ACCT})
        self.raw["order"]["headless"] = ["personal", "work", "third"]
        self.save_cfg(auto_order={"headless": True})        # interactive turned off meanwhile
        lines, problems = asw.apply_order(cfg, snap)
        disk = self.on_disk()
        self.assertEqual(disk["order"]["headless"], ["work", "personal", "third"])   # third kept
        self.assertEqual(disk["order"]["interactive"], ["personal", "work"])          # left alone
        self.assertEqual(len(lines), 1)

    def test_account_with_extra_usage_and_almost_nothing_left_goes_after_the_others(self):
        self.usage("personal", 0.5, reset_h=140)
        self.usage("work", 0.96, reset_h=70, extra=True)
        order, _ = asw.desired_order(self.snap(), ["personal", "work"])
        self.assertEqual(order, ["personal", "work"])       # work would soon bill extra usage
        self.usage("work", 0.96, reset_h=70, extra=False)
        self.assertEqual(asw.desired_order(self.snap(), ["personal", "work"])[0], ["work", "personal"])


class FollowersTest(Base):
    TEXT = '{\n  "fetch": {\n      "chrome_account":   "personal",\n      "n": 1\n  },\n  "x": "personal2"\n}\n'

    def setUp(self):
        super().setUp()
        self.target = self.tmp / "tool-settings.json"
        self.target.write_text(self.TEXT)
        self.save_cfg(followers=[{"file": str(self.target), "key": "fetch.chrome_account",
                                  "follows": "extension"}])

    def test_follows_the_extension_and_changes_one_value(self):
        self.ext(logs={7: [(20, W_ACCT)]})
        lines, problems = asw.apply_followers(self.cfg(), self.snap())
        self.assertEqual((len(lines), problems), (1, []))
        self.assertEqual(self.target.read_text(), self.TEXT.replace('"personal",', '"work",'))
        self.assertEqual(asw.apply_followers(self.cfg(), self.snap()), ([], []))

    def test_unknown_extension_changes_nothing(self):
        self.assertEqual(asw.apply_followers(self.cfg(), self.snap()), ([], []))
        self.ext(logs={7: [(20, None)]})
        self.assertEqual(asw.apply_followers(self.cfg(), self.snap()), ([], []))
        self.ext(tables={5: [(10, W_ACCT)]}, manifest=False)       # unreadable: no guess
        self.assertEqual(asw.apply_followers(self.cfg(), self.snap()), ([], []))
        self.assertEqual(self.target.read_text(), self.TEXT)

    def test_follows_the_app(self):
        self.save_cfg(followers=[{"file": str(self.target), "key": "fetch.chrome_account",
                                  "follows": "app"}])
        self.sign_in(W_ACCT)
        asw.apply_followers(self.cfg(), self.snap())
        self.assertEqual(json.loads(self.target.read_text())["fetch"]["chrome_account"], "work")

    def test_value_in_two_places_is_left_alone(self):
        text = self.TEXT.replace('"x": "personal2"', '"chrome_account": "personal"')
        self.target.write_text(text)
        self.ext(logs={7: [(20, W_ACCT)]})
        lines, problems = asw.apply_followers(self.cfg(), self.snap())
        self.assertEqual(lines, [])
        self.assertIn("not in exactly one place", problems[0])
        self.assertEqual(self.target.read_text(), text)

    def test_missing_key_or_file(self):
        self.ext(logs={7: [(20, W_ACCT)]})
        self.target.write_text('{"fetch": {"chrome_account": 5}}')
        self.assertIn("not a text value", asw.apply_followers(self.cfg(), self.snap())[1][0])
        self.target.unlink()
        self.assertIn("could not read", asw.apply_followers(self.cfg(), self.snap())[1][0])

    def test_a_linked_file_keeps_its_link(self):
        real = self.tmp / "dotfiles" / "tool.json"
        real.parent.mkdir()
        real.write_text(self.TEXT)
        self.target.unlink()
        os.symlink(str(real), str(self.target))
        self.ext(logs={7: [(20, W_ACCT)]})
        lines, problems = asw.apply_followers(self.cfg(), self.snap())
        self.assertEqual((len(lines), problems), (1, []))
        self.assertTrue(os.path.islink(str(self.target)))
        self.assertIn('"work"', real.read_text())

    def test_a_read_only_file_is_left_alone(self):
        os.chmod(str(self.target), 0o444)
        self.ext(logs={7: [(20, W_ACCT)]})
        lines, problems = asw.apply_followers(self.cfg(), self.snap())
        self.assertEqual(lines, [])
        self.assertIn("read-only", problems[0])
        self.assertEqual(self.target.read_text(), self.TEXT)


class AdviceTest(Base):
    def keys(self):
        return [n["key"].split(":")[0] for n in asw.advice(self.cfg(), self.snap())]

    def test_nothing_to_say(self):
        self.usage("personal", 0.5)
        self.usage("work", 0.1, reset_h=100)
        self.assertEqual(self.keys(), [])

    def test_app_not_known_says_nothing(self):
        self.sign_in(X_ACCT)
        self.usage("personal", 0.99)
        self.assertEqual(self.keys(), [])

    def test_near_the_limit_once_per_threshold(self):
        self.usage("personal", 0.91)
        self.usage("work", 0.0, reset_h=100)
        notes = asw.advice(self.cfg(), self.snap())
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0]["key"].endswith(":90"))
        self.assertIn("work is ready with 100% of its week left", notes[0]["text"])
        self.assertEqual(len(asw.deliver(self.cfg(), notes)), 1)
        self.assertEqual(asw.deliver(self.cfg(), asw.advice(self.cfg(), self.snap())), [])
        self.assertEqual(len(self.sent), 1)
        self.usage("personal", 0.975)
        again = asw.deliver(self.cfg(), asw.advice(self.cfg(), self.snap()))
        self.assertEqual(len(again), 1)
        self.assertTrue(again[0]["key"].endswith(":97"))

    def test_out_with_somewhere_to_go(self):
        self.save_cfg(at_limit="app")                # the opt-in: a guided switch of the app
        self.limit("personal", hours=30)
        self.usage("work", 0.25, reset_h=100)
        notes = asw.advice(self.cfg(), self.snap())
        self.assertEqual(notes[0]["title"], "Claude: switch the app to work")
        self.assertIn("personal is out of usage until", notes[0]["text"])
        self.assertIn("75% of its week left", notes[0]["text"])
        self.assertTrue(notes[0]["assistant"])

    def test_whole_week_used_with_extra_usage_on(self):
        self.usage("personal", 1.0, reset_h=30, extra=True)
        self.usage("work", 0.0, reset_h=100)
        text = asw.advice(self.cfg(), self.snap())[0]["text"]
        self.assertIn("chats there now cost extra usage", text)

    def test_every_account_out(self):
        self.limit("personal", hours=30)
        self.limit("work", hours=3)
        notes = asw.advice(self.cfg(), self.snap())
        self.assertEqual(notes[0]["title"], "Claude: every account is out of usage")
        self.assertIn("First back: work", notes[0]["text"])

    def test_unused_usage_about_to_reset(self):
        self.usage("personal", 0.4, reset_h=120)
        self.usage("work", 0.2, reset_h=20)
        self.assertEqual(self.keys(), ["unused"])
        self.usage("work", 0.2, reset_h=60)          # not close enough to its reset
        self.assertEqual(self.keys(), [])
        self.usage("work", 0.7, reset_h=20)          # little left: not worth a switch
        self.assertEqual(self.keys(), [])

    def test_other_account_not_ready(self):
        self.usage("personal", 0.8)
        with cf.edit_state() as st:
            st["accounts"].setdefault("work", {})["auth_down_until"] = cf.iso(
                cf.now_local() + dt.timedelta(minutes=20))
        notes = asw.advice(self.cfg(), self.snap())
        self.assertEqual([n["key"].split(":")[0] for n in notes], ["notready"])
        self.assertIn("work is not ready", notes[0]["text"])
        self.usage("personal", 0.3)                  # far from its limit: not worth a notice
        self.assertEqual(self.keys(), [])

    def test_extension_on_the_other_account(self):
        self.usage("personal", 0.1)
        self.usage("work", 0.1, reset_h=100)
        self.ext(logs={7: [(20, W_ACCT)]})
        notes = asw.advice(self.cfg(), self.snap())
        self.assertEqual(notes[0]["title"], "Claude: Chrome extension is still on work")
        self.assertIn("personal@example.com", notes[0]["text"])

    def test_a_guessed_reset_that_keeps_moving_is_one_notice(self):
        # No reset time was stated, so the wrapper guesses 30 minutes and writes a new
        # guess every time the limit is seen again. One notice, one helper window.
        self.save_cfg(at_limit="app")
        self.usage("work", 0.2, reset_h=100)
        os.environ.pop("CLAUDE_FAILOVER_NO_NOTIFY")
        os.environ["CLAUDE_FAILOVER_HANDOFF_DIR"] = str(self.tmp / "handoffs")
        noon = cf.now_local().replace(hour=13, minute=0, second=0, microsecond=0)
        opened = []
        with mock.patch.object(asw.subprocess, "Popen", side_effect=lambda *a, **k: opened.append(a)):
            for minutes in (0, 35, 70, 105, 140):
                at = noon + dt.timedelta(minutes=minutes)
                with cf.edit_state() as st:
                    st["accounts"]["personal"] = {"limited_until": cf.iso(at + dt.timedelta(minutes=30)),
                                                  "limit_seen": cf.iso(at)}
                snap = asw.snapshot(self.cfg(), now=at + dt.timedelta(minutes=1))
                for note in asw.deliver(self.cfg(), asw.advice(self.cfg(), snap)):
                    if note.get("assistant"):
                        asw.open_assistant(self.cfg())
        self.assertEqual([t for t, _ in self.sent], ["Claude: switch the app to work"])
        self.assertEqual(len(opened), 1)

    def test_no_claim_about_usage_that_was_not_read(self):
        self.save_cfg(at_limit="app")
        self.limit("personal", hours=30)
        note = asw.advice(self.cfg(), self.snap())[0]
        self.assertEqual(note["title"], "Claude: switch the app to work")
        self.assertIn("work may have usage left (not read yet)", note["text"])
        self.assertNotIn("% of its", note["text"].split("work may")[1])
        self.usage("work", 0.6, reset_h=3, window="five_hour",      # only a 5-hour figure is known
                   week_util=None, week_resets_at=None)
        text = asw.advice(self.cfg(), self.snap())[0]["text"]
        self.assertIn("work has 40% of its 5-hour window left", text)

    def test_one_outage_is_one_notice(self):
        self.usage("work", 0.2, reset_h=100)
        late = cf.now_local().replace(hour=23, minute=40, second=0, microsecond=0)
        told = []
        for minutes in (0, 35, 70):                    # the guess crosses midnight
            at = late + dt.timedelta(minutes=minutes)
            with cf.edit_state() as st:
                st["accounts"]["personal"] = {"limited_until": cf.iso(at + dt.timedelta(minutes=30)),
                                              "limit_seen": cf.iso(at)}
            snap = asw.snapshot(self.cfg(), now=at + dt.timedelta(minutes=1))
            asw.track_outages(snap)
            told += asw.deliver(self.cfg(), asw.advice(self.cfg(), snap))
        self.assertEqual(len(told), 1)
        # It has usage again, later it runs out again: that is a new outage.
        with cf.edit_state() as st:
            st["accounts"]["personal"] = {}
        self.usage("personal", 0.3)
        asw.track_outages(self.snap())
        self.assertEqual(asw.read_state()["out_since"], {})
        self.limit("personal", hours=3)
        snap = self.snap()
        asw.track_outages(snap)
        self.assertEqual(len(asw.deliver(self.cfg(), asw.advice(self.cfg(), snap))), 1)
        # Not known is not "has usage again": the outage goes on.
        with cf.edit_state() as st:
            st["accounts"]["personal"] = {}
        with asw.edit_state() as st:
            st["usage"].pop("personal")
        asw.track_outages(self.snap())
        self.assertIn("personal", asw.read_state()["out_since"])

    def test_helper_window_only_one_at_a_time(self):
        self.save_cfg(at_limit="app")
        os.environ.pop("CLAUDE_FAILOVER_NO_NOTIFY")
        os.environ["CLAUDE_FAILOVER_HANDOFF_DIR"] = str(self.tmp / "handoffs")
        with mock.patch.object(asw.subprocess, "Popen") as popen:
            held = asw.assistant_lock()                 # a helper window is open
            self.assertIsNotNone(held)
            self.assertIsNone(asw.assistant_lock())
            self.assertIsNone(asw.open_assistant(self.cfg()))
            held.close()
            self.assertIsNotNone(asw.open_assistant(self.cfg()))
            self.assertIsNone(asw.open_assistant(self.cfg()))       # within 6 hours: no second one
            with asw.edit_state() as st:
                st["assistant_at"] = cf.iso(cf.now_local() - dt.timedelta(hours=7))
            self.assertIsNotNone(asw.open_assistant(self.cfg()))
            self.assertEqual(popen.call_count, 2)
        rc, out = self.run_cli("switch", "--assist", "--wait", "0")
        self.assertIn("did not change account", out)
        held = asw.assistant_lock()
        rc, out = self.run_cli("switch", "--assist", "--wait", "0")
        held.close()
        self.assertEqual((rc, out.strip()), (0, "Another switch helper window is already open."))

    def test_week_numbers_come_from_the_week_not_the_fullest_window(self):
        # The 5-hour window is at 92%, the week at 30%: no "92% of its week".
        self.usage("personal", 0.92, reset_h=3, window="five_hour", week_util=0.30,
                   week_resets_at=cf.iso(cf.now_local() + dt.timedelta(hours=100)))
        self.usage("work", 0.0, reset_h=120)
        s = self.snap()
        p = s["accounts"]["personal"]
        self.assertAlmostEqual(p["room"], 0.08)
        self.assertAlmostEqual(p["week_room"], 0.70)
        self.assertEqual(asw.advice(self.cfg(), s), [])
        self.assertEqual(asw.describe(p), "30% of its week used, 92% of its 5-hour window")
        self.usage("personal", 1.0, reset_h=3, window="five_hour", week_util=0.30,
                   week_resets_at=cf.iso(cf.now_local() + dt.timedelta(hours=100)))
        s = self.snap()
        self.assertEqual(asw.describe(s["accounts"]["personal"]), "its whole 5-hour window used")
        self.assertEqual([n["key"].split(":")[0] for n in asw.advice(self.cfg(), s)], ["out"])
        self.assertEqual(asw.desired_order(s, ["personal", "work"])[0], ["work", "personal"])

    def test_custom_thresholds(self):
        self.save_cfg(warn_at=[50])
        self.usage("personal", 0.6)
        self.usage("work", 0.0, reset_h=100)
        self.assertTrue(asw.advice(self.cfg(), self.snap())[0]["key"].endswith(":50"))


class TerminalHandoffFirstTest(Base):
    """The default at a limit: the StopFailure hook is the switch. It continues each
    chat that is refused in a Terminal window on the next account. Everything here
    only tells: no window opens and nothing asks for the app to be signed out."""

    def arm(self, command=None, **handoff):
        """The hook as `claude-accounts hook install` leaves it, in the shared settings."""
        folder = Path(self.raw["default_config_dir"])
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "settings.json").write_text(json.dumps({"hooks": {"StopFailure": [
            {"hooks": [{"type": "command", "command": command or str(cf.HOOK_SCRIPT)}]}]}}))
        if handoff:
            self.raw["handoff"] = handoff
            self.save_cfg()

    def third_account(self):
        self.raw["accounts"].append({"name": "third", "email": "third@example.com",
                                     "desktop_account_uuid": X_ACCT})
        self.raw["order"] = {"interactive": ["personal", "work", "third"],
                             "headless": ["personal", "work", "third"]}
        self.save_cfg()

    def test_terminal_unless_the_config_says_app(self):
        self.assertEqual(asw.at_limit(self.cfg()), "terminal")
        for value in ("terminal", "Terminal", "APP", "app ", "", None, 1, True, ["app"]):
            self.save_cfg(at_limit=value)
            self.assertEqual(asw.at_limit(self.cfg()), "terminal", value)
        self.save_cfg(at_limit="app")
        self.assertEqual(asw.at_limit(self.cfg()), "app")
        self.assertEqual(asw.at_limit(None), "terminal")
        self.assertEqual(asw.at_limit({"switch": "app"}), "terminal")

    def test_armed_means_a_hook_that_exists_and_desktop_chats_handed_off(self):
        self.assertIn("not installed", asw.handoff_problem(self.cfg()))      # no settings.json at all
        self.assertFalse(asw.handoff_armed(self.cfg()))
        self.arm()
        self.assertEqual(asw.handoff_problem(self.cfg()), "")
        self.assertTrue(asw.handoff_armed(self.cfg()))
        self.arm(from_terminal=False)                                # the other kind off: still armed
        self.assertTrue(asw.handoff_armed(self.cfg()))
        self.arm(from_desktop=False)
        self.assertIn("handoff.from_desktop", asw.handoff_problem(self.cfg()))
        self.assertFalse(asw.handoff_armed(self.cfg()))
        self.arm(from_desktop=True)
        self.assertTrue(asw.handoff_armed(self.cfg()))
        # a hook entry whose script is gone (the folder was moved) is not a hand-off
        self.arm(command=str(self.tmp / "gone" / "bin" / "claude-failover-hook"))
        self.assertIn("script is missing", asw.handoff_problem(self.cfg()))
        self.assertFalse(asw.handoff_armed(self.cfg()))
        shared = Path(self.raw["default_config_dir"]) / "settings.json"
        other = {"hooks": {"StopFailure": [{"hooks": [{"type": "command", "command": "/bin/other"}]}]}}
        for text in ("{not json", "[]", "{}", '{"hooks": {"StopFailure": "x"}}', json.dumps(other)):
            shared.write_text(text)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertFalse(asw.handoff_armed(self.cfg()), text)
                self.assertTrue(asw.handoff_problem(self.cfg()), text)
            self.assertEqual(out.getvalue(), "", text)               # it prints nothing
        self.assertFalse(asw.handoff_armed(None))

    def test_out_and_refused_says_terminal_and_nothing_else(self):
        self.arm()
        self.limit("personal", hours=30)
        self.usage("work", 0.25, reset_h=100)
        notes = asw.advice(self.cfg(), self.snap())
        self.assertEqual(notes[0]["title"], "Claude: personal is out of usage")
        self.assertTrue(notes[0]["key"].startswith("out:personal:"))
        self.assertFalse(notes[0]["key"].endswith(":extra"))
        self.assertIn("personal is out of usage until", notes[0]["text"])
        self.assertIn("work has 75% of its week left. Chats that hit the limit continue on "
                      "work in Terminal.", notes[0]["text"])
        self.assertNotIn("assistant", notes[0])
        self.assertNotIn("claude-accounts switch", notes[0]["text"])

    def test_refused_or_still_answering_decides_the_text(self):
        # The whole table: an account refuses when the wrapper saw a limit, or when it
        # is full and has no extra usage. Only full + extra usage + no limit bills on.
        self.arm()
        self.usage("work", 0.25, reset_h=100)
        moves = "Chats that hit the limit continue on work in Terminal."
        stays = "Nothing moves while extra usage is on"

        def note():
            return asw.advice(self.cfg(), self.snap())[0]

        self.usage("personal", 1.0, reset_h=30)                      # full, no extra usage
        self.assertEqual(note()["title"], "Claude: personal is out of usage")
        self.assertIn(moves, note()["text"])
        self.usage("personal", 1.0, reset_h=30, extra=True)          # full, extra usage on
        self.assertEqual(note()["title"], "Claude: personal has used its whole week")
        self.assertIn(stays, note()["text"])
        self.assertNotIn(moves, note()["text"])
        self.limit("personal", hours=30)                             # extra usage on, and it refused
        self.assertEqual(note()["title"], "Claude: personal is out of usage")
        self.assertIn(moves, note()["text"])
        self.assertNotIn(stays, note()["text"])

    def test_extra_usage_on_means_nothing_moves_and_it_says_so(self):
        # The account still answers past 100%, on paid extra usage: no chat is
        # refused, so the hook never runs. The notice says exactly that.
        self.arm()
        self.usage("personal", 1.0, reset_h=30, extra=True)
        self.usage("work", 0.03, reset_h=100)
        note = asw.advice(self.cfg(), self.snap())[0]
        self.assertEqual(note["title"], "Claude: personal has used its whole week")
        self.assertIn("chats there now cost extra usage", note["text"])
        self.assertIn("work has 97% of its week left. Nothing moves while extra usage is on: turn it "
                      "off for personal and chats continue on work in Terminal. Moving the app instead "
                      "(claude-accounts switch) stops chats that are working.", note["text"])
        self.assertNotIn("assistant", note)

    def test_when_it_does_refuse_later_that_is_a_new_notice(self):
        # First it bills (extra usage on), later in the same outage it refuses: the
        # notice that chats now move must not be swallowed as "already said".
        self.arm()
        self.usage("personal", 1.0, reset_h=30, extra=True)
        self.usage("work", 0.03, reset_h=100)
        asw.track_outages(self.snap())
        first = asw.deliver(self.cfg(), asw.advice(self.cfg(), self.snap()))
        self.assertEqual([n["title"] for n in first], ["Claude: personal has used its whole week"])
        self.assertTrue(first[0]["key"].endswith(":extra"))
        self.assertEqual(asw.deliver(self.cfg(), asw.advice(self.cfg(), self.snap())), [])
        self.limit("personal", hours=30)
        asw.track_outages(self.snap())
        second = asw.deliver(self.cfg(), asw.advice(self.cfg(), self.snap()))
        self.assertEqual([n["title"] for n in second], ["Claude: personal is out of usage"])
        self.assertIn("continue on work in Terminal", second[0]["text"])
        self.assertEqual(asw.deliver(self.cfg(), asw.advice(self.cfg(), self.snap())), [])

    def test_a_missing_hook_is_said_plainly(self):
        self.limit("personal", hours=30)
        self.usage("work", 0.25, reset_h=100)
        note = asw.advice(self.cfg(), self.snap())[0]
        self.assertEqual(note["title"], "Claude: personal is out of usage")
        self.assertIn("Nothing takes over by itself: the hook is not installed (claude-accounts hook "
                      "install). To move the app: claude-accounts switch (it stops chats that are "
                      "working).", note["text"])
        self.assertNotIn("assistant", note)
        self.arm(from_desktop=False)                                  # installed, but told to stay out
        note = asw.advice(self.cfg(), self.snap())[0]
        self.assertIn("Nothing takes over by itself: the hand-off of desktop chats is turned off "
                      "(handoff.from_desktop in the config).", note["text"])
        self.raw.pop("handoff")
        self.save_cfg()
        Path(self.raw["default_config_dir"], "settings.json").unlink()
        with cf.edit_state() as st:
            st["accounts"]["personal"] = {}
        self.usage("personal", 1.0, reset_h=30, extra=True)
        note = asw.advice(self.cfg(), self.snap())[0]
        self.assertIn("To move the app: claude-accounts switch (it stops chats that are working).",
                      note["text"])
        self.assertNotIn("in Terminal", note["text"])
        self.assertNotIn("assistant", note)

    def test_near_the_limit_says_what_will_happen(self):
        self.usage("personal", 0.91)
        self.usage("work", 0.0, reset_h=100)
        text = asw.advice(self.cfg(), self.snap())[0]["text"]       # no hook: nothing takes over
        self.assertIn("work is ready with 100% of its week left. Nothing takes over at 100%: the hook "
                      "is not installed (claude-accounts hook install).", text)
        self.assertNotIn("claude-accounts switch", text)
        self.arm()
        text = asw.advice(self.cfg(), self.snap())[0]["text"]
        self.assertIn("work is ready with 100% of its week left. When personal runs out, chats "
                      "continue on work in Terminal.", text)
        self.assertNotIn("claude-accounts switch", text)
        self.usage("personal", 0.91, extra=True)
        text = asw.advice(self.cfg(), self.snap())[0]["text"]
        self.assertIn("Extra usage is on for personal: past 100% it bills instead of stopping. "
                      "Turn it off and chats move to work in Terminal.", text)
        self.limit("work", hours=5)                                  # nowhere to go: as before
        text = asw.advice(self.cfg(), self.snap())[0]["text"]
        self.assertIn("No other account has usage left right now.", text)

    def test_the_next_account_bills_and_the_notice_says_so(self):
        # The other account is past its week and still answers, on paid extra usage.
        # The hook hands a refused chat to it all the same (it never saw it refuse),
        # so "every account is out of usage" would be false.
        self.arm()
        self.usage("work", 0.99, reset_h=120, extra=True)
        self.assertEqual(cf.choose_handoff_target(self.cfg(), "personal", None)[0], "work")
        self.usage("personal", 0.93)
        text = asw.advice(self.cfg(), self.snap())[0]["text"]
        self.assertIn("No other account has usage left. When personal runs out, chats continue on "
                      "work in Terminal, on paid extra usage.", text)
        self.limit("personal", hours=30)
        note = asw.advice(self.cfg(), self.snap())[0]
        self.assertEqual(note["title"], "Claude: personal is out of usage")
        self.assertTrue(note["key"].startswith("out:personal:"))
        self.assertIn("Chats that hit the limit continue on work in Terminal, on paid extra usage: "
                      "work is past its own limit and still answers.", note["text"])
        self.assertNotIn("assistant", note)
        # once the wrapper has seen work refuse too, everything really is out
        self.limit("work", hours=3)
        note = asw.advice(self.cfg(), self.snap())[0]
        self.assertEqual(note["title"], "Claude: every account is out of usage")
        self.assertIn("First back: work", note["text"])
        # without the hook nothing is handed anywhere: no promise about Terminal
        with cf.edit_state() as st:
            st["accounts"]["work"] = {}
        Path(self.raw["default_config_dir"], "settings.json").unlink()
        note = asw.advice(self.cfg(), self.snap())[0]
        self.assertEqual(note["title"], "Claude: every account is out of usage")
        self.assertNotIn("in Terminal", note["text"])

    def test_first_back_is_not_said_twice(self):
        self.limit("personal", hours=3)
        self.limit("work", hours=30)
        text = asw.advice(self.cfg(), self.snap())[0]["text"]
        self.assertIn("personal is out of usage until", text)
        self.assertNotIn("First back", text)                         # it is personal: just said

    def test_the_notice_names_the_account_the_hook_really_uses(self):
        # Three accounts: the watcher would pick the one with the most room, the hook
        # takes the next one in the account order. The notice must name the hook's.
        self.arm()
        self.third_account()
        self.limit("personal", hours=30)
        self.usage("work", 0.80, reset_h=100)
        self.usage("third", 0.10, reset_h=100)
        self.assertEqual(asw.pick_target(self.snap(), exclude="personal"), "third")
        self.assertEqual(cf.choose_handoff_target(self.cfg(), "personal", None)[0], "work")
        text = asw.advice(self.cfg(), self.snap())[0]["text"]
        self.assertIn("work has 20% of its week left. Chats that hit the limit continue on work "
                      "in Terminal.", text)
        self.assertNotIn("third", text)

    def test_unused_usage_names_the_cost_of_moving_the_app(self):
        self.usage("personal", 0.4, reset_h=120)
        self.usage("work", 0.2, reset_h=20)
        note = asw.advice(self.cfg(), self.snap())[0]
        self.assertEqual(note["key"].split(":")[0], "unused")
        self.assertIn("claude-accounts switch (it stops chats that are working).", note["text"])
        self.save_cfg(at_limit="app")
        note = asw.advice(self.cfg(), self.snap())[0]
        self.assertIn("Switch the app to use it. In Terminal: claude-accounts switch", note["text"])

    def test_a_refused_notice_keeps_its_key_in_both_modes(self):
        # Keys decide what was already said: changing the mode must not repeat a notice.
        self.arm()
        self.limit("personal", hours=30)
        self.usage("work", 0.25, reset_h=100)
        terminal = [n["key"] for n in asw.advice(self.cfg(), self.snap())]
        self.save_cfg(at_limit="app")
        self.assertEqual([n["key"] for n in asw.advice(self.cfg(), self.snap())], terminal)

    def test_on_extra_usage_reads_saved_numbers_only(self):
        with mock.patch.object(asw, "extension_account", side_effect=AssertionError("not read")), \
                mock.patch.object(asw, "probe", side_effect=AssertionError("no call")):
            self.assertFalse(asw.on_extra_usage(self.cfg(), "work"))        # never read
            self.usage("work", 0.4)
            self.assertFalse(asw.on_extra_usage(self.cfg(), "work"))
            self.usage("work", 1.0, reset_h=30)                              # full, and it refuses
            self.assertFalse(asw.on_extra_usage(self.cfg(), "work"))
            self.usage("work", 0.99, reset_h=30, extra=True)                 # full, still answering
            self.assertTrue(asw.on_extra_usage(self.cfg(), "work"))
            self.usage("work", 0.5, reset_h=30, extra=True)                  # extra usage on, not needed yet
            self.assertFalse(asw.on_extra_usage(self.cfg(), "work"))
            self.usage("work", 0.99, reset_h=30, extra=True)
            self.limit("work", hours=5)                                      # the wrapper saw it refuse
            self.assertFalse(asw.on_extra_usage(self.cfg(), "work"))
            self.assertFalse(asw.on_extra_usage(self.cfg(), "nobody"))

    def test_usage_line_of_an_account_that_bills_does_not_say_back(self):
        self.usage("personal", 0.3)
        self.usage("work", 0.99, reset_h=120, extra=True)
        rows = dict((name, bits) for name, bits, _ in asw.usage_rows(self.snap()))
        self.assertEqual(rows["work"][0], "its whole week used")
        self.assertTrue(rows["work"][1].startswith("still answers on paid extra usage until "))
        self.assertEqual(len(rows["work"]), 2)
        self.assertFalse(any(bit.startswith("back ") for bit in rows["work"]))
        self.limit("work", hours=5)                                          # it refused: now it is gone
        rows = dict((name, bits) for name, bits, _ in asw.usage_rows(self.snap()))
        self.assertTrue(rows["work"][1].startswith("back "))

    def test_status_says_what_happens_at_a_limit(self):
        self.usage("personal", 0.5)
        self.usage("work", 0.1, reset_h=100)
        self.assertEqual(asw.status_lines(self.cfg())[-2],
                         "At a limit - NOTHING takes over: the hook is not installed "
                         "(claude-accounts hook install)")
        rc, out = self.run_cli("watch", "status")
        self.assertIn("  at a limit - NOTHING takes over: the hook is not installed", out)
        self.arm()
        self.assertEqual(asw.status_lines(self.cfg())[-2],
                         "At a limit - chats continue in Terminal on the other account; the app stays "
                         "signed in and no window asks you to sign it out")
        self.assertTrue(asw.status_lines(self.cfg())[-1].startswith("Watcher - "))
        rc, out = self.run_cli("watch", "status")
        self.assertIn("  at a limit - chats continue in Terminal on the other account; the app stays "
                      "signed in and no window asks you to sign it out", out)
        self.save_cfg(at_limit="app")
        self.assertIn("At a limit - the switch helper opens in Terminal (switch.at_limit is \"app\"); "
                      "signing the app out stops chats that are working",
                      asw.status_lines(self.cfg())[-2])
        rc, out = self.run_cli("watch", "status")
        self.assertIn("  at a limit - the switch helper opens in Terminal", out)
        self.save_cfg(open_assistant_at_limit=False)                 # the opt-in without its window
        rc, out = self.run_cli("watch", "status")
        self.assertIn("  at a limit - a notice says to run claude-accounts switch", out)
        self.assertNotIn("helper opens", out)

    def test_status_names_the_chats_the_hook_leaves_out(self):
        self.arm(from_terminal=False)
        self.assertTrue(asw.at_limit_words(self.cfg()).endswith(
            "Chats started in Terminal stop instead (handoff.from_terminal is off)"))
        self.raw.pop("handoff")
        self.save_cfg()
        self.assertTrue(asw.at_limit_words(self.cfg()).endswith("no window asks you to sign it out"))
        with mock.patch.object(cf, "hook_blind_lines", return_value=["work: not linked"]):
            self.assertTrue(asw.at_limit_words(self.cfg()).endswith(
                "Not for every account: see claude-accounts hook status"))
        with mock.patch.object(cf, "hook_blind_lines", side_effect=RuntimeError("boom")):
            self.assertTrue(asw.at_limit_words(self.cfg()).endswith("no window asks you to sign it out"))


class PaidExtraUsageLastTest(Base):
    """The rule: paid extra usage only when no account has usage of its own left. An
    account past its limit with extra usage on is never refused, so the wrapper never
    learns it is out. The pick itself puts it behind every account that can still run
    on its own usage, and keeps it as the last resort when all of them are full."""

    def plan(self, mode="headless"):
        return cf.plan_accounts(self.cfg(), mode)[0]

    def third_account(self):
        self.raw["accounts"].append({"name": "third", "email": "third@example.com",
                                     "desktop_account_uuid": X_ACCT})
        self.raw["order"] = {"interactive": ["personal", "work", "third"],
                             "headless": ["personal", "work", "third"]}
        self.save_cfg()

    def test_an_account_that_bills_goes_behind_one_with_usage(self):
        self.assertEqual(self.plan(), ["personal", "work"])            # no readings: the config's order
        self.usage("personal", 0.99, reset_h=30, extra=True)          # first in the order, and it bills
        self.usage("work", 0.2, reset_h=100)
        self.assertEqual(self.plan(), ["work", "personal"])
        self.assertEqual(self.plan("interactive"), ["work", "personal"])
        self.assertEqual(cf.pick_interactive(self.cfg(), cf.parse_argv([]), None), "work")
        self.assertEqual([n for n, _ in cf.headless_attempts(cf.read_state(), self.plan())],
                         ["work", "personal"])
        self.assertEqual(self.on_disk()["order"]["headless"], ["personal", "work"])   # config not rewritten

    def test_when_every_account_is_full_the_one_that_bills_is_still_used(self):
        self.usage("personal", 0.99, reset_h=30, extra=True)
        self.limit("work", hours=3)                                    # work refused: only personal can run
        self.assertEqual(cf.plan_accounts(self.cfg(), "headless"), (["personal"], ["work"]))
        with cf.edit_state() as st:
            st["accounts"]["work"] = {}
        self.usage("work", 1.0, reset_h=100, extra=True)               # both bill: the order stays
        self.assertEqual(self.plan(), ["personal", "work"])

    def test_only_a_full_account_with_extra_usage_moves(self):
        self.usage("work", 0.2, reset_h=100)
        self.usage("personal", 0.5, reset_h=30, extra=True)            # extra usage on, not needed yet
        self.assertEqual(self.plan(), ["personal", "work"])
        self.usage("personal", 1.0, reset_h=30)                        # full without extra usage: it will
        self.assertEqual(self.plan(), ["personal", "work"])            # refuse, and the wrapper learns it then

    def test_after_a_reset_it_stays_behind_until_it_is_read_again(self):
        # The full reading is from a week that has reset since. Until the next reading
        # nothing is known about the new week, so it stays behind; then it is back.
        self.usage("work", 0.2, reset_h=100)
        self.usage("personal", 0.99, reset_h=30, extra=True, age_min=60 * 30)
        with asw.edit_state() as st:
            st["usage"]["personal"]["resets_at"] = cf.iso(cf.now_local() - dt.timedelta(hours=1))
            st["usage"]["personal"]["week_resets_at"] = st["usage"]["personal"]["resets_at"]
        self.assertEqual(self.plan(), ["work", "personal"])
        self.usage("personal", 0.0, reset_h=167, extra=True)           # the watcher read the new week
        self.assertEqual(self.plan(), ["personal", "work"])

    def test_an_account_that_may_start_to_bill_goes_behind_too(self):
        # Extra usage on and almost full, or a reading too old to trust: one more job
        # there can run past 100% before the next reading, and it never refuses.
        self.usage("work", 0.2, reset_h=100)
        self.usage("personal", 0.94, reset_h=30, extra=True)           # 6% left: still first
        self.assertEqual(asw.billing_accounts(self.cfg()), set())
        self.assertEqual(self.plan(), ["personal", "work"])
        self.usage("personal", 0.96, reset_h=30, extra=True)           # under 5% left
        self.assertEqual(asw.billing_accounts(self.cfg()), {"personal"})
        self.assertEqual(self.plan(), ["work", "personal"])
        self.usage("personal", 0.5, reset_h=30, extra=True, age_min=asw.STALE_MINUTES + 5)
        self.assertEqual(asw.billing_accounts(self.cfg()), {"personal"})   # the reading no longer counts
        self.assertEqual(self.plan(), ["work", "personal"])
        self.usage("personal", 0.96, reset_h=30)                       # no extra usage: it refuses at 100%
        self.assertEqual(asw.billing_accounts(self.cfg()), set())
        with asw.edit_state() as st:
            st["usage"].pop("personal")                                # never read: nothing is assumed
        self.assertEqual(asw.billing_accounts(self.cfg()), set())
        self.assertEqual(self.plan(), ["personal", "work"])

    def test_extra_usage_already_in_use_counts_whatever_the_percent(self):
        self.usage("work", 0.2, reset_h=100)
        self.usage("personal", 0.6, reset_h=30, extra=True, extra_in_use=True)
        self.assertEqual(asw.billing_accounts(self.cfg()), {"personal"})
        self.assertEqual(self.plan(), ["work", "personal"])

    def test_a_reading_that_failed_does_not_forget_that_extra_usage_is_on(self):
        # personal leads the order and has extra usage on. Its next reading fails
        # (a timeout). Nothing is known about it now, so it must not keep leading.
        self.usage("work", 0.2, reset_h=100)
        self.usage("personal", 0.93, reset_h=30, extra=True, age_min=90)
        self.assertEqual(self.plan(), ["personal", "work"])
        failed = lambda cfg, name, now=None: {"at": cf.iso(cf.now_local()), "ok": False,
                                              "error": "error", "text": "timed out"}
        self.assertEqual(asw.refresh_usage(self.cfg(), read=failed), ["personal"])
        rec = asw.read_state()["usage"]["personal"]
        self.assertEqual((rec["ok"], rec["extra"], "util" in rec), (False, True, False))
        self.assertEqual(asw.billing_accounts(self.cfg()), {"personal"})
        self.assertEqual(self.plan(), ["work", "personal"])
        # an account without extra usage is not marked by a failed reading
        self.usage("personal", 0.93, reset_h=30, age_min=90)
        asw.refresh_usage(self.cfg(), read=failed)
        self.assertNotIn("extra", asw.read_state()["usage"]["personal"])
        self.assertEqual(self.plan(), ["personal", "work"])

    def test_a_lost_login_says_when_jobs_now_bill(self):
        self.usage("work", 0.99, reset_h=120, extra=True)
        with cf.edit_state() as st:
            st["accounts"].setdefault("personal", {})["auth_down_until"] = cf.iso(
                cf.now_local() + dt.timedelta(minutes=30))
        cf.warn_logged_out(self.cfg(), "personal", {"kind": "auth"})
        self.assertEqual(self.sent[-1], ("Claude: personal is logged out",
                                         "Fix: claude-accounts login personal. Until then jobs run on "
                                         "work, which bills extra usage"))
        with cf.edit_state() as st:
            st["accounts"]["personal"].pop("logout_warned")
        self.usage("work", 0.2, reset_h=120, extra=True)               # work has usage of its own
        cf.warn_logged_out(self.cfg(), "personal", {"kind": "auth"})
        self.assertEqual(self.sent[-1][1], "Fix: claude-accounts login personal")

    def test_the_accounts_with_usage_keep_their_order(self):
        self.third_account()
        self.usage("personal", 0.99, reset_h=30, extra=True)
        self.usage("work", 0.6, reset_h=100)
        self.usage("third", 0.1, reset_h=100)
        self.assertEqual(self.plan(), ["work", "third", "personal"])
        self.raw["order"]["headless"] = ["third", "personal", "work"]
        self.save_cfg()
        self.assertEqual(self.plan(), ["third", "work", "personal"])
        self.usage("third", 0.99, reset_h=30, extra=True)              # two that bill keep their order too
        self.assertEqual(self.plan(), ["work", "third", "personal"])

    def test_the_screen_shows_the_order_that_is_really_used(self):
        self.usage("personal", 0.99, reset_h=30, extra=True)
        self.usage("work", 0.2, reset_h=100)
        self.assertIn("  background jobs use work -> personal", asw.usage_lines(self.cfg(), self.snap()))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(cf, "auth_status", return_value={"loggedIn": True}):
            cf.accounts_status(self.cfg())
        self.assertIn("Claude accounts (headless order: work -> personal; chat order: work -> personal)",
                      out.getvalue())

    def test_a_pinned_run_and_a_single_account_are_left_alone(self):
        self.usage("personal", 0.99, reset_h=30, extra=True)
        self.usage("work", 0.2, reset_h=100)
        self.assertEqual(cf.plan_accounts(self.cfg(), "headless", pinned="personal"), (["personal"], []))
        self.assertEqual(cf.included_usage_first(self.cfg(), ["personal"]), ["personal"])
        self.assertEqual(cf.included_usage_first(self.cfg(), []), [])

    def test_a_reading_that_cannot_be_had_changes_nothing(self):
        self.usage("personal", 0.99, reset_h=30, extra=True)
        self.usage("work", 0.2, reset_h=100)
        with mock.patch.object(asw, "billing_accounts", side_effect=RuntimeError("boom")):
            self.assertEqual(self.plan(), ["personal", "work"])
        with mock.patch.object(asw, "snapshot", side_effect=RuntimeError("boom")):
            self.assertEqual(asw.billing_accounts(self.cfg()), set())
            self.assertEqual(self.plan(), ["personal", "work"])
        with mock.patch.dict(sys.modules, {"account_switch": None}):   # the wrapper without this file
            self.assertEqual(self.plan(), ["personal", "work"])
        self.assertEqual(self.plan(), ["work", "personal"])            # and back to the rule

    def test_the_pick_makes_no_call_and_starts_nothing(self):
        self.usage("personal", 0.99, reset_h=30, extra=True)
        self.usage("work", 0.2, reset_h=100)
        boom = AssertionError("the pick must only read saved numbers")
        with mock.patch.object(asw, "extension_account", side_effect=boom), \
                mock.patch.object(asw, "probe", side_effect=boom), \
                mock.patch.object(asw.subprocess, "Popen", side_effect=boom), \
                mock.patch.object(asw.subprocess, "run", side_effect=boom):
            self.assertEqual(self.plan(), ["work", "personal"])
            self.assertEqual(asw.billing_accounts(self.cfg()), {"personal"})

    def test_a_handoff_takes_the_account_with_usage_before_the_one_that_bills(self):
        # Two accounts: the chat's account refused and the other one bills. Both are
        # full, so the chat still moves there (it is the only place left).
        self.limit("personal", hours=30)
        self.usage("work", 0.99, reset_h=120, extra=True)
        self.assertEqual(cf.choose_handoff_target(self.cfg(), "personal", None), ("work", None))
        # Three accounts: the next one in the order bills, the third has usage left.
        self.third_account()
        self.usage("third", 0.2, reset_h=100)
        self.assertEqual(cf.choose_handoff_target(self.cfg(), "personal", None), ("third", None))
        self.limit("third", hours=2)                                   # now it is full too
        self.assertEqual(cf.choose_handoff_target(self.cfg(), "personal", None), ("work", None))


class PaidRunsUseOpusTest(Base):
    """The rule: a run that lands on paid extra usage uses Opus. Max effort and
    ultracode are not set here: the shared settings.json turns them on for every run."""

    def setUp(self):
        super().setUp()
        self.usage("personal", 0.2, reset_h=100)
        self.usage("work", 0.99, reset_h=120, extra=True)              # work bills
        self.settings({"model": "fable"})

    def settings(self, data):
        folder = Path(self.raw["default_config_dir"])
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "settings.json").write_text(json.dumps(data))

    def argv(self, name, *args):
        return cf.paid_model_argv(self.cfg(), name, list(args))

    def test_an_account_with_usage_of_its_own_is_left_alone(self):
        given = ["-p", "hi"]
        self.assertIs(cf.paid_model_argv(self.cfg(), "personal", given), given)
        self.assertIs(cf.paid_model_argv(self.cfg(), "nobody", given), given)

    def test_on_the_account_that_bills_the_default_model_becomes_opus(self):
        self.assertEqual(self.argv("work", "-p", "hi"), ["-p", "hi", "--model", "opus"])
        self.settings({})                                              # no default named: still Opus
        self.assertEqual(self.argv("work", "-p", "hi"), ["-p", "hi", "--model", "opus"])
        Path(self.raw["default_config_dir"], "settings.json").unlink()
        self.assertEqual(self.argv("work", "-p", "hi"), ["-p", "hi", "--model", "opus"])

    def test_a_model_that_costs_more_is_replaced_where_it_stands(self):
        self.assertEqual(self.argv("work", "-p", "hi", "--model", "claude-fable-5-2"),
                         ["-p", "hi", "--model", "opus"])
        self.assertEqual(self.argv("work", "--model", "fable", "-p", "hi"), ["--model", "opus", "-p", "hi"])
        self.assertEqual(self.argv("work", "-p", "hi", "--model=fable"), ["-p", "hi", "--model=opus"])
        self.assertEqual(self.argv("work", "-p", "hi", "--model", "some-new-model"),
                         ["-p", "hi", "--model", "opus"])
        self.assertEqual(self.argv("work", "--model", "fable", "-p", "hi", "--model", "fable"),
                         ["--model", "opus", "-p", "hi", "--model", "opus"])

    def test_replacing_the_model_never_lets_another_option_swallow_the_prompt(self):
        # Taking "--model fable" out of the middle would hand the prompt to the
        # option in front of it. Each line must parse to the same prompt and options.
        lines = [
            ["-p", "--allowedTools", "Bash", "Read", "--model", "fable", "summarize the log"],
            ["--add-dir", "/tmp/x", "--model", "claude-fable-5-2", "fix the bug"],
            ["-p", "-d", "--model", "fable", "hi"],
            ["--resume", "--model", "fable", "go on"],
            ["-p", "--allowedTools", "Bash", "--model=fable", "summarize the log"],
        ]
        for given in lines:
            before, after = cf.parse_argv(given), cf.parse_argv(cf.paid_model_argv(self.cfg(), "work", given))
            self.assertEqual([t for _, t in after["positionals"]], [t for _, t in before["positionals"]], given)
            self.assertEqual(after["opts"].pop("--model"), "opus", given)
            before["opts"].pop("--model")
            self.assertEqual(after["opts"], before["opts"], given)

    def test_a_model_flag_with_no_value_is_left_for_claude_to_reject(self):
        given = ["-p", "hi", "--model"]
        self.assertIs(cf.paid_model_argv(self.cfg(), "work", given), given)

    def test_only_a_run_that_is_on_opus_already_is_left_alone(self):
        for model in ("opus", "claude-opus-5-5", "Opus"):
            given = ["-p", "hi", "--model", model]
            self.assertIs(cf.paid_model_argv(self.cfg(), "work", given), given, model)
        self.settings({"model": "claude-opus-5-5"})                    # Opus as the default: nothing to add
        given = ["-p", "hi"]
        self.assertIs(cf.paid_model_argv(self.cfg(), "work", given), given)

    def test_sonnet_and_haiku_jobs_move_up_to_opus_too(self):
        # The rule has no exception for cheaper models: on paid extra usage, all Opus.
        for model in ("sonnet", "claude-sonnet-5-5", "haiku", "claude-haiku-4-5", "opusplan"):
            self.assertEqual(self.argv("work", "-p", "hi", "--model", model),
                             ["-p", "hi", "--model", "opus"], model)
            given = ["-p", "hi", "--model", model]                     # and never on the account with usage
            self.assertIs(cf.paid_model_argv(self.cfg(), "personal", given), given, model)
        self.settings({"model": "sonnet"})                             # a cheaper default moves up as well
        self.assertEqual(self.argv("work", "-p", "hi"), ["-p", "hi", "--model", "opus"])

    def test_an_account_that_is_out_of_opus_itself_keeps_the_callers_model(self):
        with cf.edit_state() as st:
            st["accounts"].setdefault("work", {})["model_limits"] = {
                "opus": cf.iso(cf.now_local() + dt.timedelta(hours=3))}
        given = ["-p", "hi", "--model", "sonnet"]
        self.assertIs(cf.paid_model_argv(self.cfg(), "work", given), given)
        with cf.edit_state() as st:
            st["accounts"]["work"]["model_limits"] = {
                "opus": cf.iso(cf.now_local() - dt.timedelta(hours=1))}     # that limit is over
        self.assertEqual(self.argv("work", "-p", "hi", "--model", "sonnet"), ["-p", "hi", "--model", "opus"])

    def test_the_prompt_and_other_options_are_never_touched(self):
        self.assertEqual(self.argv("work", "-p", "--model", "fable", "--", "what does --model fable do"),
                         ["-p", "--model", "opus", "--", "what does --model fable do"])
        self.assertIn("--append-system-prompt", cf.ONE)
        self.assertEqual(self.argv("work", "--append-system-prompt", "--model", "-p", "hi"),
                         ["--append-system-prompt", "--model", "-p", "hi", "--model", "opus"])
        self.assertEqual(self.argv("work", "--resume", "abc", "--fork-session", "go on"),
                         ["--resume", "abc", "--fork-session", "go on", "--model", "opus"])
        # with no model named it goes right before the terminator, and the tail is not read
        self.assertEqual(self.argv("work", "-p", "--", "--model x is my prompt"),
                         ["-p", "--model", "opus", "--", "--model x is my prompt"])
        # a "--" that is another option's value is not the terminator
        self.assertIn("--system-prompt", cf.ONE)
        self.assertEqual(self.argv("work", "-p", "--system-prompt", "--", "hi"),
                         ["-p", "--system-prompt", "--", "hi", "--model", "opus"])

    def test_the_model_can_be_changed_or_the_rule_turned_off(self):
        self.raw["extra_usage_model"] = "sonnet"
        self.save_cfg()
        self.assertEqual(self.argv("work", "-p", "hi"), ["-p", "hi", "--model", "sonnet"])
        self.assertEqual(self.argv("work", "-p", "hi", "--model", "opus"), ["-p", "hi", "--model", "sonnet"])
        given = ["-p", "hi", "--model", "claude-sonnet-5-5"]
        self.assertIs(cf.paid_model_argv(self.cfg(), "work", given), given)
        for off in (False, None, "", "  "):
            self.raw["extra_usage_model"] = off
            self.save_cfg()
            given = ["-p", "hi"]
            self.assertIs(cf.paid_model_argv(self.cfg(), "work", given), given, off)

    def test_any_doubt_leaves_the_run_as_it_was(self):
        given = ["-p", "hi"]
        with mock.patch.object(asw, "billing_accounts", side_effect=RuntimeError("boom")):
            self.assertIs(cf.paid_model_argv(self.cfg(), "work", given), given)
        with mock.patch.object(cf, "parse_argv", side_effect=RuntimeError("boom")):
            self.assertIs(cf.paid_model_argv(self.cfg(), "work", given), given)
        with mock.patch.dict(sys.modules, {"account_switch": None}):
            self.assertIs(cf.paid_model_argv(self.cfg(), "work", given), given)
        self.assertIs(cf.paid_model_argv(None, "work", given), given)

    def headless(self, *args):
        """run_headless with the real claude replaced: [(account, argv)] of each attempt."""
        calls = []

        def run_once(cfg, argv, env, stdin):
            calls.append((env.get("CLAUDE_FAILOVER_ACTIVE_ACCOUNT"), list(argv)))
            return 0, b"{}", b"", 0.1

        with mock.patch.object(cf, "run_once", side_effect=run_once), \
                mock.patch.object(cf, "slurp_stdin", return_value=None), \
                mock.patch.object(cf, "install_signal_forwarding"), \
                mock.patch.object(cf, "ensure_profile_links"), \
                contextlib.redirect_stdout(io.TextIOWrapper(io.BytesIO())):
            rc = cf.run_headless(self.cfg(), list(args), None)
        self.assertEqual(rc, 0)
        return calls

    def test_a_background_job_runs_on_opus_only_when_it_lands_on_the_account_that_bills(self):
        (account, argv), = self.headless("-p", "hi")
        self.assertEqual(account, "personal")                          # usage of its own: nothing added
        self.assertNotIn("--model", argv)
        self.limit("personal", hours=5)                                # both full now
        (account, argv), = self.headless("-p", "hi")
        self.assertEqual(account, "work")
        self.assertEqual(argv[-2:], ["--model", "opus"])
        self.assertEqual(argv[:2], ["-p", "hi"])
        (account, argv), = self.headless("-p", "hi", "--model", "claude-haiku-4-5")     # a small job too
        self.assertEqual((account, argv.count("--model"), "claude-haiku-4-5" in argv), ("work", 1, False))
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        (account, argv), = self.headless("-p", "hi", "--model", "opus")                 # Opus already
        self.assertEqual((account, argv.count("--model")), ("work", 1))
        runs = [json.loads(line) for line in (self.state / "events.jsonl").read_text().splitlines()]
        runs = [e for e in runs if e.get("event") == "run"]
        self.assertEqual([e.get("paid_model") for e in runs], [None, "opus", "opus", None])

    def test_a_job_that_moves_to_the_account_that_bills_mid_run_gets_opus_there(self):
        calls = []

        def run_once(cfg, argv, env, stdin):
            name = env.get("CLAUDE_FAILOVER_ACTIVE_ACCOUNT")
            calls.append((name, list(argv)))
            if name == "personal":                                     # it refuses: the job moves on
                return 1, "You've hit your session limit \u00b7 resets 3:45pm".encode(), b"", 0.1
            return 0, b"{}", b"", 0.1

        with mock.patch.object(cf, "run_once", side_effect=run_once), \
                mock.patch.object(cf, "slurp_stdin", return_value=None), \
                mock.patch.object(cf, "install_signal_forwarding"), \
                mock.patch.object(cf, "ensure_profile_links"), \
                contextlib.redirect_stdout(io.TextIOWrapper(io.BytesIO())):
            rc = cf.run_headless(self.cfg(), ["-p", "hi"], None)
        self.assertEqual(rc, 0)
        self.assertEqual([name for name, _ in calls], ["personal", "work"])
        self.assertNotIn("--model", calls[0][1])
        self.assertEqual(calls[1][1][calls[1][1].index("--model") + 1], "opus")
        self.assertEqual(calls[1][1][:2], ["-p", "hi"])

    def test_a_background_chat_follows_the_rule_too(self):
        self.assertIn("--bg", cf.FLAGS | cf.OPTIONAL | cf.ONE)
        self.limit("personal", hours=5)
        argv, account, said = self.start_chat("--bg", "tidy the repo")
        self.assertEqual(account, "work")
        self.assertEqual(argv.count("--model"), 1)
        self.assertEqual(argv[argv.index("--model") + 1], "opus")

    def start_chat(self, *args, tty=True, env=None):
        """main() up to the point where it would start claude: (argv, account, what it said)."""
        seen = []

        def execve(path, argv, child):
            seen.append((argv[1:], child.get("CLAUDE_FAILOVER_ACTIVE_ACCOUNT")))
            raise SystemExit(0)

        err = io.StringIO()
        with mock.patch.object(cf.os, "execve", execve), \
                mock.patch.object(cf.os, "isatty", lambda fd: tty), \
                mock.patch.object(cf, "ensure_profile_links"), \
                mock.patch.dict(os.environ, env or {}), \
                contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            cf.main(list(args))
        return seen[-1] + (err.getvalue(),)

    def test_a_terminal_chat_runs_on_opus_only_on_the_account_that_bills(self):
        argv, account, said = self.start_chat("fix the bug")
        self.assertEqual((argv, account), (["fix the bug"], "personal"))
        self.assertNotIn("extra usage", said)
        self.limit("personal", hours=5)
        argv, account, said = self.start_chat("fix the bug")
        self.assertEqual((argv, account), (["fix the bug", "--model", "opus"], "work"))
        self.assertIn("work is at or near its limit with extra usage on: using opus", said)
        argv, account, said = self.start_chat("fix the bug", tty=False)     # a script: no extra words
        self.assertEqual(argv, ["fix the bug", "--model", "opus"])
        self.assertNotIn("extra usage", said)

    def test_a_chat_handed_to_the_account_that_bills_runs_on_opus_too(self):
        # What the hook's Terminal script runs: this wrapper, pinned to the target.
        argv, account, said = self.start_chat(
            "--resume", "11111111-2222-3333-4444-555555555555", "--fork-session", "pick up where it stopped",
            env={"CLAUDE_FAILOVER_ACCOUNT": "work"})
        self.assertEqual(account, "work")
        self.assertEqual(argv[-2:], ["--model", "opus"])
        argv, account, said = self.start_chat(                             # the hook named another model
            "--resume", "11111111-2222-3333-4444-555555555555", "--fork-session", "--model", "sonnet", "go on",
            env={"CLAUDE_FAILOVER_ACCOUNT": "work"})
        self.assertEqual((argv.count("--model"), "sonnet" in argv), (1, False))
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertEqual(argv[-1], "go on")                                # the prompt is still the prompt
        argv, account, said = self.start_chat(                             # pinned to the one with usage
            "--resume", "11111111-2222-3333-4444-555555555555", "--fork-session", "go on",
            env={"CLAUDE_FAILOVER_ACCOUNT": "personal"})
        self.assertNotIn("--model", argv)


class TickTest(Base):
    def setUp(self):
        super().setUp()
        self.read = mock.patch.object(
            asw, "probe", side_effect=lambda cfg, name, now=None: {
                "at": cf.iso(cf.now_local()), "ok": True, "util": 0.87 if name == "personal" else 0.0,
                "window": "seven_day", "resets_at": cf.iso(cf.now_local() + dt.timedelta(hours=70)),
                "week_resets_at": cf.iso(cf.now_local() + dt.timedelta(hours=70))})
        self.read.start()
        self.addCleanup(self.read.stop)
        self.sync = mock.patch.object(asw, "run_sync", return_value={
            "errors": [], "chats": 12, "scheduled_tasks": {
                "on": True, "source": "work", "tasks": 5, "note": None,
                "targets": [{"account": "personal", "changes": [], "only_there": [],
                             "written": False, "note": None}]}})
        self.sync_mock = self.sync.start()
        self.addCleanup(self.sync.stop)

    def test_first_pass_reads_usage_and_announces_nothing(self):
        lines = asw.tick(self.cfg())
        self.assertEqual(lines, [])
        st = asw.read_state()
        self.assertEqual(st["app_account"], "personal")
        self.assertNotIn("app_changed_at", st)
        self.assertEqual(sorted(st["usage"]), ["personal", "work"])
        self.assertEqual(self.sent, [])

    def test_keeps_the_order_in_step(self):
        self.save_cfg(auto_order=True)
        lines = asw.tick(self.cfg())
        self.assertEqual(len(lines), 2)
        self.assertEqual(self.on_disk()["order"]["headless"], ["work", "personal"])
        self.assertEqual(asw.tick(self.cfg()), [])

    def test_finishes_a_switch_it_notices_once(self):
        self.save_cfg(auto_order=True)
        self.raw["desktop_sync"] = {"enabled": True}
        self.save_cfg()
        asw.tick(self.cfg())
        self.sign_in(W_ACCT)
        lines = asw.tick(self.cfg())
        self.assertIn("the desktop app moved from personal to work", lines)
        self.assertTrue(any("scheduled tasks - 5 on personal" in line for line in lines), lines)
        self.sync_mock.assert_called_once()
        self.assertEqual(self.on_disk()["order"]["headless"], ["personal", "work"])
        self.assertEqual([t for t, _ in self.sent], ["Claude: the app is now on work"])
        self.assertEqual(asw.read_state()["app_account"], "work")
        self.assertIn("app_changed_at", asw.read_state())
        events = [json.loads(line) for line in (self.state / "events.jsonl").read_text().splitlines()]
        self.assertEqual([(e["frm"], e["to"]) for e in events if e["event"] == "app_switch"],
                         [("personal", "work")])
        self.sent[:] = []
        self.assertEqual(asw.tick(self.cfg()), [])
        self.assertEqual(self.sent, [])

    def test_no_sync_when_desktop_sync_is_off(self):
        asw.tick(self.cfg())
        self.sign_in(W_ACCT)
        asw.tick(self.cfg())
        self.sync_mock.assert_not_called()

    def test_notice_at_the_limit_goes_out_once(self):
        self.save_cfg(at_limit="app")
        self.limit("personal", hours=20)
        lines = asw.tick(self.cfg())
        self.assertEqual([t for t, _ in self.sent], ["Claude: switch the app to work"])
        self.assertTrue(any(line.startswith("told you:") for line in lines))
        asw.tick(self.cfg())
        self.assertEqual(len(self.sent), 1)

    def test_at_a_limit_the_default_only_tells(self):
        # No "at_limit" in the config: the StopFailure hook is the switch. A real
        # pass (not the tests' quiet mode) sends one notice and opens no window.
        os.environ.pop("CLAUDE_FAILOVER_NO_NOTIFY")
        os.environ["CLAUDE_FAILOVER_HANDOFF_DIR"] = str(self.tmp / "handoffs")
        self.limit("personal", hours=20)
        with mock.patch.object(asw.subprocess, "Popen") as popen:
            lines = asw.tick(self.cfg())
            asw.tick(self.cfg())
            popen.assert_not_called()
        self.assertEqual([t for t, _ in self.sent], ["Claude: personal is out of usage"])
        self.assertTrue(any(line.startswith("told you:") for line in lines))
        self.assertFalse(any("switch helper" in line for line in lines))
        self.assertFalse((self.tmp / "handoffs").exists())
        self.assertNotIn("assistant_at", asw.read_state())

    def test_with_the_opt_in_only_the_out_notice_opens_the_window(self):
        # "at_limit": "app", a real pass: a warning at 90% is a notice and no window.
        self.save_cfg(at_limit="app")
        os.environ.pop("CLAUDE_FAILOVER_NO_NOTIFY")
        os.environ["CLAUDE_FAILOVER_HANDOFF_DIR"] = str(self.tmp / "handoffs")
        self.usage("personal", 0.93)
        self.usage("work", 0.0, reset_h=100)
        with mock.patch.object(asw, "refresh_usage"), \
                mock.patch.object(asw.subprocess, "Popen") as popen:
            asw.tick(self.cfg())
            self.assertEqual([t for t, _ in self.sent], ["Claude: personal is at 93% of its week"])
            popen.assert_not_called()
            self.limit("personal", hours=20)
            lines = asw.tick(self.cfg())
            self.assertEqual(popen.call_count, 1)
            self.assertEqual(popen.call_args[0][0][:3], ["/usr/bin/open", "-a", "Terminal"])
        self.assertIn("opened the switch helper in Terminal", lines)

    def test_a_helper_window_needs_the_opt_in(self):
        os.environ.pop("CLAUDE_FAILOVER_NO_NOTIFY")
        os.environ["CLAUDE_FAILOVER_HANDOFF_DIR"] = str(self.tmp / "handoffs")
        with mock.patch.object(asw.subprocess, "Popen") as popen:
            for value in (None, "terminal", "APP", True, "", ["app"]):
                self.save_cfg(at_limit=value, open_assistant_at_limit=True)
                self.assertIsNone(asw.open_assistant(self.cfg()), value)
            popen.assert_not_called()
            self.save_cfg(at_limit="app")
            self.assertIsNotNone(asw.open_assistant(self.cfg()))
            self.assertEqual(popen.call_count, 1)

    def test_a_second_pass_at_the_same_time_steps_aside(self):
        import fcntl
        d = self.state / "switch"
        d.mkdir(parents=True)
        with open(str(d / "tick.lock"), "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(asw.tick(self.cfg()), [])
        self.assertNotIn("app_account", asw.read_state())

    def test_a_broken_pass_is_logged_not_raised(self):
        with mock.patch.object(asw, "snapshot", side_effect=RuntimeError("boom")):
            lines = asw.tick(self.cfg())
        self.assertEqual(lines, ["watch: RuntimeError: boom"])

    def test_a_lasting_problem_is_logged_once(self):
        target = self.tmp / "tool.json"
        target.write_text('{"fetch": {"chrome_account": 5}}')
        self.save_cfg(followers=[{"file": str(target), "key": "fetch.chrome_account",
                                  "follows": "app"}])
        first = asw.tick(self.cfg())
        self.assertEqual(len([line for line in first if "not a text value" in line]), 1)
        self.assertEqual(asw.tick(self.cfg()), [])
        self.assertEqual(len(asw.read_state()["problems"]), 1)
        target.write_text('{"fetch": {"chrome_account": "personal"}}')
        self.assertEqual(asw.tick(self.cfg()), [])
        self.assertEqual(asw.read_state()["problems"], [])

    def test_a_failed_reading_stays_listed_and_is_logged_once(self):
        self.read.stop()
        fail = {"at": cf.iso(cf.now_local()), "ok": False, "error": "error", "text": "timed out"}
        ok = {"at": cf.iso(cf.now_local()), "ok": True, "util": 0.1}
        with mock.patch.object(asw, "probe", side_effect=lambda cfg, name, now=None: dict(
                fail if name == "work" else ok)) as read:
            first = asw.tick(self.cfg())
            second = asw.tick(self.cfg())                # work is not read again this soon
            self.assertEqual(read.call_count, 2)
        self.read.start()
        self.assertEqual(first, ["could not read the usage of work: timed out"])
        self.assertEqual(second, [])
        self.assertEqual(asw.read_state()["problems"], ["could not read the usage of work: timed out"])
        self.run_cli("watch", "install")
        self.assertIn("1 problem", asw.watcher_health(self.cfg()))
        rc, out = self.run_cli("watch", "status")
        self.assertIn("problem - could not read the usage of work: timed out", out)

    def test_a_pass_that_keeps_crashing_is_logged_once(self):
        with mock.patch.object(asw, "snapshot", side_effect=RuntimeError("boom")):
            self.assertEqual(asw.tick(self.cfg()), ["watch: RuntimeError: boom"])
            self.assertEqual(asw.tick(self.cfg()), [])
        self.assertEqual(asw.tick(self.cfg()), [])
        self.assertEqual(asw.read_state()["problems"], [])

    def test_a_watcher_that_stopped_is_named(self):
        self.run_cli("watch", "install", "--every", "10")
        asw.tick(self.cfg())
        self.assertEqual(asw.watcher_health(self.cfg()), "watcher every 10 min")
        with asw.edit_state() as st:
            st["last_tick"] = cf.iso(cf.now_local() - dt.timedelta(minutes=50))
        self.assertIn("has not finished a pass since", asw.watcher_health(self.cfg()))
        self.assertIn("has not finished a pass", asw.status_lines(self.cfg())[-1])

    def test_the_log_is_cut_back_in_place(self):
        d = self.state / "switch"
        d.mkdir(parents=True)
        log = d / "agent.log"
        log.write_bytes(b"".join(b"line %07d\n" % i for i in range(60000)))
        inode = os.stat(str(log)).st_ino
        asw.tick(self.cfg())
        data = log.read_bytes()
        self.assertLessEqual(len(data), asw.LOG_KEEP_BYTES)
        self.assertTrue(data.startswith(b"line ") and data.endswith(b"line 0059999\n"))
        self.assertEqual(os.stat(str(log)).st_ino, inode)

    def test_on_limit_runs_only_when_the_watcher_is_on(self):
        with mock.patch.object(asw.subprocess, "Popen") as popen:
            self.assertFalse(asw.on_limit(self.cfg()))
            popen.assert_not_called()
            self.save_cfg(watch=True)
            self.assertTrue(asw.on_limit(self.cfg()))
            argv = popen.call_args[0][0]
            self.assertEqual(argv[1:], ["watch", "--quiet"])
            self.assertEqual(popen.call_args[1]["env"]["CLAUDE_FAILOVER_CONFIG"], str(self.cfg_path))

    def test_no_helper_window_in_tests_or_when_turned_off(self):
        self.save_cfg(at_limit="app")
        self.assertIsNone(asw.open_assistant(self.cfg()))
        os.environ.pop("CLAUDE_FAILOVER_NO_NOTIFY")
        self.save_cfg(open_assistant_at_limit=False)
        with mock.patch.object(asw.subprocess, "Popen") as popen:
            self.assertIsNone(asw.open_assistant(self.cfg()))
            popen.assert_not_called()
            self.save_cfg(open_assistant_at_limit=True)
            os.environ["CLAUDE_FAILOVER_HANDOFF_DIR"] = str(self.tmp / "handoffs")
            path = asw.open_assistant(self.cfg())
            self.assertEqual(popen.call_args[0][0][:3], ["/usr/bin/open", "-a", "Terminal"])
        body = Path(path).read_text()
        self.assertIn("switch --assist", body)
        self.assertEqual(os.stat(str(path)).st_mode & 0o777, 0o700)


class SwitchCommandTest(Base):
    def fake_sync(self, cfg, dry_run=False):
        source = asw.app_account(cfg)
        other = "work" if source == "personal" else "personal"
        self.sync_calls.append({"dry_run": dry_run, "source": source})
        return {"errors": [], "chats": 12, "dry_run": dry_run, "scheduled_tasks": {
            "on": True, "source": source, "tasks": 5, "note": None,
            "targets": [{"account": other, "changes": [], "only_there": [], "written": False,
                         "note": None}]}}

    def setUp(self):
        super().setUp()
        self.usage("personal", 0.87, reset_h=140)
        self.usage("work", 0.0, reset_h=70)
        self.sync_calls = []
        p = mock.patch.object(cf, "auth_status",
                              return_value={"loggedIn": True, "email": "work@example.com"})
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(asw, "run_sync", side_effect=self.fake_sync)
        self.sync_mock = p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(asw, "probe", side_effect=AssertionError("no call expected"))
        p.start()
        self.addCleanup(p.stop)

    def test_check_says_ready_and_changes_nothing(self):
        rc, out = self.run_cli("switch", "--check")
        self.assertEqual(rc, 0, out)
        self.assertIn("Moving the desktop app from personal to work", out)
        self.assertIn("ok  login - works", out)
        self.assertIn("ok  usage - 100% of its week left", out)
        self.assertIn("ok  scheduled tasks - 5 on work, the same as on personal", out)
        self.assertIn("work can take over.", out)
        self.assertEqual(self.sync_calls, [{"dry_run": True, "source": "personal"}])
        self.assertNotIn("pending", asw.read_state())
        self.assertNotIn("app_account", asw.read_state())

    def test_by_hand_it_says_the_hook_already_covers_a_limit(self):
        said = "You only need this to move the app itself"
        rc, out = self.run_cli("switch", "--no-wait")
        self.assertEqual(rc, 0, out)
        self.assertNotIn(said, out)                    # no hook installed: nothing to point at
        self.assertIn("Your part, about 30 seconds:", out)
        folder = Path(self.raw["default_config_dir"])
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "settings.json").write_text(json.dumps({"hooks": {"StopFailure": [
            {"hooks": [{"type": "command", "command": str(cf.HOOK_SCRIPT)}]}]}}))
        rc, out = self.run_cli("switch", "--no-wait")
        self.assertEqual(rc, 0, out)
        self.assertIn(said, out)
        self.assertLess(out.index(said), out.index("Your part, about 30 seconds:"))
        rc, out = self.run_cli("switch", "--check")    # a check stays a check
        self.assertNotIn(said, out)
        rc, out = self.run_cli("switch", "--assist", "--no-wait")   # the opt-in window: it was asked for
        self.assertNotIn(said, out)
        self.assertIn("Your part, about 30 seconds:", out)

    def test_check_never_changes_anything_even_when_already_there(self):
        self.save_cfg(auto_order=True)
        text = self.cfg_path.read_text()
        state = (self.state / "switch.json").read_text()
        rc, out = self.run_cli("switch", "--check", "--to", "personal")
        self.assertEqual(rc, 0)
        self.assertIn("already on personal", out)
        self.assertEqual(self.cfg_path.read_text(), text)
        self.assertEqual((self.state / "switch.json").read_text(), state)
        self.assertEqual(self.sync_calls, [])

    def test_a_login_check_that_failed_is_not_called_signed_out(self):
        with mock.patch.object(cf, "auth_status", return_value={
                "loggedIn": False, "error": "could not run claude auth status"}):
            rc, out = self.run_cli("switch", "--check")
        self.assertEqual(rc, 0, out)
        self.assertIn("!!  login - could not be checked (could not run claude auth status)", out)
        self.assertNotIn("signed out", out)

    def test_check_fails_when_the_target_is_out_or_signed_out(self):
        self.limit("work")
        rc, out = self.run_cli("switch", "--check")
        self.assertEqual(rc, 1)
        self.assertIn("XX  usage - work is out too", out)
        with mock.patch.object(cf, "auth_status", return_value={"loggedIn": False}):
            rc, out = self.run_cli("switch", "--check", "--to", "work")
        self.assertIn("XX  login - its Claude Code login is signed out", out)
        with mock.patch.object(cf, "auth_status",
                               return_value={"loggedIn": True, "email": "else@example.com"}):
            rc, out = self.run_cli("switch", "--check", "--to", "work")
        self.assertIn("signed in as else@example.com, not work@example.com", out)

    def test_tasks_behind_and_sync_problems_are_shown(self):
        def behind(cfg, dry_run=False):
            report = self.fake_sync(cfg, dry_run)
            report["scheduled_tasks"]["targets"][0].update({
                "changes": [{"action": "add", "id": "a"}],
                "only_there": [{"id": "x", "name": "old task", "live": True}]})
            report["errors"] = ["boom"]
            return report

        self.sync_mock.side_effect = behind
        rc, out = self.run_cli("switch", "--check")
        self.assertEqual(rc, 0)
        self.assertIn("!!  chats - the sync had 1 problem", out)
        self.assertIn("!!  scheduled tasks - work is 1 change behind personal", out)
        self.assertIn("!!  scheduled tasks - only on work and still on: old task", out)

    def test_copy_of_the_tool_without_task_sync(self):
        self.sync_mock.side_effect = lambda cfg, dry_run=False: {"errors": [], "chats": 3}
        rc, out = self.run_cli("switch", "--check")
        self.assertIn("!!  scheduled tasks - not checked", out)
        self.sync_mock.side_effect = lambda cfg, dry_run=False: None
        rc, out = self.run_cli("switch", "--check")
        self.assertIn("not copied (desktop sync is not available)", out)
        self.sync_mock.side_effect = lambda cfg, dry_run=False: {"skipped": "another sync is running"}
        rc, out = self.run_cli("switch", "--check")
        self.assertIn("another sync is running", out)

    def test_bad_arguments(self):
        self.assertEqual(self.run_cli("switch", "--nope")[0], 2)
        self.assertEqual(self.run_cli("switch", "--to")[0], 2)
        self.assertEqual(self.run_cli("switch", "--to", "nobody")[0], 2)
        self.assertEqual(self.run_cli("switch", "--wait", "soon")[0], 2)
        self.assertEqual(self.run_cli("usage", "--nope")[0], 2)
        self.assertEqual(self.run_cli("watch", "--nope")[0], 2)
        self.assertEqual(self.run_cli("watch", "status", "extra")[0], 2)

    def test_one_account(self):
        self.raw["accounts"] = self.raw["accounts"][:1]
        self.save_cfg()
        rc, out = self.run_cli("switch")
        self.assertEqual(rc, 1)
        self.assertIn("Only one account", out)

    def test_wont_switch_to_an_account_that_is_out_unless_forced(self):
        self.limit("work")
        rc, out = self.run_cli("switch", "--no-wait")
        self.assertEqual(rc, 1)
        self.assertIn("cannot take over yet", out)
        self.assertNotIn("pending", asw.read_state())
        rc, out = self.run_cli("switch", "--no-wait", "--force")
        self.assertEqual(rc, 0)
        self.assertEqual(asw.read_state()["pending"]["to"], "work")

    def test_waits_for_the_sign_in_then_finishes(self):
        self.save_cfg(auto_order=True)
        self.ext(tables={5: [(10, P_ACCT)]})
        timer = threading.Timer(0.25, self.sign_in, args=(W_ACCT,))
        timer.start()
        with mock.patch.object(asw, "wait_for_extension", return_value=False) as ext_wait:
            rc, out = self.run_cli("switch", "--wait", "1")
        timer.join()
        self.assertEqual(rc, 0, out)
        before, after = out.split("Finishing")
        self.assertIn("ok  scheduled tasks - 5 on work, the same as on personal", before)
        self.assertIn("Sign in as work@example.com", before)
        self.assertIn("ok  the app is on work", after)
        self.assertIn("ok  scheduled tasks - 5 on personal, the same as on work", after)
        self.assertIn("!!  Chrome extension is on personal: sign it in as work@example.com", after)
        self.assertIn("Done. The app is on work.", after)
        ext_wait.assert_called_once()
        st = asw.read_state()
        self.assertEqual(st["app_account"], "work")
        self.assertNotIn("pending", st)
        self.assertIn("app_changed_at", st)
        self.assertEqual([t for t, _ in self.sent], ["Claude: the app is now on work"])
        self.assertEqual(self.sync_calls, [{"dry_run": False, "source": "personal"},
                                           {"dry_run": False, "source": "work"}])
        self.assertEqual(self.on_disk()["order"]["headless"], ["personal", "work"])
        # the watcher's next pass does not announce it again
        self.sent[:] = []
        with mock.patch.object(asw, "probe", return_value={"at": cf.iso(cf.now_local()), "ok": True,
                                                           "util": 0.1}):
            asw.tick(self.cfg())
        self.assertEqual([t for t, _ in self.sent if "now on" in t], [])

    def test_the_hand_over_list_is_shown_when_desktop_sync_offers_one(self):
        import desktop_sync
        self.assertEqual(asw.handover_lines(self.cfg()) if not hasattr(desktop_sync, "handover_lines")
                         else [], [])
        lines = []
        with mock.patch.object(desktop_sync, "handover_lines", create=True,
                               return_value=["Fix the parser - was mid-task", "Email draft - waiting on you"]):
            asw.finish_switch(self.cfg(), "personal", "work", lines.append, announce=False)
        text = "\n".join(lines)
        self.assertIn("chats to pick up on work:", text)
        self.assertIn("Fix the parser - was mid-task", text)
        with mock.patch.object(desktop_sync, "handover_lines", create=True,
                               side_effect=RuntimeError("boom")):
            self.assertEqual(asw.handover_lines(self.cfg()), [])

    def test_switching_back_is_announced_even_when_the_watcher_never_ran(self):
        # The state still says "work" from long ago; the app is on personal now.
        with asw.edit_state() as st:
            st["app_account"] = "work"
        self.usage("personal", 0.9, reset_h=140)
        self.usage("work", 0.1, reset_h=70)
        timer = threading.Timer(0.25, self.sign_in, args=(W_ACCT,))
        timer.start()
        rc, out = self.run_cli("switch", "--wait", "1")
        timer.join()
        self.assertEqual(rc, 0, out)
        self.assertIn("The app moved from work to personal since the last look.", out)
        self.assertEqual([t for t, _ in self.sent],
                         ["Claude: the app is now on personal", "Claude: the app is now on work"])

    def test_extension_follows_within_the_wait(self):
        target = self.tmp / "tool.json"
        target.write_text('{"fetch": {"chrome_account": "personal"}}')
        self.save_cfg(followers=[{"file": str(target), "key": "fetch.chrome_account",
                                  "follows": "extension"}])
        self.ext(tables={5: [(10, P_ACCT)]}, logs={7: []})

        def both():
            self.sign_in(W_ACCT)
            threading.Timer(0.2, self.ext_log, args=(7, [(20, W_ACCT)])).start()

        timer = threading.Timer(0.2, both)
        timer.start()
        rc, out = self.run_cli("switch", "--wait", "1")
        timer.join()
        self.assertEqual(rc, 0, out)
        self.assertIn("ok  Chrome extension is on work", out)
        self.assertEqual(json.loads(target.read_text())["fetch"]["chrome_account"], "work")

    def test_extension_wait_depends_on_the_extension_being_there(self):
        for there in (False, True):
            self.sign_in(P_ACCT)
            with asw.edit_state() as st:
                st.clear()
            self.usage("personal", 0.87, reset_h=140)
            self.usage("work", 0.0, reset_h=70)
            if there:
                self.ext(tables={5: [(10, P_ACCT)]}, manifest=False)      # there, unreadable right now
            timer = threading.Timer(0.2, self.sign_in, args=(W_ACCT,))
            timer.start()
            with mock.patch.object(asw, "wait_for_extension", return_value=False) as ext_wait:
                rc, out = self.run_cli("switch", "--wait", "1")
            timer.join()
            self.assertEqual(rc, 0, out)
            self.assertEqual(ext_wait.call_count, 1 if there else 0)
            self.assertEqual("Chrome extension not switched yet" in out, there)

    def test_extension_wait_sits_out_a_moment_it_cannot_read(self):
        answers = [{"state": "unknown"}, {"state": "ok", "account": "personal"},
                   {"state": "ok", "account": "work"}]
        with mock.patch.object(asw, "extension_account", side_effect=lambda cfg: answers.pop(0)):
            self.assertTrue(asw.wait_for_extension(self.cfg(), "work", 5, poll=0.01))
        with mock.patch.object(asw, "extension_account", return_value={"state": "unknown"}):
            self.assertFalse(asw.wait_for_extension(self.cfg(), "work", 0.1, poll=0.01))

    def test_time_is_up(self):
        rc, out = self.run_cli("switch", "--wait", "0")
        self.assertEqual(rc, 1)
        self.assertIn("did not change account", out)
        self.assertEqual(asw.read_state()["pending"]["to"], "work")

    def test_already_there(self):
        rc, out = self.run_cli("switch", "--to", "personal")
        self.assertEqual(rc, 0)
        self.assertIn("already on personal", out)
        self.assertEqual(self.sent, [])

    def test_signed_in_to_another_account_than_planned(self):
        self.raw["accounts"].append({"name": "third", "email": "third@example.com",
                                     "desktop_account_uuid": X_ACCT})
        self.save_cfg()
        self.usage("third", 0.5)
        timer = threading.Timer(0.2, self.sign_in, args=(X_ACCT,))
        timer.start()
        rc, out = self.run_cli("switch", "--to", "work", "--wait", "1")
        timer.join()
        self.assertEqual(rc, 0, out)
        self.assertIn("The app is now on third, not work.", out)
        self.assertEqual(asw.read_state()["app_account"], "third")

    def test_signed_in_to_an_account_outside_the_config(self):
        timer = threading.Timer(0.2, self.sign_in, args=(X_ACCT,))
        timer.start()
        rc, out = self.run_cli("switch", "--wait", "1")
        timer.join()
        self.assertEqual(rc, 1)
        self.assertIn("not in the config", out)

    def test_helper_window_stops_once_the_account_works_again(self):
        self.limit("personal", hours=1)
        self.assertTrue(asw.assist_needed(self.cfg(), "personal"))        # out: keep waiting
        with cf.edit_state() as st:
            st["accounts"]["personal"].pop("limited_until")
        self.usage("personal", 0.2)
        self.assertFalse(asw.assist_needed(self.cfg(), "personal"))       # has usage again
        with asw.edit_state() as st:
            st["usage"].pop("personal")
        self.assertTrue(asw.assist_needed(self.cfg(), "personal"))        # not known: keep waiting
        self.assertTrue(asw.assist_needed(self.cfg(), "nobody"))
        # the wait itself ends when it is told the helper is no longer needed
        calls = []
        self.assertIsNone(asw.wait_for_app(self.cfg(), P_ACCT, 30, poll=0.01,
                                           keep_waiting=lambda: calls.append(1) or len(calls) < 3))
        self.assertEqual(len(calls), 3)
        # and the command says so instead of "did not change account"
        self.usage("personal", 0.2)
        with mock.patch.object(asw, "wait_for_app", return_value=None):
            rc, out = self.run_cli("switch", "--assist", "--force")
        self.assertEqual(rc, 0, out)
        self.assertIn("personal has usage again, so there is no need to switch.", out)

    def test_recent_scheduled_run_is_mentioned(self):
        folder = self.desktop / "claude-code-sessions" / P_ACCT.upper() / "org"
        folder.mkdir(parents=True)
        now_ms = time.time() * 1000
        (folder / "local_a.json").write_text(json.dumps(
            {"title": "Hourly mail", "scheduledTaskId": "t1", "createdAt": now_ms - 40 * 60000}))
        (folder / "local_b.json").write_text(json.dumps(
            {"title": "A chat", "createdAt": now_ms - 60000}))
        (folder / "local_c.json").write_text(json.dumps(
            {"title": "Old run", "scheduledTaskId": "t1", "createdAt": now_ms - 3600000}))
        os.utime(str(folder / "local_c.json"), (time.time() - 3600, time.time() - 3600))
        rc, out = self.run_cli("switch", "--check")
        # started 40 minutes ago, written a moment ago: it may still be working
        self.assertIn("a scheduled task was active in the last 15 minutes (Hourly mail)", out)
        self.assertNotIn("Old run", out)


class UsageAndStatusTest(Base):
    def test_usage_prints_both_accounts(self):
        self.usage("personal", 0.87, reset_h=140, extra=True)
        self.usage("work", 0.0, reset_h=70)
        with mock.patch.object(asw, "probe", side_effect=AssertionError("fresh readings")):
            rc, out = self.run_cli("usage")
        self.assertEqual(rc, 0)
        self.assertIn("personal  87% of its week used", out)
        self.assertIn("extra usage on   <- the desktop app", out)
        self.assertIn("work      0% of its week used", out)
        self.assertIn("background jobs use personal -> work", out)

    def test_usage_fresh_reads_and_json(self):
        read = mock.Mock(side_effect=lambda cfg, name, now=None: {
            "at": cf.iso(cf.now_local()), "ok": True, "util": 0.5, "window": "seven_day"})
        with mock.patch.object(asw, "probe", read):
            rc, out = self.run_cli("usage", "--fresh", "--json")
        self.assertEqual(read.call_count, 2)
        data = json.loads(out)
        self.assertEqual(data["app"], "personal")
        self.assertEqual(data["accounts"]["work"]["room"], 0.5)

    def test_usage_states(self):
        self.limit("personal", hours=4)
        with cf.edit_state() as st:
            st["accounts"].setdefault("work", {})["auth_down_until"] = cf.iso(
                cf.now_local() + dt.timedelta(minutes=20))
        with mock.patch.object(asw, "probe", side_effect=AssertionError("neither can be read")):
            rc, out = self.run_cli("usage")
        self.assertIn("personal  out of usage, back", out)
        self.assertIn("work      login failing", out)

    def test_status_lines_never_raise(self):
        self.assertEqual(asw.status_lines(self.cfg()), [])     # never used: nothing to say
        self.usage("work", 0.25)
        lines = asw.status_lines(self.cfg())
        self.assertEqual(len(lines), 4)
        self.assertIn("personal: usage not read yet (the desktop app is here)", lines[0])
        self.assertIn("work: 25% of its week used", lines[1])
        self.assertTrue(lines[2].startswith("At a limit - "), lines[2])
        self.assertEqual(lines[-1], "Watcher - off (claude-accounts watch install)")
        with asw.edit_state() as st:
            st["usage"].clear()
        self.save_cfg(watch=True)
        self.assertEqual(len(asw.status_lines(self.cfg())), 4)
        self.assertEqual(asw.status_lines({"accounts": None}), [])
        self.assertEqual(asw.status_lines(None), [])

    def test_wired_into_claude_accounts(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cf.accounts_main(["watch", "status"])
        self.assertEqual(rc, 0)
        self.assertIn("Account watcher", out.getvalue())
        self.assertIn("switch [--to ACCOUNT]", cf.ACCOUNTS_USAGE)


class WatchJobTest(Base):
    def test_install_status_uninstall(self):
        rc, out = self.run_cli("watch", "install", "--every", "7")
        self.assertEqual(rc, 0, out)
        with open(str(self.agents / (asw.LABEL + ".plist")), "rb") as f:
            job = plistlib.load(f)
        self.assertEqual(job["StartInterval"], 420)
        self.assertEqual(job["ProgramArguments"][1:], ["watch", "--quiet"])
        self.assertEqual(job["EnvironmentVariables"]["CLAUDE_FAILOVER_CONFIG"], str(self.cfg_path))
        self.assertEqual(job["EnvironmentVariables"]["CLAUDE_FAILOVER_STATE_DIR"], str(self.state))
        self.assertIn("/usr/bin", job["EnvironmentVariables"]["PATH"])
        self.assertIs(self.on_disk()["switch"]["watch"], True)
        self.assertEqual(self.on_disk()["keep_me"], {"untouched": [1, 2, 3]})
        rc, out = self.run_cli("watch", "status")
        self.assertEqual(rc, 0)
        self.assertIn("every 7 min", out)
        self.assertEqual(asw.status_lines(self.cfg())[-1], "Watcher - every 7 min")
        rc, out = self.run_cli("watch", "uninstall")
        self.assertEqual(rc, 0)
        self.assertFalse((self.agents / (asw.LABEL + ".plist")).exists())
        self.assertIs(self.on_disk()["switch"]["watch"], False)
        rc, out = self.run_cli("watch", "uninstall")
        self.assertIn("was not installed", out)

    def test_install_rejects_a_bad_interval(self):
        self.assertEqual(self.run_cli("watch", "install", "--every", "1")[0], 2)
        self.assertEqual(self.run_cli("watch", "install", "--every")[0], 2)
        self.assertFalse((self.agents / (asw.LABEL + ".plist")).exists())

    def test_broken_job_is_named(self):
        self.agents.mkdir()
        with open(str(self.agents / (asw.LABEL + ".plist")), "wb") as f:
            plistlib.dump({"Label": asw.LABEL, "StartInterval": 600,
                           "ProgramArguments": [str(self.tmp / "gone"), "watch"]}, f)
        rc, out = self.run_cli("watch", "status")
        self.assertEqual(rc, 1)
        self.assertIn("BROKEN", out)

    def test_watch_prints_what_it_did(self):
        self.save_cfg(auto_order=True)
        self.usage("personal", 0.87, reset_h=140)
        self.usage("work", 0.0, reset_h=70)
        rc, out = self.run_cli("watch", "--quiet")
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.strip().splitlines()), 2)
        rc, out = self.run_cli("watch", "--quiet")
        self.assertEqual(out, "")
        rc, out = self.run_cli("watch")
        self.assertEqual(out.strip(), "Nothing to do.")


if __name__ == "__main__":
    unittest.main()
