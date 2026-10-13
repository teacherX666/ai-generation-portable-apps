---
name: video-sync
version: 1.1.0
description: |
  把拉片数据和原片合成一条**能直接看的视频**：一边是画面，一边是这一镜的分镜信息
  （镜号、起止、时长、景别、类别、运镜、画面描述、台词），**镜头切了信息跟着切**，
  镜头表自动滚动并高亮当前这一镜。
  版式只看原片的宽高比：**横版 / 方版 → 画面在上、信息在下；竖版 → 画面在左、信息在右**，
  画面永远原样缩放，不裁不拉。
  镜头表**随播放滚动**：切点处滚一小段把当前镜头带到锚点、随后停住，高亮条同步滑过去。
  **进度条与播放头只跟时间走、不看切点**，所以**一镜到底的片子面板也在动**（没有切点时
  唯一会动的就是这两层）。
  实现上只截四张图（底板 + 进度条遮罩 + 铺开的长图 ×2），滚动、高亮、进度推进
  全由 ffmpeg 按时间裁窗/位移，
  **布局全在 scripts/panel.css 里，改它就能改版式**，不用碰脚本。
  吃 video-shots 产出的 shots.json（有 frames/ 就把关键帧当缩略图用）。
  零 npm 依赖，用 ffmpeg 合成、无头浏览器渲面板。
  Use when asked to 导出视频、合成视频、分镜视频、带分镜信息的视频、解说版视频、
  video with shot info、annotated shot video。
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
triggers:
  - video-sync
  - 导出视频
  - 合成视频
  - 分镜视频
  - 拉片视频
  - 解说视频
  - 带分镜信息
  - shot info video
metadata:
  license: Apache-2.0
  requires:
    bins:
      - node      # >= 18，只用标准库，无 npm 依赖
      - ffmpeg    # 合成
      - ffprobe   # 读原片宽高与音轨
      - chrome    # 或 Chromium / Edge，面板是 HTML 渲的；--chrome 可指路径
  runtimes:
    - claude-code
    - codex
---

## video-sync

把 **shots.json + 原片** 合成一条视频：画面一边，**当前镜头的分镜信息**另一边，
镜头切了信息跟着切，镜头表自动滚动并高亮。

**版式只看宽高比，不看别的：**

| 原片 | 版式 | 合成方式 |
| --- | --- | --- |
| 横版（宽 > 高）、方版 | **画面在上，信息在下** | `vstack` |
| 竖版（高 > 宽） | **画面在左，信息在右** | `hstack` |

两个方向都保证**画面原样缩放、不裁不拉**，面板补足剩下的地方。

**只截四张图，动的部分交给 ffmpeg。** 面板上有**两套不同的动**，别混起来看：

| | 变的是什么 | 什么时候变 | 谁驱动 |
| --- | --- | --- | --- |
| **分段动** | 滚到哪、哪一行亮 | **只在切点**，滚完就停 | `sendcmd` 在切点下发命令 |
| **连续动** | 进度条走到哪、播放头在哪 | **一直在走**，与切点无关 | `overlay` 的 x 里直接写 `t` |

所以量一次行位置、截一张底板、截两张把镜头表铺开的长图（一张压暗、一张全亮），
滚动＝从暗底长图裁一个视窗，高亮＝从亮条长图裁一行盖上去。53 镜的片子截图
从 53 次降到 4 次（约 15 秒）。

**进度条为什么不能靠切点**：一镜到底的片子一个切点都没有 → `sendcmd` 一条命令都发不出
→ 滚动和高亮全程静止，面板就是一张死图（踩过）。进度条因此拆成两张图——底板整条
渲成「已播」、`strip-dim.png` 整条渲成「未播」——合成时让遮罩从右往左滑，左边露出来的
亮条长度就是已播比例；播放头是一块 lavfi 现画的亮色小图，跟着滑。两层的 x 都写成
含 `t` 的表达式，`overlay` 逐帧求值，**任何片子都在动**。

**当前镜头钉在第二行**：第 1、2 镜不滚（高亮往下挪一行），第 3 镜起每切一次往上滚一行。
**动在切点，静在镜头里**——切点处滚 `--ease`（默认 0.45）秒到位就停住，
16 秒的长镜头里让列表一直爬，观众要盯着它晃 16 秒。

