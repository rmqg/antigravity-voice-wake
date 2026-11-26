#!/bin/sh
set -eu

if [ "$(uname -s)" != "Darwin" ]; then
  echo "Error: voice-wake only supports macOS." >&2
  exit 1
fi

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PLUGIN_NAME="voice-wake"
PLUGIN_LINK="$HOME/.gemini/config/plugins/$PLUGIN_NAME"
LEGACY_SIDECAR="$HOME/.gemini/config/sidecars/$PLUGIN_NAME"
DATA_DIR="$HOME/.local/share/antigravity-voice-wake"
VENV_DIR="$HOME/.local/share/venvs/antigravity-voice-wake"
MODEL_DIR="$DATA_DIR/models"
KWS_NAME="sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
BIN_DIR="$DATA_DIR/bin"

mkdir -p "$HOME/.gemini/config/plugins" "$MODEL_DIR" "$BIN_DIR" "$DATA_DIR/logs"

echo "[1/6] Checking Python / uv toolchain..."
if command -v uv >/dev/null 2>&1; then
  if [ ! -x "$VENV_DIR/bin/python" ]; then
    PY_BIN=""
    for candidate in python3.12 python3.13 python3.11; do
      if command -v "$candidate" >/dev/null 2>&1; then
        PY_BIN="$candidate"
        break
      fi
    done
    if [ -n "$PY_BIN" ]; then
      uv venv --python "$PY_BIN" "$VENV_DIR"
    else
      uv venv --python 3.12 "$VENV_DIR"
    fi
  fi
  uv pip install --python "$VENV_DIR/bin/python" sherpa-onnx sounddevice numpy click sentencepiece pypinyin onnxruntime
  uv pip install --python "$VENV_DIR/bin/python" --no-deps EfficientWord-Net==1.0.5
else
  if [ ! -x "$VENV_DIR/bin/python" ]; then
    PY_BIN=""
    for candidate in python3.12 python3.13 python3.11 python3; do
      if command -v "$candidate" >/dev/null 2>&1; then
        PY_BIN="$candidate"
        break
      fi
    done
    if [ -z "$PY_BIN" ]; then
      echo "Error: uv or Python 3.11-3.13 is required." >&2
      exit 1
    fi
    "$PY_BIN" -m venv "$VENV_DIR"
  fi
  "$VENV_DIR/bin/python" -m pip install --upgrade pip
  "$VENV_DIR/bin/python" -m pip install sherpa-onnx sounddevice numpy click sentencepiece pypinyin onnxruntime
  "$VENV_DIR/bin/python" -m pip install --no-deps EfficientWord-Net==1.0.5
fi

echo "[2/6] Ensuring ONNX models..."
if [ ! -d "$MODEL_DIR/$KWS_NAME" ]; then
  curl -fL --retry 3 -o "/tmp/${KWS_NAME}.tar.bz2" \
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/${KWS_NAME}.tar.bz2"
  tar -xjf "/tmp/${KWS_NAME}.tar.bz2" -C "$MODEL_DIR"
  rm -f "/tmp/${KWS_NAME}.tar.bz2"
fi
if [ ! -f "$MODEL_DIR/silero_vad.onnx" ]; then
  curl -fL --retry 3 -o "$MODEL_DIR/silero_vad.onnx" \
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx"
fi

echo "[3/6] Compiling agy-ax-helper..."
if ! command -v swiftc >/dev/null 2>&1; then
  echo "Error: swiftc is required (run: xcode-select --install)." >&2
  exit 1
fi
swiftc -O -framework ApplicationServices -framework Cocoa -framework CoreAudio -framework Carbon \
  "$SCRIPT_DIR/swift/agy_ax_helper.swift" -o "$BIN_DIR/agy-ax-helper"

echo "[4/6] Bootstrapping config and keywords..."
if [ ! -f "$DATA_DIR/config.json" ]; then
  cp "$SCRIPT_DIR/sidecars/voice-wake/config.example.json" "$DATA_DIR/config.json"
fi
if [ ! -f "$DATA_DIR/keywords.txt" ]; then
  printf 'HEY GRAVITY :1.0 #0.25 @HEY_GRAVITY\n' > "$DATA_DIR/keywords_raw.txt"
  "$VENV_DIR/bin/sherpa-onnx-cli" text2token \
    --tokens "$MODEL_DIR/$KWS_NAME/tokens.txt" \
    --tokens-type bpe \
    --bpe-model "$MODEL_DIR/$KWS_NAME/bpe.model" \
    "$DATA_DIR/keywords_raw.txt" \
    "$DATA_DIR/keywords.txt"
fi

echo "[5/6] Linking Antigravity plugin and cleaning legacy standalone sidecar..."
if [ -L "$PLUGIN_LINK" ] || [ -e "$PLUGIN_LINK" ]; then
  rm -rf "$PLUGIN_LINK"
fi
ln -s "$SCRIPT_DIR" "$PLUGIN_LINK"

if [ -d "$LEGACY_SIDECAR" ]; then
  rm -rf "$LEGACY_SIDECAR"
fi

echo "[6/6] Requesting Sidecar activation..."
if [ -n "${ANTIGRAVITY_LS_ADDRESS:-}" ] && [ -n "${ANTIGRAVITY_CSRF_TOKEN:-}" ]; then
  curl -sS -X POST "http://${ANTIGRAVITY_LS_ADDRESS}/exa.language_server_pb.LanguageServerService/ManageSidecar" \
    -H "Content-Type: application/json" \
    -H "x-codeium-csrf-token: ${ANTIGRAVITY_CSRF_TOKEN}" \
    -d '{"sidecarId":"voice-wake/voice-wake","action":"SIDECAR_ACTION_ENABLE"}' >/dev/null 2>&1 || true
fi

chmod 755 "$SCRIPT_DIR/install.sh" "$SCRIPT_DIR/uninstall.sh" "$SCRIPT_DIR/sidecars/voice-wake/run.sh"
echo "Done! Run: $PLUGIN_LINK/sidecars/voice-wake/run.sh --check"
