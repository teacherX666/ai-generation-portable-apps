import aiosqlite
import pytest

from feishu_generation_agent.domain.document import VisionDescription
from feishu_generation_agent.storage.repository import Repository


def _description(scene: str) -> VisionDescription:
    return VisionDescription(
        asset_id="asset-1",
        subjects=[],
        scene=scene,
        style="未确认",
        composition="未确认",
        characters=[],
        actions=[],
        visible_text=[],
        colors=[],
        probable_role="视觉参考素材",
        uncertainties=[],
    )


async def test_migration_adds_columns_and_marks_legacy_rows(tmp_path):
    db = tmp_path / "business.sqlite3"
    # 先造一个"Claude 时代"的老库：只有三列，塞一行老 key
    connection = await aiosqlite.connect(db)
    await connection.executescript(
        """
        CREATE TABLE vision_cache (
          cache_key TEXT PRIMARY KEY,
          description_json TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        """
    )
    await connection.execute(
        "INSERT INTO vision_cache (cache_key, description_json, updated_at) "
        "VALUES (?, ?, ?)",
        ("legacy-sha:claude-3-5-sonnet:v1", '{"asset_id":"asset-1","scene":"老行"}', "t0"),
    )
    await connection.commit()
    await connection.close()

    repository = await Repository.open(db)
    try:
        cursor = await repository._connection.execute(
            "SELECT engine_id, schema_version, kind, description_json FROM vision_cache"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        assert len(rows) == 1
        assert rows[0][0] == "frame_v1"
        assert rows[0][1] == "frame_v1"
        assert rows[0][2] == "image"
        assert "老行" in rows[0][3]
    finally:
        await repository.close()


async def test_backfill_is_idempotent_and_does_not_clobber_new_rows(tmp_path):
    db = tmp_path / "business.sqlite3"
    first = await Repository.open(db)
    await first.save_vision_cache(
        "gemini_native:image:m:native_v1:" + "a" * 64,
        _description("原生新行"),
        engine_id="gemini_native",
        schema_version="native_v1",
        kind="image",
    )
    await first.close()

    # 再开一次：回填不能再跑，新行不能被改成 frame_v1
    second = await Repository.open(db)
    try:
        cursor = await second._connection.execute(
            "SELECT engine_id FROM vision_cache WHERE cache_key LIKE 'gemini_native:%'"
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row[0] == "gemini_native"
        cursor = await second._connection.execute("PRAGMA user_version")
        version = await cursor.fetchone()
        await cursor.close()
        assert int(version[0]) == 1
        counts = await second.list_vision_cache_engine_ids()
        assert counts == {"gemini_native": 1}
    finally:
        await second.close()


async def test_legacy_key_never_resolves(tmp_path):
    repository = await Repository.open(tmp_path / "business.sqlite3")
    try:
        assert await repository.get_vision_cache("old-sha:model:v1") is None
    finally:
        await repository.close()
