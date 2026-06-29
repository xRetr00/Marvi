"""Interactive LiveKit wakeword training helper for the Marvi wake word."""

from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes_cli.config import get_hermes_home, load_config, save_config


DEFAULT_VARIANTS = [
    "hey marvi",
    "marvi",
    "marve",
    "marvy",
    "marvie",
    "marfi",
    "marfe",
    "marvey",
]


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def _ask_yes(prompt: str, default: bool = True) -> bool:
    marker = "Y/n" if default else "y/N"
    value = input(f"{prompt} [{marker}]: ").strip().lower()
    if not value:
        return default
    return value in {"y", "yes", "1", "true"}


def _build_config(base_dir: Path, phrases: list[str], quick: bool, size: str) -> dict[str, Any]:
    samples = {
        "n_samples": 240 if quick else 10000,
        "n_samples_val": 60 if quick else 2000,
        "n_background_samples": 0 if quick else 4000,
        "n_background_samples_val": 0 if quick else 800,
        "steps": 800 if quick else 50000,
    }
    return {
        "model_name": "marvi",
        "target_phrases": phrases,
        "data_dir": (base_dir / "data").as_posix(),
        "output_dir": (base_dir / "output").as_posix(),
        **samples,
        "tts_batch_size": 12,
        "learning_rate": 0.0001,
        "model": {
            "model_type": "conv_attention",
            "model_size": size,
        },
        "batch_n_per_class": {
            "positive": 16,
            "adversarial_negative": 16,
        },
    }


def _run(argv: list[str], env: dict[str, str]) -> None:
    print("\n> " + " ".join(argv))
    result = subprocess.run(argv, cwd=ROOT, env=env)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def _ensure_eval_deps(env: dict[str, str]) -> None:
    try:
        __import__("matplotlib")
    except ImportError:
        _run([sys.executable, "-m", "pip", "install", "matplotlib"], env)


def _save_runtime_config() -> None:
    config = load_config()
    voice = config.setdefault("voice", {})
    wake_word = voice.setdefault("wake_word", {})
    wake_word.update({
        "enabled": True,
        "provider": "livekit",
        "model": "livekit-marvi",
        "threshold": wake_word.get("threshold", 0.5),
    })
    save_config(config)


def main() -> None:
    parser = argparse.ArgumentParser(description="Train the LiveKit marvi.onnx wakeword model.")
    parser.add_argument("--yes", action="store_true", help="Use defaults and do not prompt.")
    parser.add_argument("--full", action="store_true", help="Use a production-sized training run.")
    parser.add_argument("--skip-setup", action="store_true", help="Skip LiveKit dependency downloads.")
    args = parser.parse_args()

    print("Marvi LiveKit wakeword trainer")
    home = get_hermes_home()
    base_dir = home / "wakeword-training" / "marvi"
    cache_model = home / "cache" / "livekit-wakeword" / "livekit-marvi" / "marvi.onnx"

    if args.yes:
        phrases = DEFAULT_VARIANTS
        quick = not args.full
        size = "small"
        token = ""
    else:
        raw_phrases = _ask("Wake words, comma-separated", ", ".join(DEFAULT_VARIANTS))
        phrases = [item.strip().lower() for item in raw_phrases.split(",") if item.strip()]
        quick = not _ask_yes("Use full production training", default=False)
        size = _ask("Model size", "small").strip().lower() or "small"
        token = getpass.getpass("Hugging Face token for downloads (optional): ").strip()

    base_dir.mkdir(parents=True, exist_ok=True)
    config_path = base_dir / "marvi-wake.yaml"
    wake_config = _build_config(base_dir, phrases, quick, size)
    config_path.write_text(yaml.safe_dump(wake_config, sort_keys=False), encoding="utf-8")
    print(f"Wrote {config_path}")

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if token:
        env["HF_TOKEN"] = token
        env["HUGGING_FACE_HUB_TOKEN"] = token

    if not args.skip_setup:
        setup_cmd = [sys.executable, "-m", "livekit.wakeword", "setup", "--config", str(config_path)]
        if quick:
            setup_cmd.append("--skip-acav")
        _run(setup_cmd, env)

    _ensure_eval_deps(env)
    _run([sys.executable, "-m", "livekit.wakeword", "run", str(config_path)], env)

    trained_model = base_dir / "output" / "marvi" / "marvi.onnx"
    if not trained_model.exists():
        raise SystemExit(f"Training finished but ONNX was not found at {trained_model}")

    cache_model.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(trained_model, cache_model)
    _save_runtime_config()
    print(f"\nInstalled {cache_model}")
    print("Enabled voice.wake_word.provider=livekit and voice.wake_word.model=livekit-marvi")


if __name__ == "__main__":
    main()
