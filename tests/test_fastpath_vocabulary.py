"""Guards every fast-path phrase against the real Vosk model's vocabulary.

Vosk silently DROPS words it doesn't know from a grammar — the command simply
never matches, with no runtime error and no log line. "unmute" was exactly this
case (found by the 2026-08-01 spike), which is why the unmute command is
spelled "sound on".

This has to run in a subprocess. On Windows the vosk DLL links its own CRT and
holds a separate stderr from the Python process, so os.dup2 on our fd 2 does
not capture its warnings — but an OS pipe on a child process does.
"""
import json
import re
import subprocess
import sys

import pytest

from friday.fastpath import registry
from friday.fastpath.model import MODEL_DIR

_MISSING_WORD = re.compile(r"missing in vocabulary:\s*'([^']+)'")

_PROBE = """
import json, sys
from vosk import KaldiRecognizer, Model, SetLogLevel
SetLogLevel(-1)
model = Model(sys.argv[1])
KaldiRecognizer(model, 16000, sys.argv[2])
"""


def _missing_words(grammar: list[str]) -> list[str]:
    """Build a grammar recognizer in a child process; return dropped words."""
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, str(MODEL_DIR), json.dumps(grammar)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, f"probe failed: {proc.stderr[-500:]}"
    return sorted(set(_MISSING_WORD.findall(proc.stderr)))


requires_model = pytest.mark.skipif(
    not (MODEL_DIR.is_dir() and any(MODEL_DIR.iterdir())),
    reason="Vosk model not downloaded",
)


@requires_model
def test_probe_detects_a_known_bad_word():
    """Negative control.

    Without this, a probe that silently stopped capturing stderr would make
    the real test below pass vacuously forever.
    """
    missing = _missing_words(["mute", "unmute", "zzzznotaword", "[unk]"])
    assert "unmute" in missing
    assert "zzzznotaword" in missing


@requires_model
def test_every_registry_phrase_is_in_vocabulary():
    missing = _missing_words(registry.grammar_phrases())
    if missing:
        owners = {w: registry.command_for_word(w) for w in missing}
        pytest.fail(
            f"Out-of-vocabulary words in the fast-path grammar: {owners}. "
            "Vosk drops these silently, so those commands can never match. "
            "Pick different wording in friday/fastpath/registry.py."
        )
