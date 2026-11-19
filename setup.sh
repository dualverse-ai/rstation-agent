#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-$(command -v python3.11 || command -v python3)}"
"$PY" -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ is required"'
"$PY" -m venv "$HERE/.venv"
"$HERE/.venv/bin/python" -m pip install -r "$HERE/requirements.txt"
echo "Ready. Add provider keys and endpoints to .env, then run ./agents start PROBLEM"
