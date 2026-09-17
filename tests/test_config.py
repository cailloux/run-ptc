import logging

import pytest

from app.config import CONFIG_DIR, Settings, load_settings


def test_repo_settings_file_loads():
    assert isinstance(load_settings(), Settings)


def test_unknown_key_is_dropped_with_a_warning(tmp_path, caplog):
    base = (CONFIG_DIR / "settings.yaml").read_text()
    path = tmp_path / "settings.yaml"
    path.write_text(base + "\n    not_a_real_setting: 1\n")

    with caplog.at_level(logging.WARNING):
        settings = load_settings(path)

    assert isinstance(settings, Settings)
    assert "not_a_real_setting" in caplog.text


def test_unknown_network_raises():
    with pytest.raises(ValueError, match="doesnotexist"):
        load_settings(network="doesnotexist")


def test_missing_key_still_raises(tmp_path):
    base = (CONFIG_DIR / "settings.yaml").read_text()
    path = tmp_path / "settings.yaml"
    path.write_text(base.replace("node_spacing_m: 20\n", ""))

    with pytest.raises(TypeError):
        load_settings(path)
