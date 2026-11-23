"""Which desktop chats were left in the middle of their work.

A chat stops in the middle of a turn when the Claude desktop app is signed out
(switching accounts does that), when the app or the Mac goes down, or when the
account runs out of usage. Nothing continues by itself afterwards, and the app
leaves no mark on such a chat. What does show it is the chat's transcript: it
ends with a tool call or a tool result where a finished turn ends with a reply.

This module reads how a transcript ends and says which case it is, so the next
account can pick the work up. It works on parsed transcript entries only:
desktop_sync.py reads the files and checks which chats still have a process.

A transcript is one JSON object per line. The entries that count are the "user"
and "assistant" ones of the main conversation:
- an assistant entry whose stop_reason is end_turn ends a turn
- a tool call is an assistant entry with a tool_use block and stop_reason tool_use
- its answer is a "user" entry that holds a tool_result
"""

DONE = "done"          # the last turn ended with a reply
CUT_OFF = "cut_off"    # it stopped in the middle of a turn
STOPPED = "stopped"    # the person pressed stop
FAILED = "failed"      # the turn ended with an error message (a usage limit, a lapsed login, ...)
UNKNOWN = "unknown"    # cannot tell: such a chat is never listed

FINISHED_REASONS = ("end_turn", "stop_sequence", "refusal")
INTERRUPTED = "[Request interrupted by user"
# What a slash command or a "!" shell line leaves in the transcript: not a prompt.
LOCAL_OUTPUT = ("<local-command", "<command-name>", "<command-message>", "<command-args>",
                "<bash-input>", "<bash-stdout>", "<bash-stderr>")
NEW_CHAT_SLACK = 120   # seconds between a new chat's process start and its transcript's birth
CLAIM_MINUTES = 15     # a restart that was claimed but never came may be claimed again after this
SYNTHETIC = "<synthetic>"   # the "model" of an assistant entry the CLI wrote itself


def blocks(entry):
    """The content blocks of an entry's message, [] when it has none."""
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def text_of(entry):
    """All the plain text in an entry, joined."""
    return "\n".join(b["text"] for b in blocks(entry)
                     if b.get("type") == "text" and isinstance(b.get("text"), str)).strip()


def has_block(entry, kind):
    return any(b.get("type") == kind for b in blocks(entry))


def is_conversation(entry):
    """True for an entry of the main conversation: a prompt, a reply, a tool call
    or a tool result. Subagent entries, the CLI's own filler replies and the
    output of slash commands are not part of what the turn is waiting for."""
    if not isinstance(entry, dict) or entry.get("type") not in ("user", "assistant"):
        return False
    message = entry.get("message")
    if entry.get("isSidechain") or not isinstance(message, dict):
        return False
    if entry["type"] == "assistant":
        # Before a message that restarts a chat, the CLI writes a filler reply itself
        # ("No response requested."). That is no reply. An error message is written
        # the same way, and that one does end the turn.
        return message.get("model") != SYNTHETIC or bool(entry.get("isApiErrorMessage"))
    if has_block(entry, "tool_result"):
        return True
    # A message from another chat, a skill's text or an attached picture is marked
    # isMeta and still is a prompt the chat answers. Only what a local command
    # printed is not.
    return not text_of(entry).startswith(LOCAL_OUTPUT)


def last_tool(entries):
    """What the newest tool call in the entries was, as short text such as
    "Bash - Run the tests", or "" when there is none."""
    for entry in reversed(entries):
        if isinstance(entry, dict) and entry.get("type") == "assistant" and not entry.get("isSidechain"):
            for block in reversed(blocks(entry)):
                if block.get("type") == "tool_use" and isinstance(block.get("name"), str):
                    args = block.get("input")
                    what = args.get("description") if isinstance(args, dict) else None
                    if isinstance(what, str) and what.strip():
                        return "%s - %s" % (block["name"], " ".join(what.split()))
                    return block["name"]
    return ""


