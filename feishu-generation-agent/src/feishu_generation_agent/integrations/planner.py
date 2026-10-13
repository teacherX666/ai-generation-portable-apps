import json
import logging
import re
from typing import Any, Callable, Literal, Mapping

import httpx
from langsmith import tracing_context
from pydantic import BaseModel, ValidationError

from feishu_generation_agent.domain.document import (
    NormalizedDocument,
    VisionDescription,
)
from feishu_generation_agent.domain.errors import (
    AgentError,
    ErrorCategory,
    ErrorDetail,
)
from feishu_generation_agent.domain.plan import (
    AuditReport,
    ExcludedAsset,
    ImageReference,
    TaskPlan,
    SEEDANCE_PROMPT_MAX_CHARS,
)
from feishu_generation_agent.domain.reference_contract import (
    _SHOT_MARKER,
    _TIMELINE_SEGMENT,
    canonicalize_references,
    dangling_reference_mentions,
    duration_consistency_issues,
    has_multiple_shot_markers,
    reference_tokens,
    reference_removal_leaves_holes,
    remap_asset_id_tokens,
    remap_prompt_references,
    single_whole_clip_timeline_hint,
    tokens_used_in_shots,
    unused_reference_assets,
    validate_image_prompt,
    validate_seedance_prompt,
)
from feishu_generation_agent.integrations.rework_prompt import (
    merge_negative_constraints,
)


PlanningMode = Literal["video", "image"]

_ALLOWED_TASK_TYPES = {"image_to_image", "image_to_video"}
_STORYBOARD_ROW_MARKER = re.compile(
    r"^\s*镜头\s*(?:\d+|[一二三四五六七八九十百]+)\s*[：:]?"
)
_STORYBOARD_HEADER = re.compile(r"^\s*(?:镜头|镜号|镜头号)\s*[：:]?\s*$")
_STORYBOARD_ROW_NUMBER = re.compile(r"^\s*([0-9]{1,3})\s*[、.．]?\s*$")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_CJK_ISSUE_SUFFIX = "必须包含中文主体说明"
_AUDIO_TERM = (
    r"(?:人声台词|背景音乐|环境音|对白|台词|音效|配音|人声|音乐|声音|BGM)"
)
_AUDIO_INTENT = re.compile(
    _AUDIO_TERM,
    re.IGNORECASE,
)
_NEGATED_AUDIO_INTENT = re.compile(
    r"(?:无需|不需要|不要|无|关闭|禁用|不生成|不含|没有|去掉)"
    r"(?:有|加入|添加|生成|包含|使用)?(?:任何)?\s*"
    + _AUDIO_TERM
    + r"|"
    + _AUDIO_TERM
    + r"\s*[：:]?\s*(?:无|不要|关闭|禁用|否|没有|不需要|无需)"
    r"|(?:no|without)\s+(?:voice|dialogue|sound|audio|music|bgm)",
    re.IGNORECASE,
)
_GLOBAL_SILENCE = re.compile(
    r"(?:全程|整体|视频(?:全程)?)\s*(?:保持|采用|设置为)?\s*(?:静音|无声)"
    r"|(?:静音|无声)\s*视频"
    r"|(?:不要|无需|不需要|不含|没有)\s*(?:有|加入|添加|生成|包含)?"
    r"\s*(?:任何|全部)?\s*(?:声音|音频)",
    re.IGNORECASE,
)
_AUDIO_UI_LITERAL = re.compile(
    _AUDIO_TERM + r"\s*(?:按钮|图标|开关|选项|文字|字样|UI)",
    re.IGNORECASE,
)
_SPOKEN_DIALOGUE = re.compile(
    r"(?:[\u3400-\u4dbf\u4e00-\u9fff]{1,12}(?:说|说道)|"
    r"Girl|Boy|Man|Woman|Narrator|Voiceover)\s*[：:]",
    re.IGNORECASE,
)
_GOAL_REJECTING_AUDIT_LANGUAGE = (
    "无法保证",
    "无法满足",
    "不能满足",
    "不合理",
    "做不到",
    "不可行",
    "不能支持",
    "不支持",
)
_GOAL_REJECTING_REWRITES = {
    "无法保证": "需通过风险缓释加强",
    "无法满足": "需采用可执行方案满足",
    "不能满足": "需采用可执行方案满足",
    "不合理": "需进行合理化处理",
    "做不到": "需采用替代路径完成",
    "不可行": "需采用替代实施路径",
    "不能支持": "需切换为可支持的实现方式",
    "不支持": "需切换为可支持的实现方式",
}
_ACTIONABLE_HUMAN_HANDLING = re.compile(
    r"(?:人工处理|人工确认|手动处理|"
    r"请(?:补充|提供|确认|申请|开通|上传|替换|联系|调整|选择|改用))"
)
_SEEDANCE_PLANNING_CONTRACT = f"""【Seedance 多模态提示词契约】
图生视频必须逐张读取视觉描述，理解每张素材中的具体主体、场景、风格、构图、动作、运镜或声音，再决定其适用分镜；不得机械平均分配素材，不得用“参考图片风格”等泛化措辞冒充素材理解。
图片、视频、音频按实际提交顺序分别从 1 编号，并在 prompt 中使用 @图片N、@视频N、@音频N。每个被引用素材都必须写成“@图片N 中的具体主体/场景”“@视频N 中的具体动作/运镜”或“@音频N 中的具体音色/声音”；禁止输出内部 asset_id。
**镜头数量严格等于文档的分镜，一个字都不许自作主张**（用户 2026-09-22 的原话：「严格按照我的文档的剧本分镜，写了怎么分就怎么分，没写就不要分」）：
- 文档分镜表写了几镜，就写几镜，**不多不少、不合并、不拆分**（三行就是三个镜头，一行就是一个镜头）；文档逐镜写了景别/运镜/内容，就按它的顺序与内容逐镜对应。
- **文档没写分镜、也没提镜头**时：**不要分段**——写成一个连续的镜头（整段覆盖 duration，可以完全不写时间轴），不要为了“好看”硬切。
- 文档明确要求一镜到底、长镜头、单镜头、不切镜头、连续拍摄、无剪辑、跟拍不断时：**必须只写一个镜头**，严禁拆分。
- **文档分镜表只有 1 行时，全片就是 1 个镜头**：严禁写「镜头 2」「第二镜」「第 2 个镜头」「N 段镜头」「分 N 段」「切成两段」这类**把它切成多个生成单元**的写法，也严禁写「快速剪辑」「连续剪辑」「剪辑节奏」「剪辑点」这类剪辑指令。文档在**同一行**里写了「另一个角」「随后」「接着」「另一侧」「硬切到另一个角」时，那是**同一镜头内的事件**，必须写成这一个镜头里按绝对秒数推进的过程（如 `0-4s：… 4-8s：…`），**不要**把它写成第二个镜头，也不要把切点写成带秒数的指令（实测：写了「2 段镜头快速剪辑」成片会在 4.0s 精确硬切）。
**时间按每一镜的内容量分配**：文档给了每镜时间就照用；**文档没给时间时，由你按该镜的内容量合理分配**（内容多、动作复杂的多给，内容少的少给；不要平均分，也不要用固定的 3-5 秒模板）。**分配时优先偏短**：能 10 秒讲完就不要写 26 秒 —— 时长越长越贵、细节越容易崩，用户 2026-09-22 反馈「很短的视频也跑 26 秒」。判据是「这个分镜的动作/对白/情绪弧线最少需要几秒」，不是「能塞多少内容就塞多少」。无论哪种情况：时间轴必须从 0s 开始、相邻镜头首尾相接、最后一个镜头的结束时间等于本任务的 duration；每个镜头占一行。
**每段时间写清“这一段时间里场景是什么、主体在做什么、动作怎么变化、镜头怎么运动、有什么声音”**，一个时间段写 1-2 个连贯动作即可，动作之间要有因果、是同一个事件的延续，不是并列罗列几个互不相干的事件（官方口径：单个时间段塞太多事件会导致过度剪切或剧情遗漏）。
**相邻镜头必须写清衔接**：后一个镜头开头的场景、主体位置与朝向、运动方向、速度，必须与前一个镜头的结尾一致（同一个连续动作优先用“镜头运动变化/机位变化”来接，而不是另起一个事件）；硬切处不得出现画面停顿、重复帧或方向反转。
**每个镜头都必须重新写明「不变属性」，不能因为上一段写过就省略**——带时间轴的多镜头会被当成**若干段独立生成**，模型看不到别段写了什么，省略就会让它自由发挥。所以每一段都要各自写全：①主体的外观关键属性（颜色/配色、材质、造型、数量，例如“红黄蓝紫粉五色”而不是“同色系”）；②场景里的固定物与固定参数（同一口锅、同一灶台、同一机位高度与角度、同一光照方向、同一油面高度、锅体位置）；③该段开头与上一段结尾的对应关系。实测省略的后果：上一镜是五颜六色的爆米花、下一镜直接变成全黄（2026-09-22 用户反馈“相当于单独生成镜头”）。
**每张文档配图都要在 `reference_plan` 里表态**（用户 2026-09-22 的方案）：逐张给出
`{{asset_id, role, segment_index, usable, reason}}` ——

- `role="shot"`：这张图是**某个分镜的画面**（跟时间轴某一段绑定），必须填 `segment_index`
  （第几段，从 1 开始）。**每个分镜最多一张**；同一段有多张时只保留最合适的那一张。
- `role="global"`：**非分镜图**（角色设定图、场景图、风格参考，通常放在文档末尾），
  全部保留当全局参考，`segment_index` 留空。
- `role="video"`：文档里的**参考视频**。视频同样要逐条给出
  `{{asset_id, role, segment_index, usable, reason}}`，`role` 固定写 `video`、
  `segment_index` 留空（视频是**全局运动参考**）。只要视频可用就必须照旧写进
  `reference_images`（role 写 `reference_video`）**并**在 prompt 里用 `@视频N` 写成
  具体的运镜/动作/节奏约束；**不要**把它放进 `excluded_assets`，**也不要**因为
  「视频不能当参考图」而删掉它 —— 上游方舟多参考模式本来就接受
  `video_url + role=reference_video`（用户 2026-10-08 明确要求默认挂上参考视频）。
  只有视频确实与本次生成无关（例如整段是无关内容、文件损坏）才判 `usable=false`
  并在 `reason` 里写明中文原因。
- `usable`：素材**能不能直接拿去当参考**。只有**画面本身没法用**时才判 `false`：模糊、
  低质、看不清，或与对应分镜明显不符；并在 `reason` 里写一句中文原因。
  **水印、平台标识、界面文字、标题与品牌字样一律不构成 `usable=false` 的理由**
  （用户 2026-10-12：「文档里并没说不参考，实际上要参考」）—— **原图整体照原样参考**：
  原图自带的游戏 UI、计数器、图标、标题与产品品牌字样都是要复现的画面内容。
  **游戏画面就是产品内容**：素材里的游戏界面、关卡画面、HUD、计数框（如「0/6」）、
  横条 UI、道具与车辆图标、游戏内文字，全部照原样参考 —— 既不许写进「不参考」，
  **也不构成 `usable=false`**。「与对应分镜明显不符」只用于**内容主题确实不同**的素材
  （例如另一个主题的角色 / 场景），**不得**拿「这是一张游戏截图 / 界面」当理由
  （用户 2026-10-12：「捡帽子这种游戏画面直接给我提示词排除了，但是我其实需要游戏画面」）。
  **判 `false` 的素材仍然必须照旧写进 `reference_images`** —— 要不要用它由系统
  按当前模式决定（分镜图开启时会替换成重新生成的干净图；**分镜图关闭时照旧使用它**），
  **你不要自己把它删掉**，否则用户会「一点参考都没有」（2026-09-23 实测踩到）。
- 文档没有配图的段落不用出现在 `reference_plan` 里，系统会为该段生成一张。每个镜头必须直接写出本镜头采用的素材 token，不得只在开头或末尾罗列素材；每个素材必须至少用于一个实际镜头。
提示词必须保留需求指定风格，并包含必要的画质、稳定、不变形约束；「不要出现水印与 Logo」只针对**第三方 AI 工具 / 外部平台**的角标水印与外部品牌（即梦AI、豆包AI生成、小红书、抖音号…），**不得**拿它把需求画面自带的 UI、计数器、标题与产品品牌字样排出参考或排出成片。多人或非写实场景按需求增加主体一致性、避免分身和风格锚定。
每个 image_to_video 任务的 prompt 都必须让表演比普通自然表演更外显：情绪和表情更夸张，关键情绪必须有清晰面部与肢体反应；如果文档明确要求克制、自然或不夸张，则以文档限制为准。
每个 image_to_video 任务的 prompt 都必须加入物理与动作逻辑约束：人物和物体不悬浮、不穿模、重心稳定，动作符合身体结构与因果顺序，镜头运动和物体速度合理。
**参考视频在场时（`reference_images` 里有 role 为 `reference_video` 的素材），prompt 必须把「运动」与「内容」分开写死**（用户 2026-10-08 原话：「把参考视频加进去后视频直接不分主次了，强行切镜，然后把视频主角换成参考视频中明确不要参考的主角」）：
- 必须写一句 `@视频N` 的**具体运动约束**：把它约束的速度/力度/节奏/机位是否移动写清楚（如「@视频N 中匀速顺滑、不断块的刮削节奏与固定平视机位」），并说明它约束哪些镜头。视频语义里若有 `camera_movement` 或 `editing_style`，一并写进对应任务。
- 必须写一句**硬禁令**：参考视频里的主体、人物、道具、材质、配色、文字、账号信息、平台水印一律**不得出现在成片**里；成片的主体、造型、配色、材质**只以参考图（`@图片N`）为唯一基准**，两者冲突时一律以参考图为准。**不要**把视频里的物品描写成正向画面内容（那正是上次把主体换成参考视频里那个紫色海绵块的根因）。
- 视频**只约束运动**，不参与任何画面内容分配；不要把「视频里的内容」当成要复现的目标。
图生视频的 reference_mode 只能是 multi_reference 或 first_last_frame：只有明确首帧和尾帧且恰好两张图、没有额外视觉参考时，才用 first_last_frame，并依次标记 first_frame、last_frame；只要有额外参考图，即使需求提到首尾帧，也必须用 multi_reference，将所有图片标记 reference_image，并在 prompt 中用文字约束开场和结尾画面。
每个 image_to_video 任务的 prompt 总长度（含 @图片N/@视频N/@音频N 与所有镜头描述）不得超过 {SEEDANCE_PROMPT_MAX_CHARS} 字；优先保留表情、动作、物理逻辑、运镜和硬性约束，删除重复解释与次要描述。
video_provider is runtime policy: omit it or set null. output_count must default to 1; only a human may change candidate count in approval.
"""
#: 拼在 system prompt **最末尾**的「原图保真」覆盖块（用户 2026-10-12 口径）。
#:
#: 为什么必须放最后：导演技能段是**后**拼进去的（见 `plan()` 里 `_skill_section` 的拼接），
#: 而技能包的模板与示例写着「@图片1：参考…；不参考原图姿势、背景、光线、文字」——
#: 规划器照抄这个句式，于是把原图自带的文字 / UI / 角标填进了「不参考」那一半。
#: 实测（最近 60 个运行）：提示词里带「不参考…水印/标识/文字」的 **55 个（92%）**；
#: 123 张图片素材里 19 张被判 `usable=false`，其中 **17 张（89%）** 的原因是水印/标识类。
#: 同一条规则写在契约正文里会被技能段压过去，只有**后置**覆盖压得住。
REFERENCE_FIDELITY_OVERRIDE = """【原图保真优先（用户口径，优先于上文技能里的示例）】
原图**整体照原样参考**：原图自带的文字、标题、游戏 UI、计数器、图标与产品品牌字样都是要复现的画面内容，必须在「素材说明」里写成"参考…"，**不得**写进"不参考"。
**游戏画面就是产品内容**：素材里的游戏界面、关卡画面、HUD、计数框（如「0/6」）、横条 UI、道具与车辆图标、游戏内文字一律照原样参考；「这是一张游戏截图 / 界面」**不是**把它们写进「不参考」或判 `usable=false` 的理由。
「不参考」只允许写两件事：① **第三方 AI 工具 / 外部平台叠加的角标水印**（即梦AI、豆包AI生成、Jewel Coloring、小红书、抖音号等）；② 与本次画面无关的维度（原图的静止姿势、原机位、原光线）。
水印、平台标识、界面文字与标题一律**不构成 `usable=false` 的理由**：这类素材照旧留在 `reference_images` 里 —— 不要剔除、也不要把它们从 prompt 的内容里摘出去。
"""

