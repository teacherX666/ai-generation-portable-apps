"""生成女巫甜甜圈那套交付物（桌面版）。

改动（用户 2026-10-20 口径）：之前那两张「凭空造的四个新罐子」**作废**，改用
「在图1 基础上、以图1+图3 为参考」生成的淋酱构图图当 @图片5。

产物：
  桌面\\女巫甜甜圈\\
    ① 参考图_四罐淋酱（加进飞书文档）.png
    ② 参考图_备选.png
    ③ 新提示词（复制这段）.txt
    ④ 上一版提示词（对照用）.txt
    ⑤ 怎么做（双击打开）.html
    作废\\  …（旧的纯罐子图 + 旧提示词）

用法：python _diag/export_readable.py
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "_diag" / "anchor"
SCENE = SRC / "scene"
SCENE2 = SRC / "scene8"
DESKTOP = Path.home() / "Desktop" / "女巫甜甜圈"
VOID = DESKTOP / "作废"

HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>女巫甜甜圈 · 下一轮怎么做</title>
<style>
 body{font:15px/1.75 -apple-system,"Microsoft YaHei",sans-serif;max-width:920px;margin:32px auto;padding:0 20px;color:#1f2937}
 h1{font-size:24px} h2{font-size:18px;margin-top:28px;border-left:4px solid #7c3aed;padding-left:10px}
 pre{white-space:pre-wrap;word-break:break-word;background:#f7f7fb;border:1px solid #e5e7eb;border-radius:10px;padding:14px;font-size:13px;line-height:1.7}
 button{font-size:15px;padding:8px 16px;border:0;border-radius:8px;background:#7c3aed;color:#fff;cursor:pointer;margin:6px 0 10px}
 button:hover{filter:brightness(1.1)}
 code{background:#f1f5f9;padding:1px 5px;border-radius:4px}
 img{max-width:100%;border-radius:10px;border:1px solid #e5e7eb}
 .tip{background:#fef3c7;border:1px solid #fde68a;border-radius:8px;padding:10px 12px}
</style></head><body>
<h1>女巫甜甜圈 · 下一轮怎么做</h1>
<div class="tip"><b>第一步</b>：把同目录的 <code>① 参考图_四罐淋酱（加进飞书文档）.png</code> 加进飞书文档
（放在原来 4 张参考图后面）→ 它成为 <code>@图片5</code>。<br>
<b>第二步</b>：审批页 → 任务 T1 →「提示词」框，点下面按钮复制、整段覆盖粘贴。<br>
<b>第三步</b>：设置保持「16 秒 / 720p / 9:16 / 1 条」，提交。<br>
（<code>作废\\</code> 里是我上一轮凭空造的四色罐图，别再用了 —— 就是它导致成片里"开头是图1 的罐子、后面变了"。）</div>

<h2>新提示词（点按钮复制）</h2>
<button onclick="copyPrompt(this)">复制提示词</button>
<pre id="p">__PROMPT__</pre>

<h2>新参考图（右键另存，或直接拖进飞书文档）</h2>
<p><img src="① 参考图_四罐淋酱（加进飞书文档）.png" alt="主用"></p>
<p><img src="② 参考图_备选.png" alt="备选"></p>

<h2>说明</h2>
<pre>__HOWTO__</pre>

<script>
function copyPrompt(btn){
  var text = document.getElementById('p').innerText;
  function done(){ btn.textContent = '已复制'; }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(selectAll);
  } else { selectAll(); }
  function selectAll(){
    var range = document.createRange();
    range.selectNodeContents(document.getElementById('p'));
    var sel = getSelection(); sel.removeAllRanges(); sel.addRange(range);
    btn.textContent = '已选中，请按 Ctrl+C';
  }
}
</script>
</body></html>"""


