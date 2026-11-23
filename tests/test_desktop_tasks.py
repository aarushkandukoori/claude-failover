"""Tests for desktop_tasks.py: the rules that mirror desktop scheduled tasks.

These are plain lists in and lists out, so nothing here touches a file. The
tests that read and write scheduled-tasks.json are in test_desktop_sync.py.

Run:  /usr/bin/python3 -m unittest discover -s tests -p 'test_desktop_tasks.py' -v
"""
import copy
import json
import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import desktop_tasks as dt  # noqa: E402

NOW = 1795400000000          # "now" in ms, like the app's fireAt
HOUR = 3600 * 1000


def cron(tid, expr="0 9 * * *", **more):
    """A repeating task, shaped like the app's entries."""
    task = {"id": tid, "displayName": "Task %s" % tid, "cronExpression": expr, "enabled": True,
            "filePath": "/tasks/%s/SKILL.md" % tid, "createdAt": NOW - 90 * HOUR}
    task.update(more)
    return task


def once(tid, fire_at, **more):
    """A one-time task."""
    task = {"id": tid, "displayName": "Task %s" % tid, "fireAt": fire_at, "enabled": True,
            "filePath": "/tasks/%s/SKILL.md" % tid, "createdAt": NOW - 90 * HOUR}
    task.update(more)
    return task


def ran(task, at="2026-11-22T14:00:07.000Z", slot="2026-11-22T14:00:00.000Z"):
    """The task the way the app leaves it after its one run."""
    return dict(task, enabled=False, lastRunAt=at, lastScheduledFor=slot)


def actions(changes):
    return [(c["action"], c["id"]) for c in changes]


class ParseTest(unittest.TestCase):

    def test_reads_the_apps_file(self):
        doc = {"scheduledTasks": [cron("a"), once("b", NOW + HOUR)], "recordedSkips": {}}
        self.assertEqual(dt.parse(json.dumps(doc, indent=2).encode()), doc)

    def test_anything_else_is_refused(self):
        bad = [b"", b"not json", b"[]", b"{}", b'{"scheduledTasks": {}}',
               b'{"scheduledTasks": ["a"]}',
               b'{"scheduledTasks": [{"enabled": true}]}',                    # no id
               b'{"scheduledTasks": [{"id": "", "enabled": true}]}',
               b'{"scheduledTasks": [{"id": 7, "enabled": true}]}',
               b'{"scheduledTasks": [{"id": "a"}]}',                          # neither on nor off
               b'{"scheduledTasks": [{"id": "a", "enabled": 1}]}',
               b'{"scheduledTasks": [{"id": "a", "enabled": true}, {"id": "a", "enabled": false}]}',
               b'{"scheduledTasks": [{"id": "a", "enabled": true, "fireAt": NaN}]}',
               b'{"scheduledTasks": [{"id": "a", "enabled": true, "fireAt": 1e400}]}',  # infinity
               b'\xff\xfe{"scheduledTasks": []}',                             # not UTF-8
               b'\xef\xbb\xbf{"scheduledTasks": []}',                         # a byte order mark
               '{"scheduledTasks": []}'.encode("utf-16")]
        for data in bad:
            with self.assertRaises(dt.NotATaskList, msg=data):
                dt.parse(data)

    def test_writes_the_file_the_way_the_app_does(self):
        doc = {"scheduledTasks": [cron("a", displayName="Caf\u00e9 report")], "recordedSkips": {}}
        data = dt.encode(doc)
        self.assertEqual(data, json.dumps(doc, indent=2, ensure_ascii=False).encode("utf-8"))
        self.assertTrue(data.startswith(b'{\n  "scheduledTasks": [\n    {\n      "id": "a",'))
        self.assertFalse(data.endswith(b"\n"))
        self.assertIn("Caf\u00e9".encode("utf-8"), data)
        self.assertEqual(dt.parse(data), doc)

    def test_half_an_emoji_still_writes(self):
        doc = {"scheduledTasks": [cron("a", displayName="bad \ud83d end")]}
        self.assertEqual(dt.parse(dt.encode(doc)), doc)

    def test_a_number_json_cannot_hold_is_not_written(self):
        with self.assertRaises(ValueError):
            dt.encode({"scheduledTasks": [cron("a", createdAt=float("nan"))]})


