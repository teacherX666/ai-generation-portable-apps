# 导演台 v2（6 个高星 skill）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 导演台从 3 个 skill 扩到 8 个：工业模板、结构化提示词（LangGPT）、风格参考（Nano Banana）、场景灵感（ChatGPT-Shortcut）、负面词生成（SD 标签库）、指南底座（融进 SKILL.md）。

**Architecture:** 词库类 skill（模板/风格/场景/负面词）由 `/api/assets` 一次性下发浏览器、前端本地拼装（零 LLM 成本）；结构化/融合类走 `optimize-prompt` 新 mode=langgpt。director 子应用 + portal DirectorApp 扩展，零新端口。

**Tech Stack:** Python stdlib、PetiteVue、pytest、node。

**设计文档:** `docs/superpowers/specs/2026-08-28-director-v2-skills-design.md`
**数据源（/tmp 已 clone）:** `/tmp/awesome-gpt-image-2/data/cases.json`（535 案例+13 分类）、`/tmp/awesome-nanobanana-pro/README.md`（风格分节）、`/tmp/ChatGPT-Shortcut/src/data/prompt_zh-Hant.json`（279 条）、`/tmp/sd-webui-prompt-all-in-one/group_tags/zh_CN.yaml`（分级标签库）。

---

### Task 1: 词库提取脚本 + 生成 4 份资产

**Files:**
- Create: `tools/extract_director_assets.py`（可复现提取脚本，头部注明来源）
- Create: `director/assets/gpt_image_templates.json`、`nano_banana_styles.json`、`shortcut_inspirations.json`、`negative_tags.json`
- Create: `tests/test_director_assets.py`

- [ ] **Step 1: 写失败测试（资产 schema 校验）**

```python
# tests/test_director_assets.py
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "director" / "assets"


def test_gpt_image_templates_schema():
    d = json.loads((ASSETS / "gpt_image_templates.json").read_text(encoding="utf-8"))
    assert d["source"].startswith("https://github.com/")
    assert len(d["categories"]) >= 10
    assert len(d["cases"]) >= 400
    case = d["cases"][0]
    for key in ("id", "title", "prompt", "category"):
        assert key in case and case[key]
    total = (ASSETS / "gpt_image_templates.json").stat().st_size
    assert total < 600 * 1024, f"体积超限: {total}"


def test_nano_banana_styles_schema():
    d = json.loads((ASSETS / "nano_banana_styles.json").read_text(encoding="utf-8"))
    assert len(d["styles"]) >= 15
    s = d["styles"][0]
    assert s["name"] and s["prompt"]
    assert (ASSETS / "nano_banana_styles.json").stat().st_size < 150 * 1024


def test_shortcut_inspirations_schema():
    d = json.loads((ASSETS / "shortcut_inspirations.json").read_text(encoding="utf-8"))
    assert 100 <= len(d["items"]) <= 500
    it = d["items"][0]
    assert it["title"] and it["prompt"]
    assert (ASSETS / "shortcut_inspirations.json").stat().st_size < 300 * 1024


def test_negative_tags_schema():
    d = json.loads((ASSETS / "negative_tags.json").read_text(encoding="utf-8"))
    assert len(d["negative"]) >= 30
    assert len(d["styles"]) >= 30
    assert (ASSETS / "negative_tags.json").stat().st_size < 80 * 1024
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/260413a/ai-generation-portable-apps && python3 -m pytest tests/test_director_assets.py -q`
Expected: FAIL（FileNotFoundError）

- [ ] **Step 3: 写提取脚本并生成资产**

