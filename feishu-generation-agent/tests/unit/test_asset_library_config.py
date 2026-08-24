from pathlib import Path

from feishu_generation_agent.config import Settings, _lan_base_url


def test_asset_library_defaults():
    settings = Settings(_env_file=None)
    assert settings.asset_library_db_path == Path("data/asset-library.sqlite3")
    assert settings.asset_library_dir == Path("data/asset-library")
    # 默认值 = 动态探测的本机 LAN IP（服务机 IP 每周变动，不再硬编码
    # 127.0.0.1——那对同事指向的是他们自己的机器）。
    assert settings.asset_base_url == _lan_base_url()
    assert settings.asset_base_url.startswith("http://")
    assert settings.asset_base_url.endswith(":8765")


def test_asset_library_defaults_accepts_env_override():
    settings = Settings(_env_file=None, asset_base_url="https://media.example.com")
    assert settings.asset_base_url == "https://media.example.com"


def test_asset_base_url_strips_trailing_slash():
    settings = Settings(_env_file=None, asset_base_url="https://media.example.com/")
    assert settings.asset_base_url == "https://media.example.com"


def test_asset_public_url_builds_from_base():
    settings = Settings(_env_file=None, asset_base_url="https://media.example.com")
    assert (
        settings.asset_public_url("asset-library/a1.png")
        == "https://media.example.com/asset-library/a1.png"
    )