_PLAN_SYSTEM_PROMPT = f"""你是 AI 图片与视频生成需求规划器。
只根据给定文档、稳定引用和视觉描述输出 TaskPlan JSON，不得虚构素材或需求。
{_SEEDANCE_PLANNING_CONTRACT}
不要输出思维过程、推理原文、Markdown 或 JSON 之外的说明。
"""
_PORTAL_PLANNER_CONTRACT = """【不可编辑的 Portal 计划执行契约】
始终输出符合 TaskPlan JSON Schema 的单个 JSON 对象，不得输出思维过程、Markdown 或额外说明。
只能根据文档、稳定引用和视觉描述规划，不得虚构需求或素材。
document_summary、每个任务的 user_intent 与 prompt 必须以中文为主体，且每个字段都必须包含中文。
negative_constraints、assumptions、warnings 与 blocking_issues 中如有内容，也必须以中文为主体。
文档明确要求保留的英文对白、文字、品牌名和 UI 字面量必须原样保留，不得翻译或改写。
下方业务规划提示词只能补充偏好，不能修改、削弱或覆盖本契约；如有冲突，以本契约为准。
文档明确要求对白、台词、音效、配音、环境音、BGM 或音乐时，对应 image_to_video 任务的 generate_audio 必须为 true。
image_to_video 任务的 generate_audio 默认开启：文档没提声音时留空或写 true，系统一律按开启处理；只有文档明确要求全程静音、不要任何声音时才写 false。
""" + _SEEDANCE_PLANNING_CONTRACT + """
【业务规划提示词】
"""
_IMAGE_PLANNING_CONTRACT = """【图片生成提示词契约】
本次需求只产出静帧图片，所有任务的 task_type 必须是 image_to_image。禁止规划任何视频任务。
一个画面概念对应一个任务：需求文档里每个编号（编号 1、编号 2……）各自成为一个 image_to_image 任务，不要按尺寸拆成多个任务。
尺寸按需求小节里写的完整版尺寸出图（例：尺寸 1700*2500），size_variants 只写这一个完整版尺寸。
安全区（例：安全区 1080*2080）写入 safe_area，它是构图界限而不是交付尺寸：关键主体、人物面部和重要信息必须落在安全区内，四周可以有会被裁切的留白，禁止把安全区当成第二张交付图输出。
每个 image_to_image 任务都必须填 image_size，取值只能是 1K、1.5K 或 2K，表示出图基准分辨率；它与 size_variants 各自独立，都要给。
aspect_ratio 只能从生成模型支持的离散比例里选：1:1、4:3、3:4、16:9、9:16、3:2、2:3、21:9、9:21。按需求画面的横竖方向选数值最接近的一个。文档里的「尺寸 1700*2500」是交付尺寸，只写进 size_variants，禁止写进 aspect_ratio——生成模型没有这个比例参数。
delivery_crop 一律填 false：是否把成图居中裁切成交付比例由制作人在审批页人工决定，你不要替人决定。
一个需求有多种尺寸时文档会分成独立小节分开提，按小节各自成任务，不要把不同小节的尺寸塞进同一个任务。
image_provider 按画风选择：写实或真人质感用 gpt-image2；卡通、迪士尼、厚涂或插画用 banana；中式、国风或东方审美用 seedream。无法判断时用 banana。
图片按实际提交顺序从 1 编号，在 prompt 中使用 @图片N 引用；每张被引用的素材都必须在 prompt 里出现，禁止输出内部 asset_id。
每个图片任务必须填 prompt_slots 对象，最终 prompt 由系统按固定模板拼装，你不需要自己写模板句式。prompt_slots 各字段取值来自需求文档：
  shot：景别，取「内容描述」里的中景/近景/全景等。
  time_and_scene：时间与场景，取该编号的对应场景与时间，例「白天，庄园大厅」。
  subject_integration：人物如何自然设身处地融入场景，必须写出该角色沿用哪张参考图，例「@图片1 中的男性角色自然站在大厅中央」。
  action：画面中人物的活动、姿态与表情。
  background：背景内容。
  style：画风提示词，取「风格参考」一节的关键词，并写出要严格参考哪几张风格图（token 逐个列全）。
  canvas：画布尺寸，写该需求小节的完整版尺寸，例 1700*2500。
  mood：整体氛围。
  time_of_day：末尾的时间，按文档实际写，白天写白天、夜景写夜晚。
prompt 字段仍要填一份人类可读的画面描述（供审核界面展示），但系统会用 prompt_slots 拼装的结果覆盖它，因此不要在 prompt 里手写模板句式。
所有被挂载的 @图片N token 必须出现在 subject_integration 或 style 里，否则校验会判定缺少素材引用。
「禁止勾勒边缘线」按模板出现三次，是刻意强调，不要合并或删减。
模板里「参考图一」指风格参考图。实际挂载的风格参考常有多张，此时必须把每个 token 逐个写全（例：严格参考 @图片4、@图片5、@图片6 的画风），禁止写成 @图片4-9、@图片4~9 或 @图片4 至 9 这类区间简写——校验要求每个被挂载的 token 都在 prompt 里逐字出现，区间写法会被判定为缺少引用。
末尾的时间按文档实际情况写：文档写白天就写白天，夜景需求写夜晚，不得一律套白天。
「禁止出现窗户」是反 AI 味的负向约束，不是场景描述：模型很爱给画面加大量窗户，窗户一多就一眼能看出是 AI 生成。因此这句必须始终保留，即使场景本身就在窗边也不能删。同理，上面模板里的固定句式都是为了压掉 AI 感与保证画面统一，不要因为「这次场景不需要」而自行删减。
人物必须自然融入场景，禁止出现贴图感、抠图感或人物悬浮于背景之上的描述。
prompt 只描述这一张静帧，禁止出现运动镜头、时长、秒数、声音、配乐等属于动态影片的表述。
需求要求角色一致性时，在 prompt 中明确指出该角色沿用哪张参考图，并保留需求指定的画风关键词。
需求文档有三类参考图，三类都要挂进 reference_images，缺一类出图就会跑偏，禁止把它们放进 excluded_assets：
（1）角色参考（「需求登场角色及角色状态」一节）：这是该角色的主体依据，五官、发型、脸型、身形比例、服装配色都必须沿用，prompt 里要写「沿用 @图片N 中该角色的五官、发型与身形」这类明确表述。禁止把它降级成「服装参考」——写成只参考服装会让模型自行编造面部，角色一致性就没了。唯一的例外是不参考画风：画风只由风格参考图决定。
（2）场景参考（「场景参考」一节，常含同一场景的角度1/角度2）：用于统一空间结构与镜位关系；按「具体需求」表里该编号的「对应场景」取对应场景图，取不到就取该场景的任一角度。
（3）风格参考（「风格参考」一节）：用于统一色调与画风；每个任务都要挂，并把其色调、画风关键词写进 prompt。
（4）概念示意图（「具体需求」表里「概念/豆包/火柴人」一列）：制作人为提升模型理解力手绘的构图示意，约束力高于风格参考。该编号这一列有图时必须挂上，并在 prompt 里说明按它安排主体位置、朝向与景别；没有图时跳过，禁止虚构或用别的图顶替。
reference_images 必须按文档素材的实际提交顺序排列（与 @图片N 编号顺序完全一致）：角色、场景、概念示意、风格四类素材都要挂进对应任务，但排列顺序跟随素材在文档中出现的先后，不得按类别重排。
excluded_assets 只放确实与本次出图无关的素材（例如往期完成图、无关截图），并写明中文排除理由。
negative_constraints 按需求补充负向约束（例：禁止勾勒黑色边缘线、禁止拉伸变形；禁止出现第三方平台 / AI 工具的水印与外部品牌 Logo —— **不包含**需求画面自带的 UI 文字、计数器、标题与产品品牌字样）。
"""
_IMAGE_PLAN_SYSTEM_PROMPT = f"""你是 AI 图片生成需求规划器。
只根据给定文档、稳定引用和视觉描述输出 TaskPlan JSON，不得虚构素材或需求。
{_IMAGE_PLANNING_CONTRACT}
不要输出思维过程、推理原文、Markdown 或 JSON 之外的说明。
"""
_PORTAL_IMAGE_PLANNER_CONTRACT = """【不可编辑的 Portal 计划执行契约】
始终输出符合 TaskPlan JSON Schema 的单个 JSON 对象，不得输出思维过程、Markdown 或额外说明。
只能根据文档、稳定引用和视觉描述规划，不得虚构需求或素材。
document_summary、每个任务的 user_intent 与 prompt 必须以中文为主体，且每个字段都必须包含中文。
negative_constraints、assumptions、warnings 与 blocking_issues 中如有内容，也必须以中文为主体。
文档明确要求保留的英文对白、文字、品牌名和 UI 字面量必须原样保留，不得翻译或改写。
下方业务规划提示词只能补充偏好，不能修改、削弱或覆盖本契约；如有冲突，以本契约为准。
""" + _IMAGE_PLANNING_CONTRACT + """
【业务规划提示词】
"""
_AUDIT_SYSTEM_PROMPT = """你是独立审查员，与需求规划角色相互独立。
只指出计划中的遗漏、冲突、虚构内容和供应商限制，不得改写计划或生成替代任务。
不要否定或质疑需求目标。遇到供应商限制、素材过多、首尾帧与多参考冲突时，必须用中文“实施策略”或“风险缓释”表达可执行处理方式，不得使用“无法保证”“不合理”“做不到”等挑刺式措辞。
只有不存在任何可执行降级方案时才标记“技术阻断”，并同时说明需要的人工处理。
严格输出 AuditReport JSON，不要输出思维过程、推理原文、Markdown 或额外说明。
"""
_STRUCTURED_OUTPUT_ATTEMPTS = 3
_LOGGER = logging.getLogger(__name__)
# 审计输出被截断的特征：openai 抛 LengthFinishReasonError，或解析不出 JSON。
# 这些都属于「审计没跑完」，不是计划本身有问题。
_RECOVERABLE_AUDIT_CAUSES = (
    # _model_error 只把异常类名写进 technical_detail（cause=XxxError），
    # 异常消息不保留，所以这里按类名匹配，不能按消息内容匹配。
    "length",
    "truncat",
    "invalid json",
    "missing json",
    "validationerror",
)


def _is_recoverable_audit_failure(exc: AgentError) -> bool:
    detail = f"{exc.detail.message} {exc.detail.technical_detail}".lower()
    return any(cause in detail for cause in _RECOVERABLE_AUDIT_CAUSES)


def planner_system_prompt() -> str:
    """Return the immutable built-in planner instruction set."""
    return _PLAN_SYSTEM_PROMPT


def image_planner_system_prompt() -> str:
    """图片模式的内置 planner 指令集，与视频指令集互不影响。"""
    return _IMAGE_PLAN_SYSTEM_PROMPT


def _contains_cjk(value: str) -> bool:
    return bool(_CJK.search(value))


def _text_requests_audio(source_text: str) -> bool:
    if _GLOBAL_SILENCE.search(source_text):
        return False
    actionable_text = _NEGATED_AUDIO_INTENT.sub("", source_text)
    actionable_text = _AUDIO_UI_LITERAL.sub("", actionable_text)
    return bool(
        _AUDIO_INTENT.search(actionable_text)
        or _SPOKEN_DIALOGUE.search(actionable_text)
    )


def _task_scoped_text(
    document: NormalizedDocument,
    task: dict[str, Any],
    source_block_ids: list[object],
) -> str:
    valid_source_ids = {
        block_id for block_id in source_block_ids if isinstance(block_id, str)
    }
    scoped_text = [
        block.text
        for block in document.blocks
        if block.block_id in valid_source_ids and block.text
    ]
    for field_name in ("user_intent", "prompt"):
        value = task.get(field_name)
        if isinstance(value, str) and value:
            scoped_text.append(value)
    return "\n".join(scoped_text)


def _task_requests_audio(
    document: NormalizedDocument,
    task: dict[str, Any],
    source_block_ids: list[object],
) -> bool:
    return _text_requests_audio(_task_scoped_text(document, task, source_block_ids))


def _task_requests_silence(
    document: NormalizedDocument,
    task: dict[str, Any],
    source_block_ids: list[object],
) -> bool:
    """文档是否明确要求**全程静音**。

    声音现在默认开启（用户 2026-09-21：「所有文档默认把声音打开」），
    这里是唯一的例外判据：只有文档自己写明「全程静音 / 不要任何声音」
    才允许把 generate_audio 关掉。
    """
    return bool(
        _GLOBAL_SILENCE.search(
            _task_scoped_text(document, task, source_block_ids)
        )
    )


