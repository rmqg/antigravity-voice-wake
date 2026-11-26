#!/usr/bin/env python3
"""Antigravity Voice Wake plugin Sidecar: local KWS + VAD + Auxiliary Pane Web UI."""

from __future__ import annotations

import argparse
import atexit
import base64
import collections
import dataclasses
import fcntl
import json
import os
import queue
import re
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import numpy as np
import sherpa_onnx
import sounddevice as sd

PLUGIN_SIDECAR_DIR = Path(__file__).resolve().parent
LOCAL_DATA_DIR = Path.home() / ".local" / "share" / "antigravity-voice-wake"
DEFAULT_CONFIG_PATH = LOCAL_DATA_DIR / "config.json"
DEFAULT_CONFIG_TEMPLATE = PLUGIN_SIDECAR_DIR / "config.example.json"
KEYWORDS_RAW_PATH = LOCAL_DATA_DIR / "keywords_raw.txt"
KEYWORDS_PATH = LOCAL_DATA_DIR / "keywords.txt"
MODEL_ROOT = LOCAL_DATA_DIR / "models"
KWS_MODEL_DIR = MODEL_ROOT / "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
SILERO_VAD_PATH = MODEL_ROOT / "silero_vad.onnx"
EFFWORD_MODEL_DIR = MODEL_ROOT / "efficientword"
VOICE_SAMPLES_DIR = LOCAL_DATA_DIR / "voice_samples"
VOICE_EMBEDDINGS_PATH = VOICE_SAMPLES_DIR / "embeddings.json"
AX_HELPER_BIN = LOCAL_DATA_DIR / "bin" / "agy-ax-helper"
LOG_FILE = LOCAL_DATA_DIR / "logs" / "voice-wake.log"
LOCK_FILE = Path("/tmp/antigravity-voice-wake.lock")
PROJECTS_DIR = Path.home() / ".gemini" / "config" / "projects"
PRELOAD_JS_PATH = (
    Path.home() / ".gemini" / "antigravity" / "lib" / "node_modules" / "sidecar_sdk" / "preload.js"
)
INDEX_HTML_PATH = PLUGIN_SIDECAR_DIR / "index.html"
SAMPLE_RATE = 16_000
VERSION = "0.1.0"