def extract_prompt(md_path: Path) -> str:
    text = md_path.read_text(encoding="utf-8")
    match = re.search(r"```text\n(.*?)\n```", text, re.S)
    prompt = (match.group(1) if match else text).strip()
    #: 提示词是要**原样发给模型**的：markdown 的强调符号会被当成正文，必须剥掉。
    #: （我两次手工写稿都把它带进去了 —— 这里做成硬过滤，不再靠人记得。）
    if "**" in prompt:
        print(f"  [清理] {md_path.name} 的提示词正文里发现 markdown 加粗符号，已剥掉")
        prompt = prompt.replace("**", "")
    return prompt


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;")


def main() -> int:
    new_md = SRC / "女巫甜甜圈_新提示词_四色罐版.md"
    old_md = SRC / "女巫甜甜圈_上一版提示词.md"
    how_md = SRC / "女巫甜甜圈_下一轮怎么做.md"
    #: 两张参考配合：A = 镜头1 桌面（桌上已是那四只彩罐）、B = 镜头2 漂浮淋酱（一一对应）
    primary = SRC / "scene7" / "v7_1_1.png"
    backup = SRC / "scene8" / "v8_1_1.png"
    quick1 = SRC / "scene7" / "v7_1_1_对应区域.png"
    quick2 = SRC / "scene8" / "v8_1_1_对应区域.png"
    for path in (new_md, old_md, how_md, primary, backup, quick1, quick2):
        if not path.is_file():
            print("缺文件：", path)
            return 1

    DESKTOP.mkdir(parents=True, exist_ok=True)
    VOID.mkdir(parents=True, exist_ok=True)

    # 历史版本一律移进作废（不删）：凭空造的罐子、v2~v6、v8 单图命名、旧编号文件
    for name in ("四色酱罐_候选A.png", "四色酱罐_候选B.png",
                 "① 参考图_四罐淋酱（加进飞书文档）.png", "② 参考图_备选.png",
                 "①b 快速核对_罐子与甜甜圈区域.png", "②b 快速核对_备选区域.png",
                 "① 参考图A_镜头1桌面（桌上已是4只彩罐）.png", "② 参考图B_镜头2漂浮淋酱（一一对应）.png",
                 "①b 核对_镜头1桌面.png", "②b 核对_镜头2漂浮.png",
                 "① 新提示词（复制这段）.txt", "② 上一版提示词（对照用）.txt", "③ 怎么做（双击打开）.html"):
        old = DESKTOP / name
        if old.exists():
            shutil.move(str(old), str(VOID / name))

    shutil.copy2(primary, DESKTOP / "① 参考图A_镜头1桌面（桌上已是4只彩罐）.png")
    shutil.copy2(backup, DESKTOP / "② 参考图B_镜头2漂浮淋酱（一一对应）.png")
    shutil.copy2(quick1, DESKTOP / "①b 核对_镜头1桌面.png")
    shutil.copy2(quick2, DESKTOP / "②b 核对_镜头2漂浮.png")
    (DESKTOP / "③ 新提示词（复制这段）.txt").write_text(
        extract_prompt(new_md), encoding="utf-8-sig"
    )
    (DESKTOP / "④ 上一版提示词（对照用）.txt").write_text(
        extract_prompt(old_md), encoding="utf-8-sig"
    )
    html = (
        HTML_TEMPLATE
        .replace("__PROMPT__", escape(extract_prompt(new_md)))
        .replace("__HOWTO__", escape(how_md.read_text(encoding="utf-8")))
    )
    (DESKTOP / "⑤ 怎么做（双击打开）.html").write_text(html, encoding="utf-8")

    # 清掉旧的编号文件，避免两套并存混淆
    for stale in ("① 新提示词（复制这段）.txt", "② 上一版提示词（对照用）.txt", "③ 怎么做（双击打开）.html"):
        path = DESKTOP / stale
        if path.exists():
            shutil.move(str(path), str(VOID / stale))

    print("已输出到：", DESKTOP)
    for item in sorted(DESKTOP.iterdir()):
        size = "" if item.is_dir() else f"{item.stat().st_size:,} B"
        print(f"  {item.name}  {size}")
    print("作废目录：")
    for item in sorted(VOID.iterdir()):
        print(f"  {item.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
