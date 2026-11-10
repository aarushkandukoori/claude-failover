#!/usr/bin/python3 -I
"""Stand-in for /usr/bin/security in tests: the real keychain is never touched.
`find-generic-password -s S -a A -w` prints the value stored under "S/A" in the
JSON file $FAKE_SECURITY_STORE (exit 44 when missing, like the real tool). Every
call's argv is appended to $FAKE_SECURITY_LOG."""
import json, os, sys

argv = sys.argv[1:]
with open(os.environ["FAKE_SECURITY_LOG"], "a") as f:
    f.write(json.dumps(argv) + "\n")


def val(flag):
    return argv[argv.index(flag) + 1] if flag in argv else None


try:
    store = json.loads(open(os.environ["FAKE_SECURITY_STORE"]).read())
except (OSError, ValueError):
    store = {}
key = "%s/%s" % (val("-s"), val("-a"))
if argv[:1] == ["find-generic-password"] and key in store:
    print(store[key])
    sys.exit(0)
sys.stderr.write("security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain.\n")
sys.exit(44)
