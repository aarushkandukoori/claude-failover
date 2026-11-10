#!/usr/bin/python3 -I
"""Stand-in for the claude binary in the README demo (scripts/make_demo.py).

It answers the two calls the demo makes, per account, from the plan file named
by $DEMO_CLAUDE_PLAN: {email: {"plan", "login_expires_at", "reply", "exit_code"}}.
Like the real CLI it knows which account it runs as from the login saved in
$CLAUDE_CONFIG_DIR/.claude.json.

  claude auth status --json   -> who is signed in, the plan, when the login ends
  claude -p PROMPT ...        -> that account's reply and exit code
"""
import json
import os
import sys


def signed_in_email():
    """The email of the login in this account's config folder, or None."""
    path = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR", ""), ".claude.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)["oauthAccount"]["emailAddress"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def main(argv):
    with open(os.environ["DEMO_CLAUDE_PLAN"], encoding="utf-8") as f:
        plan = json.load(f)
    email = signed_in_email()
    account = plan.get(email)
    if argv[:2] == ["auth", "status"]:
        if account is None:
            print(json.dumps({"loggedIn": False}))
            return 1
        print(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "email": email,
                          "subscriptionType": account["plan"],
                          "loginExpiresAt": account["login_expires_at"]}, indent=2))
        return 0
    if "-p" in argv or "--print" in argv:
        if account is None:
            print("Not logged in · Please run /login")
            return 1
        print(account["reply"])
        return account["exit_code"]
    sys.stderr.write("demo_fake_claude: the demo never makes this call: %s\n" % " ".join(argv))
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
