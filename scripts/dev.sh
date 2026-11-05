#!/usr/bin/env bash
# Local dev loop. Run from the repo root on macOS (not inside a container -
# the Keychain is only reachable from a real macOS session).
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -d .venv ]; then
  python3 -m venv .venv
  ./.venv/bin/pip install -U pip
  ./.venv/bin/pip install -e '.[dev]'
fi
[ -f config/rules.yaml ] || cp config/rules.example.yaml config/rules.yaml
[ -f config/jobs.yaml ]  || cp config/jobs.example.yaml  config/jobs.yaml
exec ./.venv/bin/python -m uvicorn mailroom.main:app --reload --port "${PORT:-8765}"