class KindTest(unittest.TestCase):

    def test_one_time_repeating_and_by_hand(self):
        self.assertTrue(dt.is_one_time(once("a", NOW)))
        self.assertFalse(dt.is_one_time(cron("a")))
        self.assertFalse(dt.is_one_time({"id": "a", "enabled": True}))          # runs by hand
        self.assertFalse(dt.is_one_time({"id": "a", "enabled": True, "fireAt": True}))

    def test_live_and_pending(self):
        self.assertTrue(dt.is_live(cron("a")))
        self.assertFalse(dt.is_live(cron("a", enabled=False)))
        self.assertTrue(dt.is_live(once("a", NOW - HOUR)))        # overdue: it runs at sign-in
        self.assertFalse(dt.is_live(ran(once("a", NOW - HOUR))))
        self.assertFalse(dt.is_live(once("a", NOW + HOUR, lastRunAt="2026-11-22T14:00:07.000Z")))
        self.assertTrue(dt.is_pending(once("a", NOW + HOUR), NOW))
        self.assertFalse(dt.is_pending(once("a", NOW - HOUR), NOW))
        self.assertFalse(dt.is_pending(once("a", NOW + HOUR, enabled=False), NOW))
        self.assertFalse(dt.is_pending(cron("a"), NOW))


class PlanTest(unittest.TestCase):

    def plan(self, source, target):
        """plan(), with a check that it never changes what it was given."""
        before = copy.deepcopy((source, target))
        out = dt.plan(source, target, NOW)
        self.assertEqual((source, target), before)
        return out

    # -- same id: replace --

    def test_same_id_is_replaced_by_an_exact_copy(self):
        mine = cron("mail", "0 * * * *", displayName="Mail - every hour",
                    lastRunAt="2026-11-23T03:08:13.063Z",
                    lastScheduledFor="2026-11-23T03:00:00.000Z",
                    missedRunScanFloor="2026-11-23T00:55:51.170Z", permissionMode="bypassPermissions")
        theirs = cron("mail", "0 9 * * *", displayName="Mail - daily 9 AM",
                      lastRunAt="2026-11-22T14:01:00.000Z",
                      lastScheduledFor="2026-11-22T14:00:00.000Z", notifySessionId="local_x")
        new, changes, only_there = self.plan([mine], [theirs])
        self.assertEqual(new, [mine])            # the old entry is gone, field for field
        self.assertEqual(actions(changes), [("update", "mail")])
        self.assertEqual(only_there, [])
        changed = {f["field"]: (f["from"], f["to"]) for f in changes[0]["fields"]}
        self.assertEqual(changed["cronExpression"], ("0 9 * * *", "0 * * * *"))
        self.assertEqual(changed["lastRunAt"],
                         ("2026-11-22T14:01:00.000Z", "2026-11-23T03:08:13.063Z"))
        self.assertEqual(changed["notifySessionId"], ("local_x", None))
        self.assertEqual(changed["permissionMode"], (None, "bypassPermissions"))
        self.assertNotIn("id", changed)

    def test_a_run_moves_the_run_times_so_a_switch_does_not_replay_it(self):
        theirs = cron("mail", lastRunAt="2026-11-22T14:00:09.000Z",
                      lastScheduledFor="2026-11-22T14:00:00.000Z")
        mine = dict(theirs, lastRunAt="2026-11-23T14:00:09.000Z",
                    lastScheduledFor="2026-11-23T14:00:00.000Z")
        new, changes, _ = self.plan([mine], [theirs])
        self.assertEqual((new[0]["lastRunAt"], new[0]["lastScheduledFor"]),
                         ("2026-11-23T14:00:09.000Z", "2026-11-23T14:00:00.000Z"))
        self.assertTrue(dt.only_run_times(changes[0]))

    def test_equal_lists_need_nothing(self):
        tasks = [cron("a"), once("b", NOW + HOUR), ran(once("c", NOW - HOUR))]
        new, changes, only_there = self.plan(tasks, copy.deepcopy(tasks))
        self.assertEqual((new, changes, only_there), (tasks, [], []))

    def test_key_order_is_not_a_difference_but_true_and_1_are(self):
        mine = cron("a")
        shuffled = dict(reversed(list(mine.items())))
        self.assertEqual(self.plan([mine], [shuffled])[1], [])
        looks_equal = dict(mine, createdAt=True)
        mine = dict(mine, createdAt=1)
        self.assertEqual(looks_equal, mine)      # Python calls them equal
        new, changes, _ = self.plan([mine], [looks_equal])
        self.assertEqual(actions(changes), [("update", "a")])
        self.assertIs(new[0]["createdAt"], 1)

    # -- missing: add --

    def test_missing_repeating_tasks_are_added(self):
        off = cron("paused", enabled=False)
        by_hand = {"id": "by-hand", "enabled": True, "filePath": "/tasks/by-hand/SKILL.md",
                   "createdAt": NOW}
        keep = cron("keep")
        new, changes, _ = self.plan([cron("new"), off, by_hand, keep], [keep])
        self.assertEqual(new, [keep, cron("new"), off, by_hand])   # theirs first, then the new ones
        self.assertEqual(actions(changes), [("add", "new"), ("add", "paused"), ("add", "by-hand")])
        self.assertEqual([c["schedule"] for c in changes], ["0 9 * * *", "0 9 * * *", "run by hand"])

    def test_one_time_task_is_added_only_while_on_and_ahead(self):
        ahead = once("ahead", NOW + HOUR)
        source = [ahead,
                  once("overdue", NOW - HOUR),                  # about to run here
                  once("off", NOW + HOUR, enabled=False),
                  ran(once("finished", NOW - HOUR)),
                  once("ran-early", NOW + HOUR, lastRunAt="2026-11-22T14:00:07.000Z")]
        new, changes, only_there = self.plan(source, [])
        self.assertEqual(new, [ahead])
        self.assertEqual(actions(changes), [("add", "ahead")])
        self.assertEqual(only_there, [])
        self.assertTrue(changes[0]["schedule"].startswith("once, "))

    # -- one-time tasks that ran --

    def test_one_time_task_that_ran_is_marked_as_run_on_its_twin(self):
        armed = once("renew", NOW - HOUR, notifySessionId="local_x")
        mine = ran(once("renew", NOW - HOUR))
        new, changes, only_there = self.plan([mine], [armed])
        self.assertEqual(new, [mine])
        self.assertEqual((new[0]["enabled"], new[0]["lastRunAt"]),
                         (False, "2026-11-22T14:00:07.000Z"))
        self.assertFalse(dt.is_live(new[0]))                 # it cannot fire a second time
        self.assertEqual(actions(changes), [("mark_run", "renew")])
        self.assertEqual(only_there, [])

    def test_twin_under_another_id_is_marked_as_run_too(self):
        # A copy made by hand under another id: same name, same time.
        theirs = once("renew", NOW - HOUR, displayName="Renew the phone app",
                      permissionMode="auto", notifySessionId="local_x")
        mine = ran(once("renew-2", NOW - HOUR, displayName="Renew the phone app"))
        new, changes, only_there = self.plan([mine], [theirs])
        self.assertEqual(new, [dict(theirs, enabled=False, lastRunAt=mine["lastRunAt"],
                                    lastScheduledFor=mine["lastScheduledFor"])])
        self.assertEqual(actions(changes), [("mark_run", "renew")])
        self.assertEqual([f["field"] for f in changes[0]["fields"]],
                         ["enabled", "lastRunAt", "lastScheduledFor"])
        self.assertEqual(only_there, [])                     # a twin, not a stranger
        self.assertEqual(self.plan([mine], new)[1], [])      # and then it is settled

    def test_twin_under_another_id_is_not_added_again_while_both_wait(self):
        theirs = once("renew", NOW + HOUR, displayName="Renew the phone app")
        mine = once("renew-2", NOW + HOUR, displayName="Renew the phone app")
        new, changes, only_there = self.plan([mine], [theirs])
        self.assertEqual((new, changes, only_there), ([theirs], [], []))

    def test_switching_a_one_time_task_off_here_leaves_a_renamed_copy_on(self):
        # Moving a task to the other account by hand: copy it there, switch it off here.
        theirs = once("recheck-2", NOW + HOUR, displayName="Re-check the list")
        mine = once("recheck", NOW + HOUR, displayName="Re-check the list", enabled=False)
        new, changes, only_there = self.plan([mine], [theirs])
        self.assertEqual((new, changes, only_there), ([theirs], [], []))

    def test_a_pending_task_is_added_when_its_twin_is_no_longer_on(self):
        # The copy under another id already ran or was switched off there: what
        # still waits here must wait there too.
        mine = once("renew-2", NOW + HOUR, displayName="Renew the phone app")
        for dead in (ran(once("renew", NOW + HOUR, displayName="Renew the phone app")),
                     once("renew", NOW + HOUR, displayName="Renew the phone app", enabled=False)):
            new, changes, only_there = self.plan([mine], [dead])
            self.assertEqual(new, [dead, mine])
            self.assertEqual((actions(changes), only_there), ([("add", "renew-2")], []))

    def test_one_ran_and_one_waits_under_the_same_name_in_either_order(self):
        done = ran(once("a", NOW + HOUR, displayName="Same name"))
        waiting = once("b", NOW + HOUR, displayName="Same name")
        twin = once("y", NOW + HOUR, displayName="Same name")
        for source in ([done, waiting], [waiting, done]):
            new, changes, only_there = self.plan(source, [twin])
            self.assertEqual(sorted(actions(changes)), [("add", "b"), ("mark_run", "y")])
            self.assertEqual([(t["id"], dt.is_live(t)) for t in new], [("y", False), ("b", True)])
            self.assertEqual(only_there, [])
            self.assertEqual(self.plan(source, new)[1], [])

    def test_another_name_or_time_is_not_a_twin(self):
        mine = ran(once("renew-2", NOW - HOUR, displayName="Renew the phone app"))
        other_time = once("a", NOW, displayName="Renew the phone app")
        other_name = once("b", NOW - HOUR, displayName="Renew the laptop app")
        repeating = cron("c", displayName="Renew the phone app")
        target = [other_time, other_name, repeating]
        new, changes, only_there = self.plan([mine], target)
        self.assertEqual((new, changes), (target, []))
        self.assertEqual([o["id"] for o in only_there], ["a", "b", "c"])

    def test_same_id_ran_and_a_renamed_copy_are_both_marked(self):
        mine = ran(once("renew", NOW - HOUR))
        new, changes, _ = self.plan([mine], [once("renew", NOW - HOUR), once("renew-2", NOW - HOUR,
                                                                              displayName="Task renew")])
        self.assertEqual(sorted(actions(changes)), [("mark_run", "renew"), ("mark_run", "renew-2")])
        self.assertEqual([dt.is_live(t) for t in new], [False, False])

    # -- only on the other account: keep and report --

    def test_entry_only_on_the_other_account_is_kept_and_reported(self):
        stranger = cron("theirs-only", displayName="Only there")
        old = ran(once("old-one", NOW - 9 * HOUR, displayName="Long done"))
        off = cron("theirs-off", enabled=False)
        overdue = once("theirs-due", NOW - HOUR, displayName="Overdue there")
        target = [stranger, old, off, overdue]
        new, changes, only_there = self.plan([cron("mine")], target)
        self.assertEqual(new, target + [cron("mine")])       # nothing deleted, nothing changed
        self.assertEqual(actions(changes), [("add", "mine")])
        self.assertEqual(only_there, [
            {"id": "theirs-only", "name": "Only there", "live": True},
            {"id": "old-one", "name": "Long done", "live": False},
            {"id": "theirs-off", "name": "Task theirs-off", "live": False},
            {"id": "theirs-due", "name": "Overdue there", "live": True}])

    def test_empty_source_deletes_nothing(self):
        target = [cron("a"), once("b", NOW + HOUR)]
        new, changes, only_there = self.plan([], target)
        self.assertEqual((new, changes), (target, []))
        self.assertEqual([o["id"] for o in only_there], ["a", "b"])

    # -- the whole thing --

    def test_a_second_plan_on_the_result_changes_nothing(self):
        source = [cron("a", lastRunAt="2026-11-23T03:08:13.063Z"), cron("new"),
                  once("ahead", NOW + HOUR), ran(once("done", NOW - HOUR)),
                  ran(once("renew-2", NOW - HOUR, displayName="Renew"))]
        target = [cron("stranger"), cron("a", "0 8 * * *"), once("done", NOW - HOUR),
                  once("renew", NOW - HOUR, displayName="Renew")]
        new, changes, only_there = self.plan(source, target)
        self.assertEqual(sorted(actions(changes)),
                         [("add", "ahead"), ("add", "new"), ("mark_run", "done"),
                          ("mark_run", "renew"), ("update", "a")])
        self.assertEqual([t["id"] for t in new], ["stranger", "a", "done", "renew", "new", "ahead"])
        again, changes2, only_there2 = self.plan(source, new)
        self.assertEqual((again, changes2, only_there2), (new, [], only_there))
        self.assertEqual([o["id"] for o in only_there], ["stranger"])


