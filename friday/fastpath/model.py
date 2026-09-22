"""One-time bootstrap for the Vosk small-English recognition model.

The model is ~40 MB and lives untracked under `models/`. Every failure path
returns None, which disables the fast path for that run — the launcher then
behaves exactly as it did before this feature existed.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

logger = logging.getLogger("friday-launcher")

MODEL_NAME = "vosk-model-small-en-us-0.15"
MODEL_URL = f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip"
MODEL_ROOT = Path(__file__).parents[2] / "models"
MODEL_DIR = MODEL_ROOT / MODEL_NAME


def _looks_valid(path: Path) -> bool:
    """A usable Vosk model dir is non-empty. Vosk itself validates the rest."""
    return path.is_dir() and any(path.iterdir())


def ensure_model(download: bool = True) -> Path | None:
    """Return the model directory, downloading it once if missing.

    Returns None if the model is unavailable for any reason. Never raises.
    """
    if _looks_valid(MODEL_DIR):
        return MODEL_DIR
    if not download:
        return None

    logger.info("Vosk model missing — downloading %s (~40 MB, one time)", MODEL_NAME)
    try:
        MODEL_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "model.zip"
            urllib.request.urlretrieve(MODEL_URL, archive)
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(MODEL_ROOT)
    except Exception as e:
        logger.warning("Vosk model download failed (%s) — fast path disabled", e)
        shutil.rmtree(MODEL_DIR, ignore_errors=True)
        return None

    if not _looks_valid(MODEL_DIR):
        logger.warning("Vosk archive extracted but %s is empty — fast path disabled",
                       MODEL_DIR)
        return None

    logger.info("Vosk model ready at %s", MODEL_DIR)
    return MODEL_DIR
