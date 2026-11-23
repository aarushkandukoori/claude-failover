# Two accounts as one: what happens when an account runs out

## The switch is the Terminal hand-off

When a chat in the desktop app hits its usage limit, the StopFailure hook (see
[ARCHITECTURE.md](ARCHITECTURE.md)) continues that chat in a Terminal window on the
next account. Every chat that hits the limit gets its own window. The app stays signed
in where it is, so chats that are still working there are not touched. This is the
switch, and it needs nothing from you.

`account_switch.py` only helps around it. By default it opens no window and never asks
you to sign the app out:

| | Who does it |
|---|---|
| **Continue each chat that hits its limit, in Terminal, on the next account** | **the StopFailure hook, by itself** |
| Know how much of its week each account has used | the watcher, every hour |
| Warn before the app's account runs out, and say what will happen | the watcher |
| Keep background jobs off the app's account while another has usage | the watcher, with `switch.auto_order` on |
| Say so in the Terminal window and the notification when the account a chat moves to is at its own limit with extra usage on, so the chat may cost extra usage | the hook, from the watcher's last reading |
| Put an account that bills extra usage behind every account with usage of its own | the wrapper's pick, from the watcher's last reading |
| Copy chats and scheduled tasks to the other account | [desktop sync](DESKTOP-SYNC.md), every 10 minutes |

**One thing the hand-off depends on: the account has to refuse.** An account with paid
extra usage turned on does not stop at 100%. It keeps answering and bills. No chat is
refused, so the hook never runs and nothing moves. The watcher's notice says so when
that is the case ("…has used its whole week … Nothing moves while extra usage is on").
Turn extra usage off for that account and the hand-off works at 100% again.

## Paid extra usage comes last

An account with extra usage turned on never refuses. Past its limit it keeps answering
and bills. The rule: **nothing is started on an account that bills while another
account has usage of its own left. When every account is full, the one that bills is
used.**

Where the rule is kept: every time the wrapper picks an account (`plan_accounts` calls
`included_usage_first`). That covers background jobs, chats started in Terminal, and
the account a refused chat is handed to. The accounts that the last usage reading shows
as billing, or close to it, go behind the others: extra usage is on, the account has
not refused, and it is already on extra usage, its window is used up or has under 5%
left, or its usage is not known (read too long ago to trust, or the last reading
failed). Nothing is dropped, and the order in `config.json` is not rewritten.
`claude-accounts status` shows the order that is really used. "Full" means the account
cannot run right now: its week is used up, or a shorter limit is holding it.

Both rules hold for as long as the last reading says so, also after the watcher is
uninstalled. How to end them: [Turning it off](#turning-it-off).

**Every run that does land on the account that bills uses Opus.** When the account
bills (or may start to), the wrapper sets `--model opus` on a background job, a chat
started in Terminal and a chat the hook hands to Terminal, whatever model it would
have used: the default of `settings.json`, or one the caller named, Sonnet and Haiku
included. The caller's `--model` is replaced where it stands. Only a run that is on
Opus already is left alone, and so is every run while the account is out of Opus
itself. `"extra_usage_model"` in `config.json` names another model, and `false` turns
this off. Effort is not changed here. Chats and scheduled tasks inside the desktop
app do not go through the wrapper, so their model is whatever the app uses.

Where it is not kept, because the work is already running or goes around the wrapper:

- **A chat handed to the account that bills stays there.** Its Terminal window keeps
  billing until you close it, also after the other account has usage again.
- **A long job that started on the account that bills runs to its end there.**
- **The desktop app signed in to an account that bills** bills every chat in it. The
  watcher says so ("…has used its whole week").
- **A run pinned to an account** (`CLAUDE_FAILOVER_ACCOUNT`) goes where it is pinned.
- **An account whose login is failing counts as not there**, so jobs go to the one
  that bills although the other still has usage. The "…is logged out" notification
  says so when that happens. Renew logins before they end (`claude-accounts status`
  says when).

## Moving the app itself (by hand)

The desktop app is signed in to one account, and only a person can sign it in to
another. You rarely need to: the hand-off above covers a limit. When you do want the app
on the other account, `claude-accounts switch` walks you through it.

**Signing the app out stops every chat that is working in it, and none of them resumes
by itself.** Let them finish first, or continue them afterwards (the switch lists them).

| | Who does it |
|---|---|
| Copy chats and scheduled tasks to the other account | [desktop sync](DESKTOP-SYNC.md), once more right before the switch |
| **Log the app out and sign in to the other account** | **you, about 30 seconds** |
| Notice the switch and finish it | the watcher, or `claude-accounts switch` if it is open |
| **Sign the Claude in Chrome extension in to the same account** | **you** |
| Point other tools at the account the extension is on | the watcher |

```bash
claude-accounts usage            # each account: used so far, when it resets
claude-accounts hook status      # is the Terminal hand-off installed
claude-accounts watch status     # what happens at a limit, and the watcher's last pass
claude-accounts switch           # by hand: the guided switch of the app itself
claude-accounts switch --check   # only check that the other account can take over
claude-accounts watch install    # run the watcher every 10 minutes
```

To get the older behaviour back, where the watcher opens the guided switch in Terminal
as soon as the app's account runs out, set `"switch": {"at_limit": "app"}`.

## Where the usage numbers come from

Claude Code reports the account's limits with every answer (`rate_limit_event` in
`--output-format stream-json`): each window's share that is used, when it resets, and
whether paid extra usage is on or in use. The watcher makes one tiny haiku call per
account, the call `claude-accounts test` makes, and keeps what it says in
`switch.json` in the state folder. The call saves no chat history and starts no MCP
server.

