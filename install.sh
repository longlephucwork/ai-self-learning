#!/usr/bin/env bash
# learning-harness installer launcher for Linux and macOS.
#
#   From a clone:  ./install.sh [options]
#   One line:      curl -fsSL https://raw.githubusercontent.com/longlephucwork/ai-self-learning/main/install.sh | bash
#
# Finds Python 3.10+, clones (or updates) the repo if this script isn't inside one, then runs install.py,
# which asks before every change. Options are passed through: --yes --check --uninstall --no-hooks
# --no-mcp --no-path --skip-tests --allow-unsandboxed. Clone location: $LEARNING_HARNESS_DIR
# (default ~/ai-self-learning; an install from before the rename, in ~/ai-self-learning, is kept).
set -euo pipefail

REPO_URL="${LEARNING_HARNESS_REPO_URL:-https://github.com/longlephucwork/ai-self-learning.git}"   # override for forks

find_python() {
  for c in python3 python3.13 python3.12 python3.11 python3.10 python; do
    if command -v "$c" >/dev/null 2>&1 &&
       "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      command -v "$c"
      return 0
    fi
  done
  return 1
}

if ! PY="$(find_python)"; then
  echo "error: Python 3.10+ not found." >&2
  case "$(uname -s)" in
    Darwin) echo "  install it with:  brew install python   (or from https://www.python.org/downloads/)" >&2 ;;
    *)      echo "  install it with your package manager, e.g.  sudo apt install python3" >&2 ;;
  esac
  exit 1
fi

here=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if [ -n "$here" ] && [ -f "$here/install.py" ]; then
  repo="$here"
else
  command -v git >/dev/null 2>&1 || { echo "error: git not found" >&2; exit 1; }
  repo="${LEARNING_HARNESS_DIR:-$HOME/ai-self-learning}"
  # installed before the repo was renamed: keep using that clone (its hooks and PATH entry point there)
  if [ -z "${LEARNING_HARNESS_DIR:-}" ] && [ ! -e "$repo" ] && [ -d "$HOME/claude-learning-harness/.git" ]; then
    repo="$HOME/claude-learning-harness"
  fi
  if [ -d "$repo/.git" ]; then
    echo "already installed in $repo (re-running the installer; to get a new release: python3 install.py --update)"
  elif [ -e "$repo" ]; then
    echo "error: $repo exists but is not a git clone; set LEARNING_HARNESS_DIR to another folder" >&2
    exit 1
  else
    echo "cloning into $repo"
    git clone "$REPO_URL" "$repo"
    # start from the newest release (vX.Y.Z tag on main), not whatever is on main right now
    latest="$(git -C "$repo" tag --merged origin/main --list 'v[0-9]*' --sort=-v:refname | head -n 1)"
    if [ -n "$latest" ]; then
      echo "using release $latest"
      git -C "$repo" reset --quiet --hard "$latest"
    fi
  fi
fi

# Under `curl ... | bash` stdin is this script, not your terminal: reattach the terminal so the
# installer can ask its questions. Without a terminal it uses safe defaults.
if [ ! -t 0 ] && (exec </dev/tty) 2>/dev/null; then
  exec "$PY" "$repo/install.py" "$@" </dev/tty
fi
exec "$PY" "$repo/install.py" "$@"
