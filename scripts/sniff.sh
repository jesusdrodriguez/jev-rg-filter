#!/usr/bin/env bash
# Resolve an interpreter that has typesafe_sdk, bootstrapping a venv on first run,
# then hand off to sniper.py. All setup chatter goes to stderr so stdout stays clean.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"

venv_python() {
  if [ -x "$VENV/Scripts/python.exe" ]; then echo "$VENV/Scripts/python.exe"
  elif [ -x "$VENV/bin/python" ]; then echo "$VENV/bin/python"
  fi
}

PY="$(venv_python)"

if [ -z "$PY" ]; then
  echo "context-sniper: first run, creating $VENV ..." >&2
  BASE=""
  for candidate in python3 python py; do
    if command -v "$candidate" >/dev/null 2>&1; then BASE="$candidate"; break; fi
  done
  if [ -z "$BASE" ]; then
    echo "context-sniper: no python interpreter found on PATH." >&2
    exit 2
  fi
  "$BASE" -m venv "$VENV" >&2
  PY="$(venv_python)"
  "$PY" -m pip install --quiet --upgrade pip >&2
  "$PY" -m pip install --quiet -r "$ROOT/requirements.txt" >&2
  echo "context-sniper: ready." >&2
fi

exec "$PY" "$ROOT/scripts/sniper.py" "$@"
