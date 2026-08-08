"""Local Parakeet Realtime EOU speech-to-text for desktop mic audio."""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import tempfile
import time
import traceback
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_PARAKEET_MODEL = "nvidia/parakeet_realtime_eou_120m-v1"


@dataclass(frozen=True)
class ParakeetStreamingConfig:
    model: str = DEFAULT_PARAKEET_MODEL
    device: str = "cuda"
    dtype: str = "auto"
    max_gpu_memory_gb: float | None = None
    cpu_fallback: bool = True
    min_free_vram_mb: int = 2048
    eou_token: str = "<EOU>"
    # NOTE(duplex-phase1): STT engine selection + tuning knobs. See
    # docs/design/2026-07-05-voice-duplex-design.md (Tunables).
    #   engine: "batch" (buffer cheaply; semantic EOU is probed on a VAD pause),
    #           "auto" (legacy alias that also buffers),
    #           "cache_aware" (force streaming; error+fallback if unavailable),
    #           "rebuffer" (force the O(n^2) re-transcribe fallback).
    #   stream_chunk_seconds: cache-aware decode cadence — smaller = lower
    #           latency, more GPU calls. Tune from logs/parakeet-stt.log.
    #   debug: verbose per-chunk logs (timing, eou_prob, partial text).
    engine: str = "batch"
    stream_chunk_seconds: float = 0.5
    debug: bool = False


_MODEL_CACHE: dict[tuple[str, str, str, float | None, bool, str], Any] = {}


def resolve_parakeet_config(stt_config: dict[str, Any] | None) -> ParakeetStreamingConfig:
    streaming = (stt_config or {}).get("streaming", {})
    streaming = streaming if isinstance(streaming, dict) else {}
    nested = streaming.get("parakeet", {})
    nested = nested if isinstance(nested, dict) else {}

    def pick(key: str, default: Any) -> Any:
        value = nested.get(key, streaming.get(key, default))
        return default if value in (None, "") else value

    def pick_bool(key: str, default: bool) -> bool:
        value = pick(key, default)
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off"}
        return bool(value)

    try:
        max_gpu_memory = float(pick("max_gpu_memory_gb", 0) or 0)
    except (TypeError, ValueError):
        max_gpu_memory = 0

    try:
        min_free_vram_mb = int(pick("min_free_vram_mb", 2048) or 2048)
    except (TypeError, ValueError):
        min_free_vram_mb = 2048

    nested_model = nested.get("model")
    legacy_model = streaming.get("model")
    model_value = nested_model
    if model_value in (None, ""):
        legacy_model_text = str(legacy_model or "").strip()
        model_value = legacy_model_text if "parakeet" in legacy_model_text.lower() else DEFAULT_PARAKEET_MODEL

    try:
        stream_chunk_seconds = float(pick("stream_chunk_seconds", 0.5) or 0.5)
    except (TypeError, ValueError):
        stream_chunk_seconds = 0.5

    # ``rebuffer`` re-runs NeMo over the full utterance every 500 ms.  That is
    # useful for diagnostics, but it is O(n²), creates a temporary manifest on
    # every pass, and competes with wake-word/TTS inference for the GPU.  Keep it
    # explicit.  The production default buffers cheaply and lets the duplex
    # endpoint issue one semantic-EOU probe when VAD observes a pause.
    engine = str(pick("engine", "batch")).strip().lower() or "batch"
    if engine not in {"auto", "batch", "rebuffer", "cache_aware"}:
        engine = "batch"

    return ParakeetStreamingConfig(
        model=str(model_value).strip() or DEFAULT_PARAKEET_MODEL,
        device=str(pick("device", "cuda")).strip().lower() or "cuda",
        dtype=str(pick("dtype", "auto")).strip().lower() or "auto",
        max_gpu_memory_gb=max_gpu_memory if max_gpu_memory > 0 else None,
        cpu_fallback=pick_bool("cpu_fallback", True),
        min_free_vram_mb=min_free_vram_mb if min_free_vram_mb > 0 else 2048,
        eou_token=str(pick("eou_token", "<EOU>")).strip() or "<EOU>",
        engine=engine,
        stream_chunk_seconds=stream_chunk_seconds if stream_chunk_seconds > 0 else 0.5,
        debug=pick_bool("debug", False),
    )


