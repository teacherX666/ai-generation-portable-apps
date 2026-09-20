"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const staticDir = path.join(
  __dirname,
  "../../src/feishu_generation_agent/web/static",
);

test("artifact preview downloads through a blob to avoid browser network errors", () => {
  const source = fs.readFileSync(path.join(staticDir, "app.js"), "utf8");
  const styles = fs.readFileSync(path.join(staticDir, "styles.css"), "utf8");

  assert.match(source, /async function downloadArtifact\(artifact, button\)/);
  assert.match(source, /fetch\(agentUrl\(artifact\.preview_url\)/);
  assert.match(source, /link\.download = filename/);
  assert.match(source, /artifact-download-button/);
  assert.match(styles, /\.artifact-download-button\s*\{/);
});