#!/usr/bin/env python3
"""把使用统计页的 4 个重叠区块合并成「总览 + 使用记录（筛选/翻页）+ 汇总」。

删除：按用户统计（今日）、按日期查看、按日期段查询 / 导出、Token 消耗（火山方舟）
替换：生成明细（用量/金额）→ 总览 + 使用记录 + 汇总
保留：按用户趋势、最近活动（不同视角，不重复）

按 `<h2>标题</h2>` 定位区块（向上找最近的 `<section`，向下找 `</section>`），
不手抄大段 HTML，避免抄错。写前先备份。
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "portal" / "static" / "index.html"

NEW_BLOCK = """      <section class="stats-panel ui-panel ui-admin-page__section">
        <h2>总览</h2>
        <div class="day-picker-row ui-toolbar ui-toolbar--wrap">
          <div class="ui-toolbar__group">
            <label class="ui-date-field"><span>起</span>
              <input type="date" v-model="genStart" @change="genResetAndLoad()">
            </label>
            <label class="ui-date-field"><span>止</span>
              <input type="date" v-model="genEnd" @change="genResetAndLoad()">
            </label>
          </div>
          <div class="ui-toolbar__group">
            <label class="ui-field"><span>用户</span>
              <select v-model="genUser" @change="genResetAndLoad()">
                <option value="">全部用户</option>
                <option v-for="u in genUserOptions()" :value="u">{{ u }}</option>
              </select>
            </label>
            <label class="ui-field"><span>应用</span>
              <select v-model="genApp" @change="genResetAndLoad()">
                <option value="">全部应用</option>
                <option v-for="a in genAppOptions()" :value="a">{{ appLabel(a) }}</option>
              </select>
            </label>
            <label class="ui-field"><span>状态</span>
              <select v-model="genStatus" @change="genResetAndLoad()">
                <option value="">全部状态</option>
                <option value="succeeded">成功</option>
                <option value="failed">失败</option>
                <option value="submitted">已提交</option>
              </select>
            </label>
            <input type="search" v-model="genQuery" @keyup.enter="genResetAndLoad()"
                   placeholder="搜索 提示词 / 模型 / 任务ID" class="ui-input">
            <button type="button" @click="genResetAndLoad()" :disabled="genLoading"
                    class="admin-secondary-action ui-btn ui-btn--strong ui-btn--sm">
              {{ genLoading ? '查询中…' : '查询' }}
            </button>
          </div>
        </div>
        <div class="stats-cards">
          <div class="stats-card"><span>任务数</span><strong>{{ fmtTokens(genData.total || 0) }}</strong></div>
          <div class="stats-card"><span>用量 token</span><strong>{{ fmtTokens((genData.summary || {}).tokens || 0) }}</strong></div>
          <div class="stats-card"><span>金额</span><strong>{{ fmtMoney((genData.summary || {}).amount) }}</strong></div>
          <div class="stats-card"><span>涉及用户</span><strong>{{ (genData.summary || {}).users || 0 }}</strong></div>
        </div>
        <p v-if="genError" style="color:#f87171;font-size:12px;margin:8px 0 0">{{ genError }}</p>
        <p v-if="genNote" style="font-size:11px;color:#697386;margin:8px 0 0">{{ genNote }}</p>
      </section>

      <section class="stats-panel ui-panel ui-admin-page__section">
        <h2>使用记录</h2>
        <div class="ui-table-wrap" v-if="genData.rows.length">
          <table class="ip-stats-table ui-data-table">
            <thead>
              <tr>
                <th>时间</th><th>用户</th><th>应用</th><th>模型 / 计费项</th>
                <th>提示词</th><th>用量</th><th>单价</th><th>金额</th><th>状态</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in genData.rows" :key="(row.job_id || '') + row.ts">
                <td>{{ (row.time || '').slice(5, 16) }}</td>
                <td>{{ row.username }}</td>
                <td>{{ appLabel(row.app) }}</td>
                <td>{{ genModelLabel(row) }}</td>
                <td class="gen-prompt" :title="row.title || ''">{{ genPromptBrief(row) }}</td>
                <td>{{ fmtUsage(row) }}</td>
                <td>{{ fmtUnitPrice(row) }}</td>
                <td><strong>{{ fmtMoney(row.amount) }}</strong></td>
                <td>{{ genStatusText(row.status) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-else-if="genLoading" style="color:#697386;font-size:12px">正在加载…</p>
        <p v-else style="color:#697386;font-size:12px">该条件下没有记录</p>
        <div class="pager" v-if="genData.total > 0">
          <button type="button" class="ui-btn ui-btn--sm" :disabled="genPage <= 1 || genLoading"
                  @click="setGenPage(genPage - 1)">上一页</button>
          <span class="pager-info">第 {{ genPage }} / {{ genTotalPages() }} 页 · 共 {{ fmtTokens(genData.total) }} 条</span>
          <button type="button" class="ui-btn ui-btn--sm" :disabled="genPage >= genTotalPages() || genLoading"
                  @click="setGenPage(genPage + 1)">下一页</button>
          <label class="ui-field"><span>每页</span>
            <select v-model.number="genPageSize" @change="genResetAndLoad()">
              <option :value="50">50</option>
              <option :value="100">100</option>
              <option :value="200">200</option>
            </select>
          </label>
          <span class="pager-info">导出会带上当前筛选条件</span>
        </div>
        <div class="ui-toolbar ui-toolbar--wrap" style="margin-top:10px">
          <button type="button" @click="exportGenerations()" :disabled="genExporting || !genData.rows.length"
                  class="admin-secondary-action ui-btn ui-btn--strong ui-btn--sm"
                  title="导出当前筛选条件下的每一条记录（UTF-8 BOM，Excel/WPS 可直接打开）">
            {{ genExporting ? '导出中…' : '导出明细 CSV' }}
          </button>
          <button type="button" @click="exportGenerationsSummary()" :disabled="genSummaryExporting || !(genData.billSummary || []).length"
                  class="admin-secondary-action ui-btn ui-btn--strong ui-btn--sm"
                  title="按「日期 × 计费项」汇总，字段对齐方舟控制台消费明细，便于和账单对账">
            {{ genSummaryExporting ? '导出中…' : '导出汇总 CSV（对账）' }}
          </button>
        </div>
      </section>

      <section class="stats-panel ui-panel ui-admin-page__section">
        <h2>汇总（按天 × 计费项 · 可与方舟账单对账）</h2>
        <div class="ui-table-wrap" v-if="(genData.billSummary || []).length">
          <table class="ip-stats-table ui-data-table">
            <thead>
              <tr>
                <th>消费日期</th><th>模型</th><th>计费项</th><th>次数</th>
                <th>用量<br><small style="font-weight:400;color:#94a3b8">千tokens</small></th>
                <th>单价<br><small style="font-weight:400;color:#94a3b8">元/千tokens</small></th>
                <th>金额<br><small style="font-weight:400;color:#94a3b8">元</small></th>
                <th>备注</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(b, i) in genData.billSummary" :key="i">
                <td>{{ b['消费日期'] }}</td>
                <td>{{ b['模型'] }}</td>
                <td>{{ b['计费项'] }}</td>
                <td>{{ b['次数'] }}</td>
                <td>{{ b['用量(千tokens)'] }}</td>
                <td>{{ b['单价(元/千tokens)'] === '' ? '—' : b['单价(元/千tokens)'] }}</td>
                <td>{{ b['金额(元)'] === '' ? '—' : b['金额(元)'] }}</td>
                <td>{{ b['备注'] }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-else style="color:#697386;font-size:12px">该条件下没有可汇总的记录</p>
      </section>
"""

REMOVE = ["按用户统计", "按日期查看", "按日期段查询 / 导出", "Token 消耗（火山方舟）"]
REPLACE = "生成明细（用量 / 金额）"


def section_span(lines: list[str], heading: str) -> tuple[int, int]:
    hi = next(i for i, l in enumerate(lines) if f"<h2>{heading}</h2>" in l)
    si = hi
    while si >= 0 and not lines[si].lstrip().startswith("<section"):
        si -= 1
    ei = hi
    while ei < len(lines) and "</section>" not in lines[ei]:
        ei += 1
    if si < 0 or ei >= len(lines):
        raise SystemExit(f"找不到区块边界: {heading}")
    return si, ei


def main() -> int:
    text = TARGET.read_text(encoding="utf-8")
    shutil.copyfile(TARGET, TARGET.with_name(f"{TARGET.name}.pre-merge.{time.strftime('%Y%m%d-%H%M%S')}.bak"))
    lines = text.split("\n")
    for heading in REMOVE:
        si, ei = section_span(lines, heading)
        print(f"删除区块「{heading}」：第 {si + 1}-{ei + 1} 行")
        del lines[si:ei + 1]
        # 顺手吃掉区块上方多余的空行，避免堆空行
        while si < len(lines) and si > 0 and not lines[si - 1].strip() and not lines[si].strip():
            del lines[si]
    si, ei = section_span(lines, REPLACE)
    print(f"替换区块「{REPLACE}」：第 {si + 1}-{ei + 1} 行 → 总览/使用记录/汇总")
    lines[si:ei + 1] = NEW_BLOCK.rstrip("\n").split("\n")
    TARGET.write_text("\n".join(lines), encoding="utf-8")
    print("已写入")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