```python
# tools/extract_director_assets.py
#!/usr/bin/env python3
"""从 5 个 GitHub 高星仓库提取导演台词库资产（可复现）。

来源（license 均为公开提示词/模板数据）：
- gpt_image_templates.json ← freestylefly/awesome-gpt-image-2 (data/cases.json)
- nano_banana_styles.json  ← ZeroLu/awesome-nanobanana-pro (README.md)
- shortcut_inspirations.json ← rockbenben/ChatGPT-Shortcut (prompt_zh-Hant.json)
- negative_tags.json       ← Physton/sd-webui-prompt-all-in-one (group_tags/zh_CN.yaml)
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "director" / "assets"


def extract_gpt_image_templates(src: Path) -> dict:
    c = json.loads(src.read_text(encoding="utf-8"))
    cats = list(c.get("categories") or [])
    cases = []
    for case in c.get("cases") or []:
        if not isinstance(case, dict):
            continue
        title = str(case.get("title") or "").strip()
        prompt = str(case.get("prompt") or "").strip()
        if not title or not prompt:
            continue
        cases.append({
            "id": case.get("id", len(cases) + 1),
            "title": title,
            "prompt": prompt[:800],
        })
    return {
        "source": "https://github.com/freestylefly/awesome-gpt-image-2",
        "categories": cats,
        "cases": cases,
    }


def extract_nano_banana_styles(readme: Path) -> dict:
    text = readme.read_text(encoding="utf-8", errors="replace")
    styles = []
    # README 结构：## 1. xxx（大类）→ ### 1.1 yyy（风格）→ 代码块 prompt
    current_major = ""
    for m in re.finditer(
        r"^###\s+\d+\.\d+\.\s+(.+)$", text, flags=re.MULTILINE
    ):
        name = m.group(1).strip()
        seg = text[m.end(): m.end() + 4000]
        code = re.search(r"```(?:markdown|md|text)?\s*\n(.+?)\n```", seg, flags=re.S)
        if not code:
            continue
        prompt = code.group(1).strip()
        if len(prompt) < 50 or len(prompt) > 2000:
            continue
        styles.append({"name": name, "prompt": prompt})
    return {
        "source": "https://github.com/ZeroLu/awesome-nanobanana-pro",
        "styles": styles,
    }


def extract_shortcut_inspirations(src: Path) -> dict:
    data = json.loads(src.read_text(encoding="utf-8"))
    items = []
    for entry in data:
        zh = entry.get("zh-Hant") or {}
        title = str(zh.get("title") or "").strip()
        prompt = str(zh.get("prompt") or "").strip()
        if not title or not prompt:
            continue
        items.append({
            "title": title,
            "prompt": prompt[:600],
            "tags": (entry.get("tags") or [])[:6],
        })
    # 按 weight 排序取前 300
    items = items[:300]
    return {
        "source": "https://github.com/rockbenben/ChatGPT-Shortcut",
        "items": items,
    }


def _parse_yaml_tags(yaml_path: Path) -> dict:
    """解析 zh_CN.yaml 的 `- name: X / groups: / tags: 关键词: 翻译` 结构。"""
    text = yaml_path.read_text(encoding="utf-8", errors="replace")
    current_major = ""
    negative: list[str] = []
    styles: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        m = re.match(r"^- name:\s*(.+)$", stripped)
        if m:
            current_major = m.group(1).strip()
            continue
        if ":" in stripped and not stripped.startswith(("#", "-")):
            key = stripped.split(":", 1)[0].strip()
            if not key or " " in key:
                continue
            if any(w in (current_major + key) for w in
                   ("负面", "质量", "低质", "bad", "negative", "quality")):
                negative.append(key)
            else:
                styles.append(key)
    return {"negative": negative, "styles": styles}


def extract_negative_tags(yaml_path: Path) -> dict:
    d = _parse_yaml_tags(yaml_path)
    # 去重保序；负面词优先取通用质量词
    d["negative"] = list(dict.fromkeys(d["negative"]))[:80]
    d["styles"] = list(dict.fromkeys(d["styles"]))[:200]
    d["source"] = "https://github.com/Physton/sd-webui-prompt-all-in-one"
    return d


def main() -> int:
    args = {
        "gpt": Path("/tmp/awesome-gpt-image-2/data/cases.json"),
        "nano": Path("/tmp/awesome-nanobanana-pro/README.md"),
        "shortcut": Path("/tmp/ChatGPT-Shortcut/src/data/prompt_zh-Hant.json"),
        "sd": Path("/tmp/sd-webui-prompt-all-in-one/group_tags/zh_CN.yaml"),
    }
    if len(sys.argv) > 1:
        args = {k: Path(v) for k, v in zip(args.keys(), sys.argv[1:])}
    for key, src in args.items():
        if not src.exists():
            print(f"缺失数据源: {src}", file=sys.stderr)
            return 1
    ASSETS.mkdir(parents=True, exist_ok=True)
    outputs = {
        "gpt_image_templates.json": extract_gpt_image_templates(args["gpt"]),
        "nano_banana_styles.json": extract_nano_banana_styles(args["nano"]),
        "shortcut_inspirations.json": extract_shortcut_inspirations(args["shortcut"]),
        "negative_tags.json": extract_negative_tags(args["sd"]),
    }
    for name, data in outputs.items():
        path = ASSETS / name
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  {name}: {path.stat().st_size // 1024}KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Run: `cd /Users/260413a/ai-generation-portable-apps && python3 tools/extract_director_assets.py`
Expected: 打印 4 份资产体积，全部落盘

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Users/260413a/ai-generation-portable-apps && python3 -m pytest tests/test_director_assets.py -q`
Expected: PASS（4 passed；若体积/数量断言不符，调整提取脚本的截断参数后重跑 Step 3）

