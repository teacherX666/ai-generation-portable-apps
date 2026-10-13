#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("validate_delivery.py")


class ValidateDeliveryTests(unittest.TestCase):
    def run_validator(self, delivery: str, manifest: dict) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            delivery_path = root / "Shotlist.md"
            manifest_path = root / "script_manifest.json"
            delivery_path.write_text(delivery, encoding="utf-8")
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(SCRIPT), str(delivery_path), "--manifest", str(manifest_path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                check=False,
            )

    def test_valid_delivery_passes(self) -> None:
        manifest = {
            "events": [{"id": "EV-01", "text": "林岚推门进入"}],
            "dialogues": [{"id": "DLG-01", "speaker": "林岚", "text": "你早就知道，对吗？"}],
            "locks": [{"id": "LOCK-01", "text": "照片在右手"}],
        }
        delivery = """# 项目｜Seedance 2.5 分镜与视频提示词
## 剧本锁定
EV-01 林岚推门进入
DLG-01 林岚：“你早就知道，对吗？”
LOCK-01 照片在右手
## 资产
不使用资产图
## 故事板方式
不使用故事板
## 时长
14秒
## 覆盖
EV-01 DLG-01 LOCK-01 已覆盖
## 空间
无需位置图
## 文字分镜
实际格数：3格
## Seedance 2.5 视频提示词
```text
【一句话概述】林岚进入房间并质问对方。
【详细情节】
0—5秒｜路径节点1
画面动作概述：林岚推门进入。
机位：35mm中景。
动作/表演：照片保持在右手。
对白：无。
声音：门轴声。
5—14秒｜路径节点2
画面动作概述：林岚停下质问。
机位：50mm近景。
动作/表演：保持视线。
对白：林岚：“你早就知道，对吗？”
声音：现场声。
```
## 连续性
照片始终在右手。
"""
        result = self.run_validator(delivery, manifest)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RESULT: PASS", result.stdout)

    def test_invalid_delivery_fails(self) -> None:
        manifest = {
            "events": [{"id": "EV-01", "text": "进入"}],
            "dialogues": [{"id": "DLG-01", "speaker": "林岚", "text": "必须逐字出现"}],
            "locks": [],
        }
        delivery = """# Seedance 2.5
EV-01 DLG-01
实际格数：17格
```text
【详细情节】
1—5秒：进入。
7—31秒：离开。
机位：固定。
对白：被改写了。
```
"""
        result = self.run_validator(delivery, manifest)
        self.assertEqual(result.returncode, 1)
        self.assertIn("缺少逐字台词", result.stdout)
        self.assertIn("时间戳不连续", result.stdout)
        self.assertIn("故事板格数17超过上限16", result.stdout)

    def test_long_prompt_has_no_character_limit(self) -> None:
        manifest = {
            "events": [{"id": "EV-01", "text": "林岚进入房间"}],
            "dialogues": [],
            "locks": [],
        }
        long_detail = "空间与动作细节保持连续。" * 400
        delivery = f"""# 项目｜Seedance 2.5 分镜与视频提示词
## 剧本锁定
EV-01 林岚进入房间
## 资产
不使用资产图
## 故事板方式
不使用故事板
## 时长
10秒
## 覆盖
EV-01 已覆盖
## 空间
无需位置图
## 文字分镜
实际格数：1格
## Seedance 2.5 视频提示词
```text
【一句话概述】林岚进入房间。
【详细情节】
0—10秒｜镜头1
画面动作概述：林岚进入房间。{long_detail}
画面构图：中景保持空间关系清楚。
机位：摄影机平稳跟随。
动作/表演：动作连续。
对白：无。
声音：现场声。
```
## 连续性
人物位置与动作连续。
"""
        result = self.run_validator(delivery, manifest)
        self.assertGreater(len(long_detail), 4000)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RESULT: PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