def _is_memory_load_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(
        marker in text
        for marker in (
            "cuda out of memory",
            "outofmemoryerror",
            "paging file is too small",
            "os error 1455",
        )
    )


def _is_cuda_driver_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(
        marker in text
        for marker in (
            "cudacudaerrorinsufficientdriver",
            "cudaerrorinsufficientdriver",
            "cuda driver version is insufficient",
            "nvidia driver on your system is too old",
            "cuda driver initialization failed",
        )
    )


def _free_vram_mb() -> int | None:
    """Return free CUDA VRAM in MB, or ``None`` when it can't be determined.

    Any failure (no torch, no CUDA, driver quirk, etc.) is treated as
    "unknown, proceed as today" — callers must not let this raise.
    """
    try:
        import torch

        if not torch.cuda.is_available():
            return None
        free_bytes, _total_bytes = torch.cuda.mem_get_info()
        return int(free_bytes / (1024 * 1024))
    except Exception:
        return None


def _load_parakeet_model(config: ParakeetStreamingConfig) -> Any:
    key = (
        config.model,
        config.device,
        config.dtype,
        config.max_gpu_memory_gb,
        config.cpu_fallback,
        config.eou_token,
    )
    cached = _MODEL_CACHE.get(key)
    if cached is not None:
        return cached

    try:
        import torch
        import nemo.collections.asr as nemo_asr
    except Exception as exc:  # pragma: no cover - exercised through caller error path
        raise RuntimeError(
            "Parakeet Realtime EOU STT dependencies are missing. "
            "Run: hermes tools post-setup parakeet_stt"
        ) from exc

    requested_device = config.device
    if requested_device == "auto":
        requested_device = "cuda" if torch.cuda.is_available() else "cpu"
    if requested_device == "cuda" and not torch.cuda.is_available():
        requested_device = "cpu"

    # VRAM preflight: if there isn't enough free VRAM to even attempt the
    # load, go straight to the existing CPU fallback path instead of
    # provoking a CUDA OOM (and the driver hiccups that can follow one).
    # Free VRAM is treated as "unknown" (proceed as today) on any failure.
    if requested_device == "cuda":
        free_mb = _free_vram_mb()
        if free_mb is not None and free_mb < config.min_free_vram_mb:
            if config.cpu_fallback:
                logger.warning(
                    "Parakeet: only %dMB free VRAM (below %dMB threshold); "
                    "using CPU fallback directly instead of attempting CUDA load",
                    free_mb,
                    config.min_free_vram_mb,
                )
                requested_device = "cpu"
            else:
                logger.warning(
                    "Parakeet: only %dMB free VRAM (below %dMB threshold) but "
                    "cpu_fallback is disabled; attempting CUDA load anyway",
                    free_mb,
                    config.min_free_vram_mb,
                )

    if requested_device == "cuda" and config.max_gpu_memory_gb:
        total_bytes = int(config.max_gpu_memory_gb * 1024**3)
        try:
            torch.cuda.set_per_process_memory_fraction(
                min(1.0, total_bytes / max(1, torch.cuda.get_device_properties(0).total_memory)),
                0,
            )
        except Exception:
            logger.debug("Could not apply Parakeet CUDA memory fraction", exc_info=True)

    logger.info("Loading Parakeet Realtime EOU STT model %s on %s", config.model, requested_device)
    try:
        model = nemo_asr.models.ASRModel.from_pretrained(model_name=config.model)
        if hasattr(model, "to"):
            model = model.to(requested_device)
    except Exception as exc:
        if _is_cuda_driver_error(exc):
            raise RuntimeError(
                "Parakeet CUDA failed because the NVIDIA driver is too old for the installed CUDA runtime. "
                "Update the NVIDIA GPU driver."
            ) from exc
        elif not config.cpu_fallback or requested_device == "cpu" or not _is_memory_load_error(exc):
            raise
        else:
            logger.warning("Parakeet CUDA load failed from memory pressure; retrying on CPU: %s", exc)
        model = nemo_asr.models.ASRModel.from_pretrained(model_name=config.model)
        if hasattr(model, "to"):
            model = model.to("cpu")

    if hasattr(model, "eval"):
        model.eval()
    _MODEL_CACHE[key] = model
    return model