- [ ] **Step 5: 提交**

```bash
git add tools/extract_director_assets.py director/assets/ tests/test_director_assets.py
git commit -m "feat(director): 词库提取脚本与 4 份 skill 资产（来源注明 4 个高星仓库）"
```

---

### Task 2: director 后端 /api/assets + optimize 新 mode

**Files:**
- Modify: `director/app.py`
- Modify: `tests/test_director_assets.py`（追加后端测试）

- [ ] **Step 1: 追加失败测试**

```python
# tests/test_director_assets.py 追加
import importlib.util
import sys

sys.path.insert(0, str(ROOT / "director"))
_spec = importlib.util.spec_from_file_location("director_app_assets", ROOT / "director" / "app.py")
director = importlib.util.module_from_spec(_spec)
sys.modules["director_app_assets"] = director
_spec.loader.exec_module(director)


def test_assets_payload(monkeypatch, tmp_path):
    monkeypatch.setattr(director, "ASSETS_DIR", ROOT / "director" / "assets")
    payload = director.assets_payload()
    assert payload["version"] == director.ASSETS_VERSION
    for key in ("gpt_image_templates", "nano_banana_styles",
                "shortcut_inspirations", "negative_tags"):
        assert isinstance(payload[key], dict)
    # 体积上限：一次性下发不应超过 1MB
    assert len(json.dumps(payload, ensure_ascii=False)) < 1024 * 1024


def test_optimize_langgpt_mode(monkeypatch, tmp_path):
    skill = tmp_path / "SKILL.md"
    skill.write_text("langgpt框架", encoding="utf-8")
    monkeypatch.setattr(director, "SKILL_PATH", skill)
    monkeypatch.setattr(director, "_load_deepseek_key", lambda: "sk-test")
    captured = {}
    monkeypatch.setattr(director, "request_json",
                        lambda m, u, k, body=None, timeout=None:
                        (captured.update(body=body) or
                         {"choices": [{"message": {"content": "结构化结果"}}]}))
    result = director.optimize_prompt("一只猫", "langgpt")
    assert result["ok"] is True
    sys_msg = captured["body"]["messages"][0]["content"]
    assert "langgpt框架" in sys_msg
    user = captured["body"]["messages"][1]["content"]
    assert "结构化" in user
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/260413a/ai-generation-portable-apps && python3 -m pytest tests/test_director_assets.py -q`
Expected: FAIL（AttributeError: ASSETS_DIR / assets_payload）

- [ ] **Step 3: 写实现**

```python
# director/app.py 顶部追加
ASSETS_DIR = ROOT / "assets"
ASSETS_VERSION = "2026-08-28-v2"
_ASSET_FILES = {
    "gpt_image_templates": "gpt_image_templates.json",
    "nano_banana_styles": "nano_banana_styles.json",
    "shortcut_inspirations": "shortcut_inspirations.json",
    "negative_tags": "negative_tags.json",
}


def assets_payload() -> dict[str, Any]:
    out: dict[str, Any] = {"version": ASSETS_VERSION}
    for key, fname in _ASSET_FILES.items():
        path = ASSETS_DIR / fname
        try:
            out[key] = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            out[key] = {}
    return out
```

Handler do_GET 中 `/api/config` 分支之前追加：

```python
        if self.path == "/api/assets":
            json_response(self, 200, {"ok": True, **assets_payload()})
            return
```

`optimize_prompt` 的 mode 处理改为：

```python
    if mode == "langgpt":
        mode_text = (
            "把上面的内容改写成 LangGPT 结构化提示词：\n"
            "# Role（角色定义，一句话）\n## Profile（专业背景/能力）\n"
            "## Rules（规则：输出格式、禁止事项）\n"
            "## Workflow（步骤）\n## Initialization（开场白）\n"
            "保持用户原意，直接输出结构化结果，不要解释。"
        )
    else:
        mode_text = "按「优化 refine」规则改写" if mode == "refine" else "按「扩写 expand」规则改写"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Users/260413a/ai-generation-portable-apps && python3 -m pytest tests/test_director_assets.py tests/test_director_config.py tests/test_director_optimize.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add director/app.py tests/test_director_assets.py
git commit -m "feat(director): /api/assets 词库下发 + optimize 新增 langgpt 结构化模式"
```

