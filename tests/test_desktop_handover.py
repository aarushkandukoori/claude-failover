"""Tests for desktop_handover.py: how a transcript ends, and what that means.

Entries in, a verdict out, so nothing here touches a file. The tests that read
transcripts and chat records are in test_desktop_sync.py.

Run:  /usr/bin/python3 -m unittest discover -s tests -p 'test_desktop_handover.py' -v
"""
import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import desktop_handover as dh  # noqa: E402

T1, T2, T3 = "2026-11-23T05:26:01.000Z", "2026-11-23T05:26:20.000Z", "2026-11-23T05:26:23.000Z"


def prompt(text="do the thing", at=T1, **more):
    return dict({"type": "user", "timestamp": at, "message": {"role": "user", "content": text}}, **more)


def reply(text="All done.", at=T2, stop="end_turn", **more):
    return dict({"type": "assistant", "timestamp": at,
                 "message": {"role": "assistant", "stop_reason": stop,
                             "content": [{"type": "text", "text": text}]}}, **more)


def tool_call(name="Bash", description="Run the tests", at=T2, **more):
    args = {"command": "make test"}
    if description:
        args["description"] = description
    return dict({"type": "assistant", "timestamp": at,
                 "message": {"role": "assistant", "stop_reason": "tool_use",
                             "content": [{"type": "tool_use", "id": "t1", "name": name, "input": args}]}},
                **more)


def tool_result(text="Exit code 137", at=T3, **more):
    return dict({"type": "user", "timestamp": at,
                 "message": {"role": "user", "content": [
                     {"type": "tool_result", "tool_use_id": "t1", "content": text}]}}, **more)


NOISE = [{"type": "api-request", "timestamp": T3}, {"type": "last-prompt"}, {"type": "custom-title"},
         {"type": "system", "subtype": "stop_hook_summary", "timestamp": T3}, "not an object", 7]


