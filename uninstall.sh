#!/bin/sh
set -eu

PLUGIN_LINK="$HOME/.gemini/config/plugins/voice-wake"
DATA_DIR="$HOME/.local/share/antigravity-voice-wake"
VENV_DIR="$HOME/.local/share/venvs/antigravity-voice-wake"

if [ -n "${ANTIGRAVITY_LS_ADDRESS:-}" ] && [ -n "${ANTIGRAVITY_CSRF_TOKEN:-}" ]; then
  curl -sS -X POST "http://${ANTIGRAVITY_LS_ADDRESS}/exa.language_server_pb.LanguageServerService/ManageSidecar" \
    -H "Content-Type: application/json" \
    -H "x-codeium-csrf-token: ${ANTIGRAVITY_CSRF_TOKEN}" \
    -d '{"sidecarId":"voice-wake/voice-wake","action":"SIDECAR_ACTION_DISABLE"}' >/dev/null 2>&1 || true
fi

if [ -L "$PLUGIN_LINK" ] || [ -e "$PLUGIN_LINK" ]; then
  rm -rf "$PLUGIN_LINK"
  echo "Removed plugin link: $PLUGIN_LINK"
fi

echo "Uninstalled voice-wake plugin."
echo "To also delete local models, config, logs, and Python venv, run:"
echo "  rm -rf \"$DATA_DIR\" \"$VENV_DIR\""
