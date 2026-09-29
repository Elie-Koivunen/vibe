#!/usr/bin/env sh
# Launch VLC Bookmark Studio on Linux or inside WSL (Ubuntu).
#
# Uses the project's virtual environment in ./.venv if there is one, otherwise $PYTHON,
# otherwise python3. One-time setup:
#   python3 -m venv .venv && .venv/bin/pip install -e .
# See README.md ("Setup") for the system packages Qt and VLC need.
cd "$(dirname "$0")" || exit 1
if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
else
    PY="${PYTHON:-python3}"
fi
PYTHONPATH="$(pwd)/src${PYTHONPATH:+:$PYTHONPATH}" exec "$PY" -m bookmark_studio "$@"
