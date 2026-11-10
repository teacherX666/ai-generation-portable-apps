"""重跑（复用上一版计划）必须**继承清洗台账**，否则素材会"变回带水印原图"（2026-10-28）。

用户报：「我点重新运行素材还是变了」。查下来是这条路径特有的洞：

* 「重新运行」= `clone_run_for_approval` 复用上一版已批准计划 → **不重新入库** →
  没有清洗这一环 → 新 run 的 `watermark-cleanup.json` **是空的**；
* 而预览（`get_reference_file`）与提交（`nodes._clean_media_assets`）都只读**本 run** 的台账
  → 空台账 ⇒ 回退到**带水印的原图** ⇒ 用户看到素材变了。

清洗版文件写在**文档级** `data/runs/<doc_token>/inputs/` 下（与 run 无关），所以整份台账直接继承即可。
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

from feishu_generation_agent.graph import runtime as runtime_module
from feishu_generation_agent.integrations import reference_watermark_clean as module


def test_clone_path_inherits_the_cleanup_ledger() -> None:
    """接线守门：复用计划那条分支里必须调用台账继承（在进入等待审批之前）。"""
    source = inspect.getsource(runtime_module.GraphRuntime.clone_run_for_approval)
    assert "_inherit_cleanup_ledger" in source, "重跑必须继承清洗台账"
    assert source.index("_inherit_cleanup_ledger") < source.index("_graph_aupdate_state"), (
        "继承要发生在进入等待审批之前"
    )


def _fake_runtime(outputs_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(settings=SimpleNamespace(outputs_dir=outputs_dir))


def test_inherited_ledger_is_what_the_preview_and_submit_read(tmp_path: Path) -> None:
    """行为：继承过来的台账就是预览/提交读的那份，且生效路径指向清洗版。"""
    outputs_dir = tmp_path / "outputs"
    doc_dir = tmp_path / "inputs"
    doc_dir.mkdir(parents=True)
    cleaned = doc_dir / "ref-nowm.png"
    cleaned.write_bytes(b"cleaned-bytes")

    source_ledger = outputs_dir / "runs" / "source-run"
    source_ledger.mkdir(parents=True)
    (source_ledger / "watermark-cleanup.json").write_text(
        json.dumps(
            {
                "image-2": {
                    "status": "adopted",
                    "decision": "auto",
                    "cleaned": str(cleaned),
                    "message": "只清除了水印那一小块（确定性修补）",
                    "versions": [
                        {
                            "version": 1,
                            "path": str(cleaned),
                            "adopted": True,
                            "deterministic": True,
                            "content_lost": False,
                        }
                    ],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    fake = _fake_runtime(outputs_dir)
    asyncio.run(
        runtime_module.GraphRuntime._inherit_cleanup_ledger(
            fake, "source-run", "cloned-run"
        )
    )

    inherited = module.read_cleanup_status("cloned-run", outputs_dir)
    assert "image-2" in inherited, "台账必须被继承到新 run"
    assert module.effective_cleanup_path(inherited["image-2"]) == cleaned

    # 源运行没有台账时：不写空文件、也不报错（按原图继续）。
    asyncio.run(
        runtime_module.GraphRuntime._inherit_cleanup_ledger(
            fake, "no-such-run", "cloned-run-2"
        )
    )
    assert not (outputs_dir / "runs" / "cloned-run-2").exists()