def tail_state(entries):
    """How a transcript ends, from its last entries in file order:
    {"state": DONE | CUT_OFF | STOPPED | FAILED | UNKNOWN,
     "at": the timestamp of the last conversation entry (text) or None,
     "detail": the error message for FAILED, the last tool call for CUT_OFF, else ""}."""
    conversation = [e for e in entries if is_conversation(e)]
    if not conversation:
        return {"state": UNKNOWN, "at": None, "detail": ""}
    last = conversation[-1]
    at = last.get("timestamp") if isinstance(last.get("timestamp"), str) else None

    def result(state, detail=""):
        return {"state": state, "at": at, "detail": detail}

    if last["type"] == "user":
        if has_block(last, "tool_result"):
            return result(CUT_OFF, last_tool(entries))    # the tool answered, no reply came
        if text_of(last).startswith(INTERRUPTED):
            return result(STOPPED)
        return result(CUT_OFF)                             # a prompt that got no reply
    if last.get("isApiErrorMessage"):
        return result(FAILED, " ".join(text_of(last).split()))
    if has_block(last, "tool_use"):
        return result(CUT_OFF, last_tool(entries))         # a tool call that got no answer
    reason = last["message"].get("stop_reason")
    if reason in FINISHED_REASONS:
        return result(DONE)
    if reason is None:
        return result(UNKNOWN)                             # an old or partial entry: no guess
    return result(CUT_OFF, last_tool(entries))             # tool_use, max_tokens, pause_turn, ...


def waiting_on_person(record):
    """What a chat that ended its turn still needs from the person, or "". The app
    notes that on the chat record after a turn (postTurnSummary). It counts only
    while it is about the chat's latest reply."""
    summary = record.get("postTurnSummary")
    if not isinstance(summary, dict) or summary.get("status_category") != "blocked":
        return ""
    if record.get("postTurnSummaryFor") != record.get("lastAssistantUuid"):
        return ""
    for key in ("needs_action", "status_detail"):
        value = summary.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return "it waits for your answer"


def is_alive(cli_id, born, running):
    """Does a chat still have a process? `running` = (ids, starts): the session
    ids that running claude processes name, and the start times of those that
    name none. A brand-new chat is one of the latter until its first restart:
    its transcript is born within moments of its process."""
    ids, starts = running
    if cli_id in ids:
        return True
    return born is not None and any(abs(born - start) <= NEW_CHAT_SLACK for start in starts)


def claim_is_live(claim, cut_at, now, account=None):
    """True while a claim on a chat stands. Whoever restarts a cut-off chat claims
    it first, so that two helpers never both send it "continue". A claim is for
    one cut (`cut_at`, the time of the chat's last entry): once the chat moves
    on and is cut off again, that is a new cut. It lasts CLAIM_MINUTES, in case
    the restart was claimed but never sent. And it is for the account the app
    was signed in to (`account`): a run that claimed under another account died
    with that sign-out. `now` in unix seconds."""
    if not isinstance(claim, dict) or claim.get("cut_at") != cut_at:
        return False
    if account is not None and claim.get("account") not in (None, account):
        return False
    at = claim.get("claimed_at")
    return (isinstance(at, (int, float)) and not isinstance(at, bool)
            and 0 <= now - at < CLAIM_MINUTES * 60)


def restart_message(when, detail):
    """What a cut-off chat is told when it is restarted. `when` = the time of the
    cut as text, `detail` = the step it was in, or ""."""
    step = " The last step you had started: %s." % detail if detail else ""
    return ("Automatic pick-up after an account switch: your last turn was cut off at %s, when the "
            "Claude app was signed out.%s The app is now signed in to another account, so carry on "
            "and finish what you were doing. First check what already happened, so that nothing is "
            "done or sent twice: a command you had started may have finished or died, and files, "
            "commits, emails or messages from before the cut are already out. Then continue from "
            "where it stopped." % (when, step))