class TailStateTest(unittest.TestCase):

    def state(self, entries):
        return dh.tail_state(entries)

    def test_a_reply_ends_the_turn(self):
        got = self.state([prompt(), tool_call(), tool_result(), reply(at=T3)] + NOISE)
        self.assertEqual(got, {"state": dh.DONE, "at": T3, "detail": ""})
        for reason in ("stop_sequence", "refusal"):
            self.assertEqual(self.state([prompt(), reply(stop=reason)])["state"], dh.DONE)

    def test_a_tool_call_with_no_answer_is_cut_off(self):
        got = self.state([prompt(), reply("On it."), tool_call(at=T3)] + NOISE)
        self.assertEqual(got, {"state": dh.CUT_OFF, "at": T3, "detail": "Bash - Run the tests"})

    def test_a_tool_result_with_no_reply_is_cut_off(self):
        # How the two chats ended that a sign-out killed: the command died, nothing came after.
        got = self.state([prompt(), tool_call(), tool_result()] + NOISE)
        self.assertEqual(got, {"state": dh.CUT_OFF, "at": T3, "detail": "Bash - Run the tests"})

    def test_a_prompt_with_no_reply_is_cut_off(self):
        got = self.state([prompt(at=T1), reply(at=T2), prompt("and now the other one", at=T3)])
        self.assertEqual(got, {"state": dh.CUT_OFF, "at": T3, "detail": ""})

    def test_the_last_tool_call_is_named(self):
        entries = [prompt(), tool_call("Read", None), tool_result(), tool_call("Edit", None, at=T3)]
        self.assertEqual(self.state(entries)["detail"], "Edit")
        entries = [prompt(), tool_call("Bash", "  wait for\n the build  "), tool_result()]
        self.assertEqual(self.state(entries)["detail"], "Bash - wait for the build")

    def test_a_thinking_block_of_an_unfinished_message_is_cut_off(self):
        thinking = {"type": "assistant", "timestamp": T3,
                    "message": {"role": "assistant", "stop_reason": "tool_use",
                                "content": [{"type": "thinking", "thinking": "hm"}]}}
        self.assertEqual(self.state([prompt(), thinking])["state"], dh.CUT_OFF)
        self.assertEqual(self.state([prompt(), reply(stop="max_tokens")])["state"], dh.CUT_OFF)

    def test_pressing_stop_is_not_cut_off(self):
        for text in ("[Request interrupted by user]", "[Request interrupted by user for tool use]"):
            got = self.state([prompt(), tool_call(), tool_result(), prompt(text, at=T3)])
            self.assertEqual((got["state"], got["detail"]), (dh.STOPPED, ""))

    def test_an_error_message_is_a_failed_turn(self):
        limit = reply("You've hit your weekly limit · resets Sat 11pm\n(America/New_York)", at=T3,
                      stop="stop_sequence", isApiErrorMessage=True)
        got = self.state([prompt(), tool_call(), tool_result(), limit])
        self.assertEqual(got, {"state": dh.FAILED, "at": T3,
                               "detail": "You've hit your weekly limit · resets Sat 11pm "
                                         "(America/New_York)"})

    def test_a_filler_entry_is_not_a_reply(self):
        # When a message from another chat (or a skill) restarts a chat, the CLI first
        # writes a filler "No response requested." and then the message, marked isMeta.
        # Neither means the turn ended: until its first real reply the chat is working
        # on that prompt, and if it dies there it is cut off.
        filler = reply("No response requested.", at=T2, stop="stop_sequence")
        filler["message"]["model"] = "<synthetic>"
        message = prompt("Another Claude session sent a message: carry on", at=T3, isMeta=True)
        cut = [prompt(), tool_call(at=T1), tool_result(at=T1)]
        self.assertEqual(self.state(cut + [filler, message]),
                         {"state": dh.CUT_OFF, "at": T3, "detail": ""})
        # The filler alone changes nothing: the chat is still cut off where it was.
        self.assertEqual(self.state(cut + [filler]),
                         {"state": dh.CUT_OFF, "at": T1, "detail": "Bash - Run the tests"})
        # A skill started with a slash command: its body is the prompt.
        skill = [prompt("<command-message>review</command-message>", at=T3),
                 prompt("Base directory for this skill: /x\n\nDo the review.", at=T3, isMeta=True)]
        self.assertEqual(self.state([prompt(), reply(at=T2)] + skill)["state"], dh.CUT_OFF)
        # An error message is written the same way, and it still is a failed turn.
        error = reply("API Error: 529 Overloaded", at=T3, stop="stop_sequence", isApiErrorMessage=True)
        error["message"]["model"] = "<synthetic>"
        self.assertEqual(self.state(cut + [error])["state"], dh.FAILED)

    def test_what_is_not_the_conversation_does_not_count(self):
        done = [prompt(), reply(at=T2)]
        after = [tool_call(at=T3, isSidechain=True),                     # a subagent's call
                 tool_result(at=T3, isSidechain=True),
                 prompt("<local-command-caveat>The command below was run by the user</local-command-caveat>",
                        at=T3, isMeta=True),
                 prompt("<local-command-stdout>Set model to opus</local-command-stdout>", at=T3),
                 prompt("<command-name>/model</command-name>", at=T3),
                 prompt("<bash-input>ls</bash-input>", at=T3),
                 {"type": "user", "timestamp": T3}]                      # no message at all
        self.assertEqual(self.state(done + after + NOISE), {"state": dh.DONE, "at": T2, "detail": ""})

    def test_no_guess_without_evidence(self):
        self.assertEqual(self.state([]), {"state": dh.UNKNOWN, "at": None, "detail": ""})
        self.assertEqual(self.state(NOISE)["state"], dh.UNKNOWN)
        self.assertEqual(self.state([prompt(), reply(stop=None)])["state"], dh.UNKNOWN)
        self.assertIsNone(self.state([dict(prompt(), timestamp=12345)])["at"])