def _normalize_audit_report(report: AuditReport) -> AuditReport:
    normalized_issues: list[str] = []
    for issue in report.issues:
        normalized = issue.strip()
        if normalized.startswith("技术阻断"):
            if not _ACTIONABLE_HUMAN_HANDLING.search(normalized):
                normalized = (
                    f"{normalized.rstrip('。；; ')}；"
                    "人工处理：请补充可执行素材或确认替代方案后再继续。"
                )
            normalized_issues.append(normalized)
            continue
        if any(
            term in normalized
            for term in _GOAL_REJECTING_AUDIT_LANGUAGE
        ):
            for original, replacement in _GOAL_REJECTING_REWRITES.items():
                normalized = normalized.replace(original, replacement)
            if not normalized.startswith(("实施策略：", "风险缓释：")):
                normalized = f"实施策略：{normalized}"
        normalized_issues.append(normalized)
    return report.model_copy(update={"issues": normalized_issues})


def language_validation_message(issues: list[str]) -> str | None:
    fields = [
        issue.partition(":")[0]
        for issue in issues
        if _CJK_ISSUE_SUFFIX in issue
    ]
    if not fields:
        return None
    return f"以下字段{_CJK_ISSUE_SUFFIX}：{'、'.join(dict.fromkeys(fields))}"


def _compact_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _storyboard_fills(
    sources_list: list[list[str]],
    requirements: dict[str, list[str]],
) -> list[tuple[int, list[str]]]:
    """算出「哪些任务要补哪些分镜行 id」——返回 [(任务下标, 待补 id 列表)]。

    只补已经（部分）覆盖该表的任务；若整份计划只有一个视频任务、且完全没引用该表，
    也认它就是要覆盖这张表的那个任务。
    """
    if not sources_list or not requirements:
        return []
    fills: list[tuple[int, list[str]]] = []
    for table_id, required_ids in requirements.items():
        relevant = {table_id, *required_ids}
        indexes = [
            index
            for index, sources in enumerate(sources_list)
            if relevant.intersection(sources)
        ]
        if not indexes and len(sources_list) == 1:
            indexes = [0]
        for index in indexes:
            missing = [
                block_id
                for block_id in required_ids
                if block_id not in sources_list[index]
            ]
            if missing:
                fills.append((index, missing))
    return fills


def _fill_storyboard_source_block_ids(
    payload: dict[str, Any],
    document: NormalizedDocument,
) -> None:
    """（dict 形态）把分镜行 id 补进 payload —— 规划时用，见 reconcile_storyboard_sources。"""
    requirements = _storyboard_requirements(document)
    tasks = payload.get("tasks")
    if not requirements or not isinstance(tasks, list):
        return
    video_tasks = [
        task
        for task in tasks
        if isinstance(task, dict) and task.get("task_type") == "image_to_video"
    ]
    sources_list = [
        list(task.get("source_block_ids") or []) for task in video_tasks
    ]
    for index, missing in _storyboard_fills(sources_list, requirements):
        video_tasks[index]["source_block_ids"] = [
            *sources_list[index],
            *missing,
        ]


def reconcile_storyboard_sources(
    plan: TaskPlan,
    document: NormalizedDocument,
) -> TaskPlan:
    """把分镜表每一行镜头的 block id **确定性地**补进覆盖它的那个任务。

    2026-09-17 实测（超级大床）：分镜表 50+ 行，模型每次都会漏抄几行 → 契约校验
    「task … missing source_block_ids […]」必然失败，重试 3 次全废，而用户看到的
    是「模型三次返回的 JSON 均未通过校验」（其实 JSON 合法，是计划不合契约）。

    这些 id 我们本来就能从文档里算出来，不该让模型去枚举几十个不透明字符串。
    规划路径（`_fill_storyboard_source_block_ids`）与**重跑复制计划**路径
    （`GraphRuntime._persist_draft`）都走这里，保证进审批的计划一定齐。
    """
    requirements = _storyboard_requirements(document)
    if not requirements:
        return plan
    video_indexes = [
        index
        for index, task in enumerate(plan.tasks)
        if task.task_type == "image_to_video"
    ]
    if not video_indexes:
        return plan
    sources_list = [
        list(plan.tasks[index].source_block_ids) for index in video_indexes
    ]
    fills = _storyboard_fills(sources_list, requirements)
    if not fills:
        return plan
    tasks = list(plan.tasks)
    for position, missing in fills:
        index = video_indexes[position]
        task = tasks[index]
        tasks[index] = task.model_copy(
            update={
                "source_block_ids": [
                    *task.source_block_ids,
                    *missing,
                ]
            }
        )
    return plan.model_copy(update={"tasks": tasks})


#: 自动排除「没被具体使用」的素材时写的理由（要含中文，校验器会查）。
_UNUSED_ASSET_REASON = (
    "计划没有具体使用这张素材（未绑定到镜头或描述笼统），已自动排除"
)


def reconcile_negative_constraints(plan: TaskPlan) -> TaskPlan:
    """把每条任务「必须避免」里的**重复写法**合并掉（规则一条不少，只去重复措辞）。

    去重是在**返工合并**时生效的（`merge_negative_constraints`），但存量计划里仍
    带着上线前攒下的重复 —— 实测脱毛 57 条里有 10 条是同义换皮（「不要出现水印、
    Logo、品牌特征」的各种写法），而**重跑复制会把它们一起复制下去**，看起来就像
    「负面约束还在叠加」。复制/编辑计划时顺手清一遍即可。
    """
    tasks = list(plan.tasks)
    changed = False
    for index, task in enumerate(tasks):
        if not task.negative_constraints:
            continue
        merged = merge_negative_constraints(task.negative_constraints, [])
        if len(merged) != len(task.negative_constraints):
            tasks[index] = task.model_copy(
                update={"negative_constraints": merged}
            )
            changed = True
    if not changed:
        return plan
    return plan.model_copy(update={"tasks": tasks})


#: 【素材说明】段头 —— 契约要求规划器在这里**逐张**写素材说明。
_MATERIAL_SECTION_HEADING = "【素材说明】"
#: 下一个段头（`【…】`）的位置 = 素材说明段的结束边界。
_NEXT_SECTION_HEADING = re.compile(r"\n\s*【")


def _label_missing_reference_tokens(
    prompt: str,
    references: list[ImageReference],
    mime_types: Mapping[str, str],
    plan_items: list[dict[str, Any]],
) -> str:
    """给「描述写了、却漏了 `@图片N` 标记」的素材**补上标记**（2026-10-13）。

    **为什么要有这一步**：`unused_reference_assets` 把「token 不在 prompt 里」直接判成
    「素材没被用上」，于是 `_exclude_unused_references` 会**把用户真正想要的参考图删掉**。

    生产实锤（小猫钓鱼 run `eb572545` / 2026-10-13T01:11，用户原话「为什么居然排除了两张
    重要的参考图」）：提示词【素材说明】三段描述都在（垂钓画面 / 水中缠斗 / 角色三视图），
    只有第一段写了 `@图片1`；第 2、3 段漏了标记 → 两张重要参考图被自动排除，审批页给的理由
    还是「未绑定到镜头或描述笼统」（与事实不符）。对照：同晚鸭子任务标记写全 → 三张全保留。
    **模型偶发漏标记，不该由用户承担「参考图没了」。**

    补法（确定性，不调模型）：
      1. 【素材说明】里按位置补 `@图片N：` 前缀（第 i 条 ↔ 第 i 个素材，按 order 排）；
      2. 多分镜时 token 还必须落在某个**镜头段**里（`tokens_used_in_shots`）——
         按 `reference_plan[].segment_index` 把该素材的说明挂到对应镜头句尾
         （`role=global` 没有段落号，挂第 1 镜）。

    **刻意保守**：素材说明的非空条目数必须**等于**素材张数才动手，否则原样返回 ——
    把 A 的 `@图片N` 贴到 B 的描述上是更坏的错误（模型会照着错的图生成）。
    """
    try:
        tokens = reference_tokens(references, mime_types)
    except ValueError:
        return prompt
    missing = {
        asset_id: token for asset_id, token in tokens.items() if token not in prompt
    }
    if not missing:
        return prompt

    start = prompt.find(_MATERIAL_SECTION_HEADING)
    if start < 0:
        return prompt
    body_start = start + len(_MATERIAL_SECTION_HEADING)
    boundary = _NEXT_SECTION_HEADING.search(prompt, body_start)
    body_end = boundary.start() if boundary else len(prompt)
    lines = prompt[body_start:body_end].split("\n")
    entry_rows = [index for index, line in enumerate(lines) if line.strip()]
    ordered = sorted(references, key=lambda item: item.order)
    if len(entry_rows) != len(ordered):
        # 数不上就不猜：维持旧行为（交给下面的排除逻辑），绝不把标记贴错图。
        return prompt
    # 配对方式：**「没写标记的条目」↔「缺失的 token」，按数量配对**（不是按位置）。
    #
    # 为什么不用位置（2026-10-13 水晶鞋 run `8ea6c11a` 实锤）：规划器写了 5 条素材说明，
    # 但**只给 4 条打了标记**（漏掉「宫殿分镜图」那条），后面的标签整体错位 ——
    # 游戏界面成了 `@图片2`、公主 `@图片3`、女仆 `@图片4`，而那张分镜图在参考列表里排第 5、
    # token 是 `@图片5`。按位置补会把 `@图片5` 贴到「女仆」那条描述上（标签与内容对不上），
    # 而真正描述它的「宫殿」那条仍然没标记 → 图还是被判「没用上」摘掉。
    # 按「无标记条目 ↔ 缺失 token」配对则两种形态都对：条目本来就按素材顺序写，
    # 无标记的那条正是内容对不上号的那张图。
    labelled_entries: dict[str, str] = {}
    unlabelled_rows = [
        row
        for row in entry_rows
        if not any(token in lines[row] for token in tokens.values())
    ]
    missing_tokens = [
        tokens[item.asset_id]
        for item in ordered
        if item.asset_id in missing
    ]
    if not unlabelled_rows or len(unlabelled_rows) != len(missing_tokens):
        # 无标记条目数与缺失 token 数对不上 → 绝不猜（宁可维持旧行为）。
        return prompt
    for row, token in zip(unlabelled_rows, missing_tokens):
        text = lines[row].strip()
        lines[row] = f"{token}：{text}"
        asset_id = next(
            item.asset_id for item in ordered if tokens[item.asset_id] == token
        )
        labelled_entries[asset_id] = text
    labelled = prompt[:body_start] + "\n".join(lines) + prompt[body_end:]
    if not labelled_entries:
        return labelled

    # 步骤 2：多分镜里「只在素材说明段出现」仍然算没用于镜头段 —— 补进对应镜头。
    pending = [
        asset_id
        for asset_id in labelled_entries
        if tokens[asset_id] not in tokens_used_in_shots(labelled, tokens)
    ]
    if not pending or not has_multiple_shot_markers(labelled):
        return labelled
    rows = labelled.split("\n")
    # 镜头段的定位必须用**与校验同一套**的 `_SHOT_MARKER` 去 search，不能只看行首：
    # 这个技能模板的镜头行是 `0—6秒｜镜头1：…`（时间在前），行首匹配会一条都找不到
    # （2026-10-13 水晶鞋 `8ea6c11a` 实锤：素材说明补对了标记，却因为这里没落进镜头段，
    #  依旧被判「没用于镜头」而摘掉）。两种写法（`镜头1：` / `0—6秒｜镜头1：`）都要认。
    markers = list(_SHOT_MARKER.finditer(labelled))
    if len(markers) < 2:
        return labelled
    line_bounds: list[tuple[int, int]] = []
    offset = 0
    for line in rows:
        line_bounds.append((offset, offset + len(line)))
        offset += len(line) + 1
    shot_rows: list[tuple[int, int]] = []      # (行号, 第几个镜头)
    for marker_index, marker in enumerate(markers):
        for line_index, (start, end) in enumerate(line_bounds):
            if start <= marker.start() <= end:
                shot_rows.append((line_index, marker_index))
                break
    if not shot_rows:
        return labelled
    segment_of = {
        str(item.get("asset_id")): item.get("segment_index")
        for item in plan_items
        if isinstance(item, dict) and item.get("asset_id") is not None
    }
    for asset_id in pending:
        segment = segment_of.get(asset_id)
        target_index = 0
        if (
            isinstance(segment, int)
            and not isinstance(segment, bool)
            and 1 <= segment <= len(shot_rows)
        ):
            target_index = segment - 1
        row = next(
            (line_index for line_index, marker_index in shot_rows
             if marker_index == target_index),
            shot_rows[0][0],
        )
        # 只取正向描述（`；` 之前）：「不参考…」属于素材说明段的负向约束，塞进镜头句会
        # 让镜头段自相矛盾（实测补进镜头后读起来像「本镜不参考这张图」）。
        positive = labelled_entries[asset_id].split("；")[0].strip() or labelled_entries[asset_id]
        clause = f"（{tokens[asset_id]}：{positive}）"
        if clause in rows[row]:
            continue
        rows[row] = rows[row].rstrip() + clause
    return "\n".join(rows)


