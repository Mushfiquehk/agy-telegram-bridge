#!/usr/bin/env bash
# Alternative: run the bridge inside WSL (Ubuntu). Only use this if agy is
# installed *inside* WSL (Linux build) and signed in there. If agy lives on the
# Windows side, use scripts/setup.ps1 instead - it's simpler and faster.
set -euo pipefail
cd "$(dirname "$0")/.."
command -v python3 >/dev/null || { echo "sudo apt install python3 python3-venv"; exit 1; }
command -v agy >/dev/null || echo "WARNING: agy not on PATH inside WSL; set AGY_COMMAND in .env"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e ".[dev]"
[ -f .env ] || { cp .env.example .env; echo "Edit .env (token + your user id)"; }
echo "Check:  .venv/bin/python -m agy_bridge --doctor"
echo "Run:    .venv/bin/python -m agy_bridge"
echo "Autostart (systemd in WSL): see README 'Running under WSL'."
