"""用户口径的**长期记忆**：自动沉淀 → 自动注入 → 可人工修正。

**为什么要有它**（2026-10-13 用户原话：「为什么还会出现这种情况，之前都反馈多少次了，
没有记忆功能吗」）

规划器每次规划都是**一次性**的：它只吃「通用契约」+「这条文档自己的返工要求」。
用户反馈给「铲」的口径不会带到「钱袋子」；同一条文档新开一个 run 也未必带
（跨 run 继承只在同一条文档的重跑链里生效）。于是同一类问题被反复反馈：
参考图被静默排除、游戏画面/底部 UI/计数框被当噪声、分镜图没被参考…

这里的口径：

* **自动沉淀**：用户每次说的话（返工要求）都记一条；**同一件事说第二次就升为「生效」**
  （`PROMOTE_AFTER=2`）—— 不需要用户再去哪里配置，也不用他重复第三次；
* **自动注入**：每次规划都把生效口径喂进规划上下文（搭「知识库经验」同一通道，落在
  **用户消息**里 —— 不动系统提示词，因此不会撞 `exact_system_prompt` 逐字回放与冻结哈希）；
* **人工可修正**：`dismiss()` 随时压掉某条；种子口径按 `seed_version` 升级时自动补齐。

存储：`<data_dir>/user_rules.json`，按使用者分桶 + 一个共享桶（`SHARED_OWNER="*"`，
放产品级口径，对所有人生效）。**存储坏了只丢记忆，绝不让规划失败**。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

_LOGGER = logging.getLogger(__name__)

#: 共享桶：产品级口径（种子）放这里，对所有使用者生效。
SHARED_OWNER = "*"
#: 同一件事说够几次才算「长期口径」。1 次太容易把一次性的意见当成规则。
PROMOTE_AFTER = 2
#: 但**进提示词**要更严：实测回填 826 条历史运行后，「说过 2 次」的里头混着
#: 「先横切，然后把芒果转90度」这种**一次性任务指令**（两条不同文档里凑巧各出现一次）。
#: 所以分两级：说 2 次 = 升级记录（可在规则列表里看到），说 3 次才注入。
#: 种子口径（`SEED_RULES`）不受这条限制 —— 它们是人工整理过的产品级口径。
INJECT_AFTER = 3
#: 每次注入最多几条 —— 口径堆太多会把规划注意力稀释掉。
DEFAULT_MAX_ACTIVE = 12
#: 单条注入正文上限（超出截断，附省略号）。
MAX_RULE_CHARS = 160
#: 判重阈值（字符集合 Jaccard），与 `rework_prompt` 的判重口径一致。
_SIMILARITY_THRESHOLD = 0.75
#: **超过这个长度就不再用 Jaccard 判重**：字符集合相似度在长句上会被**共同套话**带偏 ——
#: 实测「金币要有重量感+（同一段长说明）」与「角色三视图必须挂进参考图+（同一段长说明）」
#: 会被并成一条（集合几乎相同），于是两条不同的口径互相顶替、计数虚高。
#: 长句只认「逐字相同」与「包含」，宁可少并，也不要把两件事合成一件。
_SIMILARITY_MAX_CHARS = 80
#: 每个规则最多记几条来源文档（只用于追溯）。
_MAX_DOCS = 5

#: 种子口径的版本。往 `SEED_RULES` 里**新增**口径时把这个数 +1，老存储会补齐。
SEED_VERSION = 2

#: 产品级长期口径（从用户反复反馈里整理出来的；与 `_SEEDANCE_PLANNING_CONTRACT`、
#: `REFERENCE_FIDELITY_OVERRIDE` 互补 —— 那两处是「写死在契约里的规则」，
#: 这里是「可以随时改、且会继续自动增长的口径」）。
SEED_RULES: tuple[str, ...] = (
    "文档里的分镜图/参考图必须全部挂进参考图清单，并在对应镜头里用 @图片N 明确引用，"
    "不许静默排除",
    "游戏画面（游戏界面/关卡画面/HUD/计数框「N/M」/横条 UI/道具图标/游戏内文字）"
    "是产品内容，必须照原样复现，禁止写进「不参考」",
    "原图内容（文字/标题/UI/品牌字样）照原样参考；只排除第三方 AI 工具或平台的角标水印",
    "参考图里的关键元素（例如底部 UI 横条、计数框）必须在开场镜头就写进镜头描述，"
    "不能只在结尾出现",
    "「影子/投影属于谁」这类因果必须指名来源，并明确写出禁止的错误归属"
    "（例如「不是小鸭子自己的影子」）",
    "提示词正文里的自述时长/时间轴必须与任务时长一致",
    "需要抠图的绿幕镜头必须写成物理约束：整片绿幕同一色值、不参与场景光照、"
    "无明暗渐变/暗角/噪点/纹理，主体影子不落在绿幕上、绿幕不溢色到主体边缘",
)

#: 路径 → 锁。同一个文件被多个 store 实例（每个 runtime 一个）读写，必须串行化，
#: 而且每次写入都要**锁内重新读盘再改**（这台机器上「拿旧快照写回」已经丢过数据）。
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()

_PUNCTUATION = re.compile(r"[\s，。、；：,.!?！？（）()\[\]【】「」“”\"'`~—-]+")


def _lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


def _normalize(text: str) -> str:
    return _PUNCTUATION.sub("", str(text or "")).lower()


def _similarity(left: str, right: str) -> float:
    first, second = set(left), set(right)
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)


def _same_rule(left_norm: str, right_norm: str) -> bool:
    """两条是不是「同一件事的不同说法」。

    「说第二次才生效」这条设计**完全依赖**判重：用户不会逐字重复，实测
    「底部游戏 UI 横条必须完整复现」/「底部游戏 UI 横条必须完整复现。」/「底部游戏UI横条必须完整复现」
    必须合成一条，否则永远凑不够 2 次、记忆也就永远不生效。
    """
    if not left_norm or not right_norm:
        return False
    if left_norm == right_norm:
        return True
    # 长句里包含短句（用户补了一句话）也算同一条，但太短的不做包含判断（会误并）。
    if len(left_norm) >= 8 and len(right_norm) >= 8:
        if left_norm in right_norm or right_norm in left_norm:
            return True
    if max(len(left_norm), len(right_norm)) > _SIMILARITY_MAX_CHARS:
        return False        # 长句不用 Jaccard（共同套话会让两条不同的口径并成一条）
    return _similarity(left_norm, right_norm) >= _SIMILARITY_THRESHOLD


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class UserRule:
    rule_id: str
    text: str
    count: int = 1
    status: str = "candidate"          # candidate | active | dismissed
    source: str = "rework"             # seed | rework | manual
    first_seen: str = ""
    last_seen: str = ""
    docs: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class UserRuleStore:
    """口径的唯一读写入口（文件级锁 + 锁内读改写）。"""

    def __init__(
        self,
        path: Path,
        *,
        seed_rules: Sequence[str] = SEED_RULES,
        seed_version: int = SEED_VERSION,
        promote_after: int = PROMOTE_AFTER,
        max_active: int = DEFAULT_MAX_ACTIVE,
    ) -> None:
        self.path = Path(path)
        self.seed_rules = tuple(str(item) for item in seed_rules if str(item).strip())
        self.seed_version = int(seed_version)
        self.promote_after = max(1, int(promote_after))
        self.max_active = max(1, int(max_active))
        self._ensure_seed()

    # --- 读写 ---------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return {"seed_version": 0, "owners": {}}
        try:
            payload = json.loads(raw)
        except ValueError:
            # 解析失败只丢记忆：**绝不抛**（否则规划直接起不来）。
            _LOGGER.warning("用户口径存储损坏，按空处理 path=%s", self.path)
            return {"seed_version": 0, "owners": {}}
        if not isinstance(payload, dict):
            return {"seed_version": 0, "owners": {}}
        owners = payload.get("owners")
        if not isinstance(owners, dict):
            owners = {}
        return {
            "seed_version": int(payload.get("seed_version") or 0),
            "owners": {
                str(owner): [item for item in items if isinstance(item, dict)]
                for owner, items in owners.items()
                if isinstance(items, list)
            },
        }

    def _save(self, payload: dict[str, Any]) -> None:
        """原子写：唯一临时名（pid+线程+uuid）+ `os.replace` 有限重试。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:6]}.tmp"
        )
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        try:
            tmp.write_text(body, encoding="utf-8")
            last: Exception | None = None
            for attempt in range(6):
                try:
                    os.replace(tmp, self.path)
                    return
                except OSError as exc:  # 读者持有句柄时 Windows 会拒
                    last = exc
                    time_to_wait = 0.05 * (attempt + 1)
                    threading.Event().wait(time_to_wait)
            raise last if last is not None else OSError("replace failed")
        except Exception:  # noqa: BLE001 —— 写不进去只丢记忆，不打断规划
            _LOGGER.warning("写用户口径失败 path=%s", self.path, exc_info=True)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _rules_of(payload: dict[str, Any], owner: str) -> list[UserRule]:
        items = (payload.get("owners") or {}).get(owner) or []
        rules: list[UserRule] = []
        for item in items:
            try:
                rules.append(
                    UserRule(
                        rule_id=str(item.get("rule_id") or uuid.uuid4().hex[:12]),
                        text=str(item.get("text") or ""),
                        count=int(item.get("count") or 1),
                        status=str(item.get("status") or "candidate"),
                        source=str(item.get("source") or "rework"),
                        first_seen=str(item.get("first_seen") or ""),
                        last_seen=str(item.get("last_seen") or ""),
                        docs=[str(x) for x in (item.get("docs") or [])][:_MAX_DOCS],
                    )
                )
            except (TypeError, ValueError):
                continue
        return [rule for rule in rules if rule.text.strip()]

    def _ensure_seed(self) -> None:
        """按 `seed_version` 补齐种子（升级后才新增的口径，老存储也要拿到）。"""
        with _lock_for(self.path):
            payload = self._load()
            if int(payload.get("seed_version") or 0) >= self.seed_version:
                return
            existing = self._rules_of(payload, SHARED_OWNER)
            norms = [_normalize(rule.text) for rule in existing]
            stamp = _now()
            for text in self.seed_rules:
                norm = _normalize(text)
                if any(_same_rule(norm, other) for other in norms):
                    continue
                existing.append(
                    UserRule(
                        rule_id=uuid.uuid4().hex[:12],
                        text=text,
                        count=self.promote_after,
                        status="active",
                        source="seed",
                        first_seen=stamp,
                        last_seen=stamp,
                    )
                )
                norms.append(norm)
            payload.setdefault("owners", {})[SHARED_OWNER] = [
                rule.to_json() for rule in existing
            ]
            payload["seed_version"] = self.seed_version
            self._save(payload)

    # --- 写入 ---------------------------------------------------------------

    def record(
        self,
        owner: str,
        text: str,
        *,
        source: str = "rework",
        doc: str = "",
        shared: bool = False,
    ) -> UserRule | None:
        """记一条口径。同一件事再说一次 → 计数 +1；够了就**自动生效**。

        太短/太长的整条丢掉（「好的」「谢谢」不是口径）；单条超长按 `MAX_RULE_CHARS` 截断。
        `shared=True` 写共享桶（默认给所有人用）—— 后台任务里读不到归属人时，
        写个人桶等于记了没人读。
        """
        clean = " ".join(str(text or "").split())
        if len(clean) < 4:
            return None
        if len(clean) > MAX_RULE_CHARS * 3:
            clean = clean[: MAX_RULE_CHARS * 3]
        owner_key = SHARED_OWNER if shared else (str(owner or SHARED_OWNER) or SHARED_OWNER)
        with _lock_for(self.path):
            payload = self._load()          # 锁内重新读盘（不拿旧快照写回）
            rules = self._rules_of(payload, owner_key)
            norm = _normalize(clean)
            stamp = _now()
            matched: UserRule | None = None
            for rule in rules:
                if _same_rule(norm, _normalize(rule.text)):
                    matched = rule
                    break
            if matched is None:
                matched = UserRule(
                    rule_id=uuid.uuid4().hex[:12],
                    text=clean,
                    count=1,
                    status="candidate",
                    source=source,
                    first_seen=stamp,
                    last_seen=stamp,
                    docs=[doc] if doc else [],
                )
                rules.append(matched)
            else:
                matched.count += 1
                matched.last_seen = stamp
                # 更完整的那句留着（用户后面往往说得更清楚）。
                if len(clean) > len(matched.text):
                    matched.text = clean
                if doc and doc not in matched.docs:
                    matched.docs = [*matched.docs, doc][-_MAX_DOCS:]
            if matched.count >= self.promote_after and matched.status == "candidate":
                matched.status = "active"
            payload.setdefault("owners", {})[owner_key] = [
                rule.to_json() for rule in rules
            ]
            self._save(payload)
            return matched

    def dismiss(self, owner: str, rule_id: str) -> bool:
        """人工修正：压掉某条（之后不再注入）。"""
        owner_key = str(owner or SHARED_OWNER) or SHARED_OWNER
        with _lock_for(self.path):
            payload = self._load()
            rules = self._rules_of(payload, owner_key)
            found = False
            for rule in rules:
                if rule.rule_id == rule_id:
                    rule.status = "dismissed"
                    found = True
            if found:
                payload.setdefault("owners", {})[owner_key] = [
                    rule.to_json() for rule in rules
                ]
                self._save(payload)
            return found

    # --- 读取 ---------------------------------------------------------------

    def rules(self, owner: str, *, status: str | None = None) -> list[UserRule]:
        owner_key = str(owner or SHARED_OWNER) or SHARED_OWNER
        with _lock_for(self.path):
            payload = self._load()
            rules = self._rules_of(payload, owner_key)
        if status is not None:
            rules = [rule for rule in rules if rule.status == status]
        return rules

    def active(self, owner: str, *, limit: int | None = None) -> list[UserRule]:
        """生效口径 = 共享桶 + 本人桶（同一句只留一条），按「说过次数、最近说过」排序。"""
        owner_key = str(owner or SHARED_OWNER) or SHARED_OWNER
        merged: dict[str, UserRule] = {}
        for rule in self.rules(SHARED_OWNER, status="active"):
            merged[rule.rule_id] = rule
        if owner_key != SHARED_OWNER:
            for rule in self.rules(owner_key, status="active"):
                merged[rule.rule_id] = rule
        ordered = sorted(
            merged.values(), key=lambda rule: (-rule.count, rule.last_seen), reverse=True
        )
        return ordered[: (limit or self.max_active)]

    def render(self, owner: str) -> str:
        """给规划上下文用的文本（没有够格的口径就返回空串）。

        注入门槛比「升级」更严（`INJECT_AFTER`）：种子口径照旧，自动沉淀的要说够 3 次。
        """
        rules = [
            rule
            for rule in self.active(owner, limit=self.max_active * 3)
            if rule.source == "seed" or rule.count >= INJECT_AFTER
        ][: self.max_active]
        if not rules:
            return ""
        lines = ["【这位使用者反复强调过的长期口径（每一次规划都必须遵守）】"]
        for index, rule in enumerate(rules, start=1):
            text = rule.text
            if len(text) > MAX_RULE_CHARS:
                text = text[: MAX_RULE_CHARS - 1] + "…"
            lines.append(f"{index}. {text}")
        lines.append(
            "（这些是他在以往返工里反复提出的要求，属于长期口径：本次规划请直接做到，"
            "写进对应镜头，不要等生成完再被挑出来。）"
        )
        return "\n".join(lines)


