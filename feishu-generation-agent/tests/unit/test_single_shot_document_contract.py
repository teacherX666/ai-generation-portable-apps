"""③ 单镜头文档禁止拆分 + ②-a 时长一致性（2026-10-10）。

③ 实测根因（用户报的「刮太空沙」）：文档分镜表**只有 1 行**，生成的提示词却是
`0—4秒｜镜头1 / 4—8秒｜镜头2：…` + 「采用2段镜头快速剪辑」→ 成片精确在 4.0s 硬切
（6 条里 5 条）。要求是**在规划阶段就拒绝并自动重试/修正**，不要事后拦在审批页。

②-a 实测根因（「毛线1」r5）：时长 5→8、正文「预计5秒」没改 → 成片前 4.08 秒画面冻结。
它必须在**审批提交**这条路上也拦得住，所以校验放在 `validate_plan` 的常开路径。
"""

from __future__ import annotations

from types import SimpleNamespace

from feishu_generation_agent.domain.document import DocumentBlock
from feishu_generation_agent.integrations.planner import (
    _shot_split_directives,
    _single_shot_basis,
    _storyboard_shot_count,
    validate_plan,
)


def _table_document(
    rows: int,
    *,
    ps_text: str = "",
    marker_rows: bool = False,
) -> SimpleNamespace:
    """造一个真实形状的分镜表文档：表头行 + `rows` 行编号数据行。

    形状与生产文档一致（`data/checkpoints.sqlite3` 的
    【Sandfall】ai片头需求文档1-张娜-10.9 rev195：table + table_cell(row=0 是表头
    "镜头"/"视角"/…，row=1 起是 "1"/"平视"/…）。
    """
    blocks = [
        DocumentBlock(
            block_id="page-1",
            parent_id=None,
            block_type="page",
            order=0,
            path=["page-1"],
            text="需求文档",
        ),
        DocumentBlock(
            block_id="table-1",
            parent_id="page-1",
            block_type="table",
            order=1,
            path=["page-1", "table-1"],
        ),
    ]
    text_lines = []
    header_labels = ["镜头", "视角", "景别", "运镜", "事件", "氛围/情绪", "PS"]
    for column, label in enumerate(header_labels):
        cell_id = f"hcell-{column}"
        text_id = f"htext-{column}"
        blocks.append(
            DocumentBlock(
                block_id=cell_id,
                parent_id="table-1",
                block_type="table_cell",
                order=2 + column,
                path=["page-1", "table-1", cell_id],
                table_row=0,
                table_column=column,
            )
        )
        blocks.append(
            DocumentBlock(
                block_id=text_id,
                parent_id=cell_id,
                block_type="text",
                order=3 + column,
                path=["page-1", "table-1", cell_id, text_id],
                text=label,
            )
        )
        text_lines.append(f"[block:{text_id}] {label}")
    order = 20
    for row in range(1, rows + 1):
        values = {
            0: "镜头 1：刮开太空沙外层。" if marker_rows else str(row),
            1: "平视",
            2: "特写",
            3: "固定镜头",
            4: "画面中央是绿色沙粒组成的仙人掌。",
            5: "ASMR 般解压",
            6: ps_text,
        }
        for column, value in values.items():
            if not value:
                continue
            cell_id = f"cell-{row}-{column}"
            text_id = f"text-{row}-{column}"
            blocks.append(
                DocumentBlock(
                    block_id=cell_id,
                    parent_id="table-1",
                    block_type="table_cell",
                    order=order,
                    path=["page-1", "table-1", cell_id],
                    table_row=row,
                    table_column=column,
                )
            )
            blocks.append(
                DocumentBlock(
                    block_id=text_id,
                    parent_id=cell_id,
                    block_type="text",
                    order=order + 1,
                    path=["page-1", "table-1", cell_id, text_id],
                    text=value,
                )
            )
            order += 2
            text_lines.append(f"[block:{text_id}] {value}")
    return SimpleNamespace(
        document_id="doc-sand",
        title="刮太空沙",
        revision=195,
        blocks=blocks,
        media_assets=[],
        text_view="\n".join(text_lines),
        ingest_issue_records=[],
    )


def _plan(prompt: str, *, duration: int = 8) -> dict:
    return {
        "document_summary": "刮太空沙的需求文档摘要",
        "tasks": [
            {
                "task_id": "task-1",
                "task_type": "image_to_video",
                "title": "刮太空沙",
                "source_block_ids": ["page-1"],
                "user_intent": "生成解压刮沙视频",
                "prompt": prompt,
                "reference_images": [],
                "aspect_ratio": "9:16",
                "duration": duration,
                "resolution": "720p",
            }
        ],
    }


def _issues(prompt: str, document: SimpleNamespace, **kwargs) -> list[str]:
    return validate_plan(
        _plan(prompt, duration=kwargs.pop("duration", 8)),
        document,
        4,
        enforce_seedance_prompt_contract=True,
        **kwargs,
    )


# --- 文档分镜数识别 ----------------------------------------------------------


def test_storyboard_shot_count_reads_the_real_one_row_shape() -> None:
    assert _storyboard_shot_count(_table_document(1)) == 1
    assert _storyboard_shot_count(_table_document(5)) == 5
    assert _storyboard_shot_count(_table_document(0)) == 0


