# 02 · 输出格式规范

## 整体结构

段落之间用 `===` 分隔。每段包含以下 4 个部分（缺一不可），**段标题独占一行，正文之前另起一行放置该段时长（整数，单位秒），用 `&` 号与正文隔开**：

```
[段标题]
& [时长整数] & 
integrated_multimodal_description: [Shot 1] Live-action, cinematic, ...

overall_soundscape: ...

non_diegetic_music: ...
```

## 段标题与时长格式

```
## 段N（对应成片 hh:mm:ss–hh:mm:ss）— 节拍简述
& 时长整数 & 
```

例：

```
## 段3（对应成片 00:10–00:15）— 王牌降临
& 5 & 
```

## `integrated_multimodal_description` 写法规范

### （1）开场一句

```text
[Shot 1] Live-action, cinematic, [镜头景别] [画面核心内容描述]. [摄影机运动]. [动作展开].
```

### （2）人物外观描述

- 每段**首次出现**某人物时，必须完整重述其外貌（基于用户提供的人物设定），不得省略或缩写。
- 表述模板：`— [人物角色名], [外貌特征1], [外貌特征2], [外貌特征3], [服饰/配件] —`
- 后续同段内再出现该人物时可简写为 `— 保留相同的 [关键特征] —` 或直接用 `the [角色名]` 指代。

### （3）说话者标注

- **每段首句必带完整音色描述（音色锚·铁律）**：H3 的每一段都是独立生成的视频，段与段之间没有记忆。因此每个说话者在**每一段内的第一次开口**，都必须带完整的音色锚短语（音色 + 音高/质感 + `(S#)` + `own on-screen character voice` 或 `off-screen voiceover`），格式统一为 `a [音色描述] voice, his/her own on-screen character voice (S#), says:`。仅当同一说话者**在同一段内**已建立过音色锚后，后续台词才可用短形式 `his own on-screen character voice (S#) says:`。禁止任何 `says; his voice (S#):` 之类省略音色描述的变体——每段独立生成时缺音色锚会直接导致声线错配/台词错乱。
- **全局统一 Speaker ID（跨镜头、跨段固定，绝不重置）**：按 H3 官方规范 `a speaker keeps the same ID across shots`。全片给每个说话者分配唯一且固定的 `(S1)`、`(S2)`、`(S3)`… ID，无论该人物出现在第几段、第几镜，ID 始终保持一致。例如全片旁白恒为 `(S1)`、女主恒为 `(S2)`、男主恒为 `(S3)`，段与段之间绝不重新从 `(S1)` 编号。
- **ID 排布规则（对齐官方 h3-prompt-writing）**：说话人的身份短语、ID、动作、语气一律放在 `<d>` **之外**；`<d>` 内只放语言标签和逐字原文（含标点，不翻译不改写）。多人同说用复合 ID（如 `(S1,S2)`）；全程不发声的角色不分配 ID。
- 说话者**在每一段内的首次开口**时，必须提供足够的视觉和音频上下文来建立稳定身份：角色类型、年龄、性别、是否出镜、音色、语速、口音等（理由见上方「每段首句必带完整音色描述」铁律——段间无记忆，音色锚必须逐段重建）。例（旁白）：`a calm, warm female narrator voice, off-screen, speaking at a steady storytelling pace (S1)`；例（角色）：`a clear young female voice with a sharp, slightly playful timbre, her own on-screen character voice (S2)`。**关键是让旁白音与角色音在音色描述上明显区分**，避免模型把旁白误分配给画面里唯一的同性别角色。
- 画外音（不出镜）使用固定短语 `says in an off-screen voiceover`，且在每个画外音 `<d>` 块之后立即声明画面中角色的嘴唇保持闭合：`while [角色名]'s lips remain completely closed` 或 `while no on-screen character's lips move`。
- 对话使用 `<d>[语言] 台词原文</d>` 包裹，保留原始中文/英文原文，不得翻译。
- 若同一句台词跨越段内切镜，用 `<scenetrans>` 标记，并声明音频跨切镜连续（如 `continues seamlessly across the cut`）。
- 若台词被视频结尾截断，用 `<cutoff>` 标记。
- 标注音色特征：`her voice clear, resonant, and sharply respectful`。

### （3.5）动作与道具逻辑铁律（V1.3 新增，审核重点）

