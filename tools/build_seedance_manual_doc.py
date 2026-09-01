#!/usr/bin/env python3
"""构建飞书文档《Seedance 视频生成 · 使用说明》。

实测验证过的 docx API 要点（2026-08-31，feishu_docx_probe.py 两轮探测）：
- 块型：2=文本 3/4/5=标题1/2/3 12=无序 13=有序 15=引用 19=高亮块 22=分割线
  27=图片 31=表格 32=表格单元格；31/32 只能走 descendant API
- 图片三步：建空图片块（不能带 token）→ upload_all(parent_node=图片块id,
  extra=drive_route_token) → PATCH replace_image；图片按自然尺寸显示，
  故大图先用 sips 缩到宽 ≤1200
- callout：emoji_id 只接受命名值（warning/bulb/info/loudspeaker/pushpin）；
  内容用两步法挂子块
- 表格：markdown 片段走 /documents/blocks/convert 转块，清洗 parent_id 和
  merge_info 后 descendant 插入
- 共享：PATCH /drive/v1/permissions/{doc}/public?type=docx → tenant_editable

用法：/opt/homebrew/bin/python3.12 tools/build_seedance_manual_doc.py
"""
from __future__ import annotations

import json
import re
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "feishu-output-sync"))
from feishu import FeishuClient, FeishuError  # noqa: E402

CFG = json.loads((HERE / "feishu-output-sync" / "config.json").read_text("utf-8"))
ASSETS = HERE / "docs" / "seedance-manual" / "assets"
DOC_TITLE = "Seedance 视频生成 · 使用说明"

client = FeishuClient(CFG["app_id"], CFG["app_secret"], folder_token=CFG.get("folder_token", ""))


# ============================================================
# 文档内容（最终确认版文案）
# ============================================================
# item 元组：
#   ("h2"|"h3", text)
#   ("p", text)                   支持 **粗体** 与 `行内代码`
#   ("quote", text)
#   ("bullet", [text, ...])       每条一个 bullet 块（连续块渲染成同一列表）
#   ("ordered", [text, ...])
#   ("callout", emoji, color, text)  emoji∈{bulb,info,warning,...}, color 1-7
#   ("table", [表头], [[行], ...])
#   ("image", filename)
#   ("divider",)

