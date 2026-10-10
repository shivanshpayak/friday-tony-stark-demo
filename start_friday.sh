#!/usr/bin/env bash
# FRIDAY launcher for Linux — runs friday_start from the project's venv.
# Used by the autostart entry (install_startup.py) and by `friday_ctl restart`.

cd "$(dirname "$(readlink -f "$0")")" || exit 1
export PATH="$HOME/.local/bin:$PATH"   # where the uv installer puts uv

if [ -x .venv/bin/friday_start ]; then
    exec .venv/bin/friday_start
fi
exec uv run friday_start
