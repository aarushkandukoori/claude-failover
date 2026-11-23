"""Rules for mirroring the desktop app's task backlog between accounts.

When a chat proposes a task for later and its card is set aside (dismissed, or
pushed out by newer ones), the Claude desktop app files it in a backlog, one
list per account:

    ~/Library/Application Support/Claude/claude-code-sessions/<account>/<org>/backlog/tasks.json

So the tasks set aside under one account are not there after the app switches
to another one. This module decides what every other account's list should
become, given the signed-in account's list. It works on parsed lists only:
desktop_sync.py reads and writes the files.

The rules, all in plan():
- The signed-in account's list is the truth. It is only read, because the app
  keeps it in memory and writes it back.
- An item the other account lacks is added. Two entries are the same item when
  they come from the same chat and the same proposal in it (originSessionId and
  originTaskId), which is how the app itself tells them apart.
- An item that left the signed-in account's list since the last sync was started
  or dropped there, so it is removed from the other account's list too.
  Otherwise it would come back after a switch and invite doing the work twice.
- An item only the other account has, and the signed-in account never had,
  stays where it is.
- Items older than 30 days are not copied: the app drops those itself.
"""

import copy
import json

MAX_AGE_MS = 30 * 24 * 3600 * 1000   # the app forgets a proposal this long after it was made
NEWEST_VERSION = 1                   # the file format this module knows


class NotABacklog(Exception):
    """A file that is not a backlog this module understands."""


def _no_constant(name):
    # Python's json takes NaN and Infinity; the app's JSON parser does not.
    raise ValueError("%s is not valid JSON" % name)


def parse(data):
    """The document in the bytes of a backlog file: a dict whose "items" is the
    list. Raises NotABacklog for anything this module is not sure about, also
    for a file in a newer format (the app leaves those untouched too)."""
    try:
        doc = json.loads(data.decode("utf-8"), parse_constant=_no_constant)
        json.dumps(doc, allow_nan=False)   # 1e400 reads as infinity, which JSON cannot hold
    except (ValueError, RecursionError):   # also bytes that are not UTF-8
        raise NotABacklog("not JSON")
    if not isinstance(doc, dict) or not isinstance(doc.get("items"), list):
        raise NotABacklog("no items list")
    version = doc.get("version")
    if isinstance(version, bool) or not isinstance(version, (int, float)):
        raise NotABacklog("no version number")
    if version > NEWEST_VERSION:
        raise NotABacklog("a newer format (version %s)" % version)
    if not all(isinstance(item, dict) for item in doc["items"]):
        raise NotABacklog("an entry that is not an item")
    return doc


def encode(doc):
    """The bytes of a backlog file the way the app writes it: JSON on one line,
    UTF-8, no newline at the end. ValueError for a number JSON cannot hold."""
    text = json.dumps(doc, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    # Half an emoji (a lone surrogate) has no UTF-8 form: write it as \udXXX.
    return text.encode("utf-8", "backslashreplace")


def key(item):
    """What makes two entries the same item: the chat it came from and the
    proposal in that chat. None for an entry that names neither."""
    chat, proposal = item.get("originSessionId"), item.get("originTaskId")
    if isinstance(chat, str) and chat and isinstance(proposal, str) and proposal:
        return "%s %s" % (chat, proposal)
    return None


def keys(items):
    """The keys of a list, sorted: what desktop_sync.py remembers between runs."""
    return sorted({key(item) for item in items} - {None})


def is_fresh(item, now_ms):
    """True while the app would still keep the item (it was proposed less than
    30 days ago)."""
    made = item.get("suggestedAt")
    return (isinstance(made, (int, float)) and not isinstance(made, bool)
            and now_ms - made < MAX_AGE_MS)


def label(item):
    """The item's title for people, else its id."""
    for name in ("title", "id"):
        value = item.get(name)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return "(no title)"


def plan(source, target, seen, now_ms):
    """What the other account's backlog should become.

    source   the signed-in account's items
    target   the other account's items
    seen     the keys the signed-in account's list held at the last sync, or
             None when that is not known (then nothing is removed)
    now_ms   the time now, in ms like suggestedAt

    Returns (new target list, added, removed, only_there):
      added, removed   the titles of the items added to / removed from the target
      only_there       how many items stay that only the other account has
    Neither input list is changed.
    """
    here = {key(item) for item in source} - {None}
    gone = set(seen or ()) - here     # on the signed-in list last time, not any more
    kept, removed = [], []
    for item in target:
        if key(item) in gone:
            removed.append(label(item))
        else:
            kept.append(copy.deepcopy(item))
    there = {key(item) for item in kept}
    ids = {item.get("id") for item in kept}
    added = []
    for item in source:
        k = key(item)
        if k is None or k in there or item.get("id") in ids or not is_fresh(item, now_ms):
            continue
        kept.append(copy.deepcopy(item))
        there.add(k)
        ids.add(item.get("id"))
        added.append(label(item))
    only_there = sum(1 for item in kept if key(item) not in here)
    return kept, added, removed, only_there