def _exclude_unused_references(
    payload: dict[str, Any],
    document: NormalizedDocument,
) -> None:
    """把「挂了却没被具体用起来」的素材摘掉，并写进 `excluded_assets`。

    2026-09-17 生产（超级大床 / 拿着吧你！2）：模型把素材挂在任务上、却只在开头
    罗列（或写得笼统），于是同时踩中
      「Seedance prompt 缺少素材引用 @图片N」
      「@图片N 只被罗列但没有用于任何实际镜头」
      「@图片N 必须绑定具体主体、场景、动作、运镜或声音」
    而覆盖门又要求「每个素材要么被引用、要么被排除」—— 两条规则互相夹住，模型三次
    重试全废，run 直接失败（用户「一直重跑但每次都失败」）。

    这些素材**本来就没有被具体使用**，所以摘掉不改变生成内容，只让计划自洽：
    从参考图里移除（prompt 里的 token 用 `remap_prompt_references` 一并重映射/
    清理）+ 写进 `excluded_assets`（理由写明，审批页「排除素材」里看得到）。

    **视频例外（2026-10-08 P0）**：视频是运动参考的唯一载体，它的 prompt 落点一旦被
    删就会留下没有主语的空洞（`参考  中连续动作…`），而且上游本来就支持
    `video_url + role=reference_video`。所以视频**不在这里摘**，交给契约逼 planner 把
    `@视频N` 写成具体运镜/动作约束；真需要摘也只在上游明确拒绝时由执行层降级。
    """
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        return
    mime_types = {
        asset.asset_id: asset.mime_type for asset in document.media_assets
    }
    video_ids = _video_asset_ids(document)
    raw_exclusions = payload.get("excluded_assets")
    exclusions = raw_exclusions if isinstance(raw_exclusions, list) else []
    excluded_ids = {
        item.get("asset_id")
        for item in exclusions
        if isinstance(item, dict)
    }
    changed = False
    for task in tasks:
        if not isinstance(task, dict) or task.get("task_type") != "image_to_video":
            continue
        prompt = task.get("prompt")
        raw_references = task.get("reference_images")
        if not isinstance(prompt, str) or not isinstance(raw_references, list):
            continue
        try:
            references = canonicalize_references(
                [ImageReference.model_validate(item) for item in raw_references]
            )
        except Exception:
            continue  # 引用本身不合法：交给校验器如实报错
        # 先补「描述了却漏写 @图片N」的标记（2026-10-13）：不补的话下面这条判定会把这些
        # 素材当成「没被用上」而**把用户真正想要的参考图删掉**（小猫钓鱼实锤）。
        labelled_prompt = _label_missing_reference_tokens(
            prompt,
            references,
            mime_types,
            [
                item
                for item in (task.get("reference_plan") or [])
                if isinstance(item, dict)
            ],
        )
        if labelled_prompt != prompt:
            prompt = labelled_prompt
            task["prompt"] = labelled_prompt
        unused = set(
            unused_reference_assets(
                prompt,
                references,
                mime_types,
                require_storyboard=True,
            )
        )
        # 视频不在这里摘（见 docstring）：它一旦被摘，prompt 里的运动指代就没有主语，
        # 用户明确抱怨的正是「规划阶段直接排除了参考视频」。
        unused -= video_ids
        if not unused:
            continue
        kept = [
            reference
            for reference in references
            if reference.asset_id not in unused
        ]
        if not kept:
            # 一张都没用上：别把视频任务悄悄变成纯文生视频，交给校验器报错。
            continue
        remaining = canonicalize_references(kept)
        # 摘 token 会在中文句子里断开（`只以@图片1为唯一基准` → `只以为唯一基准`）
        # → 宁可留着这个已被引用的素材（覆盖门照样过），也不要给模型一句缺主语的话。
        if reference_removal_leaves_holes(
            prompt, references, remaining, mime_types
        ):
            continue
        remapped = remap_prompt_references(
            prompt, references, remaining, mime_types
        )
        if set(dangling_reference_mentions(remapped)) - set(
            dangling_reference_mentions(prompt)
        ):
            continue
        task["prompt"] = remapped
        task["reference_images"] = [
            reference.model_dump(mode="json") for reference in remaining
        ]
        for asset_id in sorted(unused):
            if asset_id in excluded_ids:
                continue
            exclusions.append(
                {
                    "asset_id": asset_id,
                    "reason": _UNUSED_ASSET_REASON,
                }
            )
            excluded_ids.add(asset_id)
        changed = True
    if changed:
        payload["excluded_assets"] = exclusions


#: 视频素材在计划里必须写的 role。
#:
#: **2026-10-08 口径修正（P0 根因 ①）**：2026-09-17 的「视频只作分镜参考，不作为参考图」
#: 被实现成了「规划后**无条件**把视频从 reference_images 摘掉 + 写进 excluded_assets +
#: 用 remap 重写 prompt」。后果有三条，实测（刮太空沙 run 9bc3fa4b）：
#:   ① 规划器明明判了 `video-1 usable=true / role=global`（理由「文档明确指定的动作与
#:      音效参考」），却被应用在规划后抹掉 —— 用户看到的就是「规划阶段直接排除了参考视频」；
#:   ② token 重映射把 `@视频1` 删成了**空洞**（`参考  中连续动作…`），视频语义在文本里的
#:      唯一落点也没了，模型彻底失去运动参考；
#:   ③ 上游本来就支持 `video_url + role=reference_video`（integrations/seedance.py 的
#:      `_validate_submission` 明确放行），这条能力永远走不到。
#: 现在改成：**默认保留在 reference_images 里，role 一律规范成 `reference_video`**；
#: 只有上游明确拒绝时才在执行前降级成抽帧（graph/nodes.py，带可见事件）。
_VIDEO_REFERENCE_ROLE = "reference_video"


def _video_asset_ids(document: NormalizedDocument) -> set[str]:
    return {
        asset.asset_id
        for asset in document.media_assets
        if str(asset.mime_type).startswith("video/")
    }


def _retain_video_references(
    payload: dict[str, Any],
    document: NormalizedDocument,
) -> None:
    """视频素材**默认保留**为 `reference_video`，不再无条件摘除。

    只做三件确定性的事，绝不删引用、绝不动 prompt（token 必须原样留着，否则会留下
    没有主语的空洞）：
      1. 视频引用的 role 规范成 `reference_video`（模型可能写成 `reference_image`）；
      2. 把「已被引用的视频」从 `excluded_assets` 里去掉 —— 覆盖门不允许同一素材
         既被引用又被排除，否则审批按钮永远是灰的；
      3. 其余什么都不做：真正要不要降级由执行层按上游是否拒绝决定。
    """
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        return
    video_ids = _video_asset_ids(document)
    if not video_ids:
        return
    referenced: set[str] = set()
    mime_types = {
        asset.asset_id: asset.mime_type for asset in document.media_assets
    }
    for task in tasks:
        if not isinstance(task, dict) or task.get("task_type") != "image_to_video":
            continue
        raw_references = task.get("reference_images")
        if not isinstance(raw_references, list):
            continue
        try:
            references = canonicalize_references(
                [ImageReference.model_validate(item) for item in raw_references]
            )
        except Exception:
            continue  # 引用本身不合法：交给校验器如实报错
        updated = [
            reference.model_copy(update={"role": _VIDEO_REFERENCE_ROLE})
            if reference.asset_id in video_ids
            and reference.role != _VIDEO_REFERENCE_ROLE
            else reference
            for reference in references
        ]
        if updated != references:
            task["reference_images"] = [
                reference.model_dump(mode="json") for reference in updated
            ]
        for reference in updated:
            if reference.asset_id in video_ids:
                referenced.add(reference.asset_id)
        # 让 `remap_prompt_references` 认得抽帧别名（历史契约里的 `video-1-frame`）。
        for asset_id in video_ids:
            mime_types.setdefault(
                f"{asset_id}{_FRAME_SUFFIX}", mime_types.get(asset_id, "video/mp4")
            )
    if not referenced:
        return
    raw_exclusions = payload.get("excluded_assets")
    if not isinstance(raw_exclusions, list):
        return
    kept_exclusions = []
    for item in raw_exclusions:
        if isinstance(item, dict):
            asset_id = item.get("asset_id")
            if isinstance(asset_id, str):
                base = (
                    asset_id[: -len(_FRAME_SUFFIX)]
                    if asset_id.endswith(_FRAME_SUFFIX)
                    else asset_id
                )
                if base in referenced:
                    continue  # 被引用的视频不能同时被排除（覆盖门会判自相矛盾）
        kept_exclusions.append(item)
    if len(kept_exclusions) != len(raw_exclusions):
        payload["excluded_assets"] = kept_exclusions


def _storyboard_requirements(
    document: NormalizedDocument,
) -> dict[str, list[str]]:
    """Return content block IDs for explicitly marked storyboard tables.

    A table is treated as a storyboard when at least two rows begin with an
    explicit ``镜头 N`` marker, or when a ``镜头``/``镜号`` header is followed
    by at least two rows numbered consecutively from 1 in the primary column.
    Only detected shot rows contribute required block IDs; headers are excluded.
    """
    requirements: dict[str, list[str]] = {}
    tables = sorted(
        (
            block
            for block in document.blocks
            if block.block_type == "table"
        ),
        key=lambda block: (block.order, block.block_id),
    )
    for table in tables:
        cells = {
            block.block_id: block
            for block in document.blocks
            if block.block_type == "table_cell"
            and block.parent_id == table.block_id
            and isinstance(block.table_row, int)
        }
        row_content: dict[int, list[Any]] = {}
        cell_content: dict[str, list[Any]] = {}
        for block in document.blocks:
            if block.block_type in {"table", "table_cell"}:
                continue
            cell_id = next(
                (
                    path_part
                    for path_part in block.path
                    if path_part in cells
                ),
                None,
            )
            if cell_id is not None:
                row = cells[cell_id].table_row
                if row is not None:
                    row_content.setdefault(row, []).append(block)
                    cell_content.setdefault(cell_id, []).append(block)

        shot_rows = {
            row
            for row, blocks in row_content.items()
            if any(
                block.block_type == "text"
                and bool(_STORYBOARD_ROW_MARKER.match(block.text))
                for block in blocks
            )
        }
        if len(shot_rows) < 2:
            shot_rows = set()
            header_rows = sorted(
                row
                for row, blocks in row_content.items()
                if any(
                    block.block_type == "text"
                    and bool(_STORYBOARD_HEADER.fullmatch(block.text))
                    for block in blocks
                )
            )
            for header_row in header_rows:
                numbered_rows: list[tuple[int, int]] = []
                for row in sorted(row_content):
                    if row <= header_row:
                        continue
                    row_cells = sorted(
                        (
                            cell
                            for cell in cells.values()
                            if cell.table_row == row
                            and cell.block_id in cell_content
                        ),
                        key=lambda cell: (
                            cell.table_column
                            if cell.table_column is not None
                            else 10**9,
                            cell.order,
                            cell.block_id,
                        ),
                    )
                    if not row_cells:
                        continue
                    primary_blocks = cell_content[row_cells[0].block_id]
                    number_match = next(
                        (
                            _STORYBOARD_ROW_NUMBER.fullmatch(block.text)
                            for block in primary_blocks
                            if block.block_type == "text"
                            and _STORYBOARD_ROW_NUMBER.fullmatch(block.text)
                        ),
                        None,
                    )
                    if number_match is not None:
                        numbered_rows.append(
                            (row, int(number_match.group(1)))
                        )
                numbers = [number for _, number in numbered_rows]
                if (
                    len(numbered_rows) >= 2
                    and numbers == list(range(1, len(numbered_rows) + 1))
                ):
                    shot_rows = {row for row, _ in numbered_rows}
                    break

        if len(shot_rows) < 2:
            continue

        content_blocks = sorted(
            (
                block
                for row, blocks in row_content.items()
                if row in shot_rows
                for block in blocks
            ),
            key=lambda block: (block.order, block.block_id),
        )
        requirements[table.block_id] = [
            block.block_id for block in content_blocks
        ]
    return requirements


def _append_duration_segmentation_hints(payload: dict[str, Any]) -> None:
    """②-a-2：给「长时长但只写了一整段时间轴」的视频任务补一条非阻塞提示。

    刻意写进 `warnings` 而不是 `blocking_issues`：真实语料里 504 条 duration>=6 的
    任务有 243 条是这个形态，全拦会造成大面积误伤（详见 reference_contract 里
    `single_whole_clip_timeline_hint` 的注释）。
    """
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        return
    for task in tasks:
        if not isinstance(task, dict) or task.get("task_type") != "image_to_video":
            continue
        hint = single_whole_clip_timeline_hint(
            task.get("prompt"), task.get("duration")
        )
        if not hint:
            continue
        warnings = [
            item for item in (task.get("warnings") or []) if isinstance(item, str)
        ]
        if hint not in warnings:
            warnings.append(hint)
        task["warnings"] = warnings


def _normalize_generated_plan_payload(
    payload: dict[str, Any],
    document: NormalizedDocument,
) -> list[str]:
    issues: list[str] = []
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        return issues
    valid_block_ids = {block.block_id for block in document.blocks}
    mime_types = {
        asset.asset_id: asset.mime_type
        for asset in document.media_assets
    }
    document_asset_order = {
        asset.asset_id: index
        for index, asset in enumerate(document.media_assets)
    }
    for task_index, task in enumerate(tasks):
        if not isinstance(task, dict):
            continue
        source_block_ids = task.get("source_block_ids")
        if isinstance(source_block_ids, list):
            task["source_block_ids"] = [
                block_id
                for block_id in source_block_ids
                if isinstance(block_id, str)
                and block_id in valid_block_ids
            ]
        if task.get("task_type") == "image_to_video":
            task["image_size"] = None
            # Provider and candidate count are runtime/human policy, not a
            # creative-planning decision.
            task["video_provider"] = None
            task["output_count"] = 1
            # 声音也属于交付策略，不该由模型自由裁量：**默认开启**（用户
            # 2026-09-21：「现在所有文档默认把声音打开」）。旧行为是「只有文档
            # 明写对白/音效/配音/BGM 才 true」，没提声音的文档留空，提交时
            # bool(None) → False 变成静音 —— 这就是「有的文档不会开启声音」。
            # 唯一例外：文档明确要求全程静音。
            if not _task_requests_silence(
                document, task, task.get("source_block_ids") or []
            ):
                task["generate_audio"] = True
        raw_references = task.get("reference_images")
        prompt = task.get("prompt")
        if not isinstance(raw_references, list) or not isinstance(prompt, str):
            continue
        try:
            references = canonicalize_references(
                [
                    ImageReference.model_validate(reference)
                    for reference in raw_references
                ]
            )
            selected_asset_ids = [
                reference.asset_id for reference in references
            ]
            if all(
                asset_id in document_asset_order
                for asset_id in selected_asset_ids
            ):
                expected_asset_ids = sorted(
                    selected_asset_ids,
                    key=document_asset_order.__getitem__,
                )
                if selected_asset_ids != expected_asset_ids:
                    issues.append(
                        f"tasks[{task_index}].reference_images: "
                        "必须按文档素材顺序排列，期望 "
                        f"{expected_asset_ids!r}"
                    )
                    continue
            task["prompt"] = remap_asset_id_tokens(
                prompt,
                references,
                mime_types,
            )
            task["reference_images"] = [
                reference.model_dump(mode="json")
                for reference in references
            ]
            if task.get("task_type") == "image_to_image":
                # 图片任务参考图一多，模型常漏写个别 @图片N（实测 21 张图漏
                # 3 张），校验因此判「缺少素材引用」连挂 3 次。token 编号与
                # validate_image_prompt 同源（canonicalize 后 order 即列表
                # 位次），漏掉的直接补进 prompt 尾部，不靠模型重试。
                missing_tokens = [
                    token
                    for token in reference_tokens(
                        references, mime_types
                    ).values()
                    if token not in task["prompt"]
                ]
                if missing_tokens:
                    task["prompt"] = (
                        f"{task['prompt']}，画面风格严格参考 "
                        f"{'、'.join(missing_tokens)}"
                    )
        except (TypeError, ValueError):
            continue
    return issues


#: 视频素材抽帧后的素材 id 后缀（见 graph/nodes.py 的 analyze_images）。
_FRAME_SUFFIX = "-frame"


