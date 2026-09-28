#!/bin/sh
# Publish the dependency-free UI Core into iframe apps.
# Run from the repository root: sh shared-ui/sync-ui.sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
SRC="$ROOT/shared-ui"

for target in \
  "$ROOT/portal/static/ui" \
  "$ROOT/seedance/static/ui" \
  "$ROOT/nano-banana/static/ui" \
  "$ROOT/dreamina/static/ui" \
  "$ROOT/rag-assistant/static/ui" \
  "$ROOT/volcengine-portrait/static/ui" \
  "$ROOT/feishu-generation-agent/src/feishu_generation_agent/web/static/ui"
do
  mkdir -p "$target"
  cp "$SRC/portal-ui-core.css" "$target/portal-ui-core.css"
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