CONTENT = [
    ("quote", "面向 AI 美术同学：3 分钟上手，之后当工具书查。"),
    ("divider",),
    ("h2", "一、这是什么？"),
    ("p", "Seedance（豆包 Seedance）是字节跳动出品的视频生成模型，接在公司 AI 平台里。你负责两件事——**描述**（写提示词）和**给素材**（参考图/视频），它负责让画面动起来。"),
    ("table",
     ["能力", "通俗解释", "页面里的模式"],
     [["文生视频", "只写一段话，凭空生成视频", "参考生视频"],
      ["图生视频", "给一张图当「开头画面」，让它动起来", "参考生视频"],
      ["视频延长", "给一段已有的视频续开头/续结尾", "视频延长（2.5 专属）"],
      ["视频编辑", "对已有视频做局部修改", "视频编辑（2.5 专属）"]]),
    ("image", "A-portal-entry.png"),
    ("divider",),
    ("h2", "二、界面导览"),
    ("p", "点顶部 **Seedance** 标签进入。整页分三块："),
    ("bullet", ["**左侧**：参数设置 + 存档",
                "**中间**：提示词输入框 + 素材上传区（首尾帧 / 参考图 / 参考视频 / 参考音频）",
                "**右侧**：任务状态（运行中 / 生成历史 / 活动记录）"]),
    ("image", "B-overview.png"),
    ("divider",),
    ("h2", "三、快速上手：3 步出第一条视频"),
    ("h3", "第 1 步：写提示词"),
    ("p", "在中间的 **Prompt** 大框里描述画面。建议按 **主体 → 动作 → 环境 → 风格** 的顺序写："),
    ("quote", "一只橘猫戴着宇航头盔，在月球表面慢跑，身后扬起月尘，电影感，8K 细节，镜头缓慢推近"),
    ("p", "写得不专业没关系，点 **✨ 优化**，AI 帮你润色成模型更「听得懂」的版本。"),
    ("image", "C-prompt.png"),
    ("h3", "第 2 步：（可选）加参考素材"),
    ("bullet", ["想让视频**从某张图开始** → 拖进「首帧」",
                "想**固定人物/物品的长相** → 拖进「参考图」，提示词里写 `@ref_image1` 引用",
                "想**延长/修改一段已有视频** → 切「视频延长」/「视频编辑」模式，原视频拖进「参考视频」"]),
    ("image", "D-upload.png"),
    ("h3", "第 3 步：点「开始生成」，等结果"),
    ("p", "点左侧底部 **开始生成**。任务在公司服务器上后台运行（**关掉页面也照跑**），右侧实时显示进度。完成后视频进入「生成历史」，可在线播放、下载。"),
    ("image", "E1-running.png"),
    ("image", "E2-history.png"),
    ("divider",),
    ("h2", "四、三种任务模式怎么选"),
    ("p", "左侧「参数」区第一项是**任务类型**："),
    ("table",
     ["模式", "什么时候用", "要点"],
     [["参考生视频（默认）", "文生视频 / 图生视频", "时长、比例随便调"],
      ["视频延长", "已有视频，让它向前/向后继续演", "2.5 专属；参数细节见第七节"],
      ["视频编辑", "对已有视频做局部修改（删除、替换、重绘）", "2.5 专属；参数细节见第七节"]]),
    ("callout", "warning", 6,
     "提示词里一旦出现「删除 / 替换 / 续写 / 延长」这类词，模型会自动当作编辑/延长任务处理，而这两类任务的参数要求特殊。**请务必手动切到对应模式再填**，否则任务会被拒。"),
    ("image", "I-taskmode.png"),
    ("divider",),
    ("h2", "五、参考素材怎么用"),
    ("p", "中间面板四个素材区，全部支持**点击选择或直接拖拽**，上传即预览，点「移除」删掉："),
    ("table",
     ["素材区", "上限", "用途", "提示词里怎么引用"],
     [["首帧", "1 张图", "**视频的第一帧**。图生视频的灵魂：给一张海报/角色图/分镜稿，模型从这张图开始「动」", "不需要引用，上传即生效"],
      ["尾帧", "1 张图", "**视频的最后一帧**。控制镜头停在哪，多段衔接时用它「卡点」", "不需要引用"],
      ["参考图", "最多 9 张", "固定人物长相、物品造型、场景、画风", "写 @ref_image1、@ref_image2……按顺序引用"],
      ["参考视频", "最多 3 段", "延长/编辑的原料；或作为运镜、风格参考", "—"],
      ["参考音频", "最多 3 段", "背景音乐、音效、节奏参考", "—"]]),
    ("callout", "warning", 6,
     "**参考视频时长必须在 4–30 秒之间**（模型硬性要求），超了会提示你剪辑后再传。"),
    ("p", "**首尾帧进阶用法**：第 1 段勾「返回尾帧」拿到结尾画面 → 把这张图当第 2 段的「首帧」→ 两段无缝衔接（配合第七节看）。"),
    ("divider",),
    ("h2", "六、参数详解"),
    ("h3", "6.1 画面参数"),
    ("p", "**时长（秒）**：视频长度。"),
    ("bullet", ["2.0 系列模型：4–15 秒；2.5 模型：最长 30 秒",
                "实操建议：**打样 4–8 秒**（快、便宜），**成片 10–15 秒**；时长越长生成越慢",
                "填 `-1` = 时长交给模型定（视频编辑模式会自动这样填）"]),
    ("p", "**分辨率**："),
    ("table",
     ["档位", "适用"],
     [["480p", "最速打样，看构图节奏够用"],
      ["720p", "日常默认，清晰度够发内部预览"],
      ["1080p", "交付/发布用"],
      ["4k", "极致清晰（仅 2.0 标准版支持），生成慢、文件大"]]),
    ("p", "**比例**：先想好成片发哪，再选："),
    ("table",
     ["比例", "用途"],
     [["16:9", "横屏：B站、电脑、电视"],
      ["9:16", "竖屏：抖音、快手、视频号"],
      ["1:1", "方形：小红书、朋友圈"],
      ["4:3 / 3:4", "偏方横/竖屏：海报感"],
      ["21:9 / 9:21", "电影宽银幕 / 超长竖屏"],
      ["adaptive（自适应）", "跟着你上传的参考图/视频的比例走，最省心"]]),
    ("callout", "warning", 6,
     "有参考图时选错比例，画面会被裁剪或拉伸变形。拿不准就选 `adaptive`。"),
    ("p", "**Seed（随机种子）**：可以理解为画面的「基因编号」。"),
    ("bullet", ["相同参数 + 相同 Seed ≈ 同一部片子的高度相似版本；换 Seed = 完全重来",
                "用途 ①：**方向对了、细节不满意** → 固定 Seed 微调提示词重新生成，画面大致不变",
                "用途 ②：想批量出不同候选 → Seed 留空 + 勾上「每次 seed +1」（见 6.3）",
                "留空 = 每次随机"]),
    ("h3", "6.2 功能开关"),
    ("table",
     ["开关", "作用", "建议"],
     [["生成音频", "勾上视频带声音（人物口型、环境音效、氛围音）", "需要口播/现场感就勾；打算后期自己配音配乐就不勾（无声文件也更小）"],
      ["水印", "成片角落加水印", "按需求勾，默认不勾"],
      ["返回尾帧", "除视频外，额外返回「最后一帧图」", "做多段衔接、续拍时勾（见第五节进阶用法）"],
      ["Web Search", "让模型联网搜索辅助理解提示词", "一般用不上，保持默认关"]]),
    ("h3", "6.3 输出与并发"),
    ("table",
     ["参数", "说明"],
     [["输出名", "文件命名。留空 = 自动按日期时间命名；填了则多并发自动加序号：口红广告-1、口红广告-2……"],
      ["重复次数", "**这次一共出几条候选视频**。打样阶段填 3–4 出候选挑片，定稿后填 1 出正式片"],
      ["并发数", "**同时跑几条**。实际生成条数 = 重复次数和并发数里较大的那个（例：重复 4、并发 2 → 共 4 条，两两并行）"],
      ["每次 seed +1", "多条候选**各不相同**的保证，保持勾选"],
      ["轮询秒 / 超时秒", "后台刷新节奏和最长等待时间，默认即可"]]),
    ("divider",),
    ("h2", "七、视频延长 & 视频编辑（Seedance 2.5 专属）"),
    ("callout", "bulb", 1,
     "这两个模式**只有 Seedance 2.5 模型支持**。先把「模型」选到 Seedance 2.5，再切任务类型；用其他模型提交这两个模式会被拒。"),
    ("table",
     ["", "视频延长", "视频编辑"],
     [["干什么", "给视频续开头/续结尾，让剧情继续走", "对视频做局部「手术」：删掉某个东西、换成别的、改局部细节"],
      ["素材", "「参考视频」上传原视频", "「参考视频」上传原视频"],
      ["比例", "自动设为「自适应」（跟原视频一致）", "自动设为「自适应」"],
      ["时长", "**可自定义**：你想要成片多长就填多少（4–30 秒内）", "自动设为「自动」（由模型决定，通常≈原视频长度）"],
      ["提示词", "写清「向前/向后延长」+ 续出来的画面内容", "像给修图师下指令：改哪里、改成什么、其余不变"]]),
    ("p", "**提示词示例**："),
    ("bullet", ["延长：`把这段视频向后延长，镜头继续跟随主角，他走过街道拐角，迎面遇到一只小狗。`",
                "编辑：`把画面中桌上红色的咖啡杯替换成白色马克杯，其余元素保持不变。`"]),
    ("p", "**注意**："),
    ("bullet", ["切到这两个模式后，比例/时长会被自动改好——**不要再手动改动**，否则会被模型拒收",
                "原视频 4–30 秒的限制同样适用",
                "编辑 = 局部重绘，**改动幅度越小越稳**；想大幅改整段画面，请用「参考生视频」+ 原视频当参考素材",
                "延长适合「一个镜头不够长」「结尾想续剧情」；编辑适合「画面里有个多余的东西想抹掉」"]),
    ("image", "J-extend.png"),
    ("divider",),
    ("h2", "八、模型怎么选"),
    ("table",
     ["模型", "特点", "适合"],
     [["Seedance 2.0（标准版）", "质量最高，最高 4k，最长 15 秒", "正式成片"],
      ["Seedance 2.0 fast", "速度快，最高 720p", "快速打样"],
      ["Seedance 2.0 mini（最快）", "最快，最高 720p", "大量试错、跑风格测试"],
      ["Seedance 2.5", "最长 30 秒、最多 50 个素材；唯一支持视频延长/编辑", "长镜头、复杂素材、续拍改片"]]),
    ("callout", "info", 2,
     "切换模型后，时长/分辨率/比例的选项会自动收窄到该模型支持的范围——不是坏了，是帮你提前挡掉会报错的参数。"),
    ("divider",),
    ("h2", "九、多主题并行（顶部标签页）"),
    ("p", "顶部一排标签，每个标签叫一个「主题」，互不干扰："),
    ("bullet", ["点 **+ 新主题** 新建，**双击标签**改名",
                "每个主题有**独立的一套参数和素材草稿**，切换自动保存/恢复",
                "不同主题的任务**可以同时跑**——A 主题排队等结果时，切到 B 主题继续做下一条",
                "标签上的**绿点 = 该主题有任务在跑**；关掉有任务的标签会弹确认（任务其实还在后台跑，不会丢）"]),
    ("image", "F-tabs.png"),
    ("divider",),
    ("h2", "十、✨ 提示词一键优化"),
    ("p", "点 Prompt 框右上角 **✨ 优化**："),
    ("ordered", ["用大白话把你想表达的画面写进去",
                 "AI 润色成更专业的提示词",
                 "满意点 **✅ 一键替换**；不满意点 **↩ 取消**，原文不动"]),
    ("image", "G-optimize.png"),
    ("divider",),
    ("h2", "十一、存档与历史"),
    ("p", "**存档**（左下角）：把当前主题的**全部参数 + 素材**打包存起来、起个名。以后想做「同款」：选中存档 → 点「读取」，参数素材一键回来，跨天、换电脑都有效。"),
    ("p", "**生成历史**（右侧默认页）：每次提交都有记录，显示状态、提示词摘要、耗时；点 **▶** 在线预览，点 **下载** 存到本地（下载时右下角有进度条）。"),
    ("p", "**活动记录**（右侧「活动」标签）：更详细的流水账，点开能看到某次任务的完整参数；**「恢复参数」按钮把那次任务的参数+素材一键填回表单**——「上次那条片子怎么调的来着？」就靠它。"),
    ("image", "H1-archives.png"),
    ("image", "H2-activity.png"),
    ("divider",),
    ("h2", "十二、产出去哪了"),
    ("bullet", ["生成结果统一保存在公司服务机上，**直接从页面「生成历史」点「下载」存到自己电脑**即可（下载时右下角有进度条，能看到实时进度）",
                "服务机上的原文件**只保留 14 天**（定时清理），重要成片请及时下载到本地"]),
    ("divider",),
    ("h2", "十三、常见问题（FAQ）"),
    ("p", "**Q1：提示「参考视频时长需在 4–30 秒之间」？**\n模型硬性要求，用剪辑工具剪到范围内再传。"),
    ("p", "**Q2：为什么我选了某个模型后，分辨率/时长选项变少了？**\n该模型不支持，界面自动收窄（见第六、八节）。"),
    ("p", "**Q3：报「权限不足或配额已用完」？**\n账号额度问题，联系管理员。"),
    ("p", "**Q4：报「请求过于频繁」或「服务暂时不可用」？**\n等几分钟再试即可，一般能自愈。"),
    ("p", "**Q5：视频延长/编辑提交报参数错误？**\n按顺序查三样：模型是不是 2.5 → 比例是不是「自适应」→ 编辑模式的时长是不是「自动」（见第七节）。"),
    ("p", "**Q6：关了页面任务还在跑吗？**\n在。任务在服务端后台运行，回来看「生成历史」就能拿结果。"),
    ("p", "**Q7：同样的提示词，为什么每次出的结果都不一样？**\n正常，模型每次创作都是新的。想固定画面方向，用 Seed（见 6.1）；想批量出不同候选，勾「每次 seed +1」。"),
    ("p", "**Q8：怎么让第二段视频接上第一段的结尾？**\n第一段勾「返回尾帧」→ 把返回的尾帧图拖进第二段的「首帧」→ 提示词描述接下来发生什么。"),
]


