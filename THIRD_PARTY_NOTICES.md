# Third-Party Notices

This plugin relies on the following open-source projects and pre-trained models:

1. **k2-fsa / sherpa-onnx** (Apache-2.0 License)
   - Used for local streaming Zipformer Keyword Spotting (`sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01`) and local Silero VAD execution.
   - Upstream: https://github.com/k2-fsa/sherpa-onnx

2. **snakers4 / silero-vad** (MIT License)
   - Used for local voice activity detection (`silero_vad.onnx`) to detect end-of-speech pauses.
   - Upstream: https://github.com/snakers4/silero-vad

3. **playing / wake-for-codex** (Apache-2.0 License)
   - Design reference for macOS single-instance locking, `sounddevice` + `sherpa-onnx` callback streaming, and microphone lifecycle handling.
   - Upstream: https://github.com/playing/wake-for-codex

4. **Ant-Brain / EfficientWord-Net** (Apache-2.0 License)
   - Used for local few-shot voice-sample wake-word enrollment and matching (`Resnet50_Arc_loss` quantized INT8 ONNX model via `onnxruntime`).
   - Upstream: https://github.com/Ant-Brain/EfficientWord-Net
