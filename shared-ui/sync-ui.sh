#!/bin/sh
# Publish the dependency-free UI Core into iframe apps.
# Run from the repository root: sh shared-ui/sync-ui.sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
SRC="$ROOT/shared-ui"

# previz 原来是**不引 ui-core** 的（自有配色），但它的入口页照样需要
# `ui/theme-boot.js` 来防闪白（2026-10-26 深色模式），所以一起分发。
# infinite-canvas **不在**这里：它是 Vite 构建产物（`npm run build` 会清空 outDir），
# 往里放文件下次构建就没了；它自带一套等价的引导脚本，第 2 期再接 Portal 主题。
for target in \
  "$ROOT/portal/static/ui" \
  "$ROOT/seedance/static/ui" \
  "$ROOT/nano-banana/static/ui" \
  "$ROOT/dreamina/static/ui" \
  "$ROOT/rag-assistant/static/ui" \
  "$ROOT/volcengine-portrait/static/ui" \
  "$ROOT/drama-pipeline/static/ui" \
  "$ROOT/previz/static/ui" \
  "$ROOT/feishu-generation-agent/src/feishu_generation_agent/web/static/ui"
do
  mkdir -p "$target"
  cp "$SRC/portal-ui-core.css" "$target/portal-ui-core.css"
  # 防闪白脚本必须与各应用自己的 ui/ 一起分发：入口页用的是**该页同前缀**的路径，
  # 独立端口直连时命中的就是这一份（挂载在 Portal 下时命中的是 Portal 那份）。
  # `tests/frontend/dark_mode_guards.test.cjs` 会断言每个入口页都引到了它。
  cp "$SRC/theme-boot.js" "$target/theme-boot.js"
  mkdir -p "$target/tokens" "$target/styles"
  cp -R "$SRC/tokens/." "$target/tokens/"
  cp -R "$SRC/styles/." "$target/styles/"
done

echo "Portal UI Core synced to ${target} targets."

# 共享 JS 也必须同步：旧版只复制 CSS，放在 shared-ui/ 里的 JS 从来没被分发过。
# 注意这里**不含 model-capabilities.js** —— nano-banana 那份比共享源多 4 行功能
# （modes / response_formats 校验），是"领先"不是"落后"，要先回迁到共享源再同步，
# 否则会用旧源覆盖掉一个已上线的能力。
for target in "$ROOT/seedance/static" "$ROOT/nano-banana/static" "$ROOT/portal/static"; do
  mkdir -p "$target"
  cp "$SRC/job-cards.js" "$target/job-cards.js"
done
echo "Shared JS (job-cards.js) synced."
