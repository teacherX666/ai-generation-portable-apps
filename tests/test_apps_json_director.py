import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "portal"))

from app_spec import load_specs  # noqa: E402


def test_director_spec_registered():
    specs = {s.name: s for s in load_specs(ROOT / "portal" / "apps.json", ROOT)}
    assert "director" in specs
    spec = specs["director"]
    assert spec.port_default == 8895
    assert spec.job_type == "image"
    assert spec.metrics == ("images",)
    assert spec.unit_label == "张"
    assert spec.needs_ark_key is True
    assert (spec.dir_path / "app.py").exists()
