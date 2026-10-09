from __future__ import annotations

from datetime import time
from pathlib import Path

import pytest

from cue_notify.settings import CONFIG_FILE_ENV, Settings


def test_nested_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUE_DATABASE__URL", "postgresql+asyncpg://db/cue")
    monkeypatch.setenv("CUE_CHANNELS__SMS__PROVIDER", "twilio")
    monkeypatch.setenv("CUE_CHANNELS__SMS__AUTH_TOKEN", "secret")
    monkeypatch.setenv("CUE_POLICY__QUIET_HOURS__START", "23:00")
    monkeypatch.setenv("CUE_POLICY__QUIET_HOURS__END", "07:00")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.database.url == "postgresql+asyncpg://db/cue"
    assert settings.channels["sms"].provider == "twilio"
    assert settings.channels["sms"].options == {"auth_token": "secret"}
    assert settings.policy.quiet_hours is not None
    assert settings.policy.quiet_hours.start == time(23)


def test_toml_file_with_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "cue.toml"
    config.write_text(
        """
log_level = "DEBUG"

[channels.push]
provider = "fcm"
credentials_file = "/secrets/fcm.json"

[policy]
default_timezone = "Asia/Tashkent"
quiet_hours = { start = "22:00", end = "08:00" }
"""
    )
    monkeypatch.setenv(CONFIG_FILE_ENV, str(config))
    monkeypatch.setenv("CUE_LOG_LEVEL", "WARNING")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.log_level == "WARNING"  # environment wins over the file
    assert settings.channels["push"].options == {"credentials_file": "/secrets/fcm.json"}
    assert settings.policy.default_timezone == "Asia/Tashkent"


def test_invalid_values_are_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown time zone"):
        Settings(policy={"default_timezone": "Mars/Base"})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="lowercase"):
        Settings(channels={"Push Main": {"provider": "fcm"}})  # type: ignore[arg-type]
    monkeypatch.setenv(CONFIG_FILE_ENV, str(tmp_path / "missing.toml"))
    with pytest.raises(FileNotFoundError):
        Settings()