def user_rules_context(data_dir: Path | str, owner: str = "") -> str:
    """`data_dir` 下的口径存储 → 注入文本；任何异常都返回空串（规划照常走）。"""
    try:
        store = UserRuleStore(Path(data_dir) / "user_rules.json")
        return store.render(owner or current_owner())
    except Exception:  # noqa: BLE001 —— 记忆只能是加分项
        _LOGGER.warning("读取用户长期口径失败", exc_info=True)
        return ""


def current_owner(fallback: str = "prime-local") -> str:
    """当前请求的归属人（仓储的 owner 上下文变量）；读不到就退回默认。

    规划跑在后台任务里时这个变量可能是空的 —— 所以**沉淀默认写共享桶**
    （`record(..., shared=True)`），否则「记在某人名下、读的时候没人」= 记忆失效。
    """
    try:
        from feishu_generation_agent.storage.repository import _OWNER_USER_ID

        return _OWNER_USER_ID.get() or fallback
    except Exception:  # noqa: BLE001
        return fallback


_ITEM_SPLIT = re.compile(r"[\n\r；;。]+")


def split_requirement_items(text: str) -> list[str]:
    """把用户一次说的话切成若干条口径（换行/分号/句号切分，太短的整句丢掉）。

    太长的一整段（整篇返工要求）切不出句子时，整段作为一条 —— 宁可记一条长的，
    也不要因为「切不开」而完全丢掉。
    """
    raw = str(text or "").strip()
    if not raw:
        return []
    items = [" ".join(part.split()) for part in _ITEM_SPLIT.split(raw)]
    cleaned = [item for item in items if len(item) >= 4]
    if cleaned:
        return cleaned
    single = " ".join(raw.split())
    return [single] if len(single) >= 4 else []