class WaitingTest(unittest.TestCase):

    def record(self, category="blocked", same_turn=True, **summary):
        data = {"status_category": category, "status_detail": "detail text",
                "needs_action": "Pick  a\nname: A or B?"}
        data.update(summary)
        return {"postTurnSummary": data, "postTurnSummaryFor": "u1",
                "lastAssistantUuid": "u1" if same_turn else "u2"}

    def test_a_blocked_chat_says_what_it_needs(self):
        self.assertEqual(dh.waiting_on_person(self.record()), "Pick a name: A or B?")
        self.assertEqual(dh.waiting_on_person(self.record(needs_action="")), "detail text")
        self.assertEqual(dh.waiting_on_person(self.record(needs_action=None, status_detail=" ")),
                         "it waits for your answer")

    def test_other_chats_do_not_wait(self):
        for category in ("completed", "review_ready", None):
            self.assertEqual(dh.waiting_on_person(self.record(category)), "")
        self.assertEqual(dh.waiting_on_person(self.record(same_turn=False)), "")   # an older turn's note
        self.assertEqual(dh.waiting_on_person({}), "")
        self.assertEqual(dh.waiting_on_person({"postTurnSummary": "blocked"}), "")


class AliveTest(unittest.TestCase):

    def test_a_process_that_names_the_chat(self):
        self.assertTrue(dh.is_alive("abc", None, ({"abc", "def"}, [])))
        self.assertFalse(dh.is_alive("abc", 1000.0, ({"def"}, [])))

    def test_a_new_chat_is_matched_by_its_start_time(self):
        running = (set(), [5000.0])
        self.assertTrue(dh.is_alive("new", 5000.0 + dh.NEW_CHAT_SLACK, running))
        self.assertTrue(dh.is_alive("new", 5000.0 - dh.NEW_CHAT_SLACK, running))
        self.assertFalse(dh.is_alive("new", 5000.0 + dh.NEW_CHAT_SLACK + 1, running))
        self.assertFalse(dh.is_alive("new", None, running))


class ClaimTest(unittest.TestCase):
    CUT = "2026-11-23T01:03:28-05:00"

    def test_a_claim_stands_for_one_cut_and_for_a_while(self):
        claim = {"cut_at": self.CUT, "claimed_at": 1000.0, "claimed": "2026-11-23T01:14:00-05:00"}
        self.assertTrue(dh.claim_is_live(claim, self.CUT, 1000.0))
        self.assertTrue(dh.claim_is_live(claim, self.CUT, 1000.0 + dh.CLAIM_MINUTES * 60 - 1))
        self.assertFalse(dh.claim_is_live(claim, self.CUT, 1000.0 + dh.CLAIM_MINUTES * 60))   # never sent
        self.assertFalse(dh.claim_is_live(claim, "2026-11-23T02:40:00-05:00", 1000.0))       # cut off again
        self.assertFalse(dh.claim_is_live(claim, self.CUT, 999.0))                           # a clock that went back
        # A claim is for the account the app was on: a run under another account died with it.
        mine = dict(claim, account="work")
        self.assertTrue(dh.claim_is_live(mine, self.CUT, 1000.0, "work"))
        self.assertTrue(dh.claim_is_live(mine, self.CUT, 1000.0))             # nobody asks for which
        self.assertFalse(dh.claim_is_live(mine, self.CUT, 1000.0, "personal"))
        self.assertTrue(dh.claim_is_live(claim, self.CUT, 1000.0, "personal"))   # an older claim names none

    def test_what_is_not_a_claim(self):
        for claim in (None, {}, "yes", {"cut_at": self.CUT}, {"cut_at": self.CUT, "claimed_at": "now"},
                      {"cut_at": self.CUT, "claimed_at": True}):
            self.assertFalse(dh.claim_is_live(claim, self.CUT, 1000.0), claim)

    def test_the_restart_message_says_check_first(self):
        text = dh.restart_message("1:03 AM", "Bash - Run the tests")
        self.assertTrue(text.startswith("Automatic pick-up after an account switch: your last turn was "
                                        "cut off at 1:03 AM, when the Claude app was signed out. The last "
                                        "step you had started: Bash - Run the tests. "))
        self.assertIn("First check what already happened, so that nothing is done or sent twice", text)
        self.assertNotIn("\n", text)
        self.assertNotIn("The last step", dh.restart_message("1:03 AM", ""))


if __name__ == "__main__":
    unittest.main()