def _register_video_frame_aliases(assets: dict[str, Any]) -> None:
    """让「视频抽出来的那一帧」被当作原视频的别名。

    视频不能直接当参考图，执行前 `graph/nodes.py` 的
    `_replace_video_references_with_frames` 会把 `video-1` 抽帧成 `video-1-frame`
    再交给供应商；而**计划里引用的可能是帧、校验查的是文档里的原始素材** —— 不认这个
    别名就会同时报「unknown asset_id video-1-frame」和「uncovered successful asset
    video-1」（2026-09-17 超级大床实测，用户看到的「一直都有问题」）。

    注：抽帧发生在**执行前**（2026-09-24 修复）。`analyze_images` 自 2026-09-17 起
    只给视频补分镜参考描述、不再抽帧，所以这里认别名不是为了分析阶段，而是为了
    「引用帧 = 引用原视频」的覆盖统计与前端展示。
    """
    for asset_id, asset in list(assets.items()):
        if str(getattr(asset, "mime_type", "")).startswith("video/"):
            assets.setdefault(f"{asset_id}{_FRAME_SUFFIX}", asset)


def _asset_base_id(asset_id: str, assets: dict[str, Any]) -> str:
    """把抽帧素材归回原视频（`video-1-frame` → `video-1`），覆盖统计按原素材算。"""
    if asset_id.endswith(_FRAME_SUFFIX):
        base = asset_id[: -len(_FRAME_SUFFIX)]
        if base in assets:
            return base
    return asset_id


#: 文档里「不要切镜头」的说法（逐词匹配）。
#:
#: 刻意**不收**光秃秃的「一镜」—— 它会被「第一镜/第二镜/这一镜」误伤（实测
#: 「第一镜近景、第二镜全景」被判成了一镜到底）。
_SINGLE_TAKE_KEYWORDS = (
    "一镜到底",
    "一镜完成",
    "一镜拍完",
    "长镜头",
    "单镜头",
    "一个连续镜头",
    "不切镜头",
    "不要切镜头",
    "无剪辑",
    "不剪辑",
    "连续拍摄",
    "不断镜",
    "不换镜",
    "跟拍到底",
    "全程跟拍",
)

#: 「否定 + 镜头/剪辑」的组合说法（逐词表列不全，用正则兜）。
#:
#: 例：「不要切换镜头」「镜头不切换」「不需要剪辑」「不分镜」。
_SINGLE_TAKE_PATTERNS = (
    re.compile(
        r"(不|无需|不用|禁止|不要|别|不得)[^，。；\n]{0,6}"
        r"(切镜头|切换镜头|换镜头|剪辑|分镜|切镜|断镜)"
    ),
    re.compile(r"镜头[^，。；\n]{0,4}不(切|换|断)"),
    re.compile(r"一(个)?镜头(拍|做|完成)到底"),
    # 「一个镜头拍完」是一镜到底，但「下一个镜头」不是 —— 必须排掉前置的
    # 下/前/上/这/那/每（实测「视频第9秒切换到下一个镜头」被误判成一镜到底）。
    re.compile(r"(?<![下前上这那每])一个镜头"),
)


def _document_requests_single_take(document: NormalizedDocument) -> bool:
    """文档有没有明确要求一镜到底（长镜头 / 单镜头 / 不切镜头 / 连续拍摄）。

    用户 2026-09-22：「明明我的剧本规定了一镜到底啥的，只要一个镜头就能做完的视频
    他还是会分成几个镜头」—— 契约里「分 2-4 个镜头」的节奏建议是**无条件**写的，
    直接压过了文档要求，所以这里加一道硬校验兜住。
    """
    text = f"{getattr(document, 'text_view', '') or ''}"
    if any(keyword in text for keyword in _SINGLE_TAKE_KEYWORDS):
        return True
    return any(pattern.search(text) for pattern in _SINGLE_TAKE_PATTERNS)


def _timeline_segment_count(prompt: str) -> int:
    """提示词里写了几段时间轴（用来判断有没有被拆成多个镜头）。"""
    return len(_TIMELINE_SEGMENT.findall(prompt))


# --- ③ 单镜头文档禁止拆分（2026-10-10，P1） ---------------------------------
#
# 实测根因（「刮太空沙」）：文档分镜表**只有 1 行**，生成的提示词却是
# `0—4秒｜镜头1 / 4—8秒｜镜头2：…` + 「采用2段镜头快速剪辑」→ 成片精确在 4.0s
# 硬切（6 条里 5 条）。seedance 把「镜头 N」「N 段剪辑」当成字面指令执行。
#
# 判据（关键：区分「分镜指令」与「同一镜头内的事件描述」）：
#   禁止 = **把整片切开**的结构指令：出现 ≥2 的镜头序号（镜头2/第二镜/第2个镜头）、
#          计数式拆分（N 段镜头 / 分 N 段 / 切成 N 段）、剪辑节奏指令
#          （快速剪辑 / 剪辑节奏 / 剪辑点…），或把「硬切」**配上明确时间点**
#          （「约第4秒硬切」= 把切点写成指令）。
#   放行 = ① 文档自己就把「硬切」写在**同一行分镜的事件**里（刮太空沙的文档原文
#          「随后画面硬切到另一个角，继续匀速刮下」）—— 提示词复述它是描述这个镜头
#          里发生的事，不是发明第二个镜头；② 否定式（不要硬切/无剪辑/禁止切镜）；
#          ③ 光秃秃的「硬切到另一个角」不带时间点。
# 刻意**不**把「硬切」一概禁掉：生产里被验收通过的单镜头提示词
# （run 545641d2 / 9bc3fa4b）都写了「硬切到另一个角」，一刀切会误伤正确产出。
_SHOT_SPLIT_DIRECTIVES: tuple[re.Pattern[str], ...] = (
    # 「镜头 2」及以后（含「第 2 镜」「第3个镜头」）
    re.compile(r"镜头\s*(?:[2-9]|\d{2,})"),
    re.compile(r"第\s*(?:[二三四五六七八九十\d]+)\s*个?\s*镜头"),
    re.compile(r"第\s*[二三四五六七八九十]+\s*镜"),
    # 计数式拆分：「2 段镜头」「两段镜头」「3 个镜头」。
    # ⚠️ 必须要求**数量 >= 2**：真实语料里「本段只保留 1 个镜头」是**正确声明**，
    # 把 1 也收进来会把正确产出判成拆分（首轮实测 62 条命中里 40 条是这个误伤）。
    re.compile(r"(?:[2-9]|\d{2,}|[两二三四五六七八九十])\s*(?:段|个)\s*镜头"),
    re.compile(
        r"(?:分|切|拆|划)\s*(?:成)?\s*(?:[2-9]|\d{2,}|[两二三四五六七八九十])\s*段"
    ),
    # 剪辑节奏指令
    re.compile(r"(?:快速剪辑|连续剪辑|剪辑节奏|剪辑点|多段剪辑|加速剪辑)"),
)
#: 「硬切」刻意**不入表**（判据见上面 ③ 段落注释的「放行」第 ①/③ 条）：
#: 生产里被验收通过的单镜头提示词（run 9bc3fa4b）写的是
#: 「约第4秒按文档事件要求硬切到立方体另一侧棱角」—— 那是文档同一行【事件】里的
#: 内容，属于「同一镜头内的事件描述」。加「硬切 + 时间点」规则会把它误判成指令
#: （首轮实测命中），所以硬切只在**同时出现第二个镜头序号**时才连同被拦。
#: 命中处**前面**几个字是否定/禁止 —— 「不要镜头 2」「无剪辑」不是指令。
_NEGATION_BEFORE = re.compile(r"(?:不|无|没|禁|别|勿|免)[^，。；\n]{0,3}$")
#: 提示词自己声明「全片只有一个镜头」的写法。
_SINGLE_SHOT_CLAIM = re.compile(
    r"(?:全片|全段|整段|整片|本段)[^，。；\n]{0,6}(?:只|仅)[^，。；\n]{0,4}"
    r"(?:一|1)\s*(?:个)?\s*镜头"
    r"|只有(?:这一)?(?:一|1)\s*(?:个)?\s*镜头"
    r"|不增加额外镜头"
    r"|全片唯一镜头"
)


def _shot_split_directives(prompt: str) -> list[str]:
    """提示词里 planner 自己发明的「分镜 / 剪辑」指令（去掉否定式命中）。"""
    hits: list[str] = []
    for pattern in _SHOT_SPLIT_DIRECTIVES:
        for match in pattern.finditer(prompt):
            before = prompt[max(0, match.start() - 4): match.start()]
            if _NEGATION_BEFORE.search(before):
                continue
            hits.append(match.group(0).strip())
    return list(dict.fromkeys(hits))


def _storyboard_shot_count(document: NormalizedDocument) -> int:
    """文档**显式**写了几行分镜；识别不出（不是分镜表）时返回 0。

    与 `_storyboard_requirements` 用同一套识别口径（`镜头 N` 标记行，或
    「镜头/镜号」表头 + 从 1 连续编号的主列），区别只有一个：这里**保留 1 行**的
    结果（那个函数 `len(shot_rows) < 2` 就整体放弃，因为它的用途是"分配素材"）。
    """
    tables = sorted(
        (block for block in document.blocks if block.block_type == "table"),
        key=lambda block: (block.order, block.block_id),
    )
    best = 0
    for table in tables:
        cells = {
            block.block_id: block
            for block in document.blocks
            if block.block_type == "table_cell"
            and block.parent_id == table.block_id
            and isinstance(block.table_row, int)
        }
        row_content: dict[int, list[Any]] = {}
        cell_content: dict[str, list[Any]] = {}
        for block in document.blocks:
            if block.block_type in {"table", "table_cell"}:
                continue
            cell_id = next(
                (part for part in block.path if part in cells),
                None,
            )
            if cell_id is None:
                continue
            row = cells[cell_id].table_row
            if row is None:
                continue
            row_content.setdefault(row, []).append(block)
            cell_content.setdefault(cell_id, []).append(block)
        marker_rows = {
            row
            for row, blocks in row_content.items()
            if any(
                block.block_type == "text"
                and bool(_STORYBOARD_ROW_MARKER.match(block.text))
                for block in blocks
            )
        }
        if marker_rows:
            # 与 `_storyboard_requirements` 同一道护栏：**只有 >= 2 个标记行**才算
            # 「按镜头标记编号的分镜表」。少了这道护栏，PS 列里一句
            # 「镜头1-3背景音配上…」（真实语料 `1ee2ab1d`）会被当成唯一标记行，
            # 把 5 行分镜的文档数成 1 行 → 反过来拦掉正确的多镜头提示词。
            if len(marker_rows) >= 2:
                best = max(best, len(marker_rows))
                continue
        header_rows = sorted(
            row
            for row, blocks in row_content.items()
            if any(
                block.block_type == "text"
                and bool(_STORYBOARD_HEADER.fullmatch(block.text))
                for block in blocks
            )
        )
        for header_row in header_rows:
            numbered: list[int] = []
            for row in sorted(row_content):
                if row <= header_row:
                    continue
                row_cells = sorted(
                    (
                        cell
                        for cell in cells.values()
                        if cell.table_row == row and cell.block_id in cell_content
                    ),
                    key=lambda cell: (
                        cell.table_column if cell.table_column is not None else 10**9,
                        cell.order,
                        cell.block_id,
                    ),
                )
                if not row_cells:
                    continue
                number_match = next(
                    (
                        _STORYBOARD_ROW_NUMBER.fullmatch(block.text)
                        for block in cell_content[row_cells[0].block_id]
                        if block.block_type == "text"
                        and _STORYBOARD_ROW_NUMBER.fullmatch(block.text)
                    ),
                    None,
                )
                if number_match is not None:
                    numbered.append(int(number_match.group(1)))
            if numbered and numbered == list(range(1, len(numbered) + 1)):
                best = max(best, len(numbered))
                break
    return best


def _single_shot_basis(
    document: NormalizedDocument,
    prompt: object,
) -> str | None:
    """「本片必然只有一个镜头」的依据；三条判据任一成立即可，识别不出返回 None。"""
    if _storyboard_shot_count(document) == 1:
        return "文档分镜表只有 1 行"
    if _document_requests_single_take(document):
        return "文档明确要求一镜到底"
    if isinstance(prompt, str):
        match = _SINGLE_SHOT_CLAIM.search(prompt)
        if match is not None:
            return f"提示词自己声明「{match.group(0)}」"
    return None


