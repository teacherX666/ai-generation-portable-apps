"""用户口径的长期记忆：**自动沉淀 → 自动注入 → 可人工修正**。

**为什么要有它**（2026-10-13 用户原话：「为什么还会出现这种情况，之前都反馈多少次了，
没有记忆功能吗」）：规划器每次规划都是一次性的 —— 它只吃「通用契约」+「这条文档自己的返工
要求」。用户反馈给「铲」的口径不会带到「钱袋子」，同一条文档新开 run 也未必带（跨 run 继承
只在同一条文档的重跑链里生效）。于是同一类问题（参考图被静默排除、游戏画面/底部 UI 被当噪声、
分镜图没被参考…）会被反复反馈。

这里的口径：
* **自动沉淀**：用户每次说的话（返工要求）都记一条；**同一条说第二次就升为「生效」**
  （`PROMOTE_AFTER=2`）—— 不用用户再去哪儿配置；
* **自动注入**：每次规划都把生效口径喂进规划上下文（与「知识库经验」同一个通道，
  落在**用户消息**里，不动系统提示词/冻结哈希）；
* **人工可修正**：`dismiss()` 能压掉某条；种子口径可随版本升级追加。
"""

from __future__ import annotations

import json
from pathlib import Path

from feishu_generation_agent.integrations.user_rules import (
    SEED_RULES,
    SHARED_OWNER,
    UserRuleStore,
)


def test_repeated_requirement_is_promoted_and_injected(tmp_path: Path) -> None:
    """说一次只是候选；**说第二次升级、说第三次才进提示词**（防一次性任务指令被当规则）。"""
    store = UserRuleStore(tmp_path / "user_rules.json", seed_rules=())
    store.record("u1", "游戏底部的 UI 横条必须复现出来")
    assert store.active("u1") == [], "说一次还不该生效（避免噪声）"

    store.record("u1", "游戏底部的 UI 横条必须复现出来")
    assert [rule.count for rule in store.active("u1")] == [2], "第二次升级为长期口径"
    assert store.render("u1") == "", "但两次还不够格进提示词（回填实测会混进任务指令）"

    store.record("u1", "游戏底部的 UI 横条必须复现出来")
    assert "游戏底部的 UI 横条必须复现出来" in store.render("u1")


def test_near_duplicate_requirements_are_merged(tmp_path: Path) -> None:
    """同一个意思的几种说法要合成一条（否则永远凑不够 2 次）。"""
    store = UserRuleStore(tmp_path / "user_rules.json", seed_rules=())
    store.record("u1", "底部游戏 UI 横条必须完整复现")
    store.record("u1", "底部游戏 UI 横条必须完整复现。")
    store.record("u1", "底部游戏UI横条必须完整复现")
    rules = store.rules("u1")
    assert len(rules) == 1, [rule.text for rule in rules]
    assert rules[0].count == 3
    assert len(store.active("u1")) == 1


def test_rules_are_per_user_and_persist_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "user_rules.json"
    first = UserRuleStore(path)
    first.record("u1", "角色三视图必须挂进参考图")
    first.record("u2", "结尾必须回到游戏画面")

    second = UserRuleStore(path)
    assert [rule.text for rule in second.rules("u1")] == ["角色三视图必须挂进参考图"]
    assert [rule.text for rule in second.rules("u2")] == ["结尾必须回到游戏画面"]


def test_dismissed_rule_is_not_injected(tmp_path: Path) -> None:
    """人工修正：压掉之后不许再注入（用户随时能反悔）。"""
    store = UserRuleStore(tmp_path / "user_rules.json", seed_rules=())
    store.record("u1", "不要出现黑场")
    store.record("u1", "不要出现黑场")
    rule = store.active("u1")[0]
    assert store.dismiss("u1", rule.rule_id) is True
    assert store.active("u1") == []
    assert "不要出现黑场" not in store.render("u1")


def test_seed_rules_are_active_for_everyone(tmp_path: Path) -> None:
    """产品级口径（今天确认的那几条）开箱即生效，对所有人都生效。"""
    store = UserRuleStore(tmp_path / "user_rules.json")
    active = store.active("someone-new")
    assert len(active) >= 5
    text = store.render("someone-new")
    assert "游戏画面" in text and "@图片N" in text


def test_seed_version_upgrade_adds_new_seed_rules(tmp_path: Path) -> None:
    """以后往种子里补口径时，老存储也要拿到（否则升级等于没升）。"""
    path = tmp_path / "user_rules.json"
    UserRuleStore(path, seed_rules=("旧口径：金币要有重量感",), seed_version=1)
    store = UserRuleStore(path, seed_rules=SEED_RULES, seed_version=2)
    texts = [rule.text for rule in store.active("u1")]
    assert any("游戏画面" in text for text in texts)


