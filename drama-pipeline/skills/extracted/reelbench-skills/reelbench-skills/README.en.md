[![中文](https://img.shields.io/badge/%E4%B8%AD%E6%96%87-e2e6df?style=for-the-badge&labelColor=e2e6df&color=8b938a)](README.md)
[![English](https://img.shields.io/badge/English-285444?style=for-the-badge)](README.en.md)

# reelbench-skills

Claude Code / Codex skills for working with video.

| skill | what it does |
| --- | --- |
| [video-shots](skills/video-shots/README.en.md) | **Shot breakdown**: turns a finished film into a shot-by-shot table — duration, shot size, category, camera move, frame description, rhythm role. Cuts and durations are measured by ffmpeg; the model only judges what it should; 15 quality gates check every call. |
| [video-sync](skills/video-sync/README.en.md) | **Composites a video with the shot data alongside it**: footage on one side, the current shot's data on the other, switching at every cut with the list scrolling and highlighting itself. Landscape stacks, portrait sits side by side; the layout is one CSS file. |

## Install

```bash
git clone https://github.com/eternityspring/reelbench-skills.git
cd reelbench-skills
./scripts/install.sh
```

This symlinks the skills into `~/.claude/skills/` and/or `~/.codex/skills/` (whichever exists),
so **`git pull` takes effect immediately**.

```bash
./scripts/install.sh --claude      # Claude Code only
./scripts/install.sh --codex       # codex only
./scripts/install.sh video-shots   # one skill only
./scripts/install.sh --uninstall   # remove the symlinks
```

Requirements: `node` >= 18 and `ffmpeg` / `ffprobe` (macOS: `brew install node ffmpeg`).
**No npm dependencies, no API keys** — it runs on your current session.

Prefer a copy over a symlink? `cp -r skills/video-shots ~/.claude/skills/` —
each skill is self-contained.

## Example

`demo-report/` is the **real output** of running the skill on `demo-video.mp4`
(a 202.9-second AI-generated short film, *啥是AI*): 53 shots, 3.83 s average shot length,
15.7 cuts per minute, all 15 gates green. `demo-report-en/` is the same pipeline run on a
287.4-second English excerpt with `--lang en` — an entirely English report: 46 shots, 6.25 s
average, and one 46.92 s take that scene detection never cut, because it genuinely never cuts.

[![Shot breakdown report](skills/video-shots/assets/report.png)](demo-report/shots-report.html)

The report is a **single interactive page**: an embedded player (playback highlights the current
shot, click a shot to jump), a pace strip, a shot list you can search, filter and sort (list or
card view, first and last keyframe side by side, click a frame to enlarge), distributions, cast,
and the quality gates. No external dependencies — double-click it offline.

<img src="skills/video-shots/assets/report-mobile.png" width="360" alt="the shot list on a narrow screen">

```
demo-report/
├── shots-report.html   ← clone and double-click
├── shots.json          ← the breakdown data for all 53 shots
├── shots.md            ← Markdown shot list
├── track.json          ← frame-difference motion curve (the machine's evidence)
└── frames/             ← first and last keyframe of every shot, 106 files
```

What **video-sync** produces is at the bottom of this page — as a video, not a screenshot.

## What it looks like

A 287.4-second English excerpt with its 46-shot breakdown, 1280×1296 (the source is 640×360,
blown up with `--scale 2`). The panel switches with every cut; the list scrolls up and the
highlight rides along:

<video src="https://github.com/eternityspring/reelbench-skills/raw/main/demo-sync/demo-en-sync.mp4" controls muted playsinline width="760"></video>

If the player does not load, grab the file: [`demo-sync/demo-en-sync.mp4`](demo-sync/demo-en-sync.mp4)