class TextTest(unittest.TestCase):

    def test_label_and_schedule(self):
        self.assertEqual(dt.label(cron("a", displayName="Weekly report")), "Weekly report")
        self.assertEqual(dt.label({"id": "only-id", "enabled": True}), "only-id")
        self.assertEqual(dt.schedule(cron("a", "35 7 * * *")), "35 7 * * *")
        self.assertRegex(dt.schedule(once("a", NOW)), r"^once, \d{4}-\d\d-\d\d \d\d:\d\d$")
        self.assertEqual(dt.schedule({"id": "a", "enabled": True}), "run by hand")

    def test_long_values_are_cut_short_in_a_report(self):
        fields = dt.changed_fields({"a": "x" * 200, "b": [1], "c": {"k": 1}, "same": 1},
                                   {"a": "y", "b": [2], "c": {"k": 2}, "same": 1, "new": None})
        self.assertEqual(fields, [
            {"field": "a", "from": "x" * 57 + "...", "to": "y"},
            {"field": "b", "from": "(a list)", "to": "(a list)"},
            {"field": "c", "from": "(an object)", "to": "(an object)"},
            {"field": "new", "from": None, "to": None}])

    def test_only_run_times(self):
        def change(action, *names):
            return {"action": action, "fields": [{"field": n} for n in names]}
        self.assertTrue(dt.only_run_times(change("update", "lastRunAt", "lastScheduledFor")))
        self.assertTrue(dt.only_run_times(change("update", "lastRunAt")))
        self.assertFalse(dt.only_run_times(change("update", "lastRunAt", "cronExpression")))
        self.assertFalse(dt.only_run_times(change("mark_run", "lastRunAt")))
        self.assertFalse(dt.only_run_times(change("add")))


if __name__ == "__main__":
    unittest.main()