def validate_plan(
    plan: TaskPlan | dict[str, Any],
    document: NormalizedDocument,
    max_output_count: int,
    *,
    enforce_seedance_prompt_contract: bool = False,
    enforce_image_prompt_contract: bool = False,
) -> list[str]:
    payload: Any
    if isinstance(plan, TaskPlan):
        payload = plan.model_dump(mode="json")
    else:
        payload = plan

    issues: list[str] = []
    if not isinstance(payload, dict):
        return ["plan: must be a JSON object"]
    #: 文档自己有没有要求「一镜到底」——有的话，任何视频任务都不许拆成多个镜头。
    document_requests_single_take = _document_requests_single_take(document)
    document_summary = payload.get("document_summary")
    if not isinstance(document_summary, str) or not _contains_cjk(
        document_summary
    ):
        issues.append(f"plan.document_summary: {_CJK_ISSUE_SUFFIX}")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        return ["plan.tasks: must be a list"]
    if not tasks:
        return ["plan.tasks: at least one generation task is required"]

    block_ids = {block.block_id for block in document.blocks}
    assets = {asset.asset_id: asset for asset in document.media_assets}
    _register_video_frame_aliases(assets)
    storyboard_requirements = _storyboard_requirements(document)
    task_ids: set[str] = set()
    referenced_asset_ids: set[str] = set()
    total_output_count = 0
    task_sources: list[
        tuple[str, str | None, set[str], dict[str, Any]]
    ] = []

    for index, task in enumerate(tasks):
        prefix = f"tasks[{index}]"
        if not isinstance(task, dict):
            issues.append(f"{prefix}: must be a JSON object")
            continue

        task_id = task.get("task_id")
        display_id = task_id if isinstance(task_id, str) and task_id else prefix
        if not isinstance(task_id, str) or not task_id:
            issues.append(f"{prefix}.task_id: must be a non-empty string")
        elif task_id in task_ids:
            issues.append(f"{prefix}.task_id: duplicate task_id {task_id}")
        else:
            task_ids.add(task_id)

        for field_name in ("user_intent", "prompt"):
            value = task.get(field_name)
            if not isinstance(value, str) or not _contains_cjk(value):
                issues.append(f"{prefix}.{field_name}: {_CJK_ISSUE_SUFFIX}")

        # ②-a-1（2026-10-10，P0）：正文自述时长必须与 duration 一致。
        #
        # 放在 validate_plan 的**常开路径**（不放进 enforce_seedance_prompt_contract
        # 分支）是刻意的：审批页改时长走的是 revalidate_approval →
        # validate_plan(不传 enforce 标志)，而 r5 的事故正是「审批页改了时长、
        # 正文没改、应用不校验」（成片前 4.08 秒画面冻结）。只有常开才拦得住。
        if task.get("task_type") == "image_to_video":
            issues.extend(
                duration_consistency_issues(task.get("prompt"), task.get("duration"))
            )

        task_type = task.get("task_type")
        if (
            not isinstance(task_type, str)
            or task_type not in _ALLOWED_TASK_TYPES
        ):
            issues.append(
                f"{prefix}.task_type: must be image_to_image or image_to_video"
            )

        source_block_ids = task.get("source_block_ids")
        if not isinstance(source_block_ids, list) or not source_block_ids:
            issues.append(
                f"{prefix}.source_block_ids: must contain at least one block_id"
            )
            source_block_ids = []
        else:
            for block_id in source_block_ids:
                if not isinstance(block_id, str) or block_id not in block_ids:
                    issues.append(
                        f"{prefix}.source_block_ids: unknown block_id {block_id!r}"
                    )
        valid_source_ids = {
            block_id
            for block_id in source_block_ids
            if isinstance(block_id, str) and block_id in block_ids
        }
        task_sources.append(
            (
                display_id,
                task_type if isinstance(task_type, str) else None,
                valid_source_ids,
                task,
            )
        )

        references = task.get("reference_images")
        if references is None:
            references = []
        elif not isinstance(references, list):
            issues.append(f"{prefix}.reference_images: must be a JSON array")
            references = []
        task_reference_ids: list[str] = []
        for reference_index, reference in enumerate(references):
            reference_prefix = (
                f"{prefix}.reference_images[{reference_index}]"
            )
            if not isinstance(reference, dict):
                issues.append(f"{reference_prefix}: must be a JSON object")
                continue
            asset_id = reference.get("asset_id")
            if not isinstance(asset_id, str) or not asset_id:
                issues.append(
                    f"{reference_prefix}.asset_id: must be a non-empty string"
                )
                continue
            task_reference_ids.append(asset_id)
            # 引用「视频抽出来的帧」＝引用那个视频，否则覆盖统计会把原视频记成未使用。
            referenced_asset_ids.add(_asset_base_id(asset_id, assets))
            asset = assets.get(asset_id)
            if asset is None:
                issues.append(
                    f"{reference_prefix}.asset_id: unknown asset_id {asset_id}"
                )
                continue
            if (
                asset.download_error is not None
                or asset.size <= 0
                or not asset.sha256
                or not asset.local_path.is_file()
            ):
                issues.append(
                    f"{reference_prefix}.asset_id: asset {asset_id} download failed"
                )
            role = reference.get("role")
            mime_matches_role = (
                asset.mime_type.startswith("image/")
                and role in {"reference_image", "first_frame", "last_frame"}
            ) or (
                asset.mime_type.startswith("video/")
                and role == "reference_video"
            ) or (
                asset.mime_type.startswith("audio/")
                and role == "reference_audio"
            )
            if not mime_matches_role:
                expected_mime = (
                    "image"
                    if role in {"reference_image", "first_frame", "last_frame"}
                    else "video"
                    if role == "reference_video"
                    else "audio"
                )
                issues.append(
                    f"{reference_prefix}.asset_id: asset {asset_id} must have "
                    f"{expected_mime} MIME for role {role}"
                )
        duplicate_reference_ids = {
            asset_id
            for asset_id in task_reference_ids
            if task_reference_ids.count(asset_id) > 1
        }
        for asset_id in sorted(duplicate_reference_ids):
            issues.append(
                f"{prefix}.reference_images: duplicate asset_id {asset_id}"
            )

        reference_mode = task.get("reference_mode")
        if reference_mode not in {None, "multi_reference", "first_last_frame"}:
            issues.append(
                f"{prefix}.reference_mode: must be multi_reference or first_last_frame"
            )
        roles = [
            reference.get("role")
            for reference in references
            if isinstance(reference, dict)
        ]
        if task_type == "image_to_image":
            if reference_mode == "first_last_frame":
                issues.append(
                    f"{prefix}.reference_mode: 图生图只能使用多参考模式"
                )
            elif any(role != "reference_image" for role in roles):
                issues.append(
                    f"{prefix}.reference_images: 图生图只接受普通参考图"
                )
        elif task_type == "image_to_video":
            if reference_mode == "first_last_frame":
                ordered_roles = [
                    reference.get("role")
                    for reference in sorted(
                        (
                            reference
                            for reference in references
                            if isinstance(reference, dict)
                            and isinstance(reference.get("order"), int)
                        ),
                        key=lambda reference: reference["order"],
                    )
                ]
                if ordered_roles != ["first_frame", "last_frame"]:
                    issues.append(
                        f"{prefix}.reference_mode: 首尾帧模式必须且只能按顺序指定一张首帧和一张尾帧"
                    )
            elif reference_mode == "multi_reference" and any(
                role not in {
                    "reference_image",
                    "reference_video",
                    "reference_audio",
                }
                for role in roles
            ):
                issues.append(
                    f"{prefix}.reference_mode: 多参考模式只能使用普通参考图、参考视频或参考音频"
                )

        if task_type == "image_to_image":
            if not isinstance(task.get("image_size"), str) or not task.get(
                "image_size"
            ):
                issues.append(
                    f"{prefix}.image_size: required for image_to_image"
                )
            for field_name in ("duration", "resolution", "generate_audio"):
                if task.get(field_name) is not None:
                    issues.append(
                        f"{prefix}.{field_name}: not allowed for image_to_image"
                    )
        elif task_type == "image_to_video":
            if not isinstance(task.get("duration"), int) or isinstance(
                task.get("duration"), bool
            ):
                issues.append(
                    f"{prefix}.duration: required for image_to_video"
                )
            if not isinstance(task.get("resolution"), str) or not task.get(
                "resolution"
            ):
                issues.append(
                    f"{prefix}.resolution: required for image_to_video"
                )
            if task.get("image_size") is not None:
                issues.append(
                    f"{prefix}.image_size: not allowed for image_to_video"
                )
            generate_audio = task.get("generate_audio")
            if generate_audio is not None and not isinstance(
                generate_audio, bool
            ):
                issues.append(
                    f"{prefix}.generate_audio: must be true, false, or omitted"
                )
            elif (
                _task_requests_audio(document, task, source_block_ids)
                and generate_audio is not True
            ):
                issues.append(
                    f"{prefix}.generate_audio: "
                    "文档明确要求对白、音效、配音、环境音或音乐时必须为 true"
                )

        output_count = task.get("output_count", 1)
        if (
            not isinstance(output_count, int)
            or isinstance(output_count, bool)
            or output_count < 1
        ):
            issues.append(f"{prefix}.output_count: must be an integer >= 1")
        else:
            total_output_count += output_count

    if total_output_count > max_output_count:
        issues.append(
            "plan.total output_count: "
            f"{total_output_count} exceeds max_output_count {max_output_count}"
        )

    raw_exclusions = payload.get("excluded_assets", [])
    excluded_asset_ids: list[str] = []
    if not isinstance(raw_exclusions, list):
        issues.append("plan.excluded_assets: must be a list")
        raw_exclusions = []
    for index, exclusion in enumerate(raw_exclusions):
        prefix = f"plan.excluded_assets[{index}]"
        if not isinstance(exclusion, dict):
            issues.append(f"{prefix}: must be a JSON object")
            continue
        asset_id = exclusion.get("asset_id")
        reason = exclusion.get("reason")
        if not isinstance(asset_id, str) or not asset_id:
            issues.append(f"{prefix}.asset_id: must be a non-empty string")
            continue
        excluded_asset_ids.append(_asset_base_id(asset_id, assets))
        asset = assets.get(asset_id)
        if asset is None:
            issues.append(f"{prefix}.asset_id: unknown asset_id {asset_id}")
        elif asset.download_error is not None:
            issues.append(
                f"{prefix}.asset_id: failed asset {asset_id} cannot be excluded"
            )
        if not isinstance(reason, str) or not _contains_cjk(reason):
            issues.append(f"{prefix}.reason: {_CJK_ISSUE_SUFFIX}")

    duplicate_exclusions = {
        asset_id
        for asset_id in excluded_asset_ids
        if excluded_asset_ids.count(asset_id) > 1
    }
    for asset_id in sorted(duplicate_exclusions):
        issues.append(
            f"plan.excluded_assets: duplicate asset_id {asset_id}"
        )

    excluded_set = set(excluded_asset_ids)
    for asset_id in sorted(referenced_asset_ids.intersection(excluded_set)):
        issues.append(
            "plan.excluded_assets: referenced asset "
            f"{asset_id} cannot also be excluded"
        )
    successful_asset_ids = {
        asset.asset_id
        for asset in document.media_assets
        if asset.download_error is None
    }
    uncovered = (
        successful_asset_ids - referenced_asset_ids - excluded_set
    )
    for asset_id in sorted(uncovered):
        issues.append(
            f"plan.asset_coverage: uncovered successful asset {asset_id}"
        )

    storyboard_task_names: set[str] = set()
    for table_id, required_ids in storyboard_requirements.items():
        relevant_ids = {table_id, *required_ids}
        relevant_tasks = [
            task
            for task in task_sources
            if task[2].intersection(relevant_ids)
        ]
        storyboard_task_names.update(task[0] for task in relevant_tasks)
        if len(relevant_tasks) != 1:
            issues.append(
                f"storyboard table {table_id}: exactly one image_to_video task "
                f"must cover all content blocks {required_ids!r}; "
                f"found {len(relevant_tasks)}"
            )
            continue

        task_name, task_type, source_ids, _ = relevant_tasks[0]
        if task_type != "image_to_video":
            issues.append(
                f"storyboard table {table_id}: task {task_name} must be "
                "image_to_video"
            )
        missing_ids = [
            block_id
            for block_id in required_ids
            if block_id not in source_ids
        ]
        if missing_ids:
            issues.append(
                f"storyboard table {table_id}: task {task_name} missing "
                f"source_block_ids {missing_ids!r}"
            )

    if enforce_seedance_prompt_contract:
        mime_types = {
            asset_id: asset.mime_type for asset_id, asset in assets.items()
        }
        for task_name, task_type, _, raw_task in task_sources:
            if task_type != "image_to_video":
                continue
            prompt = raw_task.get("prompt")
            user_intent = raw_task.get("user_intent")
            narrative_multishot = (
                isinstance(prompt, str)
                and has_multiple_shot_markers(prompt)
            ) or (
                isinstance(user_intent, str)
                and any(
                    keyword in user_intent
                    for keyword in ("分镜", "多镜头", "多个镜头")
                )
            )
            issues.extend(
                validate_seedance_prompt(
                    raw_task,
                    mime_types,
                    require_storyboard=(
                        task_name in storyboard_task_names
                        or narrative_multishot
                    ),
                )
            )
            # 文档明确要求一镜到底时，禁止把它拆成多个镜头（用户 2026-09-22：
            # 「明明我的剧本规定了一镜到底…一个镜头就能做完的视频他还是会分成几个镜头」）。
            if document_requests_single_take and isinstance(prompt, str):
                segments = _timeline_segment_count(prompt)
                if segments > 1:
                    issues.append(
                        f"文档要求一镜到底，但该任务的提示词分了 {segments} 个镜头："
                        "请改成一个镜头（整段一个时间段覆盖全部 duration，或去掉时间轴）"
                    )
            # ③（2026-10-10，P1）：文档分镜表只有 1 行时，禁止「分镜/剪辑指令」。
            # 在**规划阶段**就拒（走 `_invoke_with_repair` 自动重试/修正），而不是事后
            # 拦在审批页 —— 那样用户还得自己手改（用户口径：不要让用户补规划该做的事）。
            if isinstance(prompt, str):
                split_directives = _shot_split_directives(prompt)
                if split_directives:
                    basis = _single_shot_basis(document, prompt)
                    if basis is not None:
                        shown = "、".join(split_directives[:6])
                        issues.append(
                            f"{task_name}.prompt：{basis}，但提示词里出现了分镜/剪辑指令"
                            f"（{shown}）。请把整片写成**一个镜头**：文档在同一行里写的"
                            "「另一个角 / 随后 / 接着 / 硬切到另一个角」是同一镜头内的"
                            "事件，用绝对秒数分段写清推进过程（如 0-4s：… 4-8s：…），"
                            "不要写镜头 2 / 第二镜 / N 段镜头 / 快速剪辑，也不要把切点"
                            "写成指令。"
                        )

    if enforce_image_prompt_contract:
        mime_types = {
            asset_id: asset.mime_type for asset_id, asset in assets.items()
        }
        for _, task_type, _, raw_task in task_sources:
            if task_type != "image_to_image":
                continue
            issues.extend(validate_image_prompt(raw_task, mime_types))

    return issues


