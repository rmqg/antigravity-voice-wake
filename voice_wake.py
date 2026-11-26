#!/usr/bin/env python3
"""Always-listening local wake-word orchestrator for Antigravity on macOS."""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import json
import os
import queue
import signal
import subprocess
import sys
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np
import sherpa_onnx
import sounddevice as sd

BASE_DIR = Path(__file__).resolve().parent
LOCAL_DATA_DIR = Path.home() / ".local" / "share" / "antigravity-voice-wake"
MODEL_ROOT = LOCAL_DATA_DIR / "models"
KWS_MODEL_DIR = MODEL_ROOT / "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
SILERO_VAD_PATH = MODEL_ROOT / "silero_vad.onnx"
AX_HELPER_BIN = LOCAL_DATA_DIR / "bin" / "agy-ax-helper"
LOG_FILE = LOCAL_DATA_DIR / "logs" / "voice-wake.log"
LOCK_FILE = Path("/tmp/antigravity-voice-wake.lock")
SAMPLE_RATE = 16_000


@dataclasses.dataclass(frozen=True)
class SoundConfig:
    ready: str
    submit: str
    cancel: str
    volume: float


@dataclasses.dataclass(frozen=True)
class WakeConfig:
    project_name: str
    model_keyword: str
    mic_mode: str
    keywords_file: Path
    keywords_score: float
    keywords_threshold: float
    wait_for_speech_timeout_sec: float
    silence_timeout_sec: float
    max_dictation_sec: float
    transcript_settle_sec: float
    cooldown_sec: float
    sounds: SoundConfig


def load_config(path: Path) -> WakeConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    kw_path = Path(raw.get("keywords_file", "keywords.txt"))
    if not kw_path.is_absolute():
        kw_path = (path.parent / kw_path).resolve()
    sounds_raw = raw.get("sounds", {})
    return WakeConfig(
        project_name=str(raw.get("project_name", "Code")),
        model_keyword=str(raw.get("model_keyword", "Orchestrator")),
        mic_mode=str(raw.get("mic_mode", "built-in")),
        keywords_file=kw_path,
        keywords_score=float(raw.get("keywords_score", 1.5)),
        keywords_threshold=float(raw.get("keywords_threshold", 0.25)),
        wait_for_speech_timeout_sec=float(raw.get("wait_for_speech_timeout_sec", 10.0)),
        silence_timeout_sec=float(raw.get("silence_timeout_sec", 1.8)),
        max_dictation_sec=float(raw.get("max_dictation_sec", 60.0)),
        transcript_settle_sec=float(raw.get("transcript_settle_sec", 0.4)),
        cooldown_sec=float(raw.get("cooldown_sec", 1.5)),
        sounds=SoundConfig(
            ready=str(sounds_raw.get("ready", "/System/Library/Sounds/Tink.aiff")),
            submit=str(sounds_raw.get("submit", "/System/Library/Sounds/Glass.aiff")),
            cancel=str(sounds_raw.get("cancel", "/System/Library/Sounds/Pop.aiff")),
            volume=float(sounds_raw.get("volume", 0.5)),
        ),
    )


