import pytest

from jmercury.validator import measure
from jmercury.validator.measure import WEIGHTS, MeasureUnavailable, check_available, compute_m


def test_weights_match_design():
    assert WEIGHTS == {"performance": 0.20, "accessibility": 0.25, "best-practices": 0.10, "seo": 0.15,
                       "no_hscroll": 0.10, "tel_visible": 0.20}
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)


def test_compute_m():
    perfect = {"performance": 1, "accessibility": 1, "best-practices": 1, "seo": 1}
    assert compute_m(perfect, True, True)[0] == pytest.approx(1.0)
    assert compute_m(perfect, True, False)[0] == pytest.approx(0.8)
    assert compute_m(perfect, False, True)[0] == pytest.approx(0.9)
    m, parts = compute_m({"performance": 0.5, "accessibility": 0.9, "best-practices": None, "seo": 0.8}, True, True)
    assert parts["best-practices"] == 0.0
    assert m == pytest.approx(0.2 * 0.5 + 0.25 * 0.9 + 0.15 * 0.8 + 0.1 + 0.2)


def test_missing_node_is_reported_not_faked(monkeypatch):
    monkeypatch.setattr(measure.shutil, "which", lambda _name: None)
    with pytest.raises(MeasureUnavailable, match="node"):
        check_available()
    with pytest.raises(MeasureUnavailable):
        measure.measure("http://127.0.0.1:9/", no_hscroll=True, tel_visible=True)


def test_missing_lighthouse_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(measure, "lighthouse_bin", lambda: tmp_path / "nope")
    if measure.shutil.which("node") is None:
        pytest.skip("node not installed")
    with pytest.raises(MeasureUnavailable, match="npm ci"):
        check_available()
