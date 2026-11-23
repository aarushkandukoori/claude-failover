"""Tests for desktop_backlog.py: the rules that mirror the task backlog.

Plain lists in and lists out, so nothing here touches a file. The tests that
read and write backlog files are in test_desktop_sync.py.

Run:  /usr/bin/python3 -m unittest discover -s tests -p 'test_desktop_backlog.py' -v
"""
import copy
import json
import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import desktop_backlog as db  # noqa: E402

NOW = 1795400000000          # "now" in ms, like the app's suggestedAt
DAY = 24 * 3600 * 1000


def item(name, age_days=1, **more):
    """A backlog item shaped like the app's."""
    data = {"id": "todo_%012x" % (abs(hash(name)) % (16 ** 12)), "cwd": "/Users/me/proj",
            "source": "dismissed", "title": "Task %s" % name, "tldr": "short", "prompt": "do %s" % name,
            "suggestedAt": NOW - age_days * DAY, "filedAt": NOW - age_days * DAY + 5,
            "originSessionId": "local_chat-%s" % name, "originTaskId": "task_%s" % name}
    data.update(more)
    return data


def key(name):
    return "local_chat-%s task_%s" % (name, name)


class ParseTest(unittest.TestCase):

    def test_reads_the_apps_file(self):
        doc = {"version": 1, "items": [item("a"), item("b")]}
        self.assertEqual(db.parse(json.dumps(doc, separators=(",", ":")).encode()), doc)
        self.assertEqual(db.parse(b'{"version":1,"items":[]}'), {"version": 1, "items": []})

    def test_anything_else_is_refused(self):
        bad = [b"", b"not json", b"[]", b"{}", b'{"version":1}', b'{"version":1,"items":{}}',
               b'{"items":[]}',                                   # no version
               b'{"version":true,"items":[]}', b'{"version":"1","items":[]}',
               b'{"version":2,"items":[]}',                       # a newer format
               b'{"version":1,"items":["a"]}',
               b'{"version":1,"items":[{"suggestedAt":NaN}]}',
               b'{"version":1,"items":[{"suggestedAt":1e400}]}',
               b'\xef\xbb\xbf{"version":1,"items":[]}', b'\xff\xfe']
        for data in bad:
            with self.assertRaises(db.NotABacklog, msg=data):
                db.parse(data)

    def test_writes_the_file_the_way_the_app_does(self):
        doc = {"version": 1, "items": [item("a", title="Café fix")]}
        data = db.encode(doc)
        self.assertEqual(data, json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        self.assertTrue(data.startswith(b'{"version":1,"items":[{"id":"todo_'))
        self.assertNotIn(b"\n", data)
        self.assertIn("Café".encode("utf-8"), data)
        self.assertEqual(db.parse(data), doc)
        half = {"version": 1, "items": [item("a", title="bad \ud83d end")]}
        self.assertEqual(db.parse(db.encode(half)), half)
        with self.assertRaises(ValueError):
            db.encode({"version": 1, "items": [item("a", suggestedAt=float("inf"))]})


class ItemTest(unittest.TestCase):

    def test_same_item_means_same_chat_and_same_proposal(self):
        self.assertEqual(db.key(item("a")), key("a"))
        self.assertEqual(db.key(item("a", id="todo_other", title="renamed")), key("a"))
        for broken in ({"originSessionId": ""}, {"originTaskId": None}, {"originSessionId": 5}):
            self.assertIsNone(db.key(item("a", **broken)))
        self.assertEqual(db.keys([item("b"), item("a"), item("b"), {"id": "x"}]), [key("a"), key("b")])

    def test_fresh_for_30_days(self):
        self.assertTrue(db.is_fresh(item("a", age_days=29.9), NOW))
        self.assertFalse(db.is_fresh(item("a", age_days=30), NOW))
        self.assertFalse(db.is_fresh(item("a", suggestedAt=None), NOW))
        self.assertFalse(db.is_fresh(item("a", suggestedAt=True), NOW))

    def test_label(self):
        self.assertEqual(db.label(item("a", title="  Fix\n the thing ")), "Fix the thing")
        self.assertEqual(db.label({"id": "todo_1", "title": ""}), "todo_1")
        self.assertEqual(db.label({}), "(no title)")


class PlanTest(unittest.TestCase):

    def plan(self, source, target, seen=None):
        """plan() without its list of items that found no room, with a check that it
        never changes what it was given."""
        return self.full_plan(source, target, seen)[:4]

    def full_plan(self, source, target, seen=None):
        before = copy.deepcopy((source, target, seen))
        out = db.plan(source, target, seen, NOW)
        self.assertEqual((source, target, seen), before)
        return out

    def test_the_other_list_is_never_filled_past_what_the_app_keeps(self):
        # The app keeps 20 items per folder and drops the oldest beyond that at its next
        # save. Ten of the other account's own items share the folder: only ten more fit,
        # or the app would throw out that account's own.
        own = [item("own%02d" % i, age_days=20, filedAt=NOW - 20 * DAY + i) for i in range(10)]
        mine = [item("new%02d" % i, age_days=2, filedAt=NOW - 2 * DAY + i) for i in range(15)]
        new, added, removed, only_there, no_room = self.full_plan(mine, own)
        self.assertEqual((len(new), len(added), removed, only_there), (20, 10, [], 10))
        self.assertEqual(new[:10], own)                               # all of its own are still there
        self.assertEqual(added, ["Task new%02d" % i for i in range(5, 15)])   # the newest ten, in order
        self.assertEqual(no_room, ["Task new%02d" % i for i in range(5)])
        # Another folder has room of its own, and a folder is the same folder however it is spelled.
        elsewhere = [item("else", cwd="/Users/me/other")]
        respelled = [item("slash", cwd="/Users/me/proj/"), item("upper", cwd="/Users/Me/Proj")]
        _new, added, _r, _o, no_room = self.full_plan(elsewhere + respelled, new)
        self.assertEqual((added, no_room), (["Task else"], ["Task slash", "Task upper"]))

    def test_at_most_100_items_in_all(self):
        full = [item("t%03d" % i, cwd="/f/%d" % (i // 10)) for i in range(100)]
        new, added, _r, _o, no_room = self.full_plan([item("one more", cwd="/somewhere/new")], full)
        self.assertEqual((len(new), added, no_room), (100, [], ["Task one more"]))
        # Items the app is about to drop for their age do not take up room.
        aged = [dict(i, suggestedAt=NOW - 40 * DAY) for i in full[:1]] + full[1:]
        _new, added, _r, _o, no_room = self.full_plan([item("one more", cwd="/somewhere/new")], aged)
        self.assertEqual((added, no_room), (["Task one more"], []))

    def test_missing_items_are_added(self):
        own = item("theirs")
        new, added, removed, only_there = self.plan([item("a"), item("b")], [own, item("a")])
        self.assertEqual(new, [own, item("a"), item("b")])     # theirs first, then the new one
        self.assertEqual((added, removed, only_there), (["Task b"], [], 1))

    def test_same_item_is_never_added_twice(self):
        mine = item("a")
        theirs = item("a", id="todo_aaaaaaaaaaaa", title="an older wording")   # same chat and proposal
        new, added, _removed, only_there = self.plan([mine, copy.deepcopy(mine)], [theirs])
        self.assertEqual((new, added, only_there), ([theirs], [], 0))
        # Two different items under one id: the app could not hold both.
        clash = item("b", id=theirs["id"])
        self.assertEqual(self.plan([clash], [theirs])[:2], ([theirs], []))

    def test_old_and_broken_items_are_not_copied(self):
        source = [item("old", age_days=31), item("nokey", originTaskId=""), item("fresh", age_days=2)]
        new, added, _removed, _only = self.plan(source, [])
        self.assertEqual((new, added), ([item("fresh", age_days=2)], ["Task fresh"]))

    def test_item_that_left_the_signed_in_list_is_removed_there_too(self):
        # It was on this account's list at the last sync and is gone now: started or dropped.
        target = [item("started"), item("kept"), item("theirs")]
        new, added, removed, only_there = self.plan([item("kept")], target,
                                                    seen=[key("started"), key("kept")])
        self.assertEqual(new, [item("kept"), item("theirs")])
        self.assertEqual((added, removed, only_there), ([], ["Task started"], 1))

    def test_nothing_is_removed_without_a_memory_of_the_last_sync(self):
        target = [item("x"), item("y")]
        for seen in (None, []):
            new, added, removed, only_there = self.plan([], target, seen=seen)
            self.assertEqual((new, added, removed, only_there), (target, [], [], 2))

    def test_item_the_signed_in_account_never_had_stays(self):
        target = [item("theirs"), {"id": "todo_odd", "title": "an entry without an origin"}]
        new, _added, removed, only_there = self.plan([item("a")], target, seen=[key("a"), key("gone")])
        self.assertEqual((new, removed, only_there), (target + [item("a")], [], 2))

    def test_a_second_plan_changes_nothing(self):
        source = [item("a"), item("b"), item("old", age_days=40)]
        target = [item("theirs"), item("started")]
        seen = [key("started"), key("a")]
        new, added, removed, only_there = self.plan(source, target, seen)
        self.assertEqual((added, removed), (["Task a", "Task b"], ["Task started"]))
        again = self.full_plan(source, new, db.keys(source))
        self.assertEqual(again, (new, [], [], only_there, []))

    def test_after_a_switch_the_other_account_leads(self):
        # A is signed in: its item reaches B. Then the app switches to B and the
        # task is started there, so it leaves B's list: it must leave A's too.
        a, b = [item("x"), item("y")], []
        b, added, _r, _o = self.plan(a, b, None)
        self.assertEqual(added, ["Task x", "Task y"])
        b_after_the_app = [i for i in b if i["title"] != "Task x"]        # started on B
        a, added, removed, _o = self.plan(b_after_the_app, a, seen=db.keys(b))
        self.assertEqual((a, added, removed), ([item("y")], [], ["Task x"]))


if __name__ == "__main__":
    unittest.main()