**行高由内容决定**，描述写得长行就高；高亮条按行高分层（`crop` 的高度运行时改不动）。

`{baseDir}` = 本文件所在目录。脚本 `{baseDir}/scripts/video-sync.mjs`，零依赖，`node` 直接跑。

**边界（不做的事）**：不做拉片（那是 `video-shots` 的活，本 skill 吃它的 shots.json）、
不剪辑不转场不配乐、不烧字幕到画面上、不做逐帧渲染（动效由 ffmpeg 按时间裁窗做出来）。

---

### Step 0 — 先有拉片数据

要一份 `shots.json`（`video-shots` 的产出）和**对应的原片**。有 `frames/`（每镜关键帧）
更好——镜头表里会用起手帧当缩略图，没有就留空格。

**shots.json 必须和这条片子对得上**：`meta.durationSeconds` 与原片不符就别合，
先回去把拉片做对。

### Step 1 — 先看几何，再动视频

```bash
node {baseDir}/scripts/video-sync.mjs plan shots.json --video <片>
```

只算不合成，几秒出结果：版式、画面区、面板区、输出尺寸。**先确认这三个数字合理再往下走**——
合成一条 3 分钟的片子要一两分钟，算错了再重来不值。

不满意就调：`--panel <比例>`（横版是面板高÷画面高，默认 0.8；竖版是面板宽÷画面宽，默认 1.6）、
`--width` / `--height`（画面区的上限，默认 1920 / 1080）。

**原片太小就放大画面区**：面板是跟着画面区算的，640×360 的片子面板只有 640×288，
四列挤在一起读不了。`--scale 2` 把画面区放到 1280×720、面板放到 1280×576，宽高比不动。
默认是 1（只缩不放）。`plan` / `panels` / `compose` / `export` 都要带上同一个 `--scale`。

### Step 2 — 渲面板

```bash
node {baseDir}/scripts/video-sync.mjs panels shots.json --video <片> \
  --frames frames --lang zh|en [--out panels] [--chrome <路径>]
```

产出 `static.png`（底板，进度条整条渲成「已播」）、`strip-dim.png`（进度条遮罩，整条
渲成「未播」）、`list-dim.png` / `list-lit.png`（整张镜头表铺开的长图，一暗一亮）、
`layout.json`（每行的位置**和进度条的几何**，合成时按它算滚动与推进），
外加一张 `panels/panel.html`——**用浏览器打开它就能预览面板**，`#S07` 换镜头看效果
（预览里的滚动动效和导出一致）。

**改布局就改 `{baseDir}/scripts/panel.css`**，改完重跑 `panels` 就是新版面，
不用碰脚本、不用重新想截图逻辑。样式约定见 `{baseDir}/references/panel-style.md`。

### Step 3 — 合成

```bash
node {baseDir}/scripts/video-sync.mjs compose shots.json --video <片> \
  [--panels panels] [-o out.mp4] [--crf 20] [--ease 0.45] [--anchor 1] [--no-progress]
```

音轨照搬原片（无声片自动不接音轨）。合成完 stderr 会报输出尺寸、时长、版式，**核对一眼**。
面板只有一镜时会**额外提醒一句**「没有切点，动的是进度条与播放头」——那不是报错，
是告诉你这片子该看哪一层在动。

`--no-progress` 关掉进度条与播放头（退回老行为），只在排查「画面本身的动效是不是坏了」时用。

两步也可以一条龙：

```bash
node {baseDir}/scripts/video-sync.mjs export shots.json --video <片> -o out.mp4 --frames frames
```

### Step 4 — 抽帧验收 ⛔ 不能跳

**不要只看合成没报错就交。** 至少抽三帧看看信息对不对得上画面：

```bash
for t in 5 30 60; do ffmpeg -v error -y -ss $t -i out.mp4 -frames:v 1 -q:v 3 /tmp/check-$t.jpg; done
```

看三件事：**这一帧的画面属于哪一镜、面板上的镜号是不是同一个、高亮行有没有跟着走**。
对不上通常是 shots.json 和原片不是同一条，或者 `--video` 指错了。

