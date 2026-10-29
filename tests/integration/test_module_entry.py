from __future__ import annotations

import subprocess
import sys

import blackbox


def test_python_dash_m_entry_point_runs() -> None:
    done = subprocess.run(
        [sys.executable, "-m", "blackbox", "--version"], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0
    assert done.stdout.strip() == f"blackbox {blackbox.__version__}"