# ============================================================
# 构建器
# ============================================================
INLINE_RE = re.compile(r"(\*\*.+?\*\*|`.+?`)")


def parse_inline(text: str) -> list[dict]:
    """**粗体** 与 `行内代码` → text_run 列表。"""
    runs = []
    for part in INLINE_RE.split(text):
        if not part:
            continue
        style = {}
        content = part
        if part.startswith("**") and part.endswith("**"):
            style["bold"] = True
            content = part[2:-2]
        elif part.startswith("`") and part.endswith("`"):
            style["inline_code"] = True
            content = part[1:-1]
        runs.append({"text_run": {"content": content, "text_element_style": style}})
    return runs


def text_elements(text: str) -> list[dict]:
    """支持换行：\n 分段后每段一个 elements 列表（Q&A 两行样式）。"""
    if "\n" in text:
        lines = [l for l in text.split("\n") if l]
        return [{"text_run": {"content": "", "text_element_style": {}}}] if not lines else (
            [e for l in lines for e in parse_inline(l)] +
            [{"text_run": {"content": "", "text_element_style": {}}}])
    return parse_inline(text)


class DocBuilder:
    def __init__(self, doc_id: str):
        self.doc = doc_id
        self.root = doc_id
        self.stats = {"blocks": 0, "images": 0, "tables": 0}

    def _children(self, blocks: list[dict]) -> None:
        client._json_call(
            "POST", f"/open-apis/docx/v1/documents/{self.doc}/blocks/{self.root}/children",
            {"children": blocks})
        self.stats["blocks"] += len(blocks)

    def h2(self, text: str) -> None:
        self._children([{"block_type": 4, "heading2": {"elements": parse_inline(text)}}])

    def h3(self, text: str) -> None:
        self._children([{"block_type": 5, "heading3": {"elements": parse_inline(text)}}])

    def p(self, text: str) -> None:
        self._children([{"block_type": 2, "text": {"elements": text_elements(text), "style": {}}}])

    def quote(self, text: str) -> None:
        self._children([{"block_type": 15, "quote": {"elements": text_elements(text)}}])

    def bullet(self, items: list[str]) -> None:
        for it in items:
            self._children([{"block_type": 12, "bullet": {"elements": text_elements(it)}}])

    def ordered(self, items: list[str]) -> None:
        for it in items:
            self._children([{"block_type": 13, "ordered": {"elements": text_elements(it)}}])

    def divider(self) -> None:
        self._children([{"block_type": 22, "divider": {}}])

    def callout(self, emoji: str, color: int, text: str) -> None:
        # 实测教训（2026-08-31）：
        # - emoji_id 只接受命名值（bulb/info/warning/loudspeaker/pushpin），emoji 字符会 400
        # - 建块时 API 自动塞一个空文本子块；建块后立刻 batch_delete 该子块会静默失败，
        #   且单块 DELETE 端点不存在 → 正确做法是直接对自动子块 batch_update
        #   update_text_elements 写入内容（多行时先写全部行，删多余子块仍不可靠，
        #   故约定 callout 文本为单行）
        r = client._json_call(
            "POST", f"/open-apis/docx/v1/documents/{self.doc}/blocks/{self.root}/children",
            {"children": [{"block_type": 19, "callout": {
                "background_color": color, "emoji_id": emoji}}]})
        co = r["data"]["children"][0]
        self.stats["blocks"] += 1
        kid = co["children"][0]
        client._json_call(
            "PATCH", f"/open-apis/docx/v1/documents/{self.doc}/blocks/batch_update",
            {"requests": [{"block_id": kid, "update_text_elements": {
                "elements": text_elements(text)}}]})

    def table(self, headers: list[str], rows: list[list[str]]) -> None:
        # markdown 片段 → convert → descendant 插入（31/32 只能走这条路）
        md = "| " + " | ".join(headers) + " |\n|" + "---|" * len(headers) + "\n"
        md += "\n".join("| " + " | ".join(r) + " |" for r in rows)
        conv = client._json_call(
            "POST", "/open-apis/docx/v1/documents/blocks/convert",
            {"content_type": "markdown", "content": md})
        data = conv["data"]
        descendants = []
        for b in data["blocks"]:
            b = dict(b)
            b.pop("parent_id", None)
            if b.get("block_type") == 31 and isinstance(b.get("table"), dict):
                prop = dict(b["table"].get("property") or {})
                prop.pop("merge_info", None)  # 只读字段，带着会 400
                b["table"]["property"] = prop
            descendants.append(b)
        client._json_call(
            "POST", f"/open-apis/docx/v1/documents/{self.doc}/blocks/{self.root}/descendant",
            {"children_id": data["first_level_block_ids"], "descendants": descendants, "index": -1})
        self.stats["tables"] += 1

    def image(self, fname: str) -> None:
        src = ASSETS / fname
        prep = prep_image(src)  # 宽 >1200 的缩到 1200，返回 (path, w, h)
        path, w, h = prep
        r = client._json_call(
            "POST", f"/open-apis/docx/v1/documents/{self.doc}/blocks/{self.root}/children",
            {"children": [{"block_type": 27, "image": {"width": w, "height": h}}]})
        bid = r["data"]["children"][0]["block_id"]
        blob = path.read_bytes()
        up = client._multipart(
            "/open-apis/drive/v1/medias/upload_all",
            {
                "file_name": fname,
                "parent_type": "docx_image",
                "parent_node": bid,
                "size": str(len(blob)),
                "extra": json.dumps({"drive_route_token": self.doc}),
            },
            "file", fname, blob, "image/png")
        client._json_call(
            "PATCH", f"/open-apis/docx/v1/documents/{self.doc}/blocks/{bid}",
            {"replace_image": {"token": up["data"]["file_token"]}})
        self.stats["images"] += 1
        self.stats["blocks"] += 1


