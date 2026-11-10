#!/bin/bash
# Install claude-failover for the current user (macOS only).
#
#   ./install.sh              link the commands into ~/.local/bin, write a starter config
#   ./install.sh --uninstall  remove those links again (config and state are kept)
#
# Safe to run more than once: it only adds what is missing. It never uses sudo
# and never edits shell rc files. Lines you may want to add are printed instead.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd -P)"
BIN_DIR="$HOME/.local/bin"
COMMANDS=(claude-accounts claude-failover)
PY=/usr/bin/python3   # the bin/ scripts run on this exact interpreter (stdlib only)

say() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

usage() {
  say "usage: ./install.sh [--uninstall]"
}

# Ask claude_failover.py itself, so this script and the tool always agree on
# where things are. $1 is config_path (the config file the tool would use) or
# effective_real_claude (the Claude Code binary the wrapper will run: the
# config's real_claude, else the detected one). Prints "" when there is none.
ask_tool() {
  "$PY" -I - "$REPO" "$1" <<'EOF'
import sys
sys.path.insert(0, sys.argv[1])
import claude_failover
print(getattr(claude_failover, sys.argv[2])() or "")
EOF
}

do_install() {
  [ "$(uname -s)" = "Darwin" ] || die "claude-failover runs on macOS only."
  if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    die "needs $PY 3.9 or newer. It comes with the Xcode Command Line Tools: xcode-select --install"
  fi

  say "Installing claude-failover from $REPO"
  say "  python - $("$PY" --version 2>&1)"
  local claude_bin
  claude_bin="$(ask_tool effective_real_claude)" || die "could not load $REPO/claude_failover.py"
  if [ -n "$claude_bin" ] && [ -x "$claude_bin" ]; then
    say "  claude CLI - $claude_bin"
  elif [ -n "$claude_bin" ]; then
    warn "the config names $claude_bin as the claude CLI (real_claude), and it is not there. Remove real_claude from the config to auto-detect Claude Code."
  else
    warn "the claude CLI was not found. Install Claude Code first: the wrapper runs the real binary."
  fi

  # 1. Put the two commands on PATH as symlinks into this repo.
  if [ -e "$BIN_DIR" ] && [ ! -d "$BIN_DIR" ]; then
    die "$BIN_DIR exists and is not a folder."
  fi
  mkdir -p "$BIN_DIR"
  local name src dst old relinked=0
  for name in "${COMMANDS[@]}"; do
    src="$REPO/bin/$name"
    dst="$BIN_DIR/$name"
    if [ -e "$dst" ] && [ ! -L "$dst" ]; then
      # A real file or folder we did not make: never overwrite it.
      warn "$dst is a file, not a link. Left it alone. Move it away and run ./install.sh again."
      continue
    fi
    if [ -L "$dst" ]; then
      old="$(readlink "$dst")"
      if [ "$old" = "$src" ]; then
        say "  $dst - already linked"
        continue
      fi
      ln -sfn "$src" "$dst"
      say "  $dst - relinked (was -> $old)"
      relinked=1
    else
      ln -sfn "$src" "$dst"
      say "  $dst - linked"
    fi
  done
  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "$BIN_DIR is not on your PATH. Add this line to ~/.zshrc: export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
  esac

  # 2. A starter config, only when the tool finds none (init never overwrites one).
  local cfg out
  cfg="$(ask_tool config_path)" || die "could not load $REPO/claude_failover.py"
  if [ -n "$cfg" ]; then
    say "  config - $cfg (kept as is)"
  elif out="$("$REPO/bin/claude-accounts" init 2>&1)"; then
    say "  config - $(ask_tool config_path) (new starter config)"
  else
    warn "could not write a starter config: $out"
  fi

  # 3. The option table of the installed CLI, so the wrapper knows its newest
  #    flags. Best effort: without it the built-in table is used.
  if out="$("$REPO/bin/claude-accounts" argspec 2>&1)"; then
    say "  option table - $out"
  else
    say "  option table - not rebuilt: $out"
    say "    (run claude-accounts argspec after installing or updating Claude Code)"
  fi

  # 4. The links pointed at another copy (this folder was moved, or another clone
  #    was installed before). The hook and the background sync name a copy by its
  #    full path, so show whether they still point at a copy that exists.
  if [ "$relinked" = 1 ]; then
    say "  The links pointed at another copy. Where the hook and the background sync point:"
    "$REPO/bin/claude-accounts" hook status 2>&1 | sed 's/^/    /' || true
    "$REPO/bin/claude-accounts" sync status 2>&1 | sed 's/^/    /' || true
  fi

  say ""
  say "Next steps:"
  say "  1. Add your accounts. Each one gets its own folder, such as ~/.claude-personal:"
  say "       claude-accounts add personal --email you@example.com"
  say "       claude-accounts add work --email you@work.example"
  say "  2. Sign each one in. A browser opens: sign in as that account, then click Authorize."
  say "       claude-accounts login personal"
  say "       claude-accounts login work"
  say "  3. Let a chat that hits a usage limit continue on the next account:"
  say "       claude-accounts hook install"
  say "  4. Optional - list desktop app chats under every account:"
  say "       claude-accounts sync install"
  say "  5. Check that everything is ready:"
  say "       claude-accounts status"
  say ""
  say "Optional - to send every plain claude command through the wrapper (also"
  say "inside scripts started from your shell), add this line to ~/.zshrc:"
  say "  export PATH=\"$REPO/bin:\$PATH\""
}

do_uninstall() {
  local name dst
  for name in "${COMMANDS[@]}"; do
    dst="$BIN_DIR/$name"
    if [ ! -L "$dst" ]; then
      if [ -e "$dst" ]; then
        say "  $dst - left alone (a file, not a link this script made)"
      fi
      continue
    fi
    case "$(readlink "$dst")" in
      "$REPO"/*)
        rm -f -- "$dst"
        say "  $dst - removed"
        ;;
      *) say "  $dst - left alone (points somewhere else)" ;;
    esac
  done
  say ""
  say "Your config, state, account folders and logins were not touched."
  say "To remove the hook and the background sync as well:"
  # %q quotes the path so the line can be pasted even if it contains spaces.
  printf '  %q hook uninstall\n' "$REPO/bin/claude-accounts"
  printf '  %q sync uninstall\n' "$REPO/bin/claude-accounts"
  say "If you added the export PATH line for $REPO/bin to ~/.zshrc, remove it too."
}

case "${1:-}" in
  "") do_install ;;
  --uninstall) do_uninstall ;;
  -h | --help) usage ;;
  *)
    usage >&2
    exit 2
    ;;
esac
