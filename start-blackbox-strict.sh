#!/bin/sh
set -eu
BLACKBOX_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BLACKBOX_PYTHON=${BLACKBOX_PYTHON:-"$BLACKBOX_ROOT/.venv/bin/python"}
exec "$BLACKBOX_PYTHON" "$BLACKBOX_ROOT/scripts/run_strict.py" "$@"