- An account is read once an hour, and every 20 minutes once it is past 75%.
- A reading from before the wrapper last saw the account hit a limit counts for
  nothing: the account ran out after it was taken.
- An account that is out is not read again until its reset: the answer is known, and
  with paid extra usage turned on the call itself would be billed.
- A reading older than 4 hours decides nothing. The account then counts as unknown.
- The wrapper has an optional memory check before headless runs (it asks
  `~/Projects/memguard/memguard.py` when that file exists). While it reports memory in
  danger, a reading younger than 3 hours is kept instead of starting a call (40 minutes
  for an account past 75%).

Two numbers are kept per account. The fullest window (a five-hour window can be fuller
than the week) decides whether the account can run anything now. Every "of its week"
text and threshold uses the seven-day window.

A test call uses the haiku model, so it shows the limits that count for every model. A
limit that only one model has (for example a weekly limit of the largest model) does not
show up here. The StopFailure hook handles those when a chat hits one.

## The guided switch

```
$ claude-accounts switch
Claude usage
  personal  87% of its week used, resets Sat Nov 28, 11 PM, extra usage on   <- the desktop app
  work      0% of its week used, resets Thu Nov 26, 1 AM

Moving the desktop app from personal to work

Getting work ready
  ok  login - works (ends by Dec 1)
  ok  usage - 100% of its week left, resets Thu Nov 26, 1 AM
  ok  chats - 817 in every account
  ok  scheduled tasks - 7 on work, the same as on personal
  !!  Chrome extension is on personal: after the app, sign it in as work too

You only need this to move the app itself: a chat that hits its limit already
continues in Terminal on the other account, with the app left as it is.

Your part, about 30 seconds:
  1. In the Claude app: your name (bottom left) > Log out
  2. Sign in as work@example.com
```

Step by step:

1. **Pick the target.** The other account with the most usage left, or `--to NAME`.
2. **Check it.** Its Claude Code login works and belongs to the right email, and it has
   usage left. A line marked `XX` stops the switch (`--force` goes on anyway).
3. **Sync.** One [desktop sync](DESKTOP-SYNC.md) run copies chats and scheduled tasks
   into the target's folder. This is the run that matters: the app reads an account's
   task list once, when it signs in to it, so the list has to be right before that.
4. **Wait.** The command watches `lastKnownAccountUuid` in the app's `config.json`. The
   app writes it when a sign-in completes and keeps it at log-out. Ctrl-C only stops the
   waiting.
5. **Finish.** A second sync, a fresh look at the account order, the follower settings,
   and a check of the Chrome extension. It then waits up to 5 minutes for the extension
   to change account too.

Chats that are mid-task stop when you log out. They are not lost: open them after the
switch and say "continue".

`--check` runs steps 1 to 3 with a dry-run sync and changes nothing. `--no-wait` prints
your part and returns. `--wait MINUTES` changes how long step 4 waits (30 by default).

## The watcher

`claude-accounts watch install` adds a LaunchAgent
(`com.github.claude-failover.account-watch`) that runs `claude-accounts watch --quiet`
every 10 minutes (`--every MINUTES` for another interval) and sets `switch.watch` in the
config. The StopFailure hook also starts one pass right after a chat hits a usage limit,
whether or not that chat is handed off, so the notice does not wait for the next
interval. One pass:

