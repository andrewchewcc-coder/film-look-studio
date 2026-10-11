#!/bin/bash
# One-time setup on macOS: Homebrew Python + exiftool, then a local virtualenv.
set -e
cd "$(dirname "$0")"

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is needed. Install it from https://brew.sh, then run ./setup.sh again."
  exit 1
fi
brew list python@3.12 >/dev/null 2>&1 || brew install python@3.12
brew list exiftool >/dev/null 2>&1 || brew install exiftool

PY="$(brew --prefix python@3.12)/bin/python3.12"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -e ".[dev]" -q

echo
echo "Done. Start the app with:   ./studio serve ~/Pictures/your-folder"