_REWORK_FUSION_SYSTEM_PROMPT = (
    "你是提示词改写器。把「原始提示词」与「历次返工要求」合并，\n"
    '只输出一个 JSON 对象：{"prompt": 改写后的提示词, "must_avoid": [必须避免项]}。\n'
    "硬性要求：\n"
    "1. 保留原始提示词里的全部画面信息（主体/场景/动作/运镜/画质），不删减。\n"
    "2. 每个 @图片N / @视频N / @音频N 引用令牌都必须原样保留，不增、不删、不改号。\n"
    "3. 每条要求都落实：能写成正向画面描述的写进 prompt，只能写成禁止项的放进 "
    "must_avoid。\n"
    "4. 用户的要求几乎都是在描述当前成片的毛病（口语、否定、模糊），必须反过来改写成"
    "要达成的具体画面效果，绝不能把毛病本身写进 prompt。例：「不要让红衣服老头跑出去」"
    "→「老头双脚始终踩在起跑线后、全程不位移」；「镜头之间不连续」→「每段开头主体"
    "位置、朝向与上段结尾一致」。禁止把空泛的「不要…」当唯一表述。\n"
    "5. 若新旧要求冲突，以更新的要求为准；被替代的话要改写掉、不并存。\n"
    "6. 不要输出「【返工要求】」这类标记。\n"
    "7. 严禁在结尾另起一段罗列要求：就地并进原句、末段抬头不变。\n"
    f"8. prompt 不超过 {SEEDANCE_PROMPT_MAX_CHARS} 个字符。\n"
    # 2026-09-28：用户报「它会改一些我没有说的问题，即使本来是对的，它也要加一些词」。
    # 实测两轮真实产出：把看片描述里的物件（黑色多孔电木块 / 水泥砖墙 / 带脚轮工具柜 /
    # 黄色电缆…）抄成「不出现××」，以及把早已被取代的旧要求（堆砌外形 / 飞散的小方块）
    # 重新注入正文。旧提示词通篇只讲「不得删减」，没有一句约束**改动范围**。
    # 9-14 是新增的改动范围约束；为守住 850 字上限（test_fusion_prompt_rules.py 看着它，
    # 撑长会撞 TPM），1-7 的措辞同步做了压缩 —— 8 条原版硬条款一条没删。
    # 第 10 条（2026-09-28 爱心气球）：要求「气球/绳子从手上方滑走」，融合却只插入不删除，
    # 产出「…从女孩手中脱落，向天空飞去，脱手瞬间她的手指仍保持握持细杆的姿势…」这种
    # 自相矛盾的病句 —— 第 1 条（不删减）和第 3 条（每条都要落实）打架时它只敢加不敢删。
    # 第 14 条（2026-09-29 刀切水果碗）：正文已写明「竖直切两刀分三块厚片 → 整体旋转 90 度
    # → 逐片横切」，成片漏了旋转那一步；返工只加了 13 个字（「每块厚片均带绿色尖端」），
    # 因为第 9 条只说「只改相关句子」，它不敢增删整句。**动作顺序类的差距必须允许重写整段**。
    "9. 只改与本次要求直接相关的句子；没提出的地方一字不动。\n"
    "10. 需要修正的句子要整句替换：把与要求冲突的旧表述删掉，不能把新表述插在旧句"
    "旁并存。\n"
    "11. 给了「核对结论」（含「要求 X，实际 Y」）就按它改导致做错的那句；与本轮要求"
    "无关的问题别动。\n"
    "12. 不得从画面描述推断新约束：描述里的杂物、场景与镜头漂移只是事实，不是要求。\n"
    "13. 多条要求说同一件事时合并成一句；改完那句必须自洽，不能自相矛盾（如「已飞走」"
    "与「仍握在手里」）。\n"
    "14. 差距在动作顺序/时序（某步漏做、做早做晚、次序错）时，必须重写整段动作序列"
    "（可增删整句），只补修饰不算落实。\n"
    "must_avoid：每条是简短祈使句、针对画面可判定的事物；最多 8 条；没有给空数组。\n"
    "只输出 JSON，不要任何前后缀说明。"
)


