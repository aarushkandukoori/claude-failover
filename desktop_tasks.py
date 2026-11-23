"""Rules for mirroring desktop scheduled tasks between accounts.

The Claude desktop app keeps the scheduled tasks of each account in that
account's own file:

    ~/Library/Application Support/Claude/claude-code-sessions/<account>/<org>/scheduled-tasks.json

It runs only the tasks of the account it is signed in to. So a task made under
one account is not there after the app switches to another one. This module
decides what every other account's list should become, given the signed-in
account's list. It works on parsed lists only: desktop_sync.py reads and writes
the files.

The rules, all in plan():
- The signed-in account's list is the truth. It is only read, because the app
  holds that file in memory and rewrites it.
- A task the other account has under the same id is replaced there by an exact
  copy. lastRunAt and lastScheduledFor travel with it, so the other account
  knows which runs already happened and does not repeat them after a switch.
- A repeating task the other account lacks is added.
- A one-time task the other account lacks is added only while it is still on
  and its time is ahead, and not while a twin of it (see below) is still on
  there. Finished ones are history.
- Once a one-time task has run on the signed-in account, its twin on the other
  account is marked as run (off, with lastRunAt), so it cannot fire a second
  time after a switch. The twin is the entry with the same id, or a one-time
  entry with the same name and the same time: a copy made under another id.
- A task only the other account has is never deleted. It is reported.
"""

import copy
import datetime as dt
import json

RUN_FIELDS = ("lastRunAt", "lastScheduledFor")   # what the app writes when a task runs


class NotATaskList(Exception):
    """A file that is not a scheduled-tasks.json this module understands."""


def _no_constant(name):
    # Python's json takes NaN and Infinity; the app's JSON parser does not.
    raise ValueError("%s is not valid JSON" % name)


def parse(data):
    """The document in the bytes of a scheduled-tasks.json: a dict whose
    "scheduledTasks" is the list of tasks. Raises NotATaskList for anything this
    module is not sure about. It then does nothing with the file: it never guesses."""
    try:
        # Plain UTF-8 only, like the app: no byte order mark, no UTF-16.
        doc = json.loads(data.decode("utf-8"), parse_constant=_no_constant)
    except (ValueError, RecursionError):   # also bytes that are not UTF-8
        raise NotATaskList("not JSON")
    try:
        json.dumps(doc, allow_nan=False)   # 1e400 reads as infinity, which JSON cannot hold
    except (ValueError, RecursionError):
        raise NotATaskList("a number JSON cannot hold")
    if not isinstance(doc, dict) or not isinstance(doc.get("scheduledTasks"), list):
        raise NotATaskList("no scheduledTasks list")
    ids = set()
    for task in doc["scheduledTasks"]:
        if not isinstance(task, dict) or not isinstance(task.get("id"), str) or not task["id"]:
            raise NotATaskList("an entry without an id")
        if not isinstance(task.get("enabled"), bool):
            raise NotATaskList("%s is neither on nor off" % task["id"])
        if task["id"] in ids:
            raise NotATaskList("two entries share the id %s" % task["id"])
        ids.add(task["id"])
    return doc


def encode(doc):
    """The bytes of a scheduled-tasks.json the way the app writes it: JSON with
    2 spaces, UTF-8, no newline at the end. ValueError for a number JSON cannot
    hold (NaN, Infinity)."""
    text = json.dumps(doc, indent=2, ensure_ascii=False, allow_nan=False)
    # Half an emoji (a lone surrogate) has no UTF-8 form: write it as \udXXX.
    return text.encode("utf-8", "backslashreplace")


def is_one_time(task):
    """True for a task that fires once at fireAt (a time in ms). False for a
    repeating task (cronExpression) and for one that only runs by hand."""
    fire = task.get("fireAt")
    return isinstance(fire, (int, float)) and not isinstance(fire, bool) and fire > 0


def has_run(task):
    return bool(task.get("lastRunAt"))


def is_live(task):
    """Would the app still run it: it is on, and it is not a one-time task that
    already ran."""
    return task.get("enabled") is True and not (is_one_time(task) and has_run(task))


def is_pending(task, now_ms):
    """A one-time task that is on, has not run, and whose time is still ahead."""
    return is_one_time(task) and is_live(task) and task["fireAt"] > now_ms


