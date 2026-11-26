---
name: voice-wake
description: >-
  Manage, configure, diagnose, and control the Antigravity Voice Wake macOS plugin
  (offline wake-word detection via English KWS or recorded voice samples, global
  macOS hotkey, Auxiliary Pane Web UI, microphone mode, silence auto-submit timeout,
  and logs). Use when the user asks to check voice wake status, change the wake mode
  or phrase, switch UI language, adjust pause timeout, switch microphone, or open
  the Voice Wake side panel.
---

# Voice Wake Plugin Control

The **Voice Wake** plugin runs an offline wake-word Sidecar (`voice-wake/voice-wake`) on macOS with an Auxiliary Pane Web UI.

## Quick Open Side Panel

When the Sidecar is enabled and running, surface this pill so the user can open the panel directly:

`[Voice Wake](sidecar://voice-wake/voice-wake/)`

## Paths (User Runtime Data)

- User config: `~/.local/share/antigravity-voice-wake/config.json`
- Recorded voice samples & embeddings: `~/.local/share/antigravity-voice-wake/voice_samples/`
- Wake phrase raw / BPE tokens (for `text` mode): `~/.local/share/antigravity-voice-wake/keywords_raw.txt` and `~/.local/share/antigravity-voice-wake/keywords.txt`
- Native AX helper: `~/.local/share/antigravity-voice-wake/bin/agy-ax-helper`
- Logs: `~/.local/share/antigravity-voice-wake/logs/voice-wake.log`
- Python 3.12 venv: `~/.local/share/venvs/antigravity-voice-wake`
- Plugin Sidecar runner: `~/.gemini/config/plugins/voice-wake/sidecars/voice-wake/run.sh`

## Wake Modes & Language

- `wake_mode`:
  - `"text"`: Local `sherpa-onnx` GigaSpeech Zipformer keyword spotting using `wake_phrase`, optional `wake_aliases`, `keywords_threshold` (`0.10..0.50`), and `keywords_score` (`0.5..3.0`).
  - `"voice_sample"`: Local `EfficientWord-Net` (`Resnet50_Arc_loss` INT8 ONNX) few-shot matching against user-recorded or imported samples in `~/.local/share/antigravity-voice-wake/voice_samples/`, controlled by `voice_sample_threshold` (`0.55..0.90`).
- `ui_language`: `"auto"` (detects macOS system language; Simplified Chinese for `zh*`, English otherwise), `"en"`, or `"zh"`.

## Common Tasks

1. **Check status, permissions, and live microphone RMS**:
   ```bash
   "$HOME/.gemini/config/plugins/voice-wake/sidecars/voice-wake/run.sh" --check
   ```
   Verify `ax_trusted: true`, `mic_ok: true` (`mic_rms_250ms > 0`), `kws_ok: true`, and `vad_ok: true`.

2. **Update runtime settings**:
   Use the Sidecar Web UI (`POST /api/config`, `POST /api/toggle-pause`, `POST /api/wake-test`, `POST /api/samples/*`) or edit `~/.local/share/antigravity-voice-wake/config.json` (`ui_language`, `project_name`, `model_keyword`, `thinking_effort`, `mic_mode`, `voice_wake_enabled`, `wake_mode`, `wake_phrase`, `wake_aliases`, `voice_sample_threshold`, `hotkey_enabled`, `hotkey`, `silence_timeout_sec`, `keywords_score`, `keywords_threshold`, `sounds.volume`).

3. **Change wake phrase in `text` mode**:
   Update `wake_phrase` in `~/.local/share/antigravity-voice-wake/config.json` and regenerate `keywords.txt`:
   ```bash
   cat > "$HOME/.local/share/antigravity-voice-wake/keywords_raw.txt" <<'EOF'
   HEY GRAVITY :1.0 #0.25 @HEY_GRAVITY
   EOF
   "$HOME/.local/share/venvs/antigravity-voice-wake/bin/sherpa-onnx-cli" text2token \
     --tokens "$HOME/.local/share/antigravity-voice-wake/models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01/tokens.txt" \
     --tokens-type bpe \
     --bpe-model "$HOME/.local/share/antigravity-voice-wake/models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01/bpe.model" \
     "$HOME/.local/share/antigravity-voice-wake/keywords_raw.txt" \
     "$HOME/.local/share/antigravity-voice-wake/keywords.txt"
   ```

4. **Test wake word against a WAV file or run a non-submitting workflow test**:
   ```bash
   "$HOME/.gemini/config/plugins/voice-wake/sidecars/voice-wake/run.sh" --test-wav /path/to/16k_mono.wav
   "$HOME/.gemini/config/plugins/voice-wake/sidecars/voice-wake/run.sh" --trigger-now --dry-run-submit
   ```

5. **Inspect logs**:
   ```bash
   tail -n 40 "$HOME/.local/share/antigravity-voice-wake/logs/voice-wake.log"
   ```
