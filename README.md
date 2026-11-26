<p align="center">
  <img src="assets/logo.svg" width="64" height="64" alt="Voice Wake logo" />
</p>

<h1 align="center">Voice Wake for Antigravity</h1>

<p align="center">
  Offline macOS wake-word &amp; global hotkey plugin for Google Antigravity, with a clean bilingual Auxiliary Pane UI.
</p>

<p align="center">
  <strong>English</strong> · <a href="./README.zh-CN.md">简体中文</a>
</p>

---

**Voice Wake** (`voice-wake`) runs locally on macOS in the background. Say your wake phrase or press a global hotkey to bring **Antigravity** to the front, open a new conversation in your chosen project, switch to your preferred model & thinking effort, trigger Antigravity's built-in voice dictation, and automatically submit your prompt after you finish speaking.

## Highlights

- **Dual-Mode 100% Local Wake Detection**
  - **English Phrase Mode**: Streaming `sherpa-onnx` GigaSpeech Zipformer keyword spotting with optional phonetic aliases (`wake_aliases`), sensitivity presets (`Sensitive` / `Balanced` / `Strict`), and Silero VAD utterance gating.
  - **Recorded Voice Mode**: Local few-shot speaker/phrase matching powered by `EfficientWord-Net` (`Resnet50_Arc_loss` quantized INT8 ONNX) — enroll your own voice samples live from the microphone or import `.wav` / `.m4a` / `.mp3` clips for any custom word or non-English phrase.
- **Coexisting macOS Global Hotkey**: Native Carbon hotkey (`ctrl+g` by default, customizable via in-UI key capture) works even while voice listening is paused, without needing macOS Input Monitoring permissions.
- **Bluetooth Audio Protection**: Defaults to `mic_mode: "built-in"` so listening on your Mac's built-in mic never forces Bluetooth headphones into low-bandwidth HFP call mode.
- **Auto-Submit on Speech Pause**: Combines local Silero VAD with Antigravity composer text monitoring to stop dictation and submit cleanly after a configurable pause (`1.8s` default).
- **Clean Bilingual Auxiliary Pane UI (`Status` / `Wake` / `Settings`)**:
  - Automatically follows your macOS system language (Simplified Chinese for `zh*`, English for everything else), with manual override in **Settings → Interface → Language**.
  - Pure **Status** view with persistent live status, quick Pause/Start & Test Wake controls, live config summary, and debounced auto-save across all settings.
  - Built-in environment check, non-submitting workflow test, and human-readable activity log under **Settings → Diagnostics & Activity**.

## Requirements

- macOS (Apple Silicon recommended) + **Antigravity App**
- Xcode Command Line Tools (`swiftc`)
- `uv` or Python `3.11–3.13`
- macOS Permissions (requested on first use):
  - **Microphone** (for wake detection and dictation)
  - **Accessibility** (to focus Antigravity, select project/model, and trigger dictation)

## Quick Install

```bash
git clone https://github.com/rmqg/antigravity-voice-wake.git
cd antigravity-voice-wake
chmod +x install.sh uninstall.sh
./install.sh
```

What `install.sh` does:
1. Creates an isolated Python 3.12 virtualenv at `~/.local/share/venvs/antigravity-voice-wake` with `sherpa-onnx`, `sounddevice`, `numpy`, `onnxruntime`, and `EfficientWord-Net`.
2. Downloads local ONNX wake-word and Silero VAD models into `~/.local/share/antigravity-voice-wake/models/`.
3. Compiles [agy_ax_helper.swift](./swift/agy_ax_helper.swift) to `~/.local/share/antigravity-voice-wake/bin/agy-ax-helper`.
4. Initializes `~/.local/share/antigravity-voice-wake/config.json` from [config.example.json](./sidecars/voice-wake/config.example.json).
5. Links the plugin into `~/.gemini/config/plugins/voice-wake` and enables the Sidecar UI.

Once installed, open the **Voice Wake** side panel in Antigravity (or ask your assistant `/voice-wake`).

## Panel Overview

- **Status**: View live service state, pause/resume voice listening, enter **Test Wake** mode (chimes and counts hits or similarity scores without opening chats), and see active mode, hotkey, microphone, and target project/model.
- **Wake**: Toggle voice wake & global hotkey, record a custom hotkey, switch between **English Phrase** and **Recorded Voice**, record/import/preview/delete voice samples, or tune sensitivity thresholds.
- **Settings**: Choose target project, model, and thinking effort; select microphone, auto-send pause duration, alert volume, and UI language (`Auto` / `English` / `简体中文`); run **Check Environment**, **Test Workflow (No Send)**, and inspect the **Activity Log**.

## CLI Diagnostics

```bash
# Check Accessibility permission, live mic RMS, KWS, and VAD readiness
~/.gemini/config/plugins/voice-wake/sidecars/voice-wake/run.sh --check

# Test a 16kHz mono WAV file against the current wake configuration
~/.gemini/config/plugins/voice-wake/sidecars/voice-wake/run.sh --test-wav /path/to/test.wav

# Trigger the handoff workflow once without submitting text
~/.gemini/config/plugins/voice-wake/sidecars/voice-wake/run.sh --trigger-now --dry-run-submit
```

## Uninstall

```bash
./uninstall.sh
```

## Project Layout

```text
antigravity-voice-wake/
├── assets/logo.svg                 # Flat vector plugin icon
├── plugin.json                     # Antigravity plugin manifest
├── install.sh / uninstall.sh       # Installer & uninstaller for macOS
├── swift/agy_ax_helper.swift       # Native macOS AX + Carbon hotkey helper
├── skills/voice-wake/SKILL.md      # Agent skill for managing Voice Wake
├── sidecars/voice-wake/
│   ├── sidecar.json                # Auxiliary Pane Sidecar manifest
│   ├── config.example.json         # Default configuration template
│   ├── voice_wake.py               # Audio pipeline, KWS/VAD daemon & HTTP API
│   ├── index.html                  # Bilingual 3-tab Sidecar control panel
│   └── run.sh                      # Venv runner wrapper
├── THIRD_PARTY_NOTICES.md          # Upstream open-source & model attributions
└── LICENSE                         # Apache-2.0
```

## Acknowledgments & License

See [THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md) for upstream projects and pre-trained models (`sherpa-onnx`, `silero-vad`, `wake-for-codex`, `EfficientWord-Net`).

Licensed under the [Apache License 2.0](./LICENSE).
