"""Test-session setup.

friday_launcher opens a log file handler at import time. Point it at a throwaway
directory before any test imports it (subprocess tests inherit the env), so the
suite never writes into the real logs/friday.log.
"""
import os
import tempfile

os.environ.setdefault("FRIDAY_LOG_DIR", tempfile.mkdtemp(prefix="friday-test-logs-"))