---

### Task 3: SKILL.md 指南底座 + LangGPT 框架段

**Files:**
- Modify: `director/SKILL.md`

- [ ] **Step 1: 追加内容**（Prompt-Engineering-Guide 精华 + LangGPT 框架，保持现有 refine/expand 规则不动）

```markdown

## 通用提示词工程技巧（源自 Prompt-Engineering-Guide，77.9k⭐）

在优化/扩写任务中，如适用则自然地运用以下技巧，不要机械套用：
1. 角色设定：给生成对象赋予明确身份（如「一位广告导演」），输出更聚焦。
2. 输出格式约束：明确字数、结构、语言（中英混排规则）。
3. 分步指令（Chain-of-Thought）：复杂需求拆成「先…再…最后…」。
4. 少样本示例（Few-shot）：关键风格给 1-2 个简短示例（例句而非完整段落）。
5. 负面约束：用户明确说「不要」的内容，进「负面词：」行，不在正文重复。
6. 温度与冗余：宁精炼不堆砌；同类修饰词只保留最强的一个。

## LangGPT 结构化输出（mode=langgpt 时使用）

输出格式固定为：
# Role: <一句话角色>
## Profile: <2-3 条专业能力>
## Rules: <3-5 条硬规则，含输出格式与禁止事项>
## Workflow: <3-5 个步骤>
## Initialization: <一句开场白，表示已就绪>
```

- [ ] **Step 2: 测试验证**

Run: `cd /Users/260413a/ai-generation-portable-apps && python3 -m pytest tests/test_director_assets.py tests/test_director_optimize.py -q`
Expected: PASS（SKILL 变长不影响现有断言）

- [ ] **Step 3: 提交**

```bash
git add director/SKILL.md
git commit -m "feat(director): SKILL.md 融入提示词工程指南底座与 LangGPT 输出框架"
```

---

### Task 4: 前端 DirectorApp 扩展（8 skills）

**Files:**
- Modify: `portal/static/index.html`（skill 参数区）
- Modify: `portal/static/app.js`（skills 列表、资产加载与缓存、拼装逻辑）
- Modify: `tests/test_director_sidebar.mjs`（冒烟扩展）

- [ ] **Step 1: app.js——DirectorApp 扩展**

```javascript
// skills 数组替换为：
    skills: [
      { id: "refine", label: "提示词优化" },
      { id: "expand", label: "提示词扩写" },
      { id: "langgpt", label: "结构化提示词" },
      { id: "template", label: "工业模板" },
      { id: "style", label: "风格参考" },
      { id: "inspire", label: "场景灵感" },
      { id: "negative", label: "负面词生成" },
      { id: "text2image", label: "文生图" },
    ],
// state 追加：
    assets: { version: "", gpt_image_templates: { cases: [], categories: [] },
              nano_banana_styles: { styles: [] },
              shortcut_inspirations: { items: [] },
              negative_tags: { negative: [], styles: [] } },
    templateCategory: "", stylePick: "", inspireIndex: 0, negativePick: [],
// init() 追加（reload() 之前）：
      try {
        const cached = localStorage.getItem("director-assets-v2");
        if (cached) { try { this.assets = JSON.parse(cached); } catch (e) {} }
        const res = await api("/director/api/assets", "GET");
        if (res && res.ok && res.version) {
          this.assets = res;
          localStorage.setItem("director-assets-v2", JSON.stringify(res));
        }
      } catch (e) { /* 词库加载失败时词库类 skill 显示空态 */ }
// run() 的词库分支（在 text2image 分支之前插入）：
        if (this.skill === "template") {
          const cats = this.assets.gpt_image_templates.categories || [];
          const pool = (this.assets.gpt_image_templates.cases || []).slice(0, 80);
          const list = this.templateCategory
            ? pool.filter((c) => (c.title || "").includes(this.templateCategory)) : pool;
          const picked = list.length ? list[Math.floor(Math.random() * list.length)] : null;
          this.resultText = picked
            ? `【${picked.title}】\n${picked.prompt}\n\n—— 将「主体/风格」替换为你的需求后使用`
            : "该分类暂无模板";
          this.statusText = "已生成模板参考";
          return;
        }
        if (this.skill === "style") {
          const styles = this.assets.nano_banana_styles.styles || [];
          const picked = styles.find((s) => s.name === this.stylePick) || styles[0];
          this.resultText = picked
            ? `【风格：${picked.name}】\n${picked.prompt}`
            : "风格库为空";
          this.statusText = "已输出风格片段";
          return;
        }
        if (this.skill === "inspire") {
          const items = this.assets.shortcut_inspirations.items || [];
          this.inspireIndex = (this.inspireIndex + 1) % Math.max(1, items.length);
          const it = items[this.inspireIndex];
          this.resultText = it ? `【${it.title}】\n${it.prompt}` : "灵感库为空";
          this.statusText = "换一条灵感（再点一次换下一条）";
          return;
        }
        if (this.skill === "negative") {
          const neg = this.assets.negative_tags.negative || [];
          this.resultText = "负面词：\n" + neg.join(", ");
          this.statusText = "已生成负面词，可并入文生图";
          return;
        }
```

