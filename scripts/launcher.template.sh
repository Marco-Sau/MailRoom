#!/bin/bash
# MailRoom app launcher. Bootstraps the venv on first run, then starts the
# desktop window. Finder gives GUI apps a minimal PATH, so every interpreter is
# located by absolute path.
REPO="__REPO__"
VENV="$REPO/.venv"
LOGDIR="$HOME/Library/Logs/MailRoom"
mkdir -p "$LOGDIR"
exec >>"$LOGDIR/launch.log" 2>&1
echo "--- $(date '+%Y-%m-%d %H:%M:%S') launch"

notify() { /usr/bin/osascript -e "display notification \"$1\" with title \"MailRoom\"" >/dev/null 2>&1; }
fail() {
  /usr/bin/osascript -e "display dialog \"$1\" buttons {\"OK\"} default button 1 with icon stop with title \"MailRoom\"" >/dev/null 2>&1
  echo "FATAL: $1"; exit 1
}

if [ ! -d "$REPO" ]; then
  fail "The MailRoom project folder (MailRoom) was not found at:\n$REPO\n\nMove the repo back, or re-run scripts/build_app.sh from its new location."
fi

# --- locate a Python >= 3.11 -------------------------------------------------
find_python() {
  local c
  for c in /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
           /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3 \
           /usr/local/bin/python3.13 /usr/local/bin/python3.12 \
           /usr/local/bin/python3.11 /usr/local/bin/python3 \
           /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
           /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
           /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 \
           "$HOME/miniconda3/bin/python3" "$HOME/anaconda3/bin/python3" \
           "$HOME/miniforge3/bin/python3" /usr/bin/python3; do
    [ -x "$c" ] || continue
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
      echo "$c"; return 0
    fi
  done
  return 1
}

# --- bootstrap ---------------------------------------------------------------
STAMP="$VENV/.bootstrap-stamp"
NEEDS_BOOTSTRAP=0
[ -x "$VENV/bin/python" ] || NEEDS_BOOTSTRAP=1
[ -f "$STAMP" ] || NEEDS_BOOTSTRAP=1
[ -f "$STAMP" ] && [ "$REPO/pyproject.toml" -nt "$STAMP" ] && NEEDS_BOOTSTRAP=1

if [ "$NEEDS_BOOTSTRAP" = "1" ]; then
  PY="$(find_python)" || fail "No Python 3.11 or newer was found.\n\nInstall one, for example:\n  brew install python@3.12\n\nThen open MailRoom again."
  echo "bootstrapping with $PY ($("$PY" -V 2>&1))"
  notify "First launch — installing dependencies. This takes a minute."
  if [ ! -x "$VENV/bin/python" ]; then
    "$PY" -m venv "$VENV" || fail "Could not create the virtual environment.\nSee $LOGDIR/launch.log"
  fi
  "$VENV/bin/python" -m pip install -q --upgrade pip \
    || fail "pip could not be upgraded.\nSee $LOGDIR/launch.log"
  "$VENV/bin/python" -m pip install -q -e "$REPO[desktop]" \
    || fail "Dependency installation failed.\nSee $LOGDIR/launch.log"
  date > "$STAMP"
  notify "Ready."
fi

# --- first-run config --------------------------------------------------------
[ -f "$REPO/config/rules.yaml" ] || cp "$REPO/config/rules.example.yaml" "$REPO/config/rules.yaml" 2>/dev/null
[ -f "$REPO/config/jobs.yaml" ]  || cp "$REPO/config/jobs.example.yaml"  "$REPO/config/jobs.yaml"  2>/dev/null

cd "$REPO"
exec "$VENV/bin/python" -m mailroom.desktop