def warm_parakeet_stt(stt_config: dict[str, Any] | None = None) -> bool:
    _load_parakeet_model(resolve_parakeet_config(stt_config))
    return True


def parakeet_venv_python() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "parakeet-venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def parakeet_stdio_command() -> list[str]:
    return [str(parakeet_venv_python()), "-m", "tools.parakeet_streaming_stt", "--stdio"]


def _extract_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, (list, tuple)):
        if not result:
            return ""
        first = result[0]
        return str(getattr(first, "text", first) or "")
    return str(getattr(result, "text", result) or "")


def _strip_eou(text: str, token: str) -> str:
    return text.replace(token, " ").replace("<eou>", " ").strip()


class _CacheAwareStream:
    """Frame-synchronous NeMo cache-aware streaming decode (the fast path).

    NOTE(duplex-phase1): this is the O(n) streaming engine that makes Parakeet
    emit partials + a continuous end-of-utterance signal at ~frame latency
    instead of the O(n^2) re-transcribe. It follows NeMo's
    ``conformer_stream_step`` cache-aware recipe, which is version/model
    specific — so it is *best-effort* and MUST be validated on GPU. If setup or
    any step raises, ``ParakeetStreamingSession`` catches it, logs loudly to
    logs/parakeet-stt.log, and falls back to re-transcribe. Rich logs here let
    you confirm the engine engaged and tune ``stream_chunk_seconds``.
    """

    def __init__(self, model: Any, config: ParakeetStreamingConfig) -> None:
        import torch

        self._torch = torch
        self._model = model
        self.config = config
        self._chunk_samples = max(1, int(config.stream_chunk_seconds * 16000))
        self._buf = np.empty(0, dtype=np.float32)
        self._text = ""
        self._prev_hyp: Any = None
        self._pred_out: Any = None

        try:
            self._device = next(model.parameters()).device
        except Exception:  # pragma: no cover - defensive
            self._device = "cpu"

        self._configure_decoding(model)
        if hasattr(model.encoder, "setup_streaming_params"):
            model.encoder.setup_streaming_params()
        (self._cache_ch, self._cache_t, self._cache_ch_len) = model.encoder.get_initial_cache_state(batch_size=1)
        logger.info(
            "Parakeet cache-aware stream initialised | chunk=%.3fs (%d samples) device=%s",
            config.stream_chunk_seconds,
            self._chunk_samples,
            self._device,
        )

    @staticmethod
    def _configure_decoding(model: Any) -> None:
        """Work around a NeMo RNNT streaming crash.

        The batched greedy decoder's ``loop_labels`` path calls
        ``Hypothesis.merge_``, which does ``self.timestamp.extend(...)`` on a
        dict timestamp → ``AttributeError: 'dict' object has no attribute
        'extend'`` the moment a chunk contains real speech (silence chunks emit
        nothing and slip through). ``loop_labels=False`` uses the frame-loop
        decoder that avoids ``merge_``. Timestamps/alignments are unused here, so
        turn them off too. Defensive: if the decoding cfg lacks these keys we log
        and continue (push() still falls back to re-transcribe on any error).
        """
        try:
            from omegaconf import open_dict

            decoding_cfg = model.cfg.decoding
            with open_dict(decoding_cfg):
                if "greedy" in decoding_cfg:
                    decoding_cfg.greedy.loop_labels = False
                decoding_cfg.compute_timestamps = False
                decoding_cfg.preserve_alignments = False
            model.change_decoding_strategy(decoding_cfg)
            logger.info("Parakeet streaming decoding set (loop_labels=False, timestamps off)")
        except Exception:
            logger.exception("Parakeet: could not adjust streaming decoding; may hit NeMo merge_ bug")

    def _decode_chunk(self, chunk: np.ndarray, *, last: bool) -> str:
        torch = self._torch
        sig = torch.tensor(chunk, dtype=torch.float32, device=self._device).unsqueeze(0)
        sig_len = torch.tensor([chunk.shape[0]], dtype=torch.long, device=self._device)
        with torch.inference_mode():
            processed, processed_len = self._model.preprocessor(input_signal=sig, length=sig_len)
            (
                self._pred_out,
                texts,
                self._cache_ch,
                self._cache_t,
                self._cache_ch_len,
                self._prev_hyp,
            ) = self._model.conformer_stream_step(
                processed_signal=processed,
                processed_signal_length=processed_len,
                cache_last_channel=self._cache_ch,
                cache_last_time=self._cache_t,
                cache_last_channel_len=self._cache_ch_len,
                keep_all_outputs=last,
                previous_hypotheses=self._prev_hyp,
                previous_pred_out=self._pred_out,
                drop_extra_pre_encoded=None,
                return_transcription=True,
            )
        return _extract_text(texts)

    def push(self, samples: np.ndarray) -> tuple[str, float] | None:
        """Feed new samples. Returns (partial_text, eou_prob) when a chunk
        decoded, else None (still buffering toward the next chunk)."""
        incoming = np.asarray(samples, dtype=np.float32)
        self._buf = np.concatenate([self._buf, incoming]) if self._buf.size else incoming
        result: tuple[str, float] | None = None
        while self._buf.shape[0] >= self._chunk_samples:
            chunk = self._buf[: self._chunk_samples]
            self._buf = self._buf[self._chunk_samples :]
            t0 = time.perf_counter()
            text = self._decode_chunk(chunk, last=False)
            dt_ms = (time.perf_counter() - t0) * 1000
            if text:
                self._text = text
            eou_prob = 1.0 if self.config.eou_token.lower() in self._text.lower() else 0.0
            logger.info(
                "Parakeet chunk decoded in %.0fms | eou_prob=%.2f | text=%r",
                dt_ms,
                eou_prob,
                self._text[-80:],
            )
            result = (_strip_eou(self._text, self.config.eou_token), eou_prob)
        return result

    def finish(self) -> str:
        # Flush trick: decode whatever is left immediately for the final.
        if self._buf.shape[0] > 0:
            t0 = time.perf_counter()
            text = self._decode_chunk(self._buf, last=True)
            logger.info("Parakeet flush decoded %d samples in %.0fms", self._buf.shape[0], (time.perf_counter() - t0) * 1000)
            if text:
                self._text = text
            self._buf = np.empty(0, dtype=np.float32)
        return _strip_eou(self._text, self.config.eou_token)


