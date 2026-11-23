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
- An item that left the signed-in account's list was started or dropped there,
  so it is removed from the other account's list too. Otherwise it would come
  back after a switch and invite doing the work twice. Two cases are not that:
  an item the app never took over from this tool's write (resolve() tells), and
  an item that left a folder which is full, where the app drops the oldest
  items by itself to make room. Those stay where they are.
- An item only the other account has, and the signed-in account never had,
  stays where it is.
- Items older than 30 days are not copied: the app drops those itself.
- The other list is only filled to a few places below what the app keeps (20
  items per folder, 100 in all). Beyond that the app makes room at a save by
  dropping the oldest items, and those could be that account's own.
"""

import collections
import copy
import json
import posixpath
import unicodedata

MAX_AGE_MS = 30 * 24 * 3600 * 1000   # the app forgets a proposal this long after it was made
FOLDER_CAP = 20                      # the app keeps this many items per folder,
TOTAL_CAP = 100                      # and this many in all: at a save it drops the oldest beyond that
FOLDER_ROOM = FOLDER_CAP - 3         # how far this tool fills a folder and the whole list: the
TOTAL_ROOM = TOTAL_CAP - 10          # rest stays free for the items the app files there itself
SETTLE_SECONDS = 120                 # a write that a later look still found untouched this long after
                                     # it was made is one the app reads whenever it opens that list
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


def folder(item):
    """The folder an item belongs to, spelled the way the app compares folders:
    no slash at the end, letter case ignored."""
    cwd = item.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return ""
    return unicodedata.normalize("NFC", posixpath.normpath(cwd)).lower()


def filed(item):
    """When an item was put into the backlog (ms), 0 when it does not say."""
    for name in ("filedAt", "suggestedAt"):
        value = item.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return 0


def label(item):
    """The item's title for people, else its id."""
    for name in ("title", "id"):
        value = item.get(name)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return "(no title)"


def memory(entry):
    """What is remembered about one account's list, from its entry in
    backlog-seen.json:
      held          the items the app itself is known to hold there (keys)
      pending       {key: when} for the items this tool wrote into the list that
                    the app has not been seen to take over yet
      ours_seen_at  when the file was last found exactly as this tool wrote it
    An older version kept just the list of keys."""
    if isinstance(entry, list):
        entry = {"held": entry}
    entry = entry if isinstance(entry, dict) else {}
    held = entry.get("held") if isinstance(entry.get("held"), list) else []
    pending = entry.get("pending") if isinstance(entry.get("pending"), dict) else {}
    seen = entry.get("ours_seen_at")
    number = (int, float)
    return {"held": sorted({k for k in held if isinstance(k, str)}),
            "pending": {k: float(t) for k, t in pending.items()
                        if isinstance(k, str) and isinstance(t, number) and not isinstance(t, bool)},
            "ours_seen_at": float(seen) if isinstance(seen, number) and not isinstance(seen, bool)
            else None}


def after_write(mem, new_keys, added, now):
    """The memory of a list right after this tool wrote it: what it added is
    pending from `now` (unix seconds) on, until the app is seen to hold it."""
    there = set(new_keys)
    pending = {k: t for k, t in mem["pending"].items() if k in there}
    for k in added:
        pending.setdefault(k, float(now))
    return {"held": sorted(set(mem["held"]) & there), "pending": pending, "ours_seen_at": float(now)}


def resolve(mem, now_keys, still_ours, now):
    """Look at a list again: (its memory from here on, the keys that left it).

    While the file is still byte for byte this tool's last write (`still_ours`),
    the app has saved nothing, so nothing can have left.

    Otherwise the app saved the list itself, and what it holds now is known. An
    item counts as having left only if the list held it before. For an item this
    tool had written there, that takes proof that the app read that write: the
    app kept another item of the same or a later write, or a later look found the
    write untouched at least SETTLE_SECONDS after it was made. Without that proof
    the app may have saved an older copy of the list over the write, and the item
    was never started there."""
    here = set(now_keys)
    if still_ours:
        return dict(mem, ours_seen_at=float(now)), []
    pending, seen = mem["pending"], mem["ours_seen_at"]
    kept = [t for k, t in pending.items() if k in here]
    newest = max(kept) if kept else None
    read = {k for k, t in pending.items()
            if (newest is not None and t <= newest)
            or (seen is not None and seen - t >= SETTLE_SECONDS)}
    gone = sorted((set(mem["held"]) | read) - here)
    return {"held": sorted(here), "pending": {}, "ours_seen_at": None}, gone


def plan(source, target, gone, now_ms):
    """What the other account's backlog should become.

    source   the signed-in account's items
    target   the other account's items
    gone     the keys that left the signed-in account's list (see resolve), or
             None when nothing is known to have left
    now_ms   the time now, in ms like suggestedAt

    Returns (new target list, added, removed, only_there, no_room):
      added, removed   the titles of the items added to / removed from the target
      only_there       how many items stay that only the other account has
      no_room          the titles of the items that were not added because the
                       target's folder or the whole list is full
    Neither input list is changed.
    """
    here = {key(item) for item in source} - {None}
    gone = set(gone or ()) - here
    # A folder (or a list) that is full is one the app drops the oldest items from
    # by itself. An item that left such a folder was most likely pushed out, not
    # started, so it is not taken off the other list.
    counts = [item for item in source if is_fresh(item, now_ms)]
    full_folders = {f for f, n in collections.Counter(folder(i) for i in counts).items()
                    if n >= FOLDER_CAP}
    list_full = len(counts) >= TOTAL_CAP
    kept, removed = [], []
    for item in target:
        if key(item) in gone and not list_full and folder(item) not in full_folders:
            removed.append(label(item))
        else:
            kept.append(copy.deepcopy(item))
    there = {key(item) for item in kept}
    ids = {item.get("id") for item in kept}
    wanted = []
    for item in source:
        k = key(item)
        if k is None or k in there or item.get("id") in ids or not is_fresh(item, now_ms):
            continue
        wanted.append(item)
        there.add(k)
        ids.add(item.get("id"))
    # How full the target is, the way the app counts: items it is about to drop for
    # their age take up no room. What room there is goes to the newest items first.
    counted = [item for item in kept if is_fresh(item, now_ms)]
    in_folder = collections.Counter(folder(item) for item in counted)
    total, fits = len(counted), set()
    for item in sorted(wanted, key=filed, reverse=True):
        if total < TOTAL_ROOM and in_folder[folder(item)] < FOLDER_ROOM:
            fits.add(key(item))
            in_folder[folder(item)] += 1
            total += 1
    added, no_room = [], []
    for item in wanted:
        if key(item) in fits:
            kept.append(copy.deepcopy(item))
            added.append(label(item))
        else:
            no_room.append(label(item))
    only_there = sum(1 for item in kept if key(item) not in here)
    return kept, added, removed, only_there, no_room
