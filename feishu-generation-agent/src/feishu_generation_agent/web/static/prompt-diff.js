(function (root, factory) {
  "use strict";

  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.PromptDiff = api;
  }
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  // 与后端 integrations/rework_prompt.py 的 REWORK_MARKER 保持一致：
  // 早期返工把要求直接拼在正文里，这里要能认出来。
  const MARKER = "【返工要求】";
  // 按中英标点与换行切成「可比较的句子」，标点跟着前一句走。
  const CLAUSE = /[^，。；、！？：,.;!?:\n]+[，。；、！？：,.;!?:\n]?/g;

  function asText(value) {
    return value === undefined || value === null ? "" : String(value);
  }

  function splitClauses(text) {
    const value = asText(text);
    if (!value.trim()) return [];
    const parts = value.match(CLAUSE) || [];
    return parts.map((part) => part.trim()).filter((part) => part.length > 0);
  }

  /** 句子级 LCS diff，返回按顺序排列的 same / removed / added 段落。 */
  function diffClauses(before, after) {
    const n = before.length;
    const m = after.length;
    const table = [];
    for (let i = 0; i <= n; i += 1) {
      table.push(new Array(m + 1).fill(0));
    }
    for (let i = n - 1; i >= 0; i -= 1) {
      for (let j = m - 1; j >= 0; j -= 1) {
        table[i][j] =
          before[i] === after[j]
            ? table[i + 1][j + 1] + 1
            : Math.max(table[i + 1][j], table[i][j + 1]);
      }
    }

    const ops = [];
    let i = 0;
    let j = 0;
    while (i < n && j < m) {
      if (before[i] === after[j]) {
        ops.push({ type: "same", text: before[i] });
        i += 1;
        j += 1;
      } else if (table[i + 1][j] >= table[i][j + 1]) {
        ops.push({ type: "removed", text: before[i] });
        i += 1;
      } else {
        ops.push({ type: "added", text: after[j] });
        j += 1;
      }
    }
    while (i < n) {
      ops.push({ type: "removed", text: before[i] });
      i += 1;
    }
    while (j < m) {
      ops.push({ type: "added", text: after[j] });
      j += 1;
    }

    const merged = [];
    for (const op of ops) {
      const last = merged[merged.length - 1];
      if (last && last.type === op.type) {
        last.text += op.text;
      } else {
        merged.push({ type: op.type, text: op.text });
      }
    }
    return merged;
  }

  function diffPrompt(before, after) {
    const segments = diffClauses(splitClauses(before), splitClauses(after));
    const stats = { added: 0, removed: 0, unchanged: 0 };
    for (const segment of segments) {
      if (segment.type === "added") stats.added += 1;
      else if (segment.type === "removed") stats.removed += 1;
      else stats.unchanged += 1;
    }
    return {
      segments,
      stats,
      hasChanges: stats.added > 0 || stats.removed > 0,
    };
  }

  /** 取任务的「改前原文」：优先冻结字段，退回老正文里的标记段。 */
  function reworkBasePrompt(task) {
    const item = task || {};
    const frozen = asText(item.rework_base_prompt).trim();
    if (frozen) return frozen;
    const current = asText(item.prompt);
    const index = current.indexOf(MARKER);
    if (index < 0) return "";
    return current.slice(0, index).trim();
  }

  function reworkRequirements(task) {
    const item = task || {};
    if (!Array.isArray(item.rework_requirements)) return [];
    return item.rework_requirements
      .map((entry) => asText(entry).trim())
      .filter((entry) => entry.length > 0);
  }

  /**
   * 供审批页展示「这条新提示词是怎么来的」：
   * 改前原文 + 历次返工要求 + 当前提示词 + 两者差异。
   */
  function reworkComparison(task) {
    const item = task || {};
    const basePrompt = reworkBasePrompt(item);
    const currentPrompt = asText(item.prompt);
    const diff = diffPrompt(basePrompt, currentPrompt);
    return {
      hasRework: basePrompt.length > 0,
      basePrompt,
      currentPrompt,
      requirements: reworkRequirements(item),
      segments: diff.segments,
      stats: diff.stats,
      hasChanges: diff.hasChanges,
    };
  }

  return {
    MARKER,
    diffPrompt,
    reworkBasePrompt,
    reworkComparison,
    reworkRequirements,
    splitClauses,
  };
});