class ParakeetStreamingSession:
    """Session fed by browser Float32 mic frames and finalized with Parakeet EOU.

    Three engines: cheap batch buffering (the default), an explicit whole-buffer
    partial mode for diagnostics, and the cache-aware fast path when requested
    and supported. The full sample buffer is retained for final transcription.
    """

    # Re-transcribe cadence for the FALLBACK engine only. ponytail: O(n^2) over
    # the utterance; fine for short turns. The cache-aware engine replaces this.
    _PARTIAL_INTERVAL_SAMPLES = 8000  # 0.5s at 16 kHz

    def __init__(
        self,
        stt_config: dict[str, Any] | None = None,
        *,
        loader: Callable[[ParakeetStreamingConfig], Any] = _load_parakeet_model,
        temp_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        self.config = resolve_parakeet_config(stt_config)
        self._loader = loader
        self._model: Any = None
        self._stream: _CacheAwareStream | None = None
        self._samples: list[float] = []
        self._since_last = 0
        self._last_partial = ""
        self.last_eou = False
        self.last_eou_prob = 0.0
        self._closed = False
        self._temp_dir = Path(temp_dir) if temp_dir is not None else None

    def start(self) -> None:
        self._model = self._loader(self.config)
        self._stream = self._maybe_start_cache_aware()

    def _maybe_start_cache_aware(self) -> _CacheAwareStream | None:
        # Cache-aware streaming is OPT-IN only (engine=cache_aware). It needs a
        # specific NeMo/CUDA config and currently crashes on some setups
        # (partial_hypotheses + frame-looping + cuda graphs -> NotImplementedError),
        # so the default engines never attempt it — a failed attempt would just
        # burn a GPU decode per turn before falling back.
        if self.config.engine != "cache_aware":
            return None

        model = self._model
        encoder = getattr(model, "encoder", None)
        has_api = (
            hasattr(model, "conformer_stream_step")
            and encoder is not None
            and hasattr(encoder, "get_initial_cache_state")
        )
        if not has_api:
            level = logger.error if self.config.engine == "cache_aware" else logger.warning
            level(
                "Parakeet STT: cache-aware streaming API not found on model %s; "
                "using re-transcribe fallback (higher latency). engine=%s",
                self.config.model,
                self.config.engine,
            )
            return None

        try:
            stream = _CacheAwareStream(model, self.config)
            logger.info("Parakeet STT engine=cache_aware ACTIVE (frame-synchronous streaming)")
            return stream
        except Exception:
            logger.exception("Parakeet STT: cache-aware init failed; using re-transcribe fallback")
            return None

    def accept_samples(self, samples: Iterable[float]) -> None:
        if self._closed:
            return
        clamped = [float(max(-1.0, min(1.0, sample))) for sample in samples]
        self._samples.extend(clamped)
        self._since_last += len(clamped)

    def accept_bytes(self, chunk: bytes) -> str:
        """Accept a Float32 mic chunk; return a fresh partial transcript or ''."""
        samples = np.frombuffer(chunk, dtype=np.float32)
        self.accept_samples(samples)  # retained for finish + fallback
        if self._closed or not self._samples:
            return ""
        if self._model is None:
            self.start()

        # Fast path: cache-aware streaming.
        if self._stream is not None:
            try:
                pushed = self._stream.push(samples)
            except Exception:
                logger.exception("Parakeet cache-aware push failed; switching to re-transcribe fallback")
                self._stream = None
                pushed = None
            if self._stream is not None:
                if pushed is None:
                    return ""  # still buffering toward the next chunk
                text, eou_prob = pushed
                self.last_eou_prob = eou_prob
                self.last_eou = eou_prob >= 1.0
                self._last_partial = text
                return text

        # Live partials via whole-buffer re-transcribe are OPT-IN (engine=rebuffer):
        # they run the model every ~0.5s DURING listening, competing with the wake
        # word + TTS for the GPU (a cause of stutter/freezes). The default (batch/
        # auto) buffers and is decoded only by a VAD-pause probe or finish(),
        # keeping active listening cheap.
        if self.config.engine != "rebuffer":
            return ""
        if self._since_last < self._PARTIAL_INTERVAL_SAMPLES:
            return ""
        self._since_last = 0
        raw = self._transcribe_current()
        self.last_eou = self.config.eou_token.lower() in raw.lower()
        self.last_eou_prob = 1.0 if self.last_eou else 0.0
        self._last_partial = _strip_eou(raw, self.config.eou_token)
        return self._last_partial

    def drain_text(self) -> str:
        return self._last_partial

    def probe(self) -> str:
        """Decode the current buffer without closing the utterance.

        Duplex voice calls this once at a VAD-confirmed pause.  It preserves
        Parakeet's semantic ``<EOU>`` decision while avoiding the old 500 ms
        whole-buffer retranscription loop during active speech.
        """
        if self._closed or not self._samples:
            return self._last_partial
        if self._stream is not None:
            # Cache-aware mode already reports EOU as chunks are pushed.
            return self._last_partial
        if self._model is None:
            self.start()
        raw = self._transcribe_current()
        self.last_eou = self.config.eou_token.lower() in raw.lower()
        self.last_eou_prob = 1.0 if self.last_eou else 0.0
        self._last_partial = _strip_eou(raw, self.config.eou_token)
        self._since_last = 0
        return self._last_partial

    def finish(self) -> str:
        self._closed = True
        if self._stream is not None:
            try:
                return self._stream.finish()
            except Exception:
                logger.exception("Parakeet cache-aware finish failed; re-transcribing buffer")
                self._stream = None
        if not self._samples:
            return ""
        # rebuffer: if the last live partial already covered every sample (no new
        # audio since), reuse it instead of re-transcribing — makes the "finalizing"
        # step instant when the user stops right after a partial.
        if self._last_partial and self._since_last == 0:
            return self._last_partial
        if self._model is None:
            self.start()
        return _strip_eou(self._transcribe_current(), self.config.eou_token)

    def _transcribe_current(self) -> str:
        temp_path = ""
        try:
            import soundfile as sf

            with tempfile.NamedTemporaryFile(
                prefix="hermes-parakeet-", suffix=".wav", dir=self._temp_dir, delete=False
            ) as tmp:
                temp_path = tmp.name
            sf.write(temp_path, np.asarray(self._samples, dtype=np.float32), 16000)
            return _extract_text(self._model.transcribe([temp_path], batch_size=1))
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def close(self) -> None:
        self._closed = True


def _emit_stdio(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _call_with_stdout_on_stderr(fn: Callable[[], Any]) -> Any:
    # NeMo logs to stdout on Windows; stdout is our JSON protocol.
    with redirect_stdout(sys.stderr):
        return fn()


def _configure_logging(debug: bool) -> None:
    """Send rich helper logs to stderr, which the backend tees into
    logs/parakeet-stt.log. Set stt.streaming.parakeet.debug=true for per-chunk
    detail while tuning latency."""
    root = logging.getLogger()
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s parakeet-stt %(levelname)s %(message)s"))
        root.addHandler(handler)
    root.setLevel(logging.DEBUG if debug else logging.INFO)


def _run_stdio_server() -> int:
    # PERSISTENT across utterances: the model loads once (process-level cache) and
    # the process stays alive for every 'start'/'stop' cycle. Previously the
    # backend spawned+killed this per turn, reloading NeMo/CUDA (~12-20s) every
    # time — the cause of "STT takes forever to start" and the GPU thrash that
    # froze the device and starved the wake word. Each 'start' makes a fresh
    # session that reuses the cached model, so only the FIRST start pays the cost.
    session: ParakeetStreamingSession | None = None
    logging_configured = False
    try:
        for line in sys.stdin:
            if not line:
                break
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            event_type = payload.get("type")

            if event_type == "start":
                stt_config = payload.get("stt_config") if isinstance(payload.get("stt_config"), dict) else {}
                cfg = resolve_parakeet_config(stt_config)
                if not logging_configured:
                    _configure_logging(cfg.debug)
                    logging_configured = True
                logger.info(
                    "Parakeet utterance start | model=%s device=%s engine=%s chunk=%.3fs",
                    cfg.model,
                    cfg.device,
                    cfg.engine,
                    cfg.stream_chunk_seconds,
                )
                session = ParakeetStreamingSession(stt_config)
                _call_with_stdout_on_stderr(session.start)
                _emit_stdio({"type": "ready"})

            elif event_type == "audio":
                if session is None:
                    _emit_stdio({"type": "error", "error": "Parakeet helper received audio before start"})
                    continue
                raw = base64.b64decode(str(payload.get("data") or ""))
                partial = _call_with_stdout_on_stderr(lambda: session.accept_bytes(raw))
                eou_prob = getattr(session, "last_eou_prob", 1.0 if session.last_eou else 0.0)
                if partial:
                    _emit_stdio({"type": "partial", "text": partial, "eou": session.last_eou, "eou_prob": eou_prob})
                else:
                    _emit_stdio({"type": "ok", "eou": session.last_eou, "eou_prob": eou_prob})

            elif event_type == "probe":
                if session is None:
                    _emit_stdio({"type": "error", "error": "Parakeet helper received probe before start"})
                    continue
                partial = _call_with_stdout_on_stderr(session.probe)
                _emit_stdio({
                    "type": "probe",
                    "text": partial,
                    "eou": session.last_eou,
                    "eou_prob": session.last_eou_prob,
                })

            elif event_type == "stop":
                final = _call_with_stdout_on_stderr(session.finish) if session is not None else ""
                _emit_stdio({"type": "final", "text": final})
                session = None  # keep the process + model cache warm for the next turn

            elif event_type == "close":
                break

            else:
                _emit_stdio({"type": "error", "error": f"Unknown Parakeet helper event: {event_type}"})
        return 0
    except BaseException as exc:  # noqa: BLE001
        logger.exception("Parakeet stdio helper failed")
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        _emit_stdio({"type": "error", "error": str(exc)})
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parakeet Realtime EOU STT helper")
    parser.add_argument("--stdio", action="store_true", help="Run JSON-lines stdio streaming helper")
    args = parser.parse_args(argv)
    if args.stdio:
        return _run_stdio_server()
    parser.error("no mode selected")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
