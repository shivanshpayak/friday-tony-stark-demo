"""Tests for the Vosk model bootstrap."""
import friday.fastpath.model as model_mod


def test_returns_dir_when_model_present(tmp_path, monkeypatch):
    d = tmp_path / "vosk-model-small-en-us-0.15"
    d.mkdir()
    (d / "am").mkdir()
    monkeypatch.setattr(model_mod, "MODEL_DIR", d)
    assert model_mod.ensure_model(download=False) == d


def test_returns_none_when_absent_and_download_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(model_mod, "MODEL_DIR", tmp_path / "nope")
    assert model_mod.ensure_model(download=False) is None


def test_returns_none_when_dir_exists_but_empty(tmp_path, monkeypatch):
    d = tmp_path / "vosk-model-small-en-us-0.15"
    d.mkdir()
    monkeypatch.setattr(model_mod, "MODEL_DIR", d)
    assert model_mod.ensure_model(download=False) is None


def test_download_failure_returns_none_not_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(model_mod, "MODEL_DIR", tmp_path / "nope")
    monkeypatch.setattr(model_mod, "MODEL_ROOT", tmp_path)

    def boom(*a, **kw):
        raise OSError("no network")

    monkeypatch.setattr(model_mod.urllib.request, "urlretrieve", boom)
    assert model_mod.ensure_model(download=True) is None