def log_event(event: str, **details: Any) -> None:
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "event": event,
        **details,
    }
    line = json.dumps(record, ensure_ascii=False)
    print(line, file=sys.stderr, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


class SingleInstanceLock:
    """Advisory file lock so only one daemon listens on the microphone."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Any = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w", encoding="utf-8")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._handle.close()
            self._handle = None
            raise RuntimeError(f"Another voice-wake instance is already running ({self.path})") from exc
        self._handle.write(f"{os.getpid()}\n")
        self._handle.flush()

    def release(self) -> None:
        if self._handle is not None:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            self._handle.close()
            self._handle = None


def run_ax_helper(*args: str, timeout: float = 10.0) -> dict[str, Any]:
    if not AX_HELPER_BIN.is_file():
        return {"ok": False, "error": f"helper_missing:{AX_HELPER_BIN}"}
    try:
        proc = subprocess.run(
            [str(AX_HELPER_BIN), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "helper_timeout", "args": list(args)}
    out = proc.stdout.strip()
    if out:
        try:
            data = json.loads(out.splitlines()[-1])
            data.setdefault("ok", proc.returncode == 0)
            return data
        except json.JSONDecodeError:
            pass
    return {
        "ok": False,
        "error": "invalid_helper_output",
        "returncode": proc.returncode,
        "stdout": out,
        "stderr": proc.stderr.strip(),
    }


def play_sound(sound_path: str, volume: float, wait: bool = True) -> None:
    if not sound_path or not Path(sound_path).is_file():
        return
    cmd = ["/usr/bin/afplay", "-v", f"{max(0.0, min(1.0, volume)):.2f}", sound_path]
    try:
        if wait:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3.0, check=False)
        else:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass


def resolve_input_device(mic_mode: str) -> tuple[int | None, str]:
    """Resolve sounddevice input index, preferring Built-in Mic to protect Bluetooth A2DP."""
    devices = sd.query_devices()
    input_devices = [
        (idx, dev) for idx, dev in enumerate(devices) if int(dev.get("max_input_channels", 0)) > 0
    ]
    if not input_devices:
        return None, "none"

    if mic_mode == "default":
        default_in = sd.default.device[0] if isinstance(sd.default.device, (list, tuple)) else sd.default.device
        if default_in is not None and int(default_in) >= 0:
            return int(default_in), str(devices[int(default_in)]["name"])
        return None, "default"

    if mic_mode == "built-in":
        ax_info = run_ax_helper("check")
        builtin_name = str(ax_info.get("builtin_mic_name", "")).strip()
        if builtin_name:
            for idx, dev in input_devices:
                if dev["name"] == builtin_name:
                    return idx, str(dev["name"])
            for idx, dev in input_devices:
                if builtin_name.lower() in str(dev["name"]).lower():
                    return idx, str(dev["name"])
        for idx, dev in input_devices:
            name_low = str(dev["name"]).lower()
            if "macbook" in name_low or "built-in" in name_low or "内置" in name_low:
                return idx, str(dev["name"])
        return None, "default_fallback"

    # Explicit device name substring
    target = mic_mode.lower()
    for idx, dev in input_devices:
        if target in str(dev["name"]).lower():
            return idx, str(dev["name"])
    return None, f"unmatched:{mic_mode}"


def build_keyword_spotter(cfg: WakeConfig) -> sherpa_onnx.KeywordSpotter:
    encoder = KWS_MODEL_DIR / "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
    decoder = KWS_MODEL_DIR / "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
    joiner = KWS_MODEL_DIR / "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
    tokens = KWS_MODEL_DIR / "tokens.txt"
    for req in (encoder, decoder, joiner, tokens, cfg.keywords_file):
        if not req.is_file():
            raise FileNotFoundError(f"Missing KWS file: {req}")
    return sherpa_onnx.KeywordSpotter(
        tokens=str(tokens),
        encoder=str(encoder),
        decoder=str(decoder),
        joiner=str(joiner),
        keywords_file=str(cfg.keywords_file),
        num_threads=1,
        sample_rate=SAMPLE_RATE,
        max_active_paths=4,
        keywords_score=cfg.keywords_score,
        keywords_threshold=cfg.keywords_threshold,
        provider="cpu",
    )


def build_vad() -> sherpa_onnx.VoiceActivityDetector:
    if not SILERO_VAD_PATH.is_file():
        raise FileNotFoundError(f"Missing Silero VAD model: {SILERO_VAD_PATH}")
    vad_cfg = sherpa_onnx.VadModelConfig()
    vad_cfg.silero_vad.model = str(SILERO_VAD_PATH)
    vad_cfg.silero_vad.threshold = 0.5
    vad_cfg.silero_vad.min_silence_duration = 0.4
    vad_cfg.silero_vad.min_speech_duration = 0.2
    vad_cfg.sample_rate = SAMPLE_RATE
    vad_cfg.num_threads = 1
    vad_cfg.provider = "cpu"
    return sherpa_onnx.VoiceActivityDetector(vad_cfg, buffer_size_in_seconds=30)


def listen_for_wake_word(
    spotter: sherpa_onnx.KeywordSpotter,
    device_idx: int | None,
    stop_flag: list[bool],
) -> str | None:
    audio_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=32)
    stream = spotter.create_stream()

    def callback(indata: np.ndarray, frames: int, timing: object, status: object) -> None:
        del frames, timing
        if status:
            log_event("audio_status", stage="kws", detail=str(status))
        samples = indata[:, 0].copy()
        try:
            audio_q.put_nowait(samples)
        except queue.Full:
            try:
                audio_q.get_nowait()
            except queue.Empty:
                pass
            audio_q.put_nowait(samples)

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
        blocksize=1_600,
        device=device_idx,
        callback=callback,
    ):
        while not stop_flag[0]:
            try:
                samples = audio_q.get(timeout=0.5)
            except queue.Empty:
                continue
            stream.accept_waveform(SAMPLE_RATE, samples)
            while spotter.is_ready(stream):
                spotter.decode_stream(stream)
                keyword = spotter.get_result(stream)
                if keyword:
                    return keyword
    return None


def monitor_dictation_and_submit(
    cfg: WakeConfig,
    device_idx: int | None,
    dry_run_submit: bool = False,
) -> dict[str, Any]:
    vad = build_vad()
    audio_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=64)

    def callback(indata: np.ndarray, frames: int, timing: object, status: object) -> None:
        del frames, timing, status
        samples = indata[:, 0].copy()
        try:
            audio_q.put_nowait(samples)
        except queue.Full:
            try:
                audio_q.get_nowait()
            except queue.Empty:
                pass
            audio_q.put_nowait(samples)

    start_ts = time.monotonic()
    last_activity_ts = start_ts
    last_poll_ts = 0.0
    speech_started = False
    last_text = ""
    reason = "unknown"

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
        blocksize=512,
        device=device_idx,
        callback=callback,
    ):
        while True:
            now = time.monotonic()
            elapsed = now - start_ts

            while True:
                try:
                    chunk = audio_q.get_nowait()
                except queue.Empty:
                    break
                vad.accept_waveform(chunk)
                while not vad.empty():
                    vad.pop()
                    speech_started = True
                    last_activity_ts = time.monotonic()
                if vad.is_speech_detected():
                    speech_started = True
                    last_activity_ts = time.monotonic()

            if now - last_poll_ts >= 0.25:
                last_poll_ts = now
                comp = run_ax_helper("inspect-composer", timeout=3.0)
                if comp.get("ok"):
                    cur_text = str(comp.get("text", ""))
                    rec_active = bool(comp.get("recording_active", True))
                    if cur_text.strip():
                        speech_started = True
                    if cur_text != last_text:
                        last_text = cur_text
                        last_activity_ts = now
                    if not rec_active and elapsed > 1.5:
                        reason = "dictation_stopped_externally"
                        break

            now = time.monotonic()
            if not speech_started and (now - start_ts) >= cfg.wait_for_speech_timeout_sec:
                reason = "wait_for_speech_timeout"
                break
            if speech_started and (now - last_activity_ts) >= cfg.silence_timeout_sec:
                reason = "silence_pause"
                break
            if (now - start_ts) >= cfg.max_dictation_sec:
                reason = "max_dictation_timeout"
                break

            time.sleep(0.04)

    stop_res = run_ax_helper("stop-dictation", timeout=5.0)
    if reason == "wait_for_speech_timeout":
        play_sound(cfg.sounds.cancel, cfg.sounds.volume, wait=False)
        return {"ok": False, "reason": reason, "stop_res": stop_res, "submitted": False, "text": ""}

    # Wait briefly for final transcript flush to stabilize
    settle_deadline = time.monotonic() + 1.5
    stable_since = time.monotonic()
    final_text = last_text
    while time.monotonic() < settle_deadline:
        comp = run_ax_helper("inspect-composer", timeout=3.0)
        if comp.get("ok"):
            cur_text = str(comp.get("text", ""))
            if cur_text != final_text:
                final_text = cur_text
                stable_since = time.monotonic()
            elif (time.monotonic() - stable_since) >= cfg.transcript_settle_sec:
                break
        time.sleep(0.15)

    clean_text = final_text.strip()
    if not clean_text:
        play_sound(cfg.sounds.cancel, cfg.sounds.volume, wait=False)
        return {"ok": False, "reason": f"{reason}_empty_text", "submitted": False, "text": ""}

    if dry_run_submit:
        return {"ok": True, "reason": reason, "submitted": False, "dry_run": True, "text": clean_text}

    submit_res = run_ax_helper("submit", timeout=5.0)
    if submit_res.get("ok"):
        play_sound(cfg.sounds.submit, cfg.sounds.volume, wait=False)
    else:
        play_sound(cfg.sounds.cancel, cfg.sounds.volume, wait=False)
    return {
        "ok": bool(submit_res.get("ok")),
        "reason": reason,
        "submitted": bool(submit_res.get("ok")),
        "text": clean_text,
        "submit_res": submit_res,
    }


def handle_wake_handoff(cfg: WakeConfig, device_idx: int | None, dry_run_submit: bool = False) -> dict[str, Any]:
    prep = run_ax_helper(
        "prepare-code-orchestrator",
        "--project",
        cfg.project_name,
        "--model",
        cfg.model_keyword,
        timeout=8.0,
    )
    log_event("prepare_result", **prep)
    if not prep.get("ok"):
        play_sound(cfg.sounds.cancel, cfg.sounds.volume, wait=False)
        return {"ok": False, "stage": "prepare", "detail": prep}

    # Play chime synchronously right before opening Antigravity's mic so the chime never pollutes dictation
    play_sound(cfg.sounds.ready, cfg.sounds.volume, wait=True)

    start_res = run_ax_helper("start-dictation", timeout=5.0)
    log_event("start_dictation_result", **start_res)
    if not start_res.get("ok"):
        play_sound(cfg.sounds.cancel, cfg.sounds.volume, wait=False)
        return {"ok": False, "stage": "start_dictation", "detail": start_res}

    outcome = monitor_dictation_and_submit(cfg, device_idx, dry_run_submit=dry_run_submit)
    log_event("dictation_outcome", **outcome)
    return outcome


def cmd_check(cfg: WakeConfig) -> int:
    ax_res = run_ax_helper("check", timeout=8.0)
    dev_idx, dev_name = resolve_input_device(cfg.mic_mode)

    rms = 0.0
    mic_ok = False
    mic_error = ""
    try:
        rec = sd.rec(
            int(SAMPLE_RATE * 0.25),
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            device=dev_idx,
        )
        sd.wait()
        rms = float(np.sqrt(np.mean(np.square(rec))))
        mic_ok = rms > 0.0
    except Exception as exc:  # noqa: BLE001
        mic_error = str(exc)

    kws_ok = False
    vad_ok = False
    model_error = ""
    try:
        _ = build_keyword_spotter(cfg)
        kws_ok = True
        _ = build_vad()
        vad_ok = True
    except Exception as exc:  # noqa: BLE001
        model_error = str(exc)

    overall = bool(ax_res.get("ok")) and mic_ok and kws_ok and vad_ok
    report = {
        "ok": overall,
        "ax": ax_res,
        "resolved_device_index": dev_idx,
        "resolved_device_name": dev_name,
        "mic_rms_250ms": round(rms, 6),
        "mic_ok": mic_ok,
        "mic_error": mic_error,
        "kws_ok": kws_ok,
        "vad_ok": vad_ok,
        "model_error": model_error,
        "keywords_file": str(cfg.keywords_file),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if overall else 1


def cmd_test_wav(cfg: WakeConfig, wav_path: Path) -> int:
    spotter = build_keyword_spotter(cfg)
    with wave.open(str(wav_path), "rb") as wf:
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2 or wf.getframerate() != SAMPLE_RATE:
            raise ValueError(
                f"Expected 16kHz mono 16-bit PCM WAV, got {wf.getframerate()}Hz ch={wf.getnchannels()} width={wf.getsampwidth()}"
            )
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0

    # Append 0.5s trailing silence to flush decoder paths
    samples = np.concatenate([pcm, np.zeros(int(SAMPLE_RATE * 0.5), dtype=np.float32)])
    stream = spotter.create_stream()
    detected: list[str] = []
    chunk_size = 1_600
    for start in range(0, len(samples), chunk_size):
        stream.accept_waveform(SAMPLE_RATE, samples[start : start + chunk_size])
        while spotter.is_ready(stream):
            spotter.decode_stream(stream)
            res = spotter.get_result(stream)
            if res:
                detected.append(res)

    print(json.dumps({"wav": str(wav_path), "detected": detected, "triggered": len(detected) > 0}, ensure_ascii=False))
    return 0


def cmd_daemon(cfg: WakeConfig, dry_run_submit: bool = False) -> int:
    lock = SingleInstanceLock(LOCK_FILE)
    lock.acquire()
    stop_flag = [False]

    def _on_signal(signum: int, _frame: Any) -> None:
        log_event("signal_received", signum=signum)
        stop_flag[0] = True

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        spotter = build_keyword_spotter(cfg)
        log_event(
            "daemon_started",
            project=cfg.project_name,
            model_keyword=cfg.model_keyword,
            mic_mode=cfg.mic_mode,
            score=cfg.keywords_score,
            threshold=cfg.keywords_threshold,
        )
        while not stop_flag[0]:
            dev_idx, dev_name = resolve_input_device(cfg.mic_mode)
            try:
                log_event("listening", device_index=dev_idx, device_name=dev_name)
                keyword = listen_for_wake_word(spotter, dev_idx, stop_flag)
            except Exception as exc:  # noqa: BLE001
                log_event("audio_error", error=str(exc))
                time.sleep(2.0)
                continue

            if stop_flag[0] or not keyword:
                continue

            log_event("wake_detected", keyword=keyword)
            handle_wake_handoff(cfg, dev_idx, dry_run_submit=dry_run_submit)
            time.sleep(cfg.cooldown_sec)
    finally:
        lock.release()
        log_event("daemon_stopped")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=BASE_DIR / "config.json")
    parser.add_argument("--check", action="store_true", help="Run diagnostics on AX, microphone, and ONNX models.")
    parser.add_argument("--test-wav", type=Path, help="Test KWS detection on a 16kHz mono PCM16 WAV file.")
    parser.add_argument("--trigger-now", action="store_true", help="Skip KWS and run the wake handoff immediately.")
    parser.add_argument("--dry-run-submit", action="store_true", help="Run dictation flow without pressing Send.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cfg = load_config(args.config)
    if args.check:
        return cmd_check(cfg)
    if args.test_wav is not None:
        return cmd_test_wav(cfg, args.test_wav)
    if args.trigger_now:
        dev_idx, _ = resolve_input_device(cfg.mic_mode)
        res = handle_wake_handoff(cfg, dev_idx, dry_run_submit=args.dry_run_submit)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    return cmd_daemon(cfg, dry_run_submit=args.dry_run_submit)


if __name__ == "__main__":
    sys.exit(main())