**进度条要单独验**——镜头表和它无关，镜头表是对的也可能进度条根本没挂上。
把面板底部那条裁出来、几帧拼一张，一眼就能看出有没有在推进：

```bash
# 1728 宽的片子，进度条在 y≈1690（画面高 960 + 面板内偏移 730）
for t in 0.2 5.0 10.0 14.5; do
  ffmpeg -v error -y -ss $t -i out.mp4 -vf "crop=1728:40:0:1676" -frames:v 1 /tmp/bar-$t.png
done
ffmpeg -v error -y -i /tmp/bar-0.2.png -i /tmp/bar-5.0.png -i /tmp/bar-10.0.png -i /tmp/bar-14.5.png \
  -filter_complex "[0][1][2][3]vstack=inputs=4" /tmp/bars.png
```

四行的亮条应当**逐行变长**（约 1% → 33% → 66% → 96%），播放头跟着右移。
四行一模一样 = 进度条没挂上，回去查 `layout.json` 里有没有 `strip`、`strip-dim.png` 在不在。

汇报一句话说清：输出路径、尺寸、时长、版式、多少镜、面板哪几张没渲出来（如果有）。

---

## 边界

- **镜头表的滚动只发生在切点**：滚 `--ease` 秒到位就停住，镜头里面板是静止的。
  真要做「全程匀速爬」也行（改 `motionPlan` 的分段），但长镜头会一直晃眼睛。
  **进度条与播放头不在此列**——它们是连续走的，别拿这条去解释「进度条也没动」
- **需要一个无头浏览器**（Chrome / Chromium / Edge）。这是「布局能随便改」的代价：
  换成 ffmpeg 的 `drawtext` 画面板，中文换行、缩略图、高亮全得自己实现，改个版式要改滤镜图
- 词表（景别 / 类别 / 运镜 / 转场）在本 skill 里**自带一份**，不跨目录 import——
  skill 要能整个拷走。`video-shots` 那边加了新词，这边也要加，不然显示成枚举键
- 渲面板固定四张截图，和镜头数无关：53 镜约 15 秒。合成按片长走，203 秒的片子约 75 秒
- **进度条得挂得上才有动效**：`layout.json` 里没有 `strip` 几何、或 `strip-dim.png` 不在，
  脚本会**静默跳过**这两层退回老行为（老产物照样能 compose）。要确认挂了，看 stderr
  那句「底板 …… + 进度条遮罩」有没有「（面板没开进度条，跳过遮罩）」

## Windows 上跑的两个坑（各踩过一次）

1. **`--chrome` 要传 Windows 路径，不能传 MSYS / Git-Bash 路径。**
   `--chrome "/c/Program Files/Google/Chrome/Application/chrome.exe"` 会被判「不存在」——
   脚本在 Node 里查文件，Node 不认 `/c/...`。要写成
   `--chrome "C:/Program Files/Google/Chrome/Application/chrome.exe"`（正斜杠没问题）。
   常见位置：`C:/Program Files/Google/Chrome/Application/chrome.exe`、
   `C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe`。
2. **`sendcmd` 的命令文件路径必须转义**（已在代码里处理，见 `escapeFilterPath`）。
   ffmpeg 的 filter 字符串比命令行参数多一层解析：`C:\Users\a\b\motion.cmd` 里每个 `\`
   都被当转义符吃掉、盘符的 `:` 又被当参数分隔符，报
   `Cannot read file 'Usersabmotion.cmd'`（整条路径被啃成一段）。
   现在统一转成正斜杠 + 冒号写 `\:`，`selftest` 有 3 条断言守着，别改回去。

## 自测

```bash
node {baseDir}/scripts/selftest.mjs
```

144 项断言，不碰 ffmpeg、不开浏览器：几何（横/方/竖、偶数边长、上下限、旋钮）、
面板页面的数据契约（词表下发、节奏角色、缩略图有没有才给、转义）、
动画命令（每条都必须短——表达式长了 ffmpeg 直接配置失败）、ffmpeg 参数、
**进度条与播放头**（表达式必须含 `t`、分母不能是 0、拿不到几何就降级退回老行为）、
**Windows 路径转义**（`escapeFilterPath`：反斜杠换正斜杠、盘符冒号写 `\:`）。
改完脚本先跑这个。