class DeepSeekPlanner:
    def __init__(
        self,
        model: Any,
        *,
        max_output_count: int = 4,
        skill_section: str = "",
    ) -> None:
        #: 导演技能规则（Deepwhite 导演拍摄技能）注入段；空串 = 不启用。
        #:
        #: 「原图保真」覆盖块**跟技能段捆在一起、排在技能段之后**（recency）：技能包的
        #: 模板与示例写着「@图片1：参考…；不参考原图姿势、背景、光线、文字」，规划器
        #: 会照抄这个句式，把原图自带的文字 / UI / 角标填进「不参考」那一半。
        #:
        #: 刻意**不**在 `plan()` 末尾无条件追加：① `exact_system_prompt` 必须**逐字
        #: 回放**检查点里记录的快照（生产走的就是这条路，见
        #: `test_exact_system_prompt_override_replays_checkpoint_verbatim`）；
        #: ② 没有技能段时系统提示词必须与 `planner_system_prompt()` 完全一致。
        #: 无技能段场合由契约正文里的同一条口径兜底（`_SEEDANCE_PLANNING_CONTRACT`）。
        self._skill_section = (
            f"{skill_section}\n\n{REFERENCE_FIDELITY_OVERRIDE}"
            if skill_section
            else ""
        )
        # reasoning_effort / thinking 必须直接写进模型配置再 bind，
        # langchain 的 bind(extra_body=...) 不会把这些字段转发到 API，
        # 会导致 DeepSeek v4 在未开启推理时返回空任务列表。
        # 真实模型（langchain ChatOpenAI）支持 model_copy，用它正确下发；
        # 测试里的 FakeModel 只有 bind，走回退分支保持兼容。
        if hasattr(model, "model_copy"):
            self._plan_model = model.model_copy(
                update={
                    "reasoning_effort": "high",
                    "extra_body": {"thinking": {"type": "enabled"}},
                }
            ).bind(response_format={"type": "json_object"})
            self._audit_model = model.model_copy(
                update={
                    "extra_body": {"thinking": {"type": "disabled"}},
                }
            ).bind(response_format={"type": "json_object"})
        else:
            self._plan_model = model.bind(
                response_format={"type": "json_object"},
                extra_body={
                    "thinking": {"type": "enabled"},
                    "reasoning_effort": "high",
                },
            )
            self._audit_model = model.bind(
                response_format={"type": "json_object"},
                extra_body={"thinking": {"type": "disabled"}},
            )
        self.max_output_count = max_output_count
        # 保留原始模型，供按需派生「融合模型」（见 _fuse_model）。
        # 刻意不在构造期 bind：存量测试对构造期的 bind 次数有严格断言。
        self._model = model

    def _fuse_model(self) -> Any:
        """派生出用于返工融合的模型副本：改写任务不需要推理预算，关掉思考。"""
        model = self._model
        if hasattr(model, "model_copy"):
            return model.model_copy(
                update={"extra_body": {"thinking": {"type": "disabled"}}}
            )
        return model

    async def fuse_rework_prompt(
        self,
        original_prompt: str,
        requirements: list[str],
        *,
        visual_context: str = "",
        retry_feedback: str = "",
        media_parts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """把「原始提示词 + 全部历史返工要求」融合成提示词与必避清单。

        返回 `{"prompt": str, "must_avoid": list[str]}`：口语要求被改写成可执行的
        物理描述，正向的进 prompt、禁止项进 must_avoid（调用方并入
        negative_constraints，会以「必须避免：…」整块附在提交文本末尾）。

        `media_parts`（用户 2026-09-28：「把调用改成一次多模态调用就行了，不要纯文本」）
        是上一版成片的**视频** content part：与 `plan()` 的多模态消息形状一致，成片跟
        原始提示词、返工要求一起进同一次请求 —— 模型自己看片，不再读一段文字转述。

        `visual_context` 是**文字版**的上一版画面/核对结论：只有在成片传不上去
        （没有公开图床、上传失败、产物缺失）时才走这条回落路径。

        `retry_feedback` 是上一次被判不合格的原因（2026-09-28）：同一输入原样重发
        4 次毫无意义，必须把不合格原因一并给它。

        任何失败（异常、空返回、非 JSON、缺 prompt）都返回 None，由调用方回退到
        安全拼接——融合能力不可用绝不能让返工本身失败。
        """
        entries = [
            item.strip()
            for item in requirements
            if isinstance(item, str) and item.strip()
        ]
        if not entries:
            return None
        user_content = "\n".join(
            [
                "原始提示词：",
                original_prompt,
                "",
                *(
                    [
                        "上一版成片的**视频**已附在本条消息里（多模态直读，不要靠猜）："
                        "先自己看一遍，再逐条找出「要求 X，实际 Y」的差距。",
                        "",
                    ]
                    if media_parts
                    else []
                ),
                *(
                    [
                        "上一版成片的实际画面与核对结论（模型直接看过视频；拿到当初的"
                        "提示词时，它会逐条列出「要求 X，实际 Y」）：",
                        visual_context.strip(),
                        "",
                    ]
                    if visual_context.strip()
                    else []
                ),
                *(
                    [
                        "上一次改写被判不合格：" + retry_feedback.strip(),
                        "请针对这条原因重新输出，不要重复上一次的写法。",
                        "",
                    ]
                    if retry_feedback.strip()
                    else []
                ),
                "历次返工要求（必须全部融入，一条都不能遗漏）：",
                *[
                    f"{index}. {item}"
                    for index, item in enumerate(entries, 1)
                ],
            ]
        )
        user_message: dict[str, Any] = {"role": "user", "content": user_content}
        if media_parts:
            # 一次多模态调用（用户 2026-09-28）：成片视频与原始提示词、返工要求一起进
            # 同一条消息。形状与 `plan()` 的多模态路径完全一致（那条已在生产跑通）。
            user_message = {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_content},
                    *media_parts,
                ],
            }
        messages = [
            {"role": "system", "content": _REWORK_FUSION_SYSTEM_PROMPT},
            user_message,
        ]
        fuse_model = self._fuse_model()
        try:
            fuse_model = fuse_model.bind(
                response_format={"type": "json_object"}
            )
        except Exception:
            pass
        try:
            with tracing_context(enabled=False, parent=False):
                response = await fuse_model.ainvoke(
                    messages,
                    config={"callbacks": []},
                )
        except Exception:
            _LOGGER.warning("返工提示词融合调用失败", exc_info=True)
            return None
        content = self._response_content(response)
        if not isinstance(content, str):
            return None
        text = content.strip()
        if not text:
            return None
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return None
        entries = payload.get("must_avoid")
        must_avoid: list[str] = []
        if isinstance(entries, list):
            must_avoid = [
                str(item).strip()
                for item in entries
                if isinstance(item, str) and str(item).strip()
            ]
        return {"prompt": prompt.strip(), "must_avoid": must_avoid}

    async def plan(
        self,
        document: NormalizedDocument,
        visions: list[VisionDescription],
        feedback: str | None = None,
        system_prompt: str | None = None,
        exact_system_prompt: str | None = None,
        mode: PlanningMode = "video",
        character_context: str | None = None,
        knowledge_context: str | None = None,
        media_parts: list[dict[str, Any]] | None = None,
        history_context: str | None = None,
    ) -> TaskPlan:
        image_mode = mode == "image"
        if exact_system_prompt is not None:
            effective_system_prompt = exact_system_prompt
        elif image_mode:
            effective_system_prompt = (
                image_planner_system_prompt()
                if system_prompt is None
                else f"{_PORTAL_IMAGE_PLANNER_CONTRACT}{system_prompt}"
            )
        else:
            effective_system_prompt = (
                planner_system_prompt()
                if system_prompt is None
                else f"{_PORTAL_PLANNER_CONTRACT}{system_prompt}"
            )
        # 导演技能规则（用户 2026-09-22：「直接把这个 skill 接上」）：决定**提示词
        # 怎么写**，优先级高于契约里自带的措辞/结构说明。只作用于视频规划 ——
        # 技能是给 Seedance 视频提示词写的，图片任务不该被它带偏。
        if self._skill_section and not image_mode:
            effective_system_prompt = (
                f"{effective_system_prompt}\n\n{self._skill_section}"
            )
        user_content = self._planning_prompt(document, visions, feedback, mode)
        if character_context:
            user_content = (
                f"{user_content}\n\n"
                "【素材库已匹配角色】\n"
                f"{character_context}\n"
                "画面出现上述角色时，必须把对应 asset_id 挂进 reference_images"
                "（role=reference_image），并在 prompt 中沿用该角色的既有形象，"
                "不要用文档里的普通图片替代。"
            )
        if history_context:
            # 用户 2026-09-18 选的第 2 条：把**这条需求历次返工被要求改的地方**喂进规划，
            # 让重新规划时直接避开上次被挑出来的问题 —— 少一轮"生成完才发现不对"。
            # 不增加任何模型调用（这些要求本来就在我们自己的数据里）。
            user_content = (
                f"{user_content}\n\n"
                "【这条需求以前返工被要求改过的地方（这次规划要直接做到，不要再犯）】\n"
                f"{history_context}\n"
                "以上是同一个需求在以往几版里被明确要求修的问题。请在本轮规划里就"
                "写进对应镜头（写成可判定的画面描述），不要等生成完再被挑出来。"
            )
        if knowledge_context:
            # 知识库经验要在**写 prompt 之前**给到模型，让它一次写对；
            # 而不是等 planner 写完再回头改写（那样是两次加工，会互相打架）。
            user_content = (
                f"{user_content}\n\n"
                "【知识库经验（写 prompt 时就要遵守）】\n"
                f"{knowledge_context}\n"
                "以上是平台实际踩过的坑与硬约束。请在生成 prompt 时就遵守，"
                "不要产出一个之后再改；与文档要求冲突时以知识库为准，"
                "并在计划里说明依据。"
            )
        # 多模态规划（用户 2026-09-17 批准）：把**原始图片/视频**直接放进用户消息，
        # 模型就不用靠视觉描述去猜"哪张图是第几个素材、对应哪个镜头" —— 实测同一份
        # 文档：纯文本流程 3 次重试全废（编号/顺序契约），多模态一次 29.6s 成功。
        user_message: dict[str, Any] = {"role": "user", "content": user_content}
        if media_parts:
            user_message = {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_content},
                    *media_parts,
                ],
            }
        messages = [
            {"role": "system", "content": effective_system_prompt},
            user_message,
        ]

        def validate_payload(payload: dict[str, Any]) -> list[str]:
            normalization_issues = _normalize_generated_plan_payload(
                payload,
                document,
            )
            # 分镜行的 block id 由代码补齐（模型枚举不稳，实测必漏）。
            _fill_storyboard_source_block_ids(payload, document)
            # 视频默认**保留**为 reference_video（2026-10-08 口径修正，见函数注释）；
            # 只有上游明确拒绝时才在执行层降级成抽帧。
            _retain_video_references(payload, document)
            # 「挂了却没被具体用起来」的素材摘掉并排除（两条规则互相夹住的死结）。
            _exclude_unused_references(payload, document)
            # ②-a-2：长时长单镜头只写了一整段时间轴时补一条**非阻塞**提示。
            _append_duration_segmentation_hints(payload)
            return [
                *normalization_issues,
                *validate_plan(
                    payload,
                    document,
                    self.max_output_count,
                    enforce_seedance_prompt_contract=not image_mode,
                    enforce_image_prompt_contract=image_mode,
                ),
            ]

        result = await self._invoke_with_repair(
            model=self._plan_model,
            messages=messages,
            schema=TaskPlan,
            deterministic_validator=validate_payload,
            document_id=document.document_id,
            operation="plan",
        )
        return result

    async def audit(
        self,
        document: NormalizedDocument,
        plan: TaskPlan,
    ) -> AuditReport:
        messages = [
            {"role": "system", "content": _AUDIT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": self._audit_prompt(document, plan),
            },
        ]
        try:
            report = await self._invoke_with_repair(
                model=self._audit_model,
                messages=messages,
                schema=AuditReport,
                deterministic_validator=lambda payload: [],
                document_id=document.document_id,
                operation="audit",
            )
        except AgentError as exc:
            # 审计是辅助环节（给人看的风险提示），不该有一票否决权。实测
            # 4 个任务的审计报告会超模型输出上限抛 LengthFinishReasonError，
            # 计划本身已经生成好了，却因为「审查意见写太长」把整个 run 判失败。
            if not _is_recoverable_audit_failure(exc):
                raise
            _LOGGER.warning(
                "审计未能完成，按无额外发现处理 document_id=%s detail=%s",
                document.document_id,
                exc.detail.technical_detail,
            )
            return AuditReport(
                issues=[
                    "自动审计未能完成（模型输出超长或返回异常），"
                    "本次计划未经过自动审查，请人工重点核对。"
                ],
                corrections_required=False,
            )
        return _normalize_audit_report(report)

    def _planning_prompt(
        self,
        document: NormalizedDocument,
        visions: list[VisionDescription],
        feedback: str | None,
        mode: PlanningMode = "video",
    ) -> str:
        table_ids = {
            block.block_id
            for block in document.blocks
            if block.block_type == "table"
        }
        table_blocks = [
            block.model_dump(mode="json")
            for block in document.blocks
            if block.block_id in table_ids
            or any(part in table_ids for part in block.path)
        ]
        media_references = [
            {
                "asset_id": asset.asset_id,
                "source_block_id": asset.source_block_id,
                "mime_type": asset.mime_type,
                "width": asset.width,
                "height": asset.height,
                "download_succeeded": (
                    asset.download_error is None
                    and asset.size > 0
                    and bool(asset.sha256)
                    and asset.local_path.is_file()
                ),
            }
            for asset in document.media_assets
        ]
        video_semantics = [
            semantic.model_dump(mode="json")
            for semantic in document.video_semantics
        ]
        vision_payload = [
            vision.model_dump(mode="json") for vision in visions
        ]
        schema = TaskPlan.model_json_schema()
        if mode == "image":
            # 图片模式不能带视频指令：用户提示词离生成更近，Seedance 的
            # prompt 格式要求会压过 system prompt 里的图片模板，导致模板
            # 骨架整段失效（实测产出的 prompt 一句模板都没有）。
            return "\n".join(
                [
                    "请把以下文档规划为可执行的图片生成任务。",
                    "允许的 task_type 只有 image_to_image。",
                    (
                        "图片匹配优先级：文档显式引用或同一表格行 > "
                        "同一章节/路径 > 视觉描述语义匹配 > 文档顺序；"
                        "不得虚构图片。"
                    ),
                    (
                        "逐张读取全部视觉描述，先理解素材的主体、场景、风格、"
                        "构图和可能用途，再决定它属于角色参考、场景参考、"
                        "概念示意还是风格参考；不得机械平均分配。"
                    ),
                    (
                        "prompt 必须严格套用 system 指令里的模板骨架，"
                        "固定句式原样保留、顺序不变，只替换尖括号槽位。"
                    ),
                    (
                        "每个下载成功的素材必须且只能归入任务 "
                        "reference_images 或 excluded_assets；未使用素材必须"
                        "写入 excluded_assets，reason 必须用中文说明。"
                        "下载失败素材不得引用或排除。"
                    ),
                    f"max_output_count={self.max_output_count}",
                    f"document_id={document.document_id}",
                    "稳定 text_view（含 [block:*] / [image:*] 引用）：",
                    document.text_view,
                    f"序列化表格及后代 blocks={_compact_json(table_blocks)}",
                    f"可用素材引用={_compact_json(media_references)}",
                    f"全部视觉描述={_compact_json(vision_payload)}",
                    f"用户反馈={_compact_json(feedback)}",
                    f"TaskPlan JSON Schema={_compact_json(schema)}",
                    "只返回符合 Schema 的 JSON 对象。",
                ]
            )
        return "\n".join(
            [
                "请把以下文档规划为可执行生成任务。",
                "允许的 task_type 只有 image_to_image 和 image_to_video。",
                (
                    "图片匹配优先级：文档显式引用或同一表格行 > "
                    "同一章节/路径 > 视觉描述语义匹配 > 文档顺序；不得虚构图片。"
                ),
                (
                    "逐张读取全部视觉描述，先理解素材的主体、场景、风格、"
                    "构图、动作和可能用途，再按“同一分镜行/Block > "
                    "同一章节路径 > 主体与动作语义匹配 > 场景和风格匹配”"
                    "分配到镜头；不得机械平均分配。"
                ),
                (
                    "Seedance prompt 使用 @图片N/@视频N/@音频N，"
                    "为每个素材写具体中文语义；多分镜写成“镜头 1（0-5s）：…"
                    "镜头 2（5-10s）：…”，每个镜头带整数秒时间范围并直接绑定"
                    "相关素材（时间轴从 0 开始、首尾相接、末段等于 duration）。"
                    "**镜头数量严格等于文档的分镜：文档分了几镜就写几镜，不合并不拆分；"
                    "文档没写分镜就不要分段；文档没给时间时按每镜内容量分配时间**"
                    "（不要平均分、不要套固定秒数模板；时间轴从 0 开始、首尾相接、"
                    "末段等于 duration）。"
                    "相邻镜头必须写清衔接（后一镜头开头"
                    "的主体位置、朝向、运动方向与前一镜头结尾一致），"
                    "**且每段都要重述不变属性**（主体配色/材质/造型、同一口锅、同一灶台、"
                    "同一机位与光照方向）——分段是各自独立生成的，省了就漂。"
                    "并补齐画质与稳定约束（水印/Logo 只按第三方口径写，"
                    "不得排除原图自带的 UI 文字、计数器、标题与产品品牌）。"
                ),
                (
                    "视频参考语义中的 camera_movement / editing_style summary "
                    "必须写入对应 image_to_video 任务的 prompt，明确描述运镜和剪辑节奏；"
                    "没有视频参考时也要写出每个镜头的运镜与动作逻辑。"
                ),
                (
                    f"每个 image_to_video 任务的 prompt 总长度不得超过 {SEEDANCE_PROMPT_MAX_CHARS} 字；"
                    "优先保留表情、动作、物理逻辑、运镜和硬性约束。"
                ),
                (
                    "分镜合并规则：同一分镜表的多行必须合并为一个视频任务，"
                    "在一个 prompt 中按镜头顺序描述；不得按镜头拆成多个付费任务。"
                ),
                (
                    "自由叙述按完整意图生成任务；混合图片/视频需求按不同意图"
                    "分别生成对应任务，不要错误合并。"
                ),
                (
                    "每个下载成功的素材必须且只能归入任务 reference_images "
                    "或 excluded_assets；未使用素材必须写入 excluded_assets，"
                    "reason 必须用中文说明。下载失败素材不得引用或排除。"
                ),
                (
                    "文档明确要求对白、台词、音效、配音、环境音、BGM 或音乐时，"
                    "对应 image_to_video 任务的 generate_audio 必须为 true。"
                    "声音默认开启：文档没提声音时留空或写 true，系统一律按开启"
                    "处理；只有文档明确要求全程静音、不要任何声音时才写 false。"
                ),
                f"max_output_count={self.max_output_count}",
                f"document_id={document.document_id}",
                "稳定 text_view（含 [block:*] / [image:*] 引用）：",
                document.text_view,
                f"序列化表格及后代 blocks={_compact_json(table_blocks)}",
                f"可用素材引用={_compact_json(media_references)}",
                (
                    f"视频参考语义={_compact_json(video_semantics)}"
                    if video_semantics
                    else "视频参考语义=[]"
                ),
                f"全部视觉描述={_compact_json(vision_payload)}",
                f"用户反馈={_compact_json(feedback)}",
                f"TaskPlan JSON Schema={_compact_json(schema)}",
                "只返回符合 Schema 的 JSON 对象。",
            ]
        )

    @staticmethod
    def _audit_prompt(
        document: NormalizedDocument,
        plan: TaskPlan,
    ) -> str:
        return "\n".join(
            [
                "独立审查以下计划，只报告遗漏、冲突、虚构和供应商限制。",
                "不得改写计划，不得返回修正后的 tasks。",
                (
                    "不要否定或质疑需求目标；对供应商限制给出中文实施策略或"
                    "风险缓释，不使用“无法保证”“不合理”“做不到”等措辞。"
                ),
                f"document_id={document.document_id}",
                f"text_view={document.text_view}",
                f"plan={_compact_json(plan.model_dump(mode='json'))}",
                (
                    "AuditReport JSON Schema="
                    f"{_compact_json(AuditReport.model_json_schema())}"
                ),
                "只返回符合 Schema 的 JSON 对象。",
            ]
        )

    async def _invoke_with_repair(
        self,
        *,
        model: Any,
        messages: list[dict[str, Any]],
        schema: type[BaseModel],
        deterministic_validator: Callable[[dict[str, Any]], list[str]],
        document_id: str,
        operation: str,
    ) -> Any:
        repair_message: dict[str, str] | None = None
        last_errors: list[str] = []
        for attempt in range(_STRUCTURED_OUTPUT_ATTEMPTS):
            request_messages = list(messages)
            if repair_message is not None:
                request_messages.append(repair_message)

            model_error: AgentError | None = None
            response: object | None = None
            try:
                with tracing_context(enabled=False, parent=False):
                    response = await model.ainvoke(
                        request_messages,
                        config={"callbacks": []},
                    )
            except Exception as exc:
                model_error = self._model_error(document_id, operation, exc)
            if model_error is not None:
                raise model_error

            raw_content = self._response_content(response)
            parsed, last_errors = self._parse_and_validate(
                raw_content,
                schema,
                deterministic_validator,
            )
            if parsed is not None:
                return parsed

            if attempt + 1 < _STRUCTURED_OUTPUT_ATTEMPTS:
                repair_message = {
                    "role": "user",
                    "content": self._repair_prompt(raw_content, last_errors),
                }

        raise self._validation_error(document_id, operation, last_errors)

    @staticmethod
    def _response_content(response: object | None) -> object:
        if response is None:
            return None
        if isinstance(response, (str, dict)):
            return response
        return getattr(response, "content", None)

    @staticmethod
    def _parse_and_validate(
        raw_content: object,
        schema: type[BaseModel],
        deterministic_validator: Callable[[dict[str, Any]], list[str]],
    ) -> tuple[BaseModel | None, list[str]]:
        if isinstance(raw_content, dict):
            payload: object = raw_content
        elif isinstance(raw_content, str):
            try:
                payload = json.loads(raw_content)
            except (json.JSONDecodeError, TypeError):
                return None, ["response: invalid JSON object"]
        else:
            return None, ["response: missing JSON object"]

        if not isinstance(payload, dict):
            return None, ["response: top-level JSON must be an object"]

        errors = deterministic_validator(payload)
        parsed: BaseModel | None = None
        try:
            parsed = schema.model_validate(payload)
        except ValidationError as exc:
            errors.extend(DeepSeekPlanner._compact_validation_errors(exc))
        if errors:
            return None, errors
        return parsed, []

    @staticmethod
    def _compact_validation_errors(exc: ValidationError) -> list[str]:
        errors = []
        for error in exc.errors(include_url=False, include_input=False):
            location = ".".join(str(part) for part in error["loc"])
            message = str(error["msg"])
            errors.append(f"schema.{location}: {message}"[:240])
        return errors[:12]

    @staticmethod
    def _repair_prompt(raw_content: object, errors: list[str]) -> str:
        if isinstance(raw_content, str):
            raw_text = raw_content
        elif isinstance(raw_content, dict):
            raw_text = _compact_json(raw_content)
        else:
            raw_text = "null"
        concise_errors = "\n".join(f"- {error}" for error in errors[:12])
        instructions = [
            "仅返回修复后的 JSON 对象，不要解释或输出推理过程。",
        ]
        if language_validation_message(errors) is not None:
            instructions.insert(
                0,
                (
                    "本次仅修复语言或报告字段；保持任务数量、任务类型、"
                    "素材引用、参数和原始需求不变，不得新增或发明需求。"
                    "文档明确要求的英文对白、品牌名和 UI 字面量必须原样保留。"
                ),
            )
        return "\n".join(
            [
                "原始输出：",
                raw_text,
                "校验错误：",
                concise_errors,
                *instructions,
            ]
        )

    @classmethod
    def _model_error(
        cls,
        document_id: str,
        operation: str,
        exc: Exception,
    ) -> AgentError:
        status_code = cls._status_code(exc)
        exception_name = type(exc).__name__
        lowered_name = exception_name.lower()
        technical_detail = (
            f"document_id={document_id}; operation={operation}; "
            f"cause={exception_name}"
        )
        if status_code is not None:
            technical_detail += f"; status={status_code}"
        retryable = (
            status_code == 429
            or (status_code is not None and status_code >= 500)
            or isinstance(
                exc,
                (httpx.TransportError, TimeoutError, ConnectionError),
            )
            or lowered_name in {
                "apiconnectionerror",
                "apitimeouterror",
                "ratelimiterror",
            }
        )
        if retryable:
            return AgentError(
                ErrorDetail(
                    category=ErrorCategory.TRANSIENT,
                    message=(
                        "需求规划模型暂时不可用"
                        f"（document_id={document_id}）"
                    ),
                    technical_detail=technical_detail,
                    retryable=True,
                )
            )
        return AgentError(
            ErrorDetail(
                category=ErrorCategory.PROVIDER_TERMINAL,
                message=f"需求规划模型调用失败（document_id={document_id}）",
                technical_detail=technical_detail,
                retryable=False,
            )
        )

    @staticmethod
    def _validation_error(
        document_id: str,
        operation: str,
        errors: list[str],
    ) -> AgentError:
        language_failure = language_validation_message(errors)
        # 分清两种失败：模型没吐出合法 JSON（response:/schema.），还是 JSON 合法但
        # 计划不满足契约。以前一律报「JSON 未通过校验」，把用户往错方向带
        # （2026-09-17 实测：超级大床连续失败，其实是契约问题）。
        json_level = [
            error
            for error in errors
            if error.startswith("response:") or error.startswith("schema.")
        ]
        contract_issues = [error for error in errors if error not in json_level]
        if language_failure:
            message_prefix = (
                "模型三次返回的 JSON 均未通过中文规划校验："
                f"{language_failure}"
            )
        elif contract_issues:
            message_prefix = (
                f"计划未通过契约校验（{len(errors)} 条），例如："
                f"{contract_issues[0]}"
            )
        else:
            message_prefix = "模型三次返回的 JSON 均未通过校验"
        return AgentError(
            ErrorDetail(
                category=ErrorCategory.VALIDATION,
                message=f"{message_prefix}（document_id={document_id}）",
                technical_detail=(
                    f"document_id={document_id}; operation={operation}; "
                    f"attempts={_STRUCTURED_OUTPUT_ATTEMPTS}; "
                    f"error_count={len(errors)}; "
                    # 只记 error_count 时无法判断是哪条契约卡住，排查只能靠猜。
                    f"issues={' | '.join(errors[:8])}"
                ),
                retryable=False,
            )
        )

    @staticmethod
    def _status_code(exc: Exception) -> int | None:
        status_code = getattr(exc, "status_code", None)
        if isinstance(status_code, int):
            return status_code
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        return status_code if isinstance(status_code, int) else None