def detect_system_language() -> str:
    """Detect macOS preferred UI language; return 'zh' for Chinese or 'en' otherwise."""
    raw = ""
    try:
        res = subprocess.run(
            ["defaults", "read", "-g", "AppleLanguages"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        if res.returncode == 0 and res.stdout:
            m = re.search(r'"?([A-Za-z]{2,3}(?:-[A-Za-z0-9]+)*)"?', res.stdout.lstrip("(\n\r "))
            if m:
                raw = m.group(1)
    except (OSError, subprocess.SubprocessError):
        raw = ""
    if not raw:
        raw = os.environ.get("LC_ALL") or os.environ.get("LANG") or ""
    norm = raw.strip().lower().replace("_", "-")
    return "zh" if norm.startswith("zh") else "en"


SYSTEM_LANG = detect_system_language()

DEFAULT_MODEL_OPTIONS = [
    {"value": "Orchestrator", "label": "Orchestrator", "efforts": [""]},
    {"value": "Gemini 4 Neon", "label": "Gemini 4 Neon", "efforts": ["", "Low", "Medium", "High", "Extra High"]},
    {"value": "Gemini 4 Carbon", "label": "Gemini 4 Carbon", "efforts": ["", "Low", "Medium", "High"]},
    {"value": "Gemini 4 Hydrogen", "label": "Gemini 4 Hydrogen", "efforts": ["", "Low", "Medium", "High", "Extra High"]},
    {"value": "Claude Opus 5.5", "label": "Claude Opus 5.5", "efforts": ["", "Low", "Medium", "High"]},
    {"value": "Claude Sonnet 5.5", "label": "Claude Sonnet 5.5", "efforts": ["", "Low", "Medium", "High"]},
    {"value": "", "label": "Follow active model", "efforts": [""]},
]

EFFORT_CANONICAL = {
    "": "",
    "default": "",
    "keep": "",
    "low": "Low",
    "medium": "Medium",
    "med": "Medium",
    "high": "High",
    "extra": "Extra High",
    "xhigh": "Extra High",
    "extra high": "Extra High",
    "extra_high": "Extra High",
    "extra-high": "Extra High",
}


def normalize_thinking_effort(val: Any) -> str:
    key = " ".join(str(val or "").strip().lower().split())
    if key not in EFFORT_CANONICAL:
        raise ValueError(f"Unsupported thinking_effort: {val!r}")
    return EFFORT_CANONICAL[key]

FALLBACK_PRELOAD_JS = """
window.sidecar = {
  conversationId: new URLSearchParams(window.location.search).get('conversationId') || '',
  fetch: (url, opts = {}) => {
    const token = new URLSearchParams(window.location.search).get('token');
    const headers = Object.assign({}, opts.headers || {});
    if (token) headers['x-sidecar-token'] = token;
    return fetch(url, Object.assign({}, opts, { headers }));
  }
};
""".strip()


@dataclasses.dataclass(frozen=True)
class SoundConfig:
    ready: str
    submit: str
    cancel: str
    volume: float


def normalize_wake_aliases(raw: Any, primary_phrase: str = "") -> list[str]:
    primary_norm = " ".join(str(primary_phrase or "").strip().upper().split())
    if raw is None:
        items: list[str] = []
    elif isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = re.split(r"[,，;；\n]+", str(raw))

    aliases: list[str] = []
    seen: set[str] = set()
    for item in items:
        norm = " ".join(item.strip().upper().split())
        if not norm or norm == primary_norm or norm in seen:
            continue
        seen.add(norm)
        aliases.append(norm)
    return aliases


@dataclasses.dataclass(frozen=True)
class WakeConfig:
    ui_language: str
    project_name: str
    model_keyword: str
    thinking_effort: str
    mic_mode: str
    voice_wake_enabled: bool
    wake_mode: str
    wake_phrase: str
    wake_aliases: tuple[str, ...]
    voice_sample_threshold: float
    hotkey_enabled: bool
    hotkey: str
    keywords_file: Path
    keywords_score: float
    keywords_threshold: float
    wait_for_speech_timeout_sec: float
    silence_timeout_sec: float
    max_dictation_sec: float
    transcript_settle_sec: float
    cooldown_sec: float
    sounds: SoundConfig

    def to_dict(self) -> dict[str, Any]:
        return {
            "ui_language": self.ui_language,
            "project_name": self.project_name,
            "model_keyword": self.model_keyword,
            "thinking_effort": self.thinking_effort,
            "mic_mode": self.mic_mode,
            "voice_wake_enabled": self.voice_wake_enabled,
            "wake_mode": self.wake_mode,
            "wake_phrase": self.wake_phrase,
            "wake_aliases": list(self.wake_aliases),
            "voice_sample_threshold": self.voice_sample_threshold,
            "hotkey_enabled": self.hotkey_enabled,
            "hotkey": self.hotkey,
            "keywords_file": str(self.keywords_file),
            "keywords_score": self.keywords_score,
            "keywords_threshold": self.keywords_threshold,
            "wait_for_speech_timeout_sec": self.wait_for_speech_timeout_sec,
            "silence_timeout_sec": self.silence_timeout_sec,
            "max_dictation_sec": self.max_dictation_sec,
            "transcript_settle_sec": self.transcript_settle_sec,
            "cooldown_sec": self.cooldown_sec,
            "sounds": dataclasses.asdict(self.sounds),
        }


def list_projects() -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    if not PROJECTS_DIR.is_dir():
        return items
    for p in sorted(PROJECTS_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if "sidecar" in data:
            continue
        name = str(data.get("name", "")).strip()
        pid = str(data.get("id", p.stem)).strip()
        if name:
            items.append({"id": pid, "name": name})
    items.sort(key=lambda x: x["name"].lower())
    return items


def resolve_project_id(project_name: str, runtime: RuntimeState | None = None) -> str:
    target = project_name.strip()
    if not target:
        return ""
    projects: list[dict[str, str]] = []
    if runtime is not None:
        with runtime.lock:
            projects = list(runtime.cached_projects)
    if not projects:
        projects = list_projects()
    for p in projects:
        if p.get("name") == target and p.get("id"):
            return str(p["id"])
    low = target.lower()
    for p in projects:
        if str(p.get("name", "")).lower() == low and p.get("id"):
            return str(p["id"])
    return ""


def ensure_user_config(config_path: Path) -> None:
    if config_path.is_file():
        return
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if DEFAULT_CONFIG_TEMPLATE.is_file():
        raw = json.loads(DEFAULT_CONFIG_TEMPLATE.read_text(encoding="utf-8"))
    else:
        raw = {}
    if not raw.get("project_name"):
        projects = list_projects()
        if projects:
            raw["project_name"] = projects[0]["name"]
    atomic_write_text(config_path, json.dumps(raw, ensure_ascii=False, indent=2) + "\n")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), prefix=".vw-", delete=False
    ) as fh:
        tmp = Path(fh.name)
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def load_config(path: Path) -> WakeConfig:
    ensure_user_config(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    kw_raw = str(raw.get("keywords_file", "keywords.txt"))
    kw_path = Path(kw_raw).expanduser()
    if not kw_path.is_absolute():
        kw_path = (path.parent / kw_path).resolve()
    sounds_raw = raw.get("sounds", {})
    proj = str(raw.get("project_name", "")).strip()
    if not proj:
        projects = list_projects()
        proj = projects[0]["name"] if projects else "Code"
    wake_mode = str(raw.get("wake_mode", "text")).strip().lower()
    if wake_mode not in ("text", "voice_sample"):
        wake_mode = "text"
    wake_phrase = str(raw.get("wake_phrase", "HEY GRAVITY")).strip() or "HEY GRAVITY"
    wake_aliases = tuple(normalize_wake_aliases(raw.get("wake_aliases", []), wake_phrase))
    sample_thresh = max(0.50, min(0.95, float(raw.get("voice_sample_threshold", 0.72))))
    ui_lang = str(raw.get("ui_language", "auto")).strip().lower()
    if ui_lang not in ("auto", "zh", "en"):
        ui_lang = "auto"
    return WakeConfig(
        ui_language=ui_lang,
        project_name=proj,
        model_keyword=str(raw["model_keyword"]).strip() if "model_keyword" in raw else "Orchestrator",
        thinking_effort=normalize_thinking_effort(raw.get("thinking_effort", "")),
        mic_mode=str(raw.get("mic_mode", "built-in")),
        voice_wake_enabled=bool(raw.get("voice_wake_enabled", True)),
        wake_mode=wake_mode,
        wake_phrase=wake_phrase,
        wake_aliases=wake_aliases,
        voice_sample_threshold=sample_thresh,
        hotkey_enabled=bool(raw.get("hotkey_enabled", True)),
        hotkey=str(raw.get("hotkey", "ctrl+g")).strip().lower() or "ctrl+g",
        keywords_file=kw_path,
        keywords_score=float(raw.get("keywords_score", 1.0)),
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


def generate_keywords_file(
    wake_phrase: str,
    score: float,
    threshold: float,
    raw_path: Path,
    out_path: Path,
    wake_aliases: list[str] | tuple[str, ...] = (),
) -> str:
    phrase = " ".join(wake_phrase.strip().upper().split())
    if not phrase:
        raise ValueError("wake_phrase cannot be empty")
    tag = "_".join(phrase.split())
    raw_lines = [f"{phrase} :{score:.2f} #{threshold:.2f} @{tag}"]
    for alias in normalize_wake_aliases(wake_aliases, phrase):
        alias_tag = "ALIAS_" + "_".join(alias.split())
        raw_lines.append(f"{alias} :{score:.2f} #{threshold:.2f} @{alias_tag}")
    raw_text = "\n".join(raw_lines) + "\n"

    tokens = KWS_MODEL_DIR / "tokens.txt"
    bpe_model = KWS_MODEL_DIR / "bpe.model"
    cli_bin = Path(sys.executable).with_name("sherpa-onnx-cli")
    if not cli_bin.is_file():
        raise FileNotFoundError(f"sherpa-onnx-cli not found at {cli_bin}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(out_path.parent), delete=False) as rf:
        tmp_raw = Path(rf.name)
        rf.write(raw_text)
    tmp_out = out_path.with_name(f".vw-kw-{os.getpid()}.txt")
    try:
        proc = subprocess.run(
            [
                str(cli_bin),
                "text2token",
                "--tokens",
                str(tokens),
                "--tokens-type",
                "bpe",
                "--bpe-model",
                str(bpe_model),
                str(tmp_raw),
                str(tmp_out),
            ],
            capture_output=True,
            text=True,
            timeout=15.0,
            check=False,
        )
        if proc.returncode != 0 or not tmp_out.is_file():
            raise RuntimeError(f"text2token failed: {proc.stderr.strip() or proc.stdout.strip()}")
        generated = tmp_out.read_text(encoding="utf-8").strip()
        if not generated:
            raise ValueError("Generated keywords.txt is empty")
        # Validate with KeywordSpotter before committing
        _ = build_keyword_spotter_for_file(tmp_out, score, threshold)
        atomic_write_text(raw_path, raw_text)
        atomic_write_text(out_path, generated + "\n")
        return generated
    finally:
        tmp_raw.unlink(missing_ok=True)
        tmp_out.unlink(missing_ok=True)


def build_keyword_spotter_for_file(
    keywords_file: Path, score: float, threshold: float
) -> sherpa_onnx.KeywordSpotter:
    encoder = KWS_MODEL_DIR / "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
    decoder = KWS_MODEL_DIR / "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
    joiner = KWS_MODEL_DIR / "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
    tokens = KWS_MODEL_DIR / "tokens.txt"
    for req in (encoder, decoder, joiner, tokens, keywords_file):
        if not req.is_file():
            raise FileNotFoundError(f"Missing KWS file: {req}")
    return sherpa_onnx.KeywordSpotter(
        tokens=str(tokens),
        encoder=str(encoder),
        decoder=str(decoder),
        joiner=str(joiner),
        keywords_file=str(keywords_file),
        num_threads=1,
        sample_rate=SAMPLE_RATE,
        max_active_paths=4,
        keywords_score=score,
        keywords_threshold=threshold,
        provider="cpu",
    )


def build_keyword_spotter(cfg: WakeConfig) -> sherpa_onnx.KeywordSpotter:
    if not cfg.keywords_file.is_file():
        generate_keywords_file(
            cfg.wake_phrase,
            cfg.keywords_score,
            cfg.keywords_threshold,
            KEYWORDS_RAW_PATH,
            cfg.keywords_file,
            wake_aliases=cfg.wake_aliases,
        )
    return build_keyword_spotter_for_file(cfg.keywords_file, cfg.keywords_score, cfg.keywords_threshold)


def build_wake_vad() -> sherpa_onnx.VoiceActivityDetector:
    if not SILERO_VAD_PATH.is_file():
        raise FileNotFoundError(f"Missing Silero VAD model: {SILERO_VAD_PATH}")
    vad_cfg = sherpa_onnx.VadModelConfig()
    vad_cfg.silero_vad.model = str(SILERO_VAD_PATH)
    vad_cfg.silero_vad.threshold = 0.35
    vad_cfg.silero_vad.min_silence_duration = 0.25
    vad_cfg.silero_vad.min_speech_duration = 0.12
    vad_cfg.silero_vad.max_speech_duration = 8.0
    vad_cfg.sample_rate = SAMPLE_RATE
    vad_cfg.num_threads = 1
    vad_cfg.provider = "cpu"
    return sherpa_onnx.VoiceActivityDetector(vad_cfg, buffer_size_in_seconds=30)


def detect_keyword_on_segment(spotter: sherpa_onnx.KeywordSpotter, samples: np.ndarray) -> str:
    """Decode one isolated VAD speech segment with clean lead/tail silence padding."""
    lead = np.zeros(int(SAMPLE_RATE * 0.3), dtype=np.float32)
    tail = np.zeros(int(SAMPLE_RATE * 0.5), dtype=np.float32)
    padded = np.concatenate([lead, samples.astype(np.float32, copy=False), tail])
    stream = spotter.create_stream()
    chunk_size = 1_600
    for start in range(0, len(padded), chunk_size):
        stream.accept_waveform(SAMPLE_RATE, padded[start : start + chunk_size])
        while spotter.is_ready(stream):
            spotter.decode_stream(stream)
            kw = spotter.get_result(stream)
            if kw:
                return kw
    return ""


_VAD_LOCK = threading.Lock()
_WAKE_VAD: sherpa_onnx.VoiceActivityDetector | None = None
_DICTATION_VAD: sherpa_onnx.VoiceActivityDetector | None = None


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


def get_wake_vad() -> sherpa_onnx.VoiceActivityDetector:
    global _WAKE_VAD
    with _VAD_LOCK:
        if _WAKE_VAD is None:
            _WAKE_VAD = build_wake_vad()
        else:
            _WAKE_VAD.reset()
        return _WAKE_VAD


def get_dictation_vad() -> sherpa_onnx.VoiceActivityDetector:
    global _DICTATION_VAD
    with _VAD_LOCK:
        if _DICTATION_VAD is None:
            _DICTATION_VAD = build_vad()
        else:
            _DICTATION_VAD.reset()
        return _DICTATION_VAD


class VoiceSampleMatcher:
    """Few-shot ONNX wake-word embedder reusing EfficientWord-Net Resnet50_Arc_loss (qint8)."""

    WINDOW_FRAMES = 24_000  # 1.5s @ 16kHz

    def __init__(self) -> None:
        import eff_word_net
        import onnxruntime as ort
        from eff_word_net.audio_utils import logfbank

        self._logfbank = logfbank
        pkg_dir = Path(eff_word_net.__file__).resolve().parent
        qint8_candidates = [
            pkg_dir / "models" / "resnet_50_arc" / "slim_93%_accuracy_72.7390%_qint8.onnx",
            EFFWORD_MODEL_DIR / "slim_93%_accuracy_72.7390%_qint8.onnx",
        ]
        model_path = next((p for p in qint8_candidates if p.is_file()), None)
        if model_path is None:
            raise FileNotFoundError(f"Missing EfficientWord-Net qint8 ONNX model in {qint8_candidates}")
        sess_opts = ort.SessionOptions()
        sess_opts.intra_op_num_threads = 1
        sess_opts.inter_op_num_threads = 1
        sess_opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        sess_opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
        self._sess = ort.InferenceSession(
            str(model_path),
            sess_options=sess_opts,
            providers=["CPUExecutionProvider"],
        )
        self._input_name = self._sess.get_inputs()[0].name
        self._output_name = self._sess.get_outputs()[0].name
        _ = self.embed(np.zeros(self.WINDOW_FRAMES, dtype=np.float32))

    @classmethod
    def prepare_window(cls, samples: np.ndarray) -> np.ndarray:
        x = samples.astype(np.float32, copy=True)
        peak = float(np.max(np.abs(x))) if x.size > 0 else 0.0
        if peak > 1e-4:
            gain = min(10.0, 0.7 / peak)
            x = np.clip(x * gain, -1.0, 1.0)
        if len(x) > cls.WINDOW_FRAMES:
            start = (len(x) - cls.WINDOW_FRAMES) // 2
            return x[start : start + cls.WINDOW_FRAMES]
        if len(x) < cls.WINDOW_FRAMES:
            left = (cls.WINDOW_FRAMES - len(x)) // 2
            right = cls.WINDOW_FRAMES - len(x) - left
            return np.pad(x, (left, right))
        return x

    def embed(self, samples: np.ndarray) -> np.ndarray:
        win = self.prepare_window(samples)
        feat = self._logfbank(
            win, samplerate=SAMPLE_RATE, winlen=0.025, winstep=0.01, nfilt=64, nfft=512, preemph=0.0
        )
        inp = np.expand_dims(feat.astype(np.float32, copy=False), axis=(0, 1))
        out = self._sess.run([self._output_name], {self._input_name: inp})[0]
        vec = np.asarray(out, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(vec))
        return vec / (norm + 1e-9)

    def score(self, samples: np.ndarray, matrix: np.ndarray, ids: list[str]) -> tuple[float, str]:
        if matrix.size == 0 or not ids:
            return 0.0, ""
        vec = self.embed(samples)
        sims = np.matmul(matrix, vec)
        best_i = int(np.argmax(sims))
        conf = float((sims[best_i] + 1.0) / 2.0)
        return conf, ids[best_i]


_SAMPLE_MATCHER_LOCK = threading.Lock()
_SAMPLE_MATCHER: VoiceSampleMatcher | None = None


def get_sample_matcher() -> VoiceSampleMatcher:
    global _SAMPLE_MATCHER
    with _SAMPLE_MATCHER_LOCK:
        if _SAMPLE_MATCHER is None:
            _SAMPLE_MATCHER = VoiceSampleMatcher()
        return _SAMPLE_MATCHER


def load_voice_samples() -> tuple[list[dict[str, Any]], np.ndarray, list[str]]:
    if not VOICE_EMBEDDINGS_PATH.is_file():
        return [], np.zeros((0, 0), dtype=np.float32), []
    try:
        raw = json.loads(VOICE_EMBEDDINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], np.zeros((0, 0), dtype=np.float32), []
    entries = raw.get("samples", []) if isinstance(raw, dict) else []
    meta_list: list[dict[str, Any]] = []
    vecs: list[np.ndarray] = []
    ids: list[str] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("id", "")).strip()
        fname = str(item.get("file", "")).strip()
        emb = item.get("embedding")
        if not sid or not fname or not isinstance(emb, list) or not emb:
            continue
        wav_file = VOICE_SAMPLES_DIR / fname
        if not wav_file.is_file():
            continue
        vec = np.asarray(emb, dtype=np.float32).reshape(-1)
        n = float(np.linalg.norm(vec))
        if n <= 1e-9:
            continue
        vecs.append(vec / n)
        ids.append(sid)
        meta_list.append(
            {
                "id": sid,
                "file": fname,
                "label": str(item.get("label", "")),
                "duration_sec": float(item.get("duration_sec", 0.0)),
                "created_at": str(item.get("created_at", "")),
            }
        )
    if not vecs:
        return [], np.zeros((0, 0), dtype=np.float32), []
    return meta_list, np.vstack(vecs).astype(np.float32, copy=False), ids


def _read_raw_sample_entries() -> list[dict[str, Any]]:
    if not VOICE_EMBEDDINGS_PATH.is_file():
        return []
    try:
        raw = json.loads(VOICE_EMBEDDINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = raw.get("samples", []) if isinstance(raw, dict) else []
    kept: list[dict[str, Any]] = []
    for item in entries:
        if isinstance(item, dict) and item.get("id") and item.get("file"):
            if (VOICE_SAMPLES_DIR / str(item["file"])).is_file():
                kept.append(item)
    return kept


def save_voice_sample_record(samples: np.ndarray, label: str, matcher: VoiceSampleMatcher) -> dict[str, Any]:
    VOICE_SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    sid = f"{int(time.time())}_{uuid.uuid4().hex[:6]}"
    fname = f"sample_{sid}.wav"
    wav_path = VOICE_SAMPLES_DIR / fname
    pcm16 = np.clip(samples.astype(np.float32, copy=False) * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm16.tobytes())

    vec = matcher.embed(samples)
    dur_sec = round(len(samples) / float(SAMPLE_RATE), 2)
    created_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    record = {
        "id": sid,
        "file": fname,
        "label": label,
        "duration_sec": dur_sec,
        "created_at": created_at,
        "embedding": [round(float(v), 6) for v in vec.tolist()],
    }
    entries = _read_raw_sample_entries()
    entries.append(record)
    atomic_write_text(VOICE_EMBEDDINGS_PATH, json.dumps({"samples": entries}, ensure_ascii=False) + "\n")
    return {
        "id": sid,
        "file": fname,
        "label": label,
        "duration_sec": dur_sec,
        "created_at": created_at,
    }


def import_audio_file_as_samples(
    raw_bytes: bytes, filename: str, label: str, matcher: VoiceSampleMatcher
) -> list[dict[str, Any]]:
    ext = Path(filename or "audio.wav").suffix.lower() or ".wav"
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / f"in{ext}"
        wav = Path(td) / "out16k.wav"
        src.write_bytes(raw_bytes)
        proc = subprocess.run(
            ["/usr/bin/afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(src), str(wav)],
            capture_output=True,
            text=True,
            timeout=15.0,
            check=False,
        )
        if proc.returncode != 0 or not wav.is_file():
            raise RuntimeError(f"Audio conversion failed: {proc.stderr.strip() or proc.stdout.strip()}")
        with wave.open(str(wav), "rb") as wf:
            pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0

    vad = build_wake_vad()
    full = np.concatenate([pcm, np.zeros(int(SAMPLE_RATE * 0.8), dtype=np.float32)])
    saved: list[dict[str, Any]] = []
    chunk_size = 512
    for pos in range(0, len(full), chunk_size):
        chunk = full[pos : pos + chunk_size]
        if len(chunk) < chunk_size:
            chunk = np.pad(chunk, (0, chunk_size - len(chunk)))
        vad.accept_waveform(chunk)
        while not vad.empty():
            seg = vad.front
            seg_samples = np.array(seg.samples, dtype=np.float32)
            vad.pop()
            dur = len(seg_samples) / float(SAMPLE_RATE)
            rms = float(np.sqrt(np.mean(np.square(seg_samples)))) if seg_samples.size > 0 else 0.0
            if 0.12 <= dur <= 1.50 and rms >= 0.005:
                saved.append(save_voice_sample_record(seg_samples, label, matcher))
    return saved


def delete_voice_samples(sample_id: str | None = None, delete_all: bool = False) -> int:
    entries = _read_raw_sample_entries()
    removed = 0
    kept: list[dict[str, Any]] = []
    for item in entries:
        if delete_all or (sample_id and str(item.get("id")) == sample_id):
            wav_file = VOICE_SAMPLES_DIR / str(item.get("file", ""))
            try:
                wav_file.unlink(missing_ok=True)
            except OSError:
                pass
            removed += 1
        else:
            kept.append(item)
    VOICE_SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_text(VOICE_EMBEDDINGS_PATH, json.dumps({"samples": kept}, ensure_ascii=False) + "\n")
    return removed


def reload_voice_samples(runtime: RuntimeState) -> int:
    meta, matrix, ids = load_voice_samples()
    with runtime.lock:
        runtime.sample_meta = meta
        runtime.sample_matrix = matrix
        runtime.sample_ids = ids
    return len(ids)


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


_READONLY_HELPER_CMDS = frozenset(
    {"builtin-mic", "quick-check", "check", "inspect-composer", "dump-composer", "validate-hotkey"}
)


class AxHelperClient:
    """Persistent stdio client for `agy-ax-helper serve` to avoid process-spawn overhead."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._buf = bytearray()

    def _close_locked(self) -> None:
        proc = self._proc
        self._proc = None
        self._buf = bytearray()
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except OSError:
                    pass
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _read_line_locked(self, timeout: float) -> tuple[str | None, bool]:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return None, False
        fd = proc.stdout.fileno()
        deadline = time.monotonic() + max(0.05, timeout)
        while True:
            nl_idx = self._buf.find(b"\n")
            if nl_idx >= 0:
                raw_line = bytes(self._buf[:nl_idx])
                del self._buf[: nl_idx + 1]
                return raw_line.decode("utf-8", errors="replace").strip(), False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, True
            try:
                readable, _, _ = select.select([fd], [], [], remaining)
            except (OSError, ValueError):
                return None, False
            if not readable:
                return None, True
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                return None, False
            if not chunk:
                return None, False
            self._buf.extend(chunk)

    def _ensure_started_locked(self, startup_timeout: float) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        self._close_locked()
        try:
            self._proc = subprocess.Popen(
                [str(AX_HELPER_BIN), "serve"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError:
            self._proc = None
            return False
        ready_line, timed_out = self._read_line_locked(startup_timeout)
        if timed_out or not ready_line:
            self._close_locked()
            return False
        try:
            ready = json.loads(ready_line)
        except json.JSONDecodeError:
            self._close_locked()
            return False
        if ready.get("event") != "helper_ready":
            self._close_locked()
            return False
        return True

    def call(self, args: list[str], timeout: float = 10.0) -> dict[str, Any]:
        if not args:
            return {"ok": False, "error": "missing_helper_args"}
        cmd = args[0]
        attempts = 2 if cmd in _READONLY_HELPER_CMDS else 1
        with self._lock:
            for attempt in range(attempts):
                if not self._ensure_started_locked(max(timeout, 8.0)):
                    return {"ok": False, "error": "helper_start_failed", "args": list(args)}
                proc = self._proc
                if proc is None or proc.stdin is None:
                    self._close_locked()
                    continue
                payload = (json.dumps({"args": list(args)}, ensure_ascii=False) + "\n").encode("utf-8")
                try:
                    os.write(proc.stdin.fileno(), payload)
                except OSError:
                    self._close_locked()
                    if attempt + 1 < attempts:
                        continue
                    return {"ok": False, "error": "helper_write_failed", "args": list(args)}

                line, timed_out = self._read_line_locked(timeout)
                if timed_out:
                    self._close_locked()
                    return {"ok": False, "error": "helper_timeout", "args": list(args)}
                if not line:
                    self._close_locked()
                    if attempt + 1 < attempts:
                        continue
                    return {"ok": False, "error": "helper_eof", "args": list(args)}
                try:
                    data = json.loads(line)
                    if isinstance(data, dict):
                        return data
                except json.JSONDecodeError:
                    pass
                return {"ok": False, "error": "invalid_helper_output", "stdout": line, "args": list(args)}
        return {"ok": False, "error": "helper_failed", "args": list(args)}


_AX_HELPER_CLIENT = AxHelperClient()
atexit.register(_AX_HELPER_CLIENT.close)


def run_ax_helper(*args: str, timeout: float = 10.0) -> dict[str, Any]:
    if not AX_HELPER_BIN.is_file():
        return {"ok": False, "error": f"helper_missing:{AX_HELPER_BIN}"}
    return _AX_HELPER_CLIENT.call(list(args), timeout=timeout)


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


def get_default_input_index() -> int | None:
    try:
        val = sd.default.device[0]
        return int(val) if val is not None and int(val) >= 0 else None
    except Exception:  # noqa: BLE001
        try:
            val = int(sd.default.device)
            return val if val >= 0 else None
        except Exception:  # noqa: BLE001
            return None


def list_input_mics(builtin_name: str | None = None) -> list[dict[str, Any]]:
    devices = sd.query_devices()
    default_in = get_default_input_index()
    if builtin_name is None:
        ax_info = run_ax_helper("builtin-mic", timeout=3.0)
        resolved_builtin = str(ax_info.get("builtin_mic_name", "")).strip()
    else:
        resolved_builtin = str(builtin_name).strip()
    mics: list[dict[str, Any]] = []
    for idx, dev in enumerate(devices):
        if int(dev.get("max_input_channels", 0)) <= 0:
            continue
        name = str(dev.get("name", ""))
        is_builtin = bool(resolved_builtin and name == resolved_builtin) or any(
            k in name.lower() for k in ("macbook", "built-in", "内置")
        )
        mics.append(
            {
                "index": idx,
                "name": name,
                "is_builtin": is_builtin,
                "is_default": (default_in is not None and default_in == idx),
            }
        )
    return mics


def resolve_input_device(mic_mode: str) -> tuple[int | None, str]:
    """Resolve sounddevice input index, preferring Built-in Mic to protect Bluetooth A2DP."""
    devices = sd.query_devices()
    input_devices = [
        (idx, dev) for idx, dev in enumerate(devices) if int(dev.get("max_input_channels", 0)) > 0
    ]
    if not input_devices:
        return None, "none"

    if mic_mode == "default":
        default_in = get_default_input_index()
        if default_in is not None:
            return default_in, str(devices[default_in]["name"])
        return None, "default"

    if mic_mode == "built-in":
        ax_info = run_ax_helper("builtin-mic", timeout=3.0)
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

    target = mic_mode.lower()
    for idx, dev in input_devices:
        if target in str(dev["name"]).lower():
            return idx, str(dev["name"])
    return None, f"unmatched:{mic_mode}"


class RuntimeState:
    def __init__(self, config_path: Path, cfg: WakeConfig) -> None:
        self.lock = threading.Lock()
        self._probe_lock = threading.Lock()
        self.config_path = config_path
        self.cfg = cfg
        self.state = "initializing"
        self.paused = False
        self.active_device = ""
        self.active_device_index: int | None = None
        self.last_error = ""
        self.last_event: dict[str, Any] = {}
        self.hotkey_registered = False
        self.hotkey_error = ""
        self.hotkey_watcher: HotkeyWatcher | None = None
        self.spotter: sherpa_onnx.KeywordSpotter | None = None
        self.stop_event = threading.Event()
        self.reload_event = threading.Event()
        self.manual_trigger_event = threading.Event()
        self.manual_dry_run = True
        self.manual_trigger_source = "panel"
        self.cached_ax_status: dict[str, Any] = {}
        self.cached_ax_ts: float = 0.0
        self.cached_mics: list[dict[str, Any]] = []
        self.cached_mics_ts: float = 0.0
        self.cached_projects: list[dict[str, str]] = []
        self.cached_projects_ts: float = 0.0
        self.wake_test_mode: bool = False
        self.wake_test_hits: int = 0
        self.last_wake_test_ts: str = ""
        self.last_wake_test_keyword: str = ""
        meta, matrix, ids = load_voice_samples()
        self.sample_meta: list[dict[str, Any]] = meta
        self.sample_matrix: np.ndarray = matrix
        self.sample_ids: list[str] = ids
        self.last_sample_score: float = 0.0
        self.last_sample_hit: bool = False
        self.last_sample_ts: str = ""
        self.recording_sample_event = threading.Event()
        self.mic_released_event = threading.Event()
        self.mic_released_event.set()
        self.recording_done_event = threading.Event()
        self.recording_done_event.set()

    def set_state(self, state: str, error: str = "") -> None:
        with self.lock:
            self.state = state
            self.last_error = error

    def log(self, event: str, **details: Any) -> None:
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "event": event,
            **details,
        }
        with self.lock:
            self.last_event = record
        line = json.dumps(record, ensure_ascii=False)
        print(line, file=sys.stderr, flush=True)
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with LOG_FILE.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


class HotkeyWatcher:
    """Manages the native Carbon global hotkey subprocess alongside voice wake."""

    def __init__(self, runtime: RuntimeState) -> None:
        self.runtime = runtime
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._enabled = False
        self._hotkey = ""

    def reconfigure(self, enabled: bool, hotkey: str) -> None:
        with self._lock:
            self._stop_locked()
            self._enabled = enabled
            self._hotkey = hotkey
            if not enabled or not hotkey or not AX_HELPER_BIN.is_file():
                with self.runtime.lock:
                    self.runtime.hotkey_registered = False
                    self.runtime.hotkey_error = "" if not enabled else "helper_missing"
                return
            self._proc = subprocess.Popen(
                [str(AX_HELPER_BIN), "watch-hotkey", "--hotkey", hotkey],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            proc = self._proc
            self._thread = threading.Thread(
                target=self._read_loop, args=(proc, hotkey), daemon=True, name="voice-wake-hotkey"
            )
            self._thread.start()

    def _stop_locked(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=1.5)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except OSError:
                pass

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _read_loop(self, proc: subprocess.Popen[str], hotkey: str) -> None:
        if proc.stdout is not None:
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ev = msg.get("event", "")
                if ev == "hotkey_registered":
                    with self.runtime.lock:
                        self.runtime.hotkey_registered = True
                        self.runtime.hotkey_error = ""
                    self.runtime.log("hotkey_registered", hotkey=msg.get("hotkey", hotkey))
                elif ev == "hotkey_pressed":
                    with self.runtime.lock:
                        busy = self.runtime.state in ("preparing", "dictating", "submitting")
                        if not busy:
                            self.runtime.manual_dry_run = False
                            self.runtime.manual_trigger_source = "hotkey"
                            self.runtime.manual_trigger_event.set()
                    if not busy:
                        self.runtime.log("hotkey_pressed", hotkey=msg.get("hotkey", hotkey))
                elif msg.get("ok") is False:
                    err = str(msg.get("error", "hotkey_failed"))
                    with self.runtime.lock:
                        self.runtime.hotkey_registered = False
                        self.runtime.hotkey_error = err
                    self.runtime.log("hotkey_error", hotkey=hotkey, error=err, detail=msg)
        rc = proc.wait()
        if rc != 0 and not self.runtime.stop_event.is_set():
            stderr_txt = proc.stderr.read().strip() if proc.stderr else ""
            with self.runtime.lock:
                # Only update if still current proc or failed before registration
                if self._proc is proc or not self.runtime.hotkey_registered:
                    self.runtime.hotkey_registered = False
                    if not self.runtime.hotkey_error:
                        self.runtime.hotkey_error = stderr_txt or f"exit_{rc}"


def read_recent_logs(limit: int = 40) -> list[dict[str, Any]]:
    if not LOG_FILE.is_file():
        return []
    limit = max(1, min(200, limit))
    try:
        lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries: list[dict[str, Any]] = []
    for raw in lines[-limit:]:
        try:
            entries.append(json.loads(raw))
        except json.JSONDecodeError:
            entries.append({"ts": "", "event": "raw", "message": raw})
    entries.reverse()
    return entries


def monitor_dictation_and_submit(
    cfg: WakeConfig,
    device_idx: int | None,
    runtime: RuntimeState | None = None,
    dry_run_submit: bool = False,
) -> dict[str, Any]:
    vad = get_dictation_vad()
    audio_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=256)

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
    last_vad_ts = start_ts
    speech_started_vad = False
    reason = "unknown"

    comp_lock = threading.Lock()
    poll_stop = threading.Event()
    comp_state: dict[str, Any] = {
        "text": "",
        "last_change_ts": start_ts,
        "speech_started": False,
        "stopped_externally": False,
    }

    def composer_poller() -> None:
        inactive_streak = 0
        while not poll_stop.wait(0.45):
            if runtime is not None and runtime.stop_event.is_set():
                return
            comp = run_ax_helper("inspect-composer", timeout=5.0)
            if not comp.get("ok"):
                continue
            now_p = time.monotonic()
            cur_text = str(comp.get("text", ""))
            rec_active = bool(comp.get("recording_active", True))
            msg_found = bool(comp.get("message_input_found", False))
            with comp_lock:
                if cur_text.strip():
                    comp_state["speech_started"] = True
                if cur_text != comp_state["text"]:
                    comp_state["text"] = cur_text
                    comp_state["last_change_ts"] = now_p
            if rec_active:
                inactive_streak = 0
            elif msg_found and (now_p - start_ts) > 2.0:
                inactive_streak += 1
                if inactive_streak >= 2:
                    with comp_lock:
                        comp_state["stopped_externally"] = True
                    return

    poller_thread = threading.Thread(
        target=composer_poller, daemon=True, name="voice-wake-composer-poller"
    )
    poller_thread.start()

    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=1024,
            latency="high",
            device=device_idx,
            callback=callback,
        ):
            while True:
                if runtime is not None and runtime.stop_event.is_set():
                    reason = "daemon_stopping"
                    break

                # Drain audio queue first; never block VAD loop on AX polling.
                while True:
                    try:
                        chunk = audio_q.get_nowait()
                    except queue.Empty:
                        break
                    for offset in range(0, len(chunk), 512):
                        sub = chunk[offset : offset + 512]
                        if len(sub) < 512:
                            sub = np.pad(sub, (0, 512 - len(sub)))
                        vad.accept_waveform(sub)
                        while not vad.empty():
                            vad.pop()
                            speech_started_vad = True
                            last_vad_ts = time.monotonic()
                        if vad.is_speech_detected():
                            speech_started_vad = True
                            last_vad_ts = time.monotonic()

                with comp_lock:
                    comp_speech = bool(comp_state["speech_started"])
                    comp_change_ts = float(comp_state["last_change_ts"])
                    stopped_ext = bool(comp_state["stopped_externally"])

                now = time.monotonic()
                if stopped_ext:
                    reason = "dictation_stopped_externally"
                    break

                speech_started = speech_started_vad or comp_speech
                effective_last_activity = max(last_vad_ts, comp_change_ts)

                if not speech_started and (now - start_ts) >= cfg.wait_for_speech_timeout_sec:
                    reason = "wait_for_speech_timeout"
                    break
                if speech_started and (now - effective_last_activity) >= cfg.silence_timeout_sec:
                    reason = "silence_pause"
                    break
                if (now - start_ts) >= cfg.max_dictation_sec:
                    reason = "max_dictation_timeout"
                    break

                time.sleep(0.03)
    finally:
        poll_stop.set()
        poller_thread.join(timeout=1.0)

    with comp_lock:
        last_text = str(comp_state["text"])

    stop_res = run_ax_helper("stop-dictation", timeout=8.0)
    if reason in ("wait_for_speech_timeout", "daemon_stopping"):
        play_sound(cfg.sounds.cancel, cfg.sounds.volume, wait=False)
        return {"ok": False, "reason": reason, "stop_res": stop_res, "submitted": False, "text": ""}

    if runtime is not None:
        runtime.set_state("submitting")

    settle_deadline = time.monotonic() + 2.5
    stable_since = time.monotonic()
    stop_text = str(stop_res.get("text", "") or "")
    final_text = stop_text if len(stop_text) >= len(last_text) else last_text
    while time.monotonic() < settle_deadline:
        comp = run_ax_helper("inspect-composer", timeout=5.0)
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

    submit_res = run_ax_helper("submit", timeout=8.0)
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


def handle_wake_handoff(
    cfg: WakeConfig,
    device_idx: int | None,
    runtime: RuntimeState | None = None,
    dry_run_submit: bool = False,
) -> dict[str, Any]:
    def emit(ev: str, **kw: Any) -> None:
        if runtime is not None:
            runtime.log(ev, **kw)
        else:
            print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": ev, **kw}, ensure_ascii=False), file=sys.stderr)

    if runtime is not None:
        runtime.set_state("preparing")

    proj_id = resolve_project_id(cfg.project_name, runtime=runtime)
    prep_args = [
        "prepare-code-orchestrator",
        "--project",
        cfg.project_name,
    ]
    if proj_id:
        prep_args.extend(["--project-id", proj_id])
    prep_args.extend(
        [
            "--model",
            cfg.model_keyword,
            "--effort",
            cfg.thinking_effort,
        ]
    )
    prep = run_ax_helper(*prep_args, timeout=15.0)
    emit("prepare_result", **prep)
    if not prep.get("ok"):
        play_sound(cfg.sounds.cancel, cfg.sounds.volume, wait=False)
        return {"ok": False, "stage": "prepare", "detail": prep}

    play_sound(cfg.sounds.ready, cfg.sounds.volume, wait=False)

    start_res = run_ax_helper("start-dictation", timeout=10.0)
    emit("start_dictation_result", **start_res)
    if not start_res.get("ok"):
        play_sound(cfg.sounds.cancel, cfg.sounds.volume, wait=False)
        return {"ok": False, "stage": "start_dictation", "detail": start_res}

    if runtime is not None:
        runtime.set_state("dictating")
    outcome = monitor_dictation_and_submit(cfg, device_idx, runtime=runtime, dry_run_submit=dry_run_submit)
    emit("dictation_outcome", **outcome)
    return outcome


def listen_for_wake_word(
    spotter: sherpa_onnx.KeywordSpotter | None,
    device_idx: int | None,
    runtime: RuntimeState,
    cfg: WakeConfig,
) -> str | None:
    audio_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=256)
    status_events: collections.deque[str] = collections.deque(maxlen=4)
    last_audio_status_log = 0.0
    vad = get_wake_vad()
    use_sample_mode = cfg.wake_mode == "voice_sample"
    matcher = get_sample_matcher() if use_sample_mode else None
    is_single_word = len(cfg.wake_phrase.split()) <= 1
    max_utt_dur = 1.50 if use_sample_mode else (1.25 if is_single_word else 2.50)
    live_stream = None if (use_sample_mode or is_single_word or spotter is None) else spotter.create_stream()
    was_speaking = False
    silence_chunks = 0

    def callback(indata: np.ndarray, frames: int, timing: object, status: object) -> None:
        del frames, timing
        if status:
            status_events.append(str(status))
        samples = indata[:, 0].copy()
        try:
            audio_q.put_nowait(samples)
        except queue.Full:
            try:
                audio_q.get_nowait()
            except queue.Empty:
                pass
            audio_q.put_nowait(samples)

    def handle_hit(matched_kw: str, dur_sec: float, extra: dict[str, Any] | None = None) -> str | None:
        with runtime.lock:
            is_wake_test = runtime.wake_test_mode
            if is_wake_test:
                runtime.wake_test_hits += 1
                hits = runtime.wake_test_hits
                runtime.last_wake_test_ts = time.strftime("%H:%M:%S")
                runtime.last_wake_test_keyword = matched_kw
            else:
                hits = 0
        if is_wake_test:
            play_sound(cfg.sounds.ready, cfg.sounds.volume, wait=False)
            log_kw: dict[str, Any] = {
                "keyword": cfg.wake_phrase,
                "matched": matched_kw,
                "duration_sec": round(dur_sec, 2),
                "hits": hits,
                "mode": cfg.wake_mode,
            }
            if cfg.wake_mode == "voice_sample":
                log_kw["threshold"] = cfg.voice_sample_threshold
            else:
                log_kw["threshold"] = cfg.keywords_threshold
                log_kw["score"] = cfg.keywords_score
            if extra:
                log_kw.update(extra)
            runtime.log("wake_test_detected", **log_kw)
            return None
        return matched_kw

    runtime.mic_released_event.clear()
    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=1024,
            latency="high",
            device=device_idx,
            callback=callback,
        ):
            while not runtime.stop_event.is_set():
                if (
                    runtime.paused
                    or runtime.reload_event.is_set()
                    or runtime.manual_trigger_event.is_set()
                    or runtime.recording_sample_event.is_set()
                ):
                    return None
                if status_events:
                    detail = status_events.popleft()
                    status_events.clear()
                    now_st = time.monotonic()
                    if now_st - last_audio_status_log >= 5.0:
                        last_audio_status_log = now_st
                        runtime.log("audio_status", stage="kws", detail=detail)
                try:
                    samples = audio_q.get(timeout=0.08)
                except queue.Empty:
                    continue

                speaking = False
                for offset in range(0, len(samples), 512):
                    sub = samples[offset : offset + 512]
                    if len(sub) < 512:
                        sub = np.pad(sub, (0, 512 - len(sub)))
                    vad.accept_waveform(sub)
                    if vad.is_speech_detected():
                        speaking = True

                # Low-latency streaming fast path for multi-word text KWS phrases
                if live_stream is not None and spotter is not None:
                    if speaking or was_speaking:
                        live_stream.accept_waveform(SAMPLE_RATE, samples)
                        while spotter.is_ready(live_stream):
                            spotter.decode_stream(live_stream)
                            kw_live = spotter.get_result(live_stream)
                            if kw_live and not kw_live.startswith("ALIAS_"):
                                live_stream = spotter.create_stream()
                                was_speaking = False
                                silence_chunks = 0
                                ret = handle_hit(kw_live, 0.0)
                                if ret:
                                    return ret
                                break
                    if speaking:
                        was_speaking = True
                        silence_chunks = 0
                    elif was_speaking:
                        silence_chunks += 1
                        if silence_chunks >= 5:
                            live_stream = spotter.create_stream()
                            was_speaking = False
                            silence_chunks = 0

                while not vad.empty():
                    seg = vad.front
                    seg_samples = np.array(seg.samples, dtype=np.float32)
                    vad.pop()
                    dur = len(seg_samples) / float(SAMPLE_RATE)
                    if dur < 0.12 or dur > max_utt_dur:
                        continue

                    if use_sample_mode and matcher is not None:
                        rms = float(np.sqrt(np.mean(np.square(seg_samples)))) if seg_samples.size > 0 else 0.0
                        if rms < 0.005:
                            continue
                        with runtime.lock:
                            matrix = runtime.sample_matrix
                            ids = runtime.sample_ids
                        if matrix.size == 0 or not ids:
                            continue
                        conf, matched_id = matcher.score(seg_samples, matrix, ids)
                        hit = conf >= cfg.voice_sample_threshold
                        with runtime.lock:
                            runtime.last_sample_score = round(conf, 3)
                            runtime.last_sample_hit = hit
                            runtime.last_sample_ts = time.strftime("%H:%M:%S")
                        if hit:
                            ret = handle_hit(
                                f"SAMPLE:{matched_id}",
                                dur,
                                extra={"score": round(conf, 3)},
                            )
                            if ret:
                                return ret
                        elif runtime.wake_test_mode:
                            runtime.log(
                                "wake_test_miss",
                                mode="voice_sample",
                                score=round(conf, 3),
                                threshold=cfg.voice_sample_threshold,
                                duration_sec=round(dur, 2),
                            )
                        continue

                    if spotter is None:
                        continue
                    kw = detect_keyword_on_segment(spotter, seg_samples)
                    if not kw:
                        continue
                    if kw.startswith("ALIAS_") and dur > 1.25:
                        continue
                    if live_stream is not None:
                        live_stream = spotter.create_stream()
                        was_speaking = False
                        silence_chunks = 0
                    ret = handle_hit(kw, dur)
                    if ret:
                        return ret
    finally:
        runtime.mic_released_event.set()
    return None


def run_doctor_check(cfg: WakeConfig, runtime: RuntimeState | None = None) -> dict[str, Any]:
    ax_res = run_ax_helper("check", timeout=8.0)
    builtin_name = str(ax_res.get("builtin_mic_name", "")).strip() or None
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
        if cfg.wake_mode == "voice_sample":
            _ = get_sample_matcher()
            kws_ok = True
        else:
            existing_spotter = runtime.spotter if runtime is not None else None
            if existing_spotter is None:
                _ = build_keyword_spotter(cfg)
            kws_ok = True
        if SILERO_VAD_PATH.is_file():
            if _DICTATION_VAD is not None or _WAKE_VAD is not None:
                vad_ok = True
            else:
                _ = build_vad()
                vad_ok = True
    except Exception as exc:  # noqa: BLE001
        model_error = str(exc)

    del builtin_name
    overall = bool(ax_res.get("ok")) and mic_ok and kws_ok and vad_ok
    return {
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


def apply_config_update(runtime: RuntimeState, patch: dict[str, Any]) -> tuple[WakeConfig, str]:
    with runtime.lock:
        cur = runtime.cfg
        cur_dict = cur.to_dict()

    if "project_name" in patch:
        cur_dict["project_name"] = str(patch["project_name"]).strip() or cur.project_name
    if "ui_language" in patch:
        ui_lang = str(patch["ui_language"]).strip().lower()
        if ui_lang not in ("auto", "zh", "en"):
            raise ValueError(f"Unsupported ui_language: {patch['ui_language']!r}")
        cur_dict["ui_language"] = ui_lang
    if "model_keyword" in patch:
        cur_dict["model_keyword"] = str(patch["model_keyword"]).strip()
    if "thinking_effort" in patch:
        cur_dict["thinking_effort"] = normalize_thinking_effort(patch["thinking_effort"])
    if "mic_mode" in patch:
        cur_dict["mic_mode"] = str(patch["mic_mode"]).strip() or "built-in"
    if "voice_wake_enabled" in patch:
        cur_dict["voice_wake_enabled"] = bool(patch["voice_wake_enabled"])
    if "wake_mode" in patch:
        wm = str(patch["wake_mode"]).strip().lower()
        if wm not in ("text", "voice_sample"):
            raise ValueError(f"Unsupported wake_mode: {patch['wake_mode']!r}")
        cur_dict["wake_mode"] = wm
    if "wake_phrase" in patch:
        cur_dict["wake_phrase"] = " ".join(str(patch["wake_phrase"]).strip().upper().split())
    if "wake_aliases" in patch:
        cur_dict["wake_aliases"] = normalize_wake_aliases(patch["wake_aliases"], cur_dict["wake_phrase"])
    else:
        cur_dict["wake_aliases"] = normalize_wake_aliases(cur_dict.get("wake_aliases", []), cur_dict["wake_phrase"])
    if "voice_sample_threshold" in patch:
        cur_dict["voice_sample_threshold"] = max(0.50, min(0.95, float(patch["voice_sample_threshold"])))
    if "hotkey_enabled" in patch:
        cur_dict["hotkey_enabled"] = bool(patch["hotkey_enabled"])
    if "hotkey" in patch:
        cur_dict["hotkey"] = str(patch["hotkey"]).strip().lower()
    if "keywords_score" in patch:
        cur_dict["keywords_score"] = max(0.5, min(5.0, float(patch["keywords_score"])))
    if "keywords_threshold" in patch:
        cur_dict["keywords_threshold"] = max(0.05, min(0.95, float(patch["keywords_threshold"])))
    if "wait_for_speech_timeout_sec" in patch:
        cur_dict["wait_for_speech_timeout_sec"] = max(3.0, min(30.0, float(patch["wait_for_speech_timeout_sec"])))
    if "silence_timeout_sec" in patch:
        cur_dict["silence_timeout_sec"] = max(0.5, min(6.0, float(patch["silence_timeout_sec"])))
    if "max_dictation_sec" in patch:
        cur_dict["max_dictation_sec"] = max(10.0, min(180.0, float(patch["max_dictation_sec"])))
    if "volume" in patch:
        cur_dict.setdefault("sounds", {})["volume"] = max(0.0, min(1.0, float(patch["volume"])))
    elif "sounds" in patch and isinstance(patch["sounds"], dict) and "volume" in patch["sounds"]:
        cur_dict.setdefault("sounds", {})["volume"] = max(0.0, min(1.0, float(patch["sounds"]["volume"])))

    if cur_dict.get("hotkey_enabled"):
        new_hk = str(cur_dict.get("hotkey", "")).strip()
        if not new_hk:
            raise ValueError("hotkey cannot be empty when hotkey_enabled is true")
        same_active = (
            cur.hotkey_enabled
            and new_hk == cur.hotkey
            and runtime.hotkey_registered
        )
        if not same_active:
            val_res = run_ax_helper("validate-hotkey", "--hotkey", new_hk, timeout=4.0)
            if not val_res.get("ok"):
                raise ValueError(f"Invalid or occupied hotkey '{new_hk}': {val_res.get('error', 'unknown')}")
            cur_dict["hotkey"] = str(val_res.get("hotkey", new_hk))

    kw_path = Path(cur_dict["keywords_file"])
    kw_line = ""
    target_mode = str(cur_dict.get("wake_mode", "text"))
    need_kws_rebuild = (
        cur_dict["wake_phrase"] != cur.wake_phrase
        or tuple(cur_dict.get("wake_aliases", [])) != cur.wake_aliases
        or float(cur_dict["keywords_score"]) != cur.keywords_score
        or float(cur_dict["keywords_threshold"]) != cur.keywords_threshold
        or not kw_path.is_file()
    )
    if target_mode == "text" and need_kws_rebuild:
        kw_line = generate_keywords_file(
            cur_dict["wake_phrase"],
            float(cur_dict["keywords_score"]),
            float(cur_dict["keywords_threshold"]),
            KEYWORDS_RAW_PATH,
            kw_path,
            wake_aliases=cur_dict.get("wake_aliases", []),
        )

    atomic_write_text(runtime.config_path, json.dumps(cur_dict, ensure_ascii=False, indent=2) + "\n")
    new_cfg = load_config(runtime.config_path)
    new_spotter = runtime.spotter
    if new_cfg.wake_mode == "text":
        if need_kws_rebuild or new_spotter is None:
            new_spotter = build_keyword_spotter(new_cfg)
    else:
        _ = get_sample_matcher()
    with runtime.lock:
        runtime.cfg = new_cfg
        runtime.spotter = new_spotter
    if runtime.hotkey_watcher is not None and (
        new_cfg.hotkey_enabled != cur.hotkey_enabled or new_cfg.hotkey != cur.hotkey
    ):
        runtime.hotkey_watcher.reconfigure(new_cfg.hotkey_enabled, new_cfg.hotkey)
    runtime.reload_event.set()
    runtime.log(
        "config_updated",
        project=new_cfg.project_name,
        model_keyword=new_cfg.model_keyword,
        thinking_effort=new_cfg.thinking_effort,
        voice_wake_enabled=new_cfg.voice_wake_enabled,
        wake_mode=new_cfg.wake_mode,
        wake_phrase=new_cfg.wake_phrase,
        wake_aliases=list(new_cfg.wake_aliases),
        voice_sample_threshold=new_cfg.voice_sample_threshold,
        hotkey_enabled=new_cfg.hotkey_enabled,
        hotkey=new_cfg.hotkey,
        mic_mode=new_cfg.mic_mode,
        silence_timeout_sec=new_cfg.silence_timeout_sec,
        keywords_score=new_cfg.keywords_score,
        keywords_threshold=new_cfg.keywords_threshold,
    )
    return new_cfg, kw_line


def get_cached_probes(
    runtime: RuntimeState, force: bool = False, ttl: float = 60.0
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, str]]]:
    """Return cached AX status, mics, and projects; serialize refreshes and skip AX probes during busy handoffs."""
    now = time.monotonic()
    with runtime.lock:
        busy = runtime.state in ("preparing", "dictating", "submitting", "recording_sample")
        ax_status = runtime.cached_ax_status
        ax_fresh = bool(ax_status) and not force and (busy or (now - runtime.cached_ax_ts) < ttl)
        mics = runtime.cached_mics
        mics_fresh = bool(mics) and not force and (busy or (now - runtime.cached_mics_ts) < ttl)
        projects = runtime.cached_projects
        projects_fresh = bool(projects) and not force and (busy or (now - runtime.cached_projects_ts) < ttl)

    if ax_fresh and mics_fresh and projects_fresh:
        return ax_status, mics, projects

    with runtime._probe_lock:
        now = time.monotonic()
        with runtime.lock:
            busy = runtime.state in ("preparing", "dictating", "submitting", "recording_sample")
            ax_status = runtime.cached_ax_status
            ax_fresh = bool(ax_status) and not force and (busy or (now - runtime.cached_ax_ts) < ttl)
            mics = runtime.cached_mics
            mics_fresh = bool(mics) and not force and (busy or (now - runtime.cached_mics_ts) < ttl)
            projects = runtime.cached_projects
            projects_fresh = bool(projects) and not force and (busy or (now - runtime.cached_projects_ts) < ttl)

        if not ax_fresh:
            probe_res = run_ax_helper("quick-check", timeout=4.0)
            if "ax_trusted" in probe_res:
                ax_status = probe_res
                with runtime.lock:
                    runtime.cached_ax_status = ax_status
                    runtime.cached_ax_ts = time.monotonic()
            else:
                with runtime.lock:
                    prev_ax = runtime.cached_ax_status
                    if "ax_trusted" in prev_ax:
                        ax_status = prev_ax
                    else:
                        ax_status = probe_res
                        runtime.cached_ax_status = ax_status
                        runtime.cached_ax_ts = 0.0

        if not mics_fresh:
            builtin_name = str(ax_status.get("builtin_mic_name", "")).strip()
            mics = list_input_mics(builtin_name=builtin_name)
            with runtime.lock:
                runtime.cached_mics = mics
                runtime.cached_mics_ts = time.monotonic()

        if not projects_fresh:
            projects = list_projects()
            with runtime.lock:
                runtime.cached_projects = projects
                runtime.cached_projects_ts = time.monotonic()

        return ax_status, mics, projects


def start_web_server(runtime: RuntimeState, port: int, ui_token: str) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _send_bytes(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: int, payload: Any) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send_bytes(status, "application/json; charset=utf-8", data)

        def _check_token(self, parsed_url: Any) -> bool:
            if not ui_token:
                return True
            hdr = self.headers.get("x-sidecar-token", "")
            if hdr == ui_token:
                return True
            qs = parse_qs(parsed_url.query)
            return qs.get("token", [""])[0] == ui_token

        def _read_json_body(self, max_bytes: int = 256 * 1024) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            if length > max_bytes:
                raise ValueError(f"Request body too large ({length} > {max_bytes})")
            raw = self.rfile.read(length)
            if not raw:
                return {}
            parsed = json.loads(raw.decode("utf-8"))
            return parsed if isinstance(parsed, dict) else {}

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/preload.js":
                js = (
                    PRELOAD_JS_PATH.read_text(encoding="utf-8")
                    if PRELOAD_JS_PATH.is_file()
                    else FALLBACK_PRELOAD_JS
                )
                self._send_bytes(200, "application/javascript; charset=utf-8", js.encode("utf-8"))
                return

            if parsed.path == "/":
                html = INDEX_HTML_PATH.read_text(encoding="utf-8") if INDEX_HTML_PATH.is_file() else "<h1>Voice Wake</h1>"
                self._send_bytes(200, "text/html; charset=utf-8", html.encode("utf-8"))
                return

            if not self._check_token(parsed):
                self._send_json(401, {"ok": False, "error": "unauthorized"})
                return

            if parsed.path == "/api/status":
                qs = parse_qs(parsed.query)
                force_refresh = qs.get("refresh", ["0"])[0] in ("1", "true", "yes")
                with runtime.lock:
                    cfg = runtime.cfg
                    st = {
                        "ok": True,
                        "version": VERSION,
                        "state": runtime.state,
                        "paused": runtime.paused,
                        "wake_test_mode": runtime.wake_test_mode,
                        "wake_test_hits": runtime.wake_test_hits,
                        "last_wake_test_ts": runtime.last_wake_test_ts,
                        "last_wake_test_keyword": runtime.last_wake_test_keyword,
                        "sample_count": len(runtime.sample_meta),
                        "last_sample_score": runtime.last_sample_score,
                        "last_sample_hit": runtime.last_sample_hit,
                        "last_sample_ts": runtime.last_sample_ts,
                        "hotkey_registered": runtime.hotkey_registered,
                        "hotkey_error": runtime.hotkey_error,
                        "active_device": runtime.active_device,
                        "active_device_index": runtime.active_device_index,
                        "last_error": runtime.last_error,
                        "last_event": runtime.last_event,
                        "config": cfg.to_dict(),
                    }
                ax_status, mics, projects = get_cached_probes(runtime, force=force_refresh, ttl=60.0)
                st["ax_status"] = ax_status
                st["available_mics"] = mics
                st["available_projects"] = projects
                st["available_models"] = DEFAULT_MODEL_OPTIONS
                st["available_efforts"] = ["", "Low", "Medium", "High", "Extra High"]
                st["ui_mtime"] = int(INDEX_HTML_PATH.stat().st_mtime) if INDEX_HTML_PATH.is_file() else 0
                st["system_lang"] = SYSTEM_LANG
                self._send_json(200, st)
                return

            if parsed.path == "/api/samples":
                with runtime.lock:
                    samples = list(runtime.sample_meta)
                self._send_json(200, {"ok": True, "samples": samples, "count": len(samples)})
                return

            if parsed.path == "/api/samples/audio":
                qs = parse_qs(parsed.query)
                sid = qs.get("id", [""])[0].strip()
                if not re.fullmatch(r"[A-Za-z0-9_-]+", sid):
                    self._send_json(400, {"ok": False, "error": "invalid_sample_id"})
                    return
                wav_path = VOICE_SAMPLES_DIR / f"sample_{sid}.wav"
                if not wav_path.is_file():
                    self._send_json(404, {"ok": False, "error": "sample_not_found"})
                    return
                self._send_bytes(200, "audio/wav", wav_path.read_bytes())
                return

            if parsed.path == "/api/logs":
                qs = parse_qs(parsed.query)
                limit = int(qs.get("limit", ["40"])[0] or "40")
                self._send_json(200, {"ok": True, "logs": read_recent_logs(limit)})
                return

            self._send_json(404, {"ok": False, "error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if not self._check_token(parsed):
                self._send_json(401, {"ok": False, "error": "unauthorized"})
                return

            max_body = 15 * 1024 * 1024 if parsed.path == "/api/samples/import" else 256 * 1024
            try:
                body = self._read_json_body(max_bytes=max_body)
            except Exception as exc:  # noqa: BLE001
                self._send_json(400, {"ok": False, "error": f"invalid_json: {exc}"})
                return

            if parsed.path == "/api/toggle-pause":
                with runtime.lock:
                    if "paused" in body:
                        runtime.paused = bool(body["paused"])
                    else:
                        runtime.paused = not runtime.paused
                    paused = runtime.paused
                    cfg = runtime.cfg
                    if paused:
                        runtime.wake_test_mode = False
                        if runtime.state in ("listening", "hotkey_only"):
                            runtime.state = "paused"
                    else:
                        if runtime.state in ("paused", "hotkey_only"):
                            if cfg.voice_wake_enabled:
                                runtime.state = "listening"
                            elif cfg.hotkey_enabled:
                                runtime.state = "hotkey_only"
                            else:
                                runtime.state = "paused"
                    state = runtime.state
                    wake_test_mode = runtime.wake_test_mode
                runtime.reload_event.set()
                runtime.log("pause_toggled", paused=paused)
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "paused": paused,
                        "state": state,
                        "wake_test_mode": wake_test_mode,
                        "wake_phrase": cfg.wake_phrase,
                        "voice_wake_enabled": cfg.voice_wake_enabled,
                        "hotkey_enabled": cfg.hotkey_enabled,
                        "hotkey": cfg.hotkey,
                    },
                )
                return

            if parsed.path == "/api/wake-test":
                with runtime.lock:
                    cfg = runtime.cfg
                    if "enabled" in body:
                        enabled = bool(body["enabled"])
                    else:
                        enabled = not runtime.wake_test_mode
                    runtime.wake_test_mode = enabled
                    need_reload = False
                    if enabled:
                        runtime.wake_test_hits = 0
                        runtime.last_wake_test_ts = ""
                        runtime.last_wake_test_keyword = ""
                        if runtime.paused:
                            runtime.paused = False
                            need_reload = True
                        if cfg.voice_wake_enabled and runtime.state in ("paused", "hotkey_only"):
                            runtime.state = "listening"
                    elif body.get("reset"):
                        runtime.wake_test_hits = 0
                        runtime.last_wake_test_ts = ""
                        runtime.last_wake_test_keyword = ""
                    paused = runtime.paused
                    state = runtime.state
                    hits = runtime.wake_test_hits
                    last_ts = runtime.last_wake_test_ts
                    last_kw = runtime.last_wake_test_keyword
                if need_reload:
                    runtime.reload_event.set()
                runtime.log(
                    "wake_test_toggled",
                    enabled=enabled,
                    threshold=cfg.keywords_threshold,
                    score=cfg.keywords_score,
                )
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "wake_test_mode": enabled,
                        "wake_test_hits": hits,
                        "last_wake_test_ts": last_ts,
                        "last_wake_test_keyword": last_kw,
                        "paused": paused,
                        "state": state,
                    },
                )
                return

            if parsed.path == "/api/config":
                try:
                    new_cfg, kw_line = apply_config_update(runtime, body)
                    self._send_json(
                        200,
                        {"ok": True, "config": new_cfg.to_dict(), "keywords_line": kw_line},
                    )
                except Exception as exc:  # noqa: BLE001
                    self._send_json(400, {"ok": False, "error": str(exc)})
                return

            if parsed.path == "/api/check":
                with runtime.lock:
                    cfg = runtime.cfg
                report = run_doctor_check(cfg, runtime=runtime)
                with runtime.lock:
                    if isinstance(report.get("ax"), dict):
                        runtime.cached_ax_status = report["ax"]
                        runtime.cached_ax_ts = time.monotonic()
                self._send_json(200, report)
                return

            if parsed.path == "/api/samples/record":
                with runtime.lock:
                    cfg = runtime.cfg
                    if runtime.state in ("preparing", "dictating", "submitting") or runtime.recording_sample_event.is_set():
                        self._send_json(409, {"ok": False, "error": f"busy:{runtime.state}"})
                        return
                    prev_state = runtime.state
                runtime.recording_done_event.clear()
                runtime.recording_sample_event.set()
                runtime.reload_event.set()
                try:
                    runtime.mic_released_event.wait(1.5)
                    runtime.set_state("recording_sample")
                    dev_idx, _ = resolve_input_device(cfg.mic_mode)
                    vad = build_wake_vad()
                    rec_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=128)
                    max_sec = max(1.5, min(5.0, float(body.get("max_sec", 2.8))))

                    def rec_cb(indata: np.ndarray, frames: int, timing: object, status: object) -> None:
                        del frames, timing, status
                        try:
                            rec_q.put_nowait(indata[:, 0].copy())
                        except queue.Full:
                            pass

                    captured: np.ndarray | None = None
                    deadline = time.monotonic() + max_sec
                    with sd.InputStream(
                        samplerate=SAMPLE_RATE,
                        channels=1,
                        dtype="float32",
                        blocksize=512,
                        device=dev_idx,
                        callback=rec_cb,
                    ):
                        while time.monotonic() < deadline and not runtime.stop_event.is_set():
                            try:
                                chunk = rec_q.get(timeout=0.08)
                            except queue.Empty:
                                continue
                            vad.accept_waveform(chunk)
                            while not vad.empty():
                                seg = vad.front
                                arr = np.array(seg.samples, dtype=np.float32)
                                vad.pop()
                                dur = len(arr) / float(SAMPLE_RATE)
                                rms = float(np.sqrt(np.mean(np.square(arr)))) if arr.size > 0 else 0.0
                                if 0.12 <= dur <= 1.50 and rms >= 0.005:
                                    captured = arr
                                    break
                            if captured is not None:
                                break
                        if captured is None:
                            vad.accept_waveform(np.zeros(int(SAMPLE_RATE * 0.4), dtype=np.float32))
                            vad.flush()
                            while not vad.empty():
                                seg = vad.front
                                arr = np.array(seg.samples, dtype=np.float32)
                                vad.pop()
                                dur = len(arr) / float(SAMPLE_RATE)
                                rms = float(np.sqrt(np.mean(np.square(arr)))) if arr.size > 0 else 0.0
                                if 0.12 <= dur <= 1.50 and rms >= 0.005:
                                    captured = arr
                                    break

                    if captured is None:
                        self._send_json(400, {"ok": False, "error": "no_speech_detected"})
                        return
                    matcher = get_sample_matcher()
                    meta = save_voice_sample_record(captured, cfg.wake_phrase, matcher)
                    total = reload_voice_samples(runtime)
                    runtime.log("sample_recorded", id=meta["id"], duration_sec=meta["duration_sec"], total_samples=total)
                    self._send_json(200, {"ok": True, "sample": meta, "total_samples": total})
                    return
                finally:
                    runtime.recording_sample_event.clear()
                    runtime.set_state(prev_state if not runtime.paused else "paused")
                    runtime.recording_done_event.set()
                    runtime.reload_event.set()

            if parsed.path == "/api/samples/import":
                with runtime.lock:
                    cfg = runtime.cfg
                b64 = str(body.get("data_b64", "")).strip()
                fname = str(body.get("filename", "import.wav")).strip() or "import.wav"
                if not b64:
                    self._send_json(400, {"ok": False, "error": "empty_audio"})
                    return
                if "," in b64 and b64.startswith("data:"):
                    b64 = b64.split(",", 1)[1]
                try:
                    raw_bytes = base64.b64decode(b64, validate=True)
                except Exception as exc:  # noqa: BLE001
                    self._send_json(400, {"ok": False, "error": f"invalid_base64: {exc}"})
                    return
                try:
                    matcher = get_sample_matcher()
                    added = import_audio_file_as_samples(raw_bytes, fname, cfg.wake_phrase, matcher)
                except Exception as exc:  # noqa: BLE001
                    self._send_json(400, {"ok": False, "error": str(exc)})
                    return
                if not added:
                    self._send_json(400, {"ok": False, "error": "no_valid_word_segments_found"})
                    return
                total = reload_voice_samples(runtime)
                with runtime.lock:
                    samples = list(runtime.sample_meta)
                runtime.log("samples_imported", filename=fname, added=len(added), total_samples=total)
                self._send_json(200, {"ok": True, "added": len(added), "total_samples": total, "samples": samples})
                return

            if parsed.path == "/api/samples/delete":
                delete_all = bool(body.get("all", False))
                sid = str(body.get("id", "")).strip() or None
                removed = delete_voice_samples(sample_id=sid, delete_all=delete_all)
                total = reload_voice_samples(runtime)
                with runtime.lock:
                    samples = list(runtime.sample_meta)
                runtime.log("samples_deleted", removed=removed, total_samples=total)
                self._send_json(200, {"ok": True, "removed": removed, "total_samples": total, "samples": samples})
                return

            if parsed.path == "/api/trigger":
                with runtime.lock:
                    if runtime.state in ("preparing", "dictating", "submitting"):
                        self._send_json(409, {"ok": False, "error": f"busy:{runtime.state}"})
                        return
                    runtime.manual_dry_run = bool(body.get("dry_run_submit", True))
                    runtime.manual_trigger_source = "panel"
                    runtime.manual_trigger_event.set()
                self._send_json(200, {"ok": True, "started": True, "dry_run_submit": runtime.manual_dry_run})
                return

            self._send_json(404, {"ok": False, "error": "not_found"})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="voice-wake-http")
    thread.start()
    runtime.log("web_ui_started", port=port)
    return server


def cmd_test_wav(cfg: WakeConfig, wav_path: Path) -> int:
    vad = build_wake_vad()
    with wave.open(str(wav_path), "rb") as wf:
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2 or wf.getframerate() != SAMPLE_RATE:
            raise ValueError(
                f"Expected 16kHz mono 16-bit PCM WAV, got {wf.getframerate()}Hz ch={wf.getnchannels()} width={wf.getsampwidth()}"
            )
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0

    use_sample_mode = cfg.wake_mode == "voice_sample"
    if use_sample_mode:
        matcher = get_sample_matcher()
        _, matrix, ids = load_voice_samples()
        spotter = None
        max_utt_dur = 1.50
    else:
        matcher = None
        matrix = np.zeros((0, 0), dtype=np.float32)
        ids = []
        spotter = build_keyword_spotter(cfg)
        is_single_word = len(cfg.wake_phrase.split()) <= 1
        max_utt_dur = 1.25 if is_single_word else 2.50

    samples = np.concatenate([pcm, np.zeros(int(SAMPLE_RATE * 0.8), dtype=np.float32)])
    detected: list[dict[str, Any]] = []
    all_scores: list[dict[str, Any]] = []
    seg_count = 0
    chunk_size = 512
    for start in range(0, len(samples), chunk_size):
        chunk = samples[start : start + chunk_size]
        if len(chunk) < chunk_size:
            chunk = np.pad(chunk, (0, chunk_size - len(chunk)))
        vad.accept_waveform(chunk)
        while not vad.empty():
            seg = vad.front
            seg_samples = np.array(seg.samples, dtype=np.float32)
            t_start = round(seg.start / float(SAMPLE_RATE), 2)
            vad.pop()
            seg_count += 1
            dur = len(seg_samples) / float(SAMPLE_RATE)
            if dur < 0.12 or dur > max_utt_dur:
                continue
            if use_sample_mode and matcher is not None:
                rms = float(np.sqrt(np.mean(np.square(seg_samples)))) if seg_samples.size > 0 else 0.0
                if rms < 0.005:
                    continue
                conf, matched_id = matcher.score(seg_samples, matrix, ids)
                item = {
                    "t_sec": t_start,
                    "duration_sec": round(dur, 2),
                    "score": round(conf, 4),
                    "matched_id": matched_id,
                    "hit": conf >= cfg.voice_sample_threshold,
                }
                all_scores.append(item)
                if conf >= cfg.voice_sample_threshold:
                    detected.append(item)
            elif spotter is not None:
                kw = detect_keyword_on_segment(spotter, seg_samples)
                if kw and not (kw.startswith("ALIAS_") and dur > 1.25):
                    detected.append({"t_sec": t_start, "duration_sec": round(dur, 2), "keyword": kw})

    payload: dict[str, Any] = {
        "wav": str(wav_path),
        "mode": cfg.wake_mode,
        "segments": seg_count,
        "detected": detected,
        "triggered": len(detected) > 0,
    }
    if use_sample_mode:
        payload["threshold"] = cfg.voice_sample_threshold
        payload["sample_count"] = len(ids)
        payload["all_scores"] = all_scores
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def cmd_daemon(
    config_path: Path,
    cfg: WakeConfig,
    dry_run_submit: bool = False,
    web_port: int | None = None,
    no_lock: bool = False,
    no_hotkey: bool = False,
) -> int:
    lock: SingleInstanceLock | None = None
    if not no_lock:
        lock = SingleInstanceLock(LOCK_FILE)
        lock.acquire()

    runtime = RuntimeState(config_path, cfg)
    if not no_hotkey:
        runtime.hotkey_watcher = HotkeyWatcher(runtime)

    def _on_signal(signum: int, _frame: Any) -> None:
        runtime.log("signal_received", signum=signum)
        runtime.stop_event.set()
        runtime.reload_event.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    env_port = os.environ.get("ANTIGRAVITY_SIDECAR_WEB_PORT", "").strip()
    port = web_port if (web_port and web_port > 0) else (int(env_port) if env_port.isdigit() else 0)
    ui_token = os.environ.get("ANTIGRAVITY_SIDECAR_UI_TOKEN", "")
    web_server: ThreadingHTTPServer | None = None
    if port > 0:
        web_server = start_web_server(runtime, port, ui_token)

    try:
        if runtime.hotkey_watcher is not None:
            runtime.hotkey_watcher.reconfigure(cfg.hotkey_enabled, cfg.hotkey)
        threading.Thread(
            target=lambda: run_ax_helper("builtin-mic", timeout=8.0),
            daemon=True,
            name="voice-wake-helper-warmup",
        ).start()
        if cfg.wake_mode == "text":
            runtime.spotter = build_keyword_spotter(cfg)
        else:
            _ = get_sample_matcher()
        _ = get_wake_vad()
        runtime.log(
            "daemon_started",
            version=VERSION,
            project=cfg.project_name,
            model_keyword=cfg.model_keyword,
            mic_mode=cfg.mic_mode,
            voice_wake_enabled=cfg.voice_wake_enabled,
            wake_mode=cfg.wake_mode,
            wake_phrase=cfg.wake_phrase,
            wake_aliases=list(cfg.wake_aliases),
            voice_sample_threshold=cfg.voice_sample_threshold,
            sample_count=len(runtime.sample_ids),
            hotkey_enabled=cfg.hotkey_enabled,
            hotkey=cfg.hotkey,
            score=cfg.keywords_score,
            threshold=cfg.keywords_threshold,
            web_port=port,
        )

        while not runtime.stop_event.is_set():
            runtime.reload_event.clear()

            if runtime.recording_sample_event.is_set():
                runtime.set_state("recording_sample")
                runtime.recording_done_event.wait(0.2)
                continue

            if runtime.manual_trigger_event.is_set():
                runtime.manual_trigger_event.clear()
                with runtime.lock:
                    cur_cfg = runtime.cfg
                    dry = runtime.manual_dry_run
                    source = runtime.manual_trigger_source
                dev_idx, dev_name = resolve_input_device(cur_cfg.mic_mode)
                with runtime.lock:
                    runtime.active_device = dev_name
                    runtime.active_device_index = dev_idx
                runtime.log("trigger_handoff", source=source, dry_run_submit=dry)
                handle_wake_handoff(cur_cfg, dev_idx, runtime=runtime, dry_run_submit=dry)
                if runtime.paused:
                    runtime.set_state("paused")
                elif not cur_cfg.voice_wake_enabled:
                    runtime.set_state("hotkey_only" if cur_cfg.hotkey_enabled else "paused")
                else:
                    runtime.set_state("cooldown")
                time.sleep(cur_cfg.cooldown_sec)
                continue

            with runtime.lock:
                cur_cfg = runtime.cfg
                cur_spotter = runtime.spotter
                paused = runtime.paused

            if paused or not cur_cfg.voice_wake_enabled:
                runtime.set_state("hotkey_only" if (not paused and cur_cfg.hotkey_enabled) else "paused")
                runtime.manual_trigger_event.wait(0.2)
                continue

            if cur_cfg.wake_mode == "text" and cur_spotter is None:
                cur_spotter = build_keyword_spotter(cur_cfg)
                with runtime.lock:
                    runtime.spotter = cur_spotter

            dev_idx, dev_name = resolve_input_device(cur_cfg.mic_mode)
            with runtime.lock:
                runtime.active_device = dev_name
                runtime.active_device_index = dev_idx
            runtime.set_state("listening")
            try:
                runtime.log("listening", device_index=dev_idx, device_name=dev_name)
                keyword = listen_for_wake_word(cur_spotter, dev_idx, runtime, cur_cfg)
            except Exception as exc:  # noqa: BLE001
                runtime.set_state("error", str(exc))
                runtime.log("audio_error", error=str(exc))
                time.sleep(2.0)
                continue

            if runtime.stop_event.is_set() or not keyword:
                continue

            with runtime.lock:
                is_wake_test = runtime.wake_test_mode
                if is_wake_test:
                    runtime.wake_test_hits += 1
                    hits = runtime.wake_test_hits
                    runtime.last_wake_test_ts = time.strftime("%H:%M:%S")
                    runtime.last_wake_test_keyword = keyword
                else:
                    hits = 0

            if is_wake_test:
                play_sound(cur_cfg.sounds.ready, cur_cfg.sounds.volume, wait=False)
                runtime.log(
                    "wake_test_detected",
                    keyword=keyword,
                    hits=hits,
                    threshold=cur_cfg.keywords_threshold,
                    score=cur_cfg.keywords_score,
                )
                time.sleep(0.6)
                continue

            runtime.log("wake_detected", keyword=keyword)
            handle_wake_handoff(cur_cfg, dev_idx, runtime=runtime, dry_run_submit=dry_run_submit)
            runtime.set_state("cooldown")
            time.sleep(cur_cfg.cooldown_sec)
    finally:
        _AX_HELPER_CLIENT.close()
        if runtime.hotkey_watcher is not None:
            runtime.hotkey_watcher.stop()
        if web_server is not None:
            web_server.shutdown()
            web_server.server_close()
        if lock is not None:
            lock.release()
        runtime.log("daemon_stopped")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--check", action="store_true", help="Run diagnostics on AX, microphone, and ONNX models.")
    parser.add_argument("--test-wav", type=Path, help="Test KWS detection on a 16kHz mono PCM16 WAV file.")
    parser.add_argument("--trigger-now", action="store_true", help="Skip KWS and run the wake handoff immediately.")
    parser.add_argument("--dry-run-submit", action="store_true", help="Run dictation flow without pressing Send.")
    parser.add_argument("--web-port", type=int, default=0, help="Override Sidecar Web UI HTTP port for testing.")
    parser.add_argument("--no-lock", action="store_true", help="Skip single-instance flock (for isolated HTTP tests).")
    parser.add_argument("--no-hotkey", action="store_true", help="Disable Carbon global hotkey registration.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cfg = load_config(args.config)
    if args.check:
        report = run_doctor_check(cfg)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report.get("ok") else 1
    if args.test_wav is not None:
        return cmd_test_wav(cfg, args.test_wav)
    if args.trigger_now:
        dev_idx, _ = resolve_input_device(cfg.mic_mode)
        res = handle_wake_handoff(cfg, dev_idx, runtime=None, dry_run_submit=args.dry_run_submit)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    return cmd_daemon(
        args.config,
        cfg,
        dry_run_submit=args.dry_run_submit,
        web_port=args.web_port,
        no_lock=args.no_lock,
        no_hotkey=args.no_hotkey,
    )


if __name__ == "__main__":
    sys.exit(main())