def test_storyboard_shot_count_is_not_fooled_by_a_ps_cell_mentioning_shots() -> None:
    """真实陷阱（run 1ee2ab1d）：PS 列写「镜头1-3背景音配上…」，而表里其实有 5 行。

    首轮实现把这一句当成"唯一的镜头标记行"，把 5 行分镜读成 1 行 —— 结果反过来拦掉
    了正确的多镜头提示词。所以标记行必须 >= 2 个才认（与 `_storyboard_requirements`
    同一道护栏）。
    """
    document = _table_document(5, ps_text="镜头1-3背景音配上符合婚礼的浪漫乐器音乐")

    assert _storyboard_shot_count(document) == 5


def test_storyboard_shot_count_handles_marker_rows() -> None:
    assert _storyboard_shot_count(_table_document(3, marker_rows=True)) == 3


# --- 「分镜指令」判据（禁止的 vs 放行的） -------------------------------------


def test_shot_split_directives_hit_population() -> None:
    for prompt in (
        "0—4秒｜镜头1：… 4—8秒｜镜头2：…",
        "采用2段镜头快速剪辑，预计8秒。",
        "整片用两段镜头完成。",
        "分为四个镜头依次推进。",
        "按 4 个镜头连续剪辑。",
        "第二镜给出特写。",
        "第 2 个镜头拉到全景。",
        "剪辑节奏明快。",
    ):
        assert _shot_split_directives(prompt), prompt


def test_shot_split_directives_misses_the_accepted_production_prompts() -> None:
    """被生产验收通过的单镜头提示词**一条都不许命中**（这是误伤防护的核心）。"""
    for prompt in (
        # run 9bc3fa4b / 545641d2：单镜头，文档同一行事件里就写着「硬切到另一个角」
        "本段只保留文档分镜表的1个镜头，不额外增加镜头，仅按文档事件做一次硬切。",
        "约第4秒按文档事件要求硬切到立方体另一侧棱角：切点两侧机位一致。",
        "随后画面硬切到另一个角，继续匀速刮下。",
        "全片严格只用一个固定镜头，不增加额外镜头。",
        # 否定式
        "不要出现镜头 2，不要硬切，不要快速剪辑。",
        "全片无剪辑、单镜头连续。",
        # 绝对秒数分段是合法的（同一镜头内的事件）
        "0-2s：起势；2-6s：匀速推进；6-8s：收尾。",
    ):
        assert _shot_split_directives(prompt) == [], prompt


def test_single_shot_basis_three_triggers() -> None:
    assert _single_shot_basis(_table_document(1), "镜头 1（0—8秒）：匀速推进。") == (
        "文档分镜表只有 1 行"
    )
    # 多分镜文档 + 提示词自己声明只有一个镜头 → 仍然拦（自相矛盾）
    assert _single_shot_basis(
        _table_document(5), "全片严格只用一个镜头，不增加额外镜头。"
    )
    # 文档要求一镜到底
    document = _table_document(5)
    document.text_view += "\n本片一镜到底，全程跟拍。"
    assert _single_shot_basis(document, "镜头 1：匀速推进。")
    # 都识别不出来 → None（多分镜文档 + 没声明）
    assert _single_shot_basis(_table_document(5), "镜头 1：A。镜头 2：B。") is None


# --- 规划阶段真的拒绝 --------------------------------------------------------


def test_planning_rejects_splitting_a_one_row_storyboard_document() -> None:
    """③ 的核心：文档只有 1 行分镜 + 提示词写了 2 段镜头 → 规划阶段判不合格。"""
    issues = _issues(
        "0—4秒｜镜头1：刮左上角。4—8秒｜镜头2：硬切到另一个角继续刮。"
        "采用2段镜头快速剪辑。画面稳定、无水印、不要 logo。",
        _table_document(1),
    )

    split = [issue for issue in issues if "分镜表只有 1 行" in issue]
    assert split, f"应当拒绝，实际 issues={issues}"
    assert "镜头2" in split[0] or "镜头" in split[0]


def test_planning_accepts_the_accepted_single_shot_prompt() -> None:
    """同一条需求被验收通过的那版（单镜头 + 同一行事件里的「硬切到另一个角」）必须放行。"""
    issues = _issues(
        "镜头 1（0—8秒）：本段只保留文档分镜表的1个镜头，不额外增加镜头，"
        "约第4秒按文档事件要求硬切到立方体另一侧棱角继续匀速刮下。"
        "画面稳定、无水印、不要 logo。",
        _table_document(1),
    )

    assert [issue for issue in issues if "分镜" in issue] == []


def test_planning_allows_multiple_shots_for_a_multi_row_storyboard() -> None:
    """多分镜文档写「镜头 2」当然是合法的 —— 判据不能一刀切。"""
    issues = _issues(
        "镜头 1（0-4s）：A。镜头 2（4-8s）：B。画面稳定、无水印、不要 logo。",
        _table_document(5),
    )

    assert [issue for issue in issues if "分镜" in issue] == []


# --- ②-a 在审批提交那条路上也拦得住 ------------------------------------------


def test_duration_mismatch_is_blocking_even_without_the_enforce_flag() -> None:
    """②-a-1 放在 `validate_plan` 的**常开**路径：审批页改完时长提交时也拦。

    审批提交走 `revalidate_approval → validate_plan(...)`，**不传**
    `enforce_seedance_prompt_contract` —— 如果新校验挂在那个分支里，r5 那种提交
    就依然会被放行。
    """
    plan = _plan("正面近景固定机位，单镜头一镜到底，预计5秒。", duration=8)
    issues = validate_plan(plan, _table_document(1), 4)

    assert any("5" in issue and "8" in issue for issue in issues), issues
