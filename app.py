"""Vercel ASGI entrypoint with writable, instance-local demo storage."""
from pathlib import Path
import os
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
os.environ.setdefault("CARGOMIND_DATA_DIR", str(Path(tempfile.gettempdir()) / "cargomind"))
os.environ.setdefault("CARGOMIND_MCP_LOCAL", "1")
# Serverless instances are suspended between requests; scan via /api/alerts/scan.
os.environ.setdefault("CARGOMIND_WATCHTOWER", "0")

from cargomind.web.server import app  # noqa: E402,F401