1. **A switch it notices is finished** the same way step 5 above does it, and you get
   one notification. If `claude-accounts switch` got there first, the watcher stays
   quiet.
2. **Usage that is due is read.**
3. **The account order is put in step** (next section), when `switch.auto_order` is on.
4. **Follower settings are corrected** (see [Followers](#followers)).
5. **Notices that are new are shown.**

Each notice is shown once. Where a reset time is only the wrapper's guess (it moves
every time the limit is seen again), nothing repeats: one stretch of being out of usage
is one notice, and it ends only when a reading shows usage again. When a test call is
refused without a reset time, the reset time from the limits report is kept instead of a
guess. A problem that lasts (a follower file that cannot be
read, a read-only config) is written to the log once, not on every pass, and
`claude-accounts watch status` lists it. `claude-accounts status` says so when the
watcher has not finished a pass for three intervals.

| Notice | When |
|---|---|
| "…is at 91% of its week" | The app's account passes 90%, and again at 97% (`switch.warn_at`). It names the account the hook will hand chats to and says what happens at 100%: chats continue there in Terminal, or, with extra usage on for the app's account, it bills instead of stopping. If the hook is not installed it says that nothing takes over. |
| "…is out of usage" | The app's account refuses. Chats that hit the limit continue on the named account in Terminal (the hook does that; the notice only says so). When that account is at its own limit and still answers on paid extra usage, the notice says the chats continue there "on paid extra usage". If the hook is not installed, or desktop chats are left out of it, the notice says that nothing takes over and why. |
| "…has used its whole week" | The app's account is past 100% and still answers, on paid extra usage. Nothing moves: turn extra usage off for it. Moving the app by hand instead stops chats that are working. If the account does refuse later, the "…is out of usage" notice follows. |
| "switch the app to …" | Only with `"at_limit": "app"`: replaces the two notices above, the 90% and 97% warnings say to run `claude-accounts switch` instead, and a Terminal window with `claude-accounts switch --assist` opens too (`switch.open_assistant_at_limit`): one at a time, and at most one in 6 hours. |
| "every account is out of usage" | No account has usage left and none would take a chat, with the time the first other one comes back. |
| "…has unused usage" | Another account has at least half of its week left and resets within 36 hours, before the app's account does. |
| "…cannot take over yet" | The app's account is past 75% and the other account's login is failing or has ended. Once a day. |
| "Chrome extension is still on …" | After a switch, while the extension is signed in to another account than the app. |

With `"at_limit": "app"`, the helper window that opens at a limit ends its wait by itself
once the app's account is known to have usage again.

## Which account background jobs use first

With `"switch": {"auto_order": true}` the watcher rewrites `order.headless` and
`order.interactive` in the config so that:

1. An account whose week resets within 12 hours and still has at least 10% left goes
   first. What is left there is lost at the reset.
2. Then the accounts the desktop app is **not** signed in to. The app's account is the
   one that needs a person to switch, so it is spared.
3. Then the app's account.
4. Then accounts with paid extra usage turned on and less than 5% left: jobs there would
   soon be billed.
5. Accounts that are out go last, the one that comes back soonest first.

Only the order of the names changes. No account is added or dropped, and nothing changes
while an account's usage is unknown. The config is read again under a lock right before
it is written, so an account added or a setting changed in the meantime is kept, and a
config file you made read-only is left alone. The old config is copied to
`switch/config-backups/` in the state folder first (the newest 10 are kept).
`"auto_order": {"headless": true}` limits it to `-p` runs. A call pinned with
`CLAUDE_FAILOVER_ACCOUNT` is not affected.

## The Chrome extension

The Claude in Chrome extension is signed in to one claude.ai account, and the desktop
app only sees a browser whose extension is on the app's account. The extension keeps the
account id under the key `accountUuid` in its own storage in the Chrome profile
(`Local Extension Settings/<extension id>/`, a LevelDB database). Chrome holds the
database's lock, so the watcher reads the files without opening it and never writes
there. It follows LevelDB's own rule: of every version of the key in the files the
MANIFEST lists as in use (the write-ahead log and the table files, Snappy blocks
included), the one with the highest sequence number wins. A removal means signed out.

This is not a documented interface. The answer is `unknown` when the storage is
missing, when any file that is part of the database does not parse or the database
changed while it was read, or when the extension is installed in more than one Chrome
profile and they do not all give the same answer (name the one to use with
`switch.chrome_profile`). Nothing is decided from `unknown`. The extension id and the
Chrome folder can be set with `switch.chrome_extension_id` and `switch.chrome_dir`.

## Followers

Some tools have to name the account the extension (or the app) is on, for example a job
that runs `claude --chrome` pinned to one account. List such settings and the watcher
keeps them right:

```json
"switch": {
  "followers": [
    {"file": "~/Projects/some-tool/settings.json", "key": "fetch.chrome_account", "follows": "extension"}
  ]
}
```

`key` is a dotted path to a text value in a JSON file. `follows` is `extension` (the
default) or `app`. When the account is known and the value names another one, that one
value is replaced in the file's text, so key order and indentation stay as they are. A
linked file keeps its link. Nothing is written when the account is unknown, when the
value is not text, when the same key and value appear in more than one place, or when
the file is read-only.

## Configuration

All keys sit in the `"switch"` block of the config and are optional.

| Key | Default | Meaning |
|---|---|---|
| `watch` | `false` | Set by `watch install` / `watch uninstall`. While `true`, the hook starts a watcher pass at a usage limit and `claude-accounts status` adds usage lines. |
| `interval_minutes` | `10` | The interval `watch install` uses when you give no `--every`. |
| `auto_order` | `false` | `true` keeps `order.headless` and `order.interactive` in step with usage. `{"headless": true}` or `{"interactive": true}` for one of them. |
| `probe_minutes` | `60` | How often an account's usage is read (5 to 720). |
| `warn_at` | `[90, 97]` | Percent of the week at which the app's account gets a notice. |
| `at_limit` | `"terminal"` | What takes over when the app's account runs out. `"terminal"`: the StopFailure hook alone; the watcher tells and opens nothing. `"app"`: the watcher also opens the guided switch of the app in Terminal. Any other value counts as `"terminal"`. |
| `open_assistant_at_limit` | `true` | Only with `"at_limit": "app"`: `false` keeps the notice and leaves the helper window out. |
| `followers` | none | See [Followers](#followers). |
| `chrome_dir` | `~/Library/Application Support/Google/Chrome` | Where Chrome keeps its profiles. Also `$CLAUDE_FAILOVER_CHROME_DIR`. |
| `chrome_extension_id` | the Claude in Chrome extension | The extension whose storage is read. |
| `chrome_profile` | every profile | The Chrome profile folder to read (`Default`, `Profile 1`, ...) when the extension is signed in to different accounts in two profiles. |
| `memory_check` | `~/Projects/memguard/memguard.py` if it exists | The script asked whether memory is in danger before a test call. `"0"` turns the check off. Also `$CLAUDE_FAILOVER_MEMGUARD`. |

Files in the state folder: `switch.json` (readings, which notices were shown, the app's
account as last seen), `switch/agent.log` (what the watcher did; a quiet pass writes
nothing), `switch/config-backups/` (the config before each change to it) and
`switch/probe/` (the empty folder test calls run in).

## What it does not do

- **It never signs anything in or out** and reads no token. The two sign-ins are yours.
- **claude.ai connectors and plugins are per account.** A tool that needs a connector
  works only on an account that has it connected.
- **The app's own multi-account switcher.** The app has one behind a feature flag. Where
  it is on, use it: the watcher notices the switch the same way.
- **Model-only limits** are not predicted (see above).

## Turning it off

```bash
claude-accounts watch uninstall     # stop the watcher; the hook stops starting passes
```

Set `"auto_order": false` to keep the account order as you wrote it. The order the
watcher last wrote stays in the config: change `order.headless` and `order.interactive`
by hand, or copy a file back from `switch/config-backups/`.

Neither of these ends the two rules of
[Paid extra usage comes last](#paid-extra-usage-comes-last). Both rules go by the last
usage reading in `switch.json`. Only the watcher, `claude-accounts usage` and
`claude-accounts switch` renew it, so without the watcher it gets old. After 4 hours, or
as soon as the window it was taken in has reset, it counts as not known, and an account
that had extra usage turned on then counts as one that may bill: it is tried last, and
every run that lands on it uses Opus, until you end that.

- `"extra_usage_model": false` in `config.json` turns the Opus rule off. That is the
  way to end that rule alone.
- Deleting `switch.json` from the state folder ends both rules: an account that was
  never read is not taken for one that bills. The file also holds which warnings were
  shown and which account is out until its reset, and that is forgotten with it. The
  next `claude-accounts usage` or `claude-accounts switch` then reads every account
  again with one call each, and for an account that is out with extra usage turned on
  that call is billed. From then on both rules go by the new readings.
