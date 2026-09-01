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
