import os
import tempfile

import pytest

# Fixed zone: the expected times in the tests are Central European.
os.environ.setdefault("MAILROOM_TIMEZONE", "Europe/Rome")
# Tests never read or write the real data/ folder (database, run lock, schedule, logs).
os.environ.setdefault("MAILROOM_DATA_DIR", tempfile.mkdtemp(prefix="mailroom-test-"))

from mailroom import model_prefs


@pytest.fixture(autouse=True)
def _isolated_model_prefs(tmp_path, monkeypatch):
    """Tests never touch the real config/models.yaml (choices, efforts, served models)."""
    monkeypatch.setattr(model_prefs, "PATH", tmp_path / "models.yaml")
    from mailroom import app_settings
    monkeypatch.setattr(app_settings, "LOCAL_PATH", tmp_path / "app_settings.json")