def same_one_time(a, b):
    """The same one-time task under two ids: the same name and the same time."""
    name = a.get("displayName")
    return (is_one_time(a) and is_one_time(b) and isinstance(name, str) and name != ""
            and name == b.get("displayName") and a["fireAt"] == b["fireAt"])


def label(task):
    """The task's name for people, else its id."""
    name = task.get("displayName")
    return name if isinstance(name, str) and name else task["id"]


def schedule(task):
    """When a task runs, as short text."""
    if is_one_time(task):
        try:
            when = dt.datetime.fromtimestamp(task["fireAt"] / 1000).strftime("%Y-%m-%d %H:%M")
        except (OverflowError, OSError, ValueError):
            when = str(task["fireAt"])
        return "once, %s" % when
    cron = task.get("cronExpression")
    return cron if isinstance(cron, str) and cron else "run by hand"


def _canon(value):
    # true, 1 and 1.0 are equal in Python but not in the file, so compare as JSON text.
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def same(a, b):
    """True if two entries say exactly the same thing (key order aside)."""
    return _canon(a) == _canon(b)


def brief(value):
    """A value short enough for a report line."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= 60 else value[:57] + "..."
    return "(a list)" if isinstance(value, list) else "(an object)"


def changed_fields(old, new):
    """[{"field", "from", "to"}] for every field that differs between two entries.
    A field one of them lacks shows as None there."""
    out = []
    for key in sorted(set(old) | set(new)):
        if key not in old or key not in new or not same(old[key], new[key]):
            out.append({"field": key, "from": brief(old.get(key)), "to": brief(new.get(key))})
    return out


def only_run_times(change):
    """True for an update that only moves the run times forward: a task ran."""
    fields = [f["field"] for f in change["fields"]]
    return change["action"] == "update" and bool(fields) and all(f in RUN_FIELDS for f in fields)


def _change(action, task, fields=None):
    return {"action": action, "id": task["id"], "name": label(task),
            "schedule": schedule(task), "fields": fields or []}


def plan(source, target, now_ms):
    """What the other account's list should become.

    source   the signed-in account's scheduledTasks list
    target   the other account's list
    now_ms   the time now, in ms like fireAt

    Returns (new target list, changes, only_there):
      changes     [{"action": "add" | "update" | "mark_run", "id", "name", "schedule",
                    "fields"}], empty when the lists already match
      only_there  [{"id", "name", "live"}] for entries only the other account has.
                  They stay as they are. "live" = the app would still run it there.
    Neither input list is changed.
    """
    theirs = [copy.deepcopy(t) for t in target]
    place = {t["id"]: i for i, t in enumerate(theirs)}
    source_ids = {t["id"] for t in source}

    def twins_of(mine):
        """Copies of a one-time task the other account keeps under another id."""
        return [t for t in theirs if t["id"] not in source_ids and same_one_time(mine, t)]

    changes, twins = [], set()
    # First the one-time tasks that ran here: a twin must not fire there as well.
    for mine in source:
        for t in twins_of(mine):
            twins.add(t["id"])
            if has_run(mine) and is_live(t):
                before = copy.deepcopy(t)
                t["enabled"] = False
                for key in RUN_FIELDS:
                    if key in mine:
                        t[key] = mine[key]
                changes.append(_change("mark_run", t, changed_fields(before, t)))
    # Then every task: replace the entry with the same id, or add what is missing.
    for mine in source:
        i = place.get(mine["id"])
        if i is not None:
            if not same(theirs[i], mine):
                ran = is_one_time(mine) and has_run(mine) and is_live(theirs[i])
                changes.append(_change("mark_run" if ran else "update", mine,
                                       changed_fields(theirs[i], mine)))
                theirs[i] = copy.deepcopy(mine)
        elif not is_one_time(mine) or (
                # A twin that is still on would fire there next to the new copy.
                is_pending(mine, now_ms) and not any(is_live(t) for t in twins_of(mine))):
            theirs.append(copy.deepcopy(mine))
            changes.append(_change("add", mine))
    only_there = [{"id": t["id"], "name": label(t), "live": is_live(t)}
                  for t in theirs if t["id"] not in source_ids and t["id"] not in twins]
    return theirs, changes, only_there
