"""``hermes voice`` -- speaker enrollment for Marvi's duplex voice loop.

Self-contained module: builds its own argparse subparser and dispatches
internally (mirrors ``hermes_cli/presence_cmd.py``), so wiring it into
``hermes_cli/main.py`` is two lines (import + ``add_parser(subparsers)``).

Subcommands:
  enroll    -- record (or read a WAV) and enroll a speaker embedding for
               ``<name>`` in ``~/.hermes/voice/speakers.json``.
  speakers  -- list enrolled speakers, or remove one with ``--remove``.

Enrollment/verification logic lives in ``tools/voice_speaker_id.py``; this
module is just the CLI surface + audio-in-the-door handling (a WAV file, or
a short recording via ``tools.voice_mode.AudioRecorder`` when no ``--wav``
was given and a microphone is available).
"""

from __future__ import annotations

import wave
from pathlib import Path
from typing import Optional

from hermes_cli.colors import Colors, color

REQUIRED_SAMPLE_RATE = 16000
REQUIRED_SAMPLE_WIDTH = 2  # 16-bit PCM


def _print_line(ok: bool, message: str) -> None:
    mark = color("✓", Colors.GREEN) if ok else color("✗", Colors.RED)
    print(f"  {mark} {message}")


def _read_wav_pcm16_16k_mono(path: Path) -> bytes:
    """Read a WAV file and return its raw PCM16 bytes.

    Requires 16 kHz, 16-bit PCM already -- stereo is downmixed to mono, but
    the sample rate is NOT resampled (that's real DSP scope this CLI
    deliberately doesn't take on). A file at the wrong rate gets a clear
    error telling the user to convert it (e.g. with ffmpeg) instead of a
    silently-wrong embedding.
    """
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        rate = wav.getframerate()
        sampwidth = wav.getsampwidth()
        frames = wav.readframes(wav.getnframes())

    if sampwidth != REQUIRED_SAMPLE_WIDTH:
        raise ValueError(
            f"{path} is {sampwidth * 8}-bit audio; speaker enrollment needs "
            "16-bit PCM. Convert it first, e.g.: "
            f"ffmpeg -i {path} -ar 16000 -ac 1 -sample_fmt s16 out.wav"
        )
    if rate != REQUIRED_SAMPLE_RATE:
        raise ValueError(
            f"{path} is {rate} Hz; speaker enrollment needs 16000 Hz. "
            f"Convert it first, e.g.: ffmpeg -i {path} -ar 16000 -ac 1 out.wav"
        )
    if channels == 1:
        return frames
    if channels == 2:
        import array

        samples = array.array("h")
        samples.frombytes(frames)
        mono = array.array(
            "h", ((samples[i] + samples[i + 1]) // 2 for i in range(0, len(samples) - 1, 2))
        )
        return mono.tobytes()
    raise ValueError(f"{path} has {channels} channels; expected mono or stereo")


def _record_wav_via_microphone(seconds: float) -> Optional[Path]:
    """Record ``seconds`` of 16 kHz mono audio via the existing sounddevice-
    backed recorder (``tools.voice_mode.AudioRecorder``) -- the simplest
    available capture path. Returns ``None`` (never raises) when no
    microphone/sounddevice is available so the caller can fall back to
    requiring ``--wav``.
    """
    import time

    try:
        from tools.voice_mode import AudioRecorder
    except Exception:
        return None

    recorder = AudioRecorder()
    try:
        recorder.start()
    except Exception:
        return None

    try:
        time.sleep(max(0.5, seconds))
    finally:
        try:
            wav_path = recorder.stop()
        except Exception:
            wav_path = None
        try:
            recorder.shutdown()
        except Exception:
            pass

    return Path(wav_path) if wav_path else None


def _cmd_enroll(args) -> int:
    from tools.voice_speaker_id import SpeakerIdUnavailable, enroll

    name = (args.name or "").strip()
    if not name:
        print(color("  A speaker name is required.", Colors.RED))
        return 1

    wav_path: Optional[Path] = Path(args.wav).expanduser() if args.wav else None
    recorded_path: Optional[Path] = None

    if wav_path is None:
        print(f"  Recording {args.seconds:g}s of audio for {color(name, Colors.CYAN)}...")
        recorded_path = _record_wav_via_microphone(args.seconds)
        if recorded_path is None:
            print(
                color(
                    "  No microphone capture path is available here. "
                    "Pass a recording instead: hermes voice enroll "
                    f"{name} --wav path/to/clip.wav",
                    Colors.RED,
                )
            )
            return 1
        wav_path = recorded_path

    if not wav_path.exists():
        print(color(f"  WAV file not found: {wav_path}", Colors.RED))
        return 1

    try:
        pcm = _read_wav_pcm16_16k_mono(wav_path)
    except (ValueError, wave.Error) as exc:
        print(color(f"  {exc}", Colors.RED))
        return 1
    finally:
        if recorded_path is not None:
            try:
                recorded_path.unlink(missing_ok=True)
            except OSError:
                pass

    try:
        store = enroll(name, pcm)
    except SpeakerIdUnavailable as exc:
        print(color(f"  Enrollment failed: {exc}", Colors.RED))
        return 1

    is_owner = store.get("owner") == name.strip().lower()
    _print_line(True, f"Enrolled speaker {color(name, Colors.CYAN)}" + (" (owner)" if is_owner else ""))
    return 0


def _cmd_speakers(args) -> int:
    from tools.voice_speaker_id import list_speakers, remove_speaker

    if args.remove:
        removed = remove_speaker(args.remove)
        if removed:
            _print_line(True, f"Removed speaker {color(args.remove, Colors.CYAN)}")
            return 0
        print(color(f"  No enrolled speaker named {args.remove!r}", Colors.RED))
        return 1

    speakers = list_speakers()
    print()
    print(color("  Marvi Voice Speakers", Colors.MAGENTA))
    print(color("  ─────────────────────", Colors.MAGENTA))
    if not speakers:
        print("  No speakers enrolled yet. Run: hermes voice enroll <name>")
        print()
        return 0

    for entry in speakers:
        tag = color(" (owner)", Colors.GREEN) if entry["is_owner"] else ""
        plural = "embedding" if entry["embeddings"] == 1 else "embeddings"
        print(f"  {entry['name']}{tag} -- {entry['embeddings']} {plural}")
    print()
    return 0


def voice_command(args) -> int:
    sub = getattr(args, "voice_command", None)
    if sub == "enroll":
        return _cmd_enroll(args)
    if sub == "speakers":
        return _cmd_speakers(args)
    print("Usage: hermes voice enroll <name> [--wav PATH] [--seconds N]")
    print("       hermes voice speakers [--remove NAME]")
    return 1


def add_parser(subparsers) -> None:
    """Register ``hermes voice`` on the given argparse subparsers object."""
    voice_parser = subparsers.add_parser(
        "voice",
        help="Marvi voice speaker enrollment (owner/guest recognition for duplex voice)",
        description=(
            "Enroll and manage the speaker embeddings Marvi's duplex voice "
            "loop uses to tell owner from guest. Run with no subcommand for "
            "usage."
        ),
    )
    voice_sub = voice_parser.add_subparsers(dest="voice_command")

    enroll_parser = voice_sub.add_parser(
        "enroll",
        help="Enroll a speaker (records a short clip, or use --wav)",
    )
    enroll_parser.add_argument("name", help="Speaker name (first enrolled name becomes 'owner')")
    enroll_parser.add_argument("--wav", help="Path to a 16kHz mono 16-bit WAV clip to enroll instead of recording")
    enroll_parser.add_argument(
        "--seconds", type=float, default=6.0, help="Recording length in seconds when --wav is not given (default: 6)"
    )

    speakers_parser = voice_sub.add_parser(
        "speakers",
        help="List enrolled speakers, or remove one with --remove",
    )
    speakers_parser.add_argument("--remove", help="Remove the named speaker")

    voice_parser.set_defaults(func=voice_command)
