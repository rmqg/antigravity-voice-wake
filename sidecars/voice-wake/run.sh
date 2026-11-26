#!/bin/sh
set -eu
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
VENV_PY="${VOICE_WAKE_PYTHON:-$HOME/.local/share/venvs/antigravity-voice-wake/bin/python}"
exec "$VENV_PY" "$SCRIPT_DIR/voice_wake.py" "$@"
