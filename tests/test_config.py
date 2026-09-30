from observatory.config import Settings


def _clear(monkeypatch):
    for name in ("OBS_PORT", "PORT", "OBS_SECURE_COOKIES", "OBS_TRUSTED_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("observatory.config.load_dotenv", lambda **_: None)


def test_platform_port_used_when_project_port_unset(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("PORT", "4321")
    assert Settings.from_env().port == 4321


def test_project_port_overrides_platform_port(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("PORT", "4321")
    monkeypatch.setenv("OBS_PORT", "8050")
    assert Settings.from_env().port == 8050


def test_local_defaults_keep_plain_http_cookies(monkeypatch):
    _clear(monkeypatch)
    settings = Settings.from_env()
    assert settings.port == 8050
    assert settings.secure_cookies is False
    assert settings.trusted_proxy == ""


def test_hosted_cookie_and_proxy_settings(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("OBS_SECURE_COOKIES", "true")
    monkeypatch.setenv("OBS_TRUSTED_PROXY", "*")
    settings = Settings.from_env()
    assert settings.secure_cookies is True
    assert settings.trusted_proxy == "*"


def test_private_record_asset_configuration_is_server_only(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("OBS_RECORD_ASSET_ROOT", "/private/mounted-assets")
    monkeypatch.setenv("OBS_RECORD_ASSET_MANIFEST_SHA256", "a" * 64)
    settings = Settings.from_env()
    assert settings.record_asset_root == "/private/mounted-assets"
    assert settings.record_asset_manifest_sha256 == "a" * 64
    assert "/private/mounted-assets" not in repr(settings)


def test_private_record_assets_are_optional_by_default(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.delenv("OBS_RECORD_ASSET_ROOT", raising=False)
    monkeypatch.delenv("OBS_RECORD_ASSET_MANIFEST_SHA256", raising=False)
    settings = Settings.from_env()
    assert settings.record_asset_root == ""
    assert settings.record_asset_manifest_sha256 == ""