def png_size(path: Path) -> tuple[int, int]:
    with open(path, "rb") as f:
        f.read(16)
        w, h = struct.unpack(">II", f.read(8))
    return w, h


def prep_image(src: Path) -> tuple[Path, int, int]:
    w, h = png_size(src)
    if w <= 1200:
        return src, w, h
    dst = Path(tempfile.gettempdir()) / f"sd-manual-{src.stem}.png"
    subprocess.run(
        ["sips", "--resampleWidth", "1200", str(src), "--out", str(dst)],
        check=True, capture_output=True)
    nw, nh = png_size(dst)
    return dst, nw, nh


def main() -> int:
    r = client._json_call(
        "POST", "/open-apis/docx/v1/documents",
        {"title": DOC_TITLE, "folder_token": CFG.get("folder_token", "")})
    doc_id = r["data"]["document"]["document_id"]
    print("文档已创建:", doc_id)
    b = DocBuilder(doc_id)
    for item in CONTENT:
        kind = item[0]
        if kind == "h2":
            b.h2(item[1])
        elif kind == "h3":
            b.h3(item[1])
        elif kind == "p":
            b.p(item[1])
        elif kind == "quote":
            b.quote(item[1])
        elif kind == "bullet":
            b.bullet(item[1])
        elif kind == "ordered":
            b.ordered(item[1])
        elif kind == "callout":
            b.callout(item[1], item[2], item[3])
        elif kind == "table":
            b.table(item[1], item[2])
        elif kind == "image":
            b.image(item[1])
        elif kind == "divider":
            b.divider()
        print(f"  [{kind}] done")
    client._json_call(
        "PATCH", f"/open-apis/drive/v1/permissions/{doc_id}/public?type=docx",
        {"link_share_entity": "tenant_editable"})
    print("已设为组织内链接可编辑")
    print("统计:", json.dumps(b.stats, ensure_ascii=False))
    print("DOC_URL=https://feishu.cn/docx/" + doc_id)
    print("DOC_ID=" + doc_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