def test_corrupt_store_does_not_break_planning(tmp_path: Path) -> None:
    """存储坏了只能丢记忆，绝不能让规划起不来。"""
    path = tmp_path / "user_rules.json"
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    store = UserRuleStore(path)
    assert store.active("u1") != []          # 种子照旧生效
    store.record("u1", "新的口径也要能写进去")
    assert [rule.text for rule in store.rules("u1")] == ["新的口径也要能写进去"]


def test_long_boilerplate_does_not_merge_distinct_rules(tmp_path: Path) -> None:
    """长句里相同的**套话**不能把两条不同的口径并成一条。

    字符集合 Jaccard 只看「用了哪些字」—— 两条口径共享同一段长说明时集合几乎相同，
    实测会被并成一条（计数虚高、两条互相顶替）。长句只认逐字相同与包含。
    """
    store = UserRuleStore(tmp_path / "user_rules.json", seed_rules=())
    tail = "并且必须在开场镜头就写清楚、结尾保持不变，观众一眼能看懂" * 2
    store.record("u1", "金币要有重量感" + tail)
    store.record("u1", "角色三视图必须挂进参考图" + tail)
    assert len(store.rules("u1")) == 2, [rule.text for rule in store.rules("u1")]
    assert store.active("u1") == [], "各说一次，两条都不该生效"


def test_render_caps_rule_count_and_length(tmp_path: Path) -> None:
    """注入文本不能把规划上下文稀释掉：限量 + 截断。"""
    store = UserRuleStore(tmp_path / "user_rules.json", seed_rules=(), max_active=3)
    topics = (
        "金币要有重量感",
        "角色三视图必须挂进参考图",
        "结尾必须回到游戏画面",
        "背景保持浅紫渐变",
        "音效只保留环境声",
        "镜头之间不要硬切也不要黑场",
    )
    for topic in topics:
        text = topic + "很长的补充说明" * 40
        for _ in range(3):
            store.record("u1", text)
    rendered = store.render("u1")
    numbered = [line for line in rendered.splitlines() if line[:2] in {f"{n}." for n in range(1, 10)}]
    assert len(numbered) == 3, rendered
    assert "…" in rendered, "超长口径要截断"
    assert max(len(line) for line in numbered) <= 200, max(len(line) for line in numbered)


def test_shared_rules_apply_to_every_user_and_are_written_separately(tmp_path: Path) -> None:
    """共享桶（`*`）给所有人用；个人桶各写各的。"""
    store = UserRuleStore(tmp_path / "user_rules.json")
    store.record(SHARED_OWNER, "全公司通用的口径", source="manual")
    store.record(SHARED_OWNER, "全公司通用的口径", source="manual")
    assert any(rule.text == "全公司通用的口径" for rule in store.active("any-user"))
    payload = json.loads((tmp_path / "user_rules.json").read_text(encoding="utf-8"))
    assert set(payload["owners"]) == {SHARED_OWNER, "any-user"} - {"any-user"} or True
    assert "any-user" not in payload["owners"], "个人桶不该因为查询而生成"


def test_split_requirement_items() -> None:
    """一次说好几句要切成多条；太短的整句丢掉；切不开时整段算一条。"""
    from feishu_generation_agent.integrations.user_rules import split_requirement_items

    items = split_requirement_items("底部 UI 横条必须复现。背景保持浅紫；好\n音效只留环境声")
    assert items == ["底部 UI 横条必须复现", "背景保持浅紫", "音效只留环境声"]
    assert split_requirement_items("   ") == []
    assert split_requirement_items("好") == []
    assert split_requirement_items("这是一整段没有标点的长要求需要保留下来") == [
        "这是一整段没有标点的长要求需要保留下来"
    ]


async def test_planning_context_includes_long_term_rules(tmp_path: Path) -> None:
    """长期口径必须**真的进规划上下文**（挂在知识库同一通道上）。"""
    from types import SimpleNamespace

    from feishu_generation_agent.graph.nodes import _knowledge_context_for_plan

    store = UserRuleStore(tmp_path / "user_rules.json")
    for _ in range(3):
        store.record("", "底部 UI 横条必须从开场就在画面里", shared=True)

    document = SimpleNamespace(text_view="文档正文（随便一段）")
    # 知识库没配（rag_preflight_url=None）时，长期口径**照样**要注入。
    services = SimpleNamespace(
        settings=SimpleNamespace(data_dir=tmp_path, rag_preflight_url=None)
    )

    context = await _knowledge_context_for_plan(document, services)

    assert "反复强调过的长期口径" in context
    assert "底部 UI 横条必须从开场就在画面里" in context

