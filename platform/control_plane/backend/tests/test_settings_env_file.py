"""``Settings`` reads the dotenv ``ENV_FILE`` names: unset means ``.env`` in the working directory, empty
means none. A launcher that hands the backend ``ENV_FILE=""`` therefore runs on the environment alone,
even when a ``.env`` sits in the backend directory. Each test is a subprocess, because the dotenv path is
fixed when ``core.config`` is imported.
"""

import subprocess
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_PRINT_REGISTRY_ID = "from core.config import settings\nprint(settings.AGENT_REGISTRY_ID)\n"


def _registry_id_seen_by_settings(tmp_path: Path, **env_file: str) -> str:
    (tmp_path / ".env").write_text("AGENT_REGISTRY_ID=fromdotenv\n")
    proc = subprocess.run(
        [sys.executable, "-c", _PRINT_REGISTRY_ID],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(_BACKEND_DIR / "src"), **env_file},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    return proc.stdout.strip()


def test_an_empty_env_file_reads_no_dotenv(tmp_path):
    assert _registry_id_seen_by_settings(tmp_path, ENV_FILE="") == ""


def test_an_unset_env_file_reads_the_default_dotenv(tmp_path):
    assert _registry_id_seen_by_settings(tmp_path) == "fromdotenv"
