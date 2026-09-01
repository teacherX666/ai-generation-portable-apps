import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "portal"))

from app_spec import load_specs  # noqa: E402


def test_previz_spec_registered():
    specs = {s.name: s for s in load_specs(ROOT / "portal" / "apps.json", ROOT)}
    assert "previz" in specs
    spec = specs["previz"]
    assert spec.port_default == 8896
    assert spec.job_type == "dynamic"
    assert spec.credential_scheme == "none"
    assert spec.mount == "iframe"
    assert (spec.dir_path / "app.py").exists()