- **动作归属无歧义**：每个动作写清主语与宾语。当一句话同时出现两个人物和一件贴身物品（盖头、面纱、披风、信件）时，必须明确该物品**戴在/握在谁身上、由谁的手执行取下/盖上/展开**。禁止把 A 的物品挂在 B 的长外貌描述之后（错误示范：`Xiao Zhi — [十项外貌] — stands before the bed, a beam lifting Shen Jinghong's red veil` 易被误读为王爷盖/戴盖头；正确：先写 `Shen Jinghong sits on the bed edge, a red bridal veil covering her head`，再写 `Xiao Zhi uses the balance beam to lift the veil off her head`）。
- **道具状态连续**：同一道具在同段内的开/合、折/展、在桌/在手状态不得自相矛盾；状态改变必须由明确动作交代（不能前文"已展开在案上"、后文又"他缓缓展开它"）。
- **相似物品区分**：同段出现两种易混淆物品时分别锚定归属，如 `her red bridal veil covering her head` 与 `a red gauze soft scarf draped over his shoulder`，必要时加 `distinct from her veil`。
- **一次性动作链**：前置动作（咳嗽、转身、递物）与开口说话**分时序**描述——先 `gives one refined cough`，随即 `then says`；禁止"边咳边说"式叠加。
- **跨段状态跳变**：人物/道具状态在相邻段间变化时（盖头从"未戴"到"戴上"），后段开场必须以画面语言重新确立新状态。

### （4）镜头运动

镜头运动由三个维度组成：**运动类型**（Motion Type）+ **幅度**（Amplitude）+ **速度**（Speed）。幅度和速度在有意义时才添加；中等幅度和正常速度通常省略。

| 维度 | 可用表达 | 说明 |
|-|-|-|
| 运动类型 | `Zoom In / Zoom Out` | 焦距变化，机身不动 |
| 运动类型 | `Push In / Pull Out` | 摄影机前后移动 |
| 运动类型 | `Pan Left / Pan Right` | 镜头水平摆动 |
| 运动类型 | `Truck Left / Truck Right` | 摄影机水平平移 |
| 运动类型 | `Tilt Up / Tilt Down` | 镜头垂直摆动 |
| 运动类型 | `Pedestal Up / Pedestal Down` | 摄影机整体上下移动 |
| 运动类型 | `Arc Shot` | 摄影机围绕主体弧线运动 |
| 运动类型 | `Tracking Shot` | 摄影机跟随移动主体 |
| 运动类型 | `Static Shot` | 摄影机位置和镜头保持静止 |
| 运动类型 | `Shake Slightly / Shake Strongly` | 轻微/强烈抖动 |
| 运动类型 | `POV` | 主体主观视角 |
| 幅度 | `with small amplitude` | 小范围变化 |
| 幅度 | `with large amplitude` | 大范围变化 |
| 速度 | `at slow speed` | 慢速 |
| 速度 | `at fast speed` | 快速 |

- 镜头运动应写为 shot 内的自然英文动作，而非句末堆叠标签。
- 完整表达示例：`The camera pushes in with small amplitude at slow speed toward his face.`

### （5）多镜头

- 段内第一个镜头无时间戳：`[Shot 1]`
- 后续镜头：`[Shot 2] At 00:02.500, the camera cuts to...`

### （6）结尾

- 描述本段的最终画面状态（定格/切黑/淡出等）。

### （7）场内可见文字

- 画面中实际可见的招牌、标志、标签、字幕、霓虹文字等，用英文双引号包裹，保留原文和标点，不得翻译。
- 例：`A massive backlit sign reading "天海集团" mounted on the far wall.`

### （8）环境与光照

- 每段 Shot 1 开场必须建立场景的环境细节和光照条件：地面材质、墙面装饰、天花板结构、光源类型和色温、空间混响感等。
- 环境描述应与剧本的「场景描述」字段一致，且在跨段间保持统一。

## `overall_soundscape` 写法规范

- 1–4 句英文**连续段落**，描述全段的环境音 + 物理动作音 + 非语言人声（风声、脚步、衣料摩擦、门的开合、呼吸/咳嗽/笑声等）。
- 包含：空间混响、脚步声、衣料摩擦、门的开合、呼吸/沉默。
- 不重复对话和歌词（它们已在 multimodal_description 中描述）。
- 若用户明确要求全片无声，写 `N/A`。

## `non_diegetic_music` 写法规范

- 1–3 句英文，描述观众能听到但角色听不到的背景音乐。
- 包含：乐器配置、BPM/速度、节奏模式、动态变化（起/落/爆/收）。
- **只写乐器、速度、节奏、动态，不用抽象情绪词、不解释配乐的情感功能**（对齐官方 h3-prompt-writing：`tense and mournful` ❌ → `an erhu sustained bow weaving against a plucked guzheng at a moderate tempo` ✅）。
- 标注 BGM 状态与上一段的衔接关系（如「延续上一段的 Trap 节奏，hi-hats 减薄」「从上一段的嘲讽 SFX 延续，在 0:03 处切为重低音爆点」）。
- 若无 BGM，写 `N/A`。