- [ ] **Step 2: index.html——skill 参数区追加**（`风格补充` 字段之后、`输入` 字段之前）

```html
      <label class="director-field" v-show="skill === 'template'">
        <span class="director-field-label">模板分类（可选，留空随机）</span>
        <select v-model="templateCategory">
          <option value="">全部</option>
          <option v-for="c in assets.gpt_image_templates.categories" :key="c" :value="c">{{ c }}</option>
        </select>
      </label>
      <label class="director-field" v-show="skill === 'style'">
        <span class="director-field-label">风格选择</span>
        <select v-model="stylePick">
          <option v-for="s in assets.nano_banana_styles.styles" :key="s.name" :value="s.name">{{ s.name }}</option>
        </select>
      </label>
```

- [ ] **Step 3: 冒烟测试扩展**

```javascript
// tests/test_director_sidebar.mjs 追加断言
if (app.skills.length !== 8) throw new Error("skills must contain 8 entries");
if (typeof app.assets !== "object") throw new Error("assets state missing");
```

- [ ] **Step 4: 跑测试**

Run: `cd /Users/260413a/ai-generation-portable-apps && node tests/test_director_sidebar.mjs`
Expected: `history sidebar: ok` 与 `director sidebar: ok`（两文件都跑）

- [ ] **Step 5: 提交**

```bash
git add portal/static/index.html portal/static/app.js tests/test_director_sidebar.mjs
git commit -m "feat(portal): 导演台扩到 8 个 skill——词库本地拼装 + localStorage 缓存"
```

---

### Task 5: 部署 + live 实测

- [ ] **Step 1: 全量回归**

Run: `cd /Users/260413a/ai-generation-portable-apps && python3 -m pytest tests/ -q 2>&1 | tail -1`
Expected: 无新增失败（既有 7 个不变）

- [ ] **Step 2: 重启 director（杀进程让 portal 看门狗拉起，无需动 portal）**

Run: `OLD=$(lsof -tiTCP:8895 -sTCP:LISTEN | head -1); kill -9 $OLD; sleep 30; lsof -tiTCP:8895 -sTCP:LISTEN | head -1`
Expected: 新 PID 出现

- [ ] **Step 3: live 验证**

```bash
curl -s http://127.0.0.1:8895/api/assets | python3 -c "import json,sys; d=json.load(sys.stdin); print('assets keys:', [k for k in d if k!='ok']); print('模板案例:', len(d['gpt_image_templates']['cases']), '风格:', len(d['nano_banana_styles']['styles']), '灵感:', len(d['shortcut_inspirations']['items']), '负面词:', len(d['negative_tags']['negative']))"
curl -s -X POST http://127.0.0.1:8895/api/optimize-prompt -H 'Content-Type: application/json' -d '{"text":"一只猫","mode":"langgpt"}' | head -c 300
```
Expected: 资产数量齐全；langgpt 返回结构化结果（真实 DeepSeek）

- [ ] **Step 4: 浏览器验收（用户）**

导演台：8 个 skill 可选；模板/风格/场景/负面词秒回；结构化提示词出 LangGPT 格式；结果可一键填入文生图。

- [ ] **Step 5: 推送**

```bash
git push origin main
```

---

## Self-Review 备注

- 统计红线：词库类 skill 不出任务不计用量；出图仍走 /api/jobs + X-Job-Id（不动）。
- 无 placeholder；所有代码块完整。
- 类型一致性：assets payload 字段名与前端 state 初始化一致（gpt_image_templates/nano_banana_styles/shortcut_inspirations/negative_tags + version）。
- 体积：4 份资产合计 ≤1MB（测试断言），localStorage 缓存带版本键 director-assets-v2。
