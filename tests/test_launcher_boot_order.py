"""First-boot latency: the agent's cold boot (~20s) is the long pole, so the
launcher must spawn it BEFORE doing its own heavy loading, letting the two
overlap.

Measured 2026-09-22: `from openwakeword.model import Model` alone took 12.7s
(openwakeword.custom_verifier_model), and it sat at the top of friday_launcher.py,
so the agent could not even be spawned until it finished. Then launcher_loop
loaded the wake-word model, Resemblyzer and Vosk (~7s more) before spawning it.
"""
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_importing_launcher_does_not_import_openwakeword():
    code = (
        "import friday_launcher, sys\n"
        "print('LOADED' if 'openwakeword.model' in sys.modules else 'LAZY')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                         capture_output=True, text=True, timeout=240)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "LAZY"


def test_agent_boot_starts_before_launcher_loads_its_models():
    src = (REPO / "friday_launcher.py").read_text(encoding="utf-8")
    loop = src[src.index("async def launcher_loop"):]
    boot = loop.index("boot_with_retries(")
    for heavy in ("WakeWordListener()", "SpeakerVerifier()", "TailRecognizer()"):
        assert boot < loop.index(heavy), (
            f"agent boot must be kicked off before {heavy} so the two overlap"
        )
