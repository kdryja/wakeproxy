"""docs/demo.py (the README screenshot) still runs and shows the made-up machine."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_demo_page_builds():
    out = subprocess.run([sys.executable, str(ROOT / "docs" / "demo.py")], capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    assert "const STATUS = " in out.stdout and '"name": "Workstation"' in out.stdout
    assert 'src="data:image/svg+xml;base64,' in out.stdout
