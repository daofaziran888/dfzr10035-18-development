<template>
  <section class="page gap-page" data-module="incr_gap_handoff">
    <header class="page-head">
      <div>
        <h2>增量缺口重放清单</h2>
        <p class="page-desc">
          前班导出增量游标与缺口日志形成交接清单，接班人按来源端点、原游标和已验证覆盖依据认领补采；
          实际补齐回执驱动待重放区间缩减，重放结果同步回导入清单。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-export" type="button" @click="showExport = true">
          导出缺口重放清单
        </button>
        <button class="btn" data-testid="open-import" type="button" @click="openImport">
          导入清单认领补采
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">清单总数</span>
        <strong class="stat-value">{{ handoffs.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待认领/补采中</span>
        <strong class="stat-value">{{ activeCount }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待重放条数合计</span>
        <strong class="stat-value">{{ totalPending }}</strong>
      </article>
    </div>

    <table class="data-table" data-testid="handoff-table">
      <thead>
        <tr>
          <th>清单编号</th>
          <th>来源端点</th>
          <th>原游标</th>
          <th>原始缺口区间</th>
          <th>当前待重放区间</th>
          <th>缺口/待重放/已补齐</th>
          <th>当前状态</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in handoffs" :key="row.id" :data-testid="`handoff-${row.id}`" :data-status="row.status">
          <td>{{ row.清单编号 }}</td>
          <td>{{ row.来源端点 }}</td>
          <td>{{ row.原游标 }}</td>
          <td>{{ row.缺口区间 }}</td>
          <td>{{ row.待重放区间 }}</td>
          <td>{{ row.缺口条数 }} / {{ row.待重放条数 }} / {{ row.已补齐条数 }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button class="link" type="button" data-testid="view-detail" @click="loadDetail(row.id)">
              交接记录
            </button>
            <button
              v-if="row.status === '待认领' || row.status === '补采中' || row.status === '部分完成'"
              class="link" type="button" @click="onClaim(row.id)"
            >认领补采</button>
            <button
              v-if="row.待重放条数 > 0"
              class="link" type="button" @click="openReceipt(row.id)"
            >提交回执</button>
            <button
              v-if="row.待重放条数 > 0"
              class="link" type="button" @click="onSync(row.id)"
            >同步重放队列</button>
            <button
              v-if="row.status === '已完成'"
              class="link" type="button" @click="onReview(row.id)"
            >确认复核</button>
          </td>
        </tr>
        <tr v-if="!handoffs.length">
          <td colspan="8" class="empty-state">暂无交接清单，先由前班值班员导出增量缺口重放清单</td>
        </tr>
      </tbody>
    </table>

    <div v-if="detail" class="detail-card" data-testid="handoff-detail">
      <h3>交接记录 · {{ detail.handoff.清单编号 }}</h3>
      <p class="detail-line">
        原始导出范围：<strong>{{ detail.original_gap }}</strong>
        ｜ 当前待重放范围：<strong>{{ detail.current_pending }}</strong>
        ｜ 文件指纹：<code>{{ detail.handoff.file_hash }}</code>
      </p>

      <h4>重放段轨迹（每段绑定来源端点 + 原游标 + 覆盖依据）</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>段编号</th><th>来源端点</th><th>原游标</th><th>闭区间</th>
            <th>条数</th><th>状态</th><th>覆盖依据</th><th>不派发说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="seg in detail.segments" :key="seg.id" :data-status="seg.status">
            <td>{{ seg.段编号 }}</td>
            <td>{{ seg.来源端点 }}</td>
            <td>{{ seg.原游标 }}</td>
            <td>[{{ seg.起始游标 }},{{ seg.结束游标 }}]</td>
            <td>{{ seg.条数 }}</td>
            <td>{{ seg.status }}</td>
            <td class="basis-cell">{{ seg.覆盖依据 || '—' }}</td>
            <td class="basis-cell">{{ seg.不派发说明 || '待派发补采' }}</td>
          </tr>
        </tbody>
      </table>

      <h4>提交留痕（同一文件重复提交只保留一份可认领工作）</h4>
      <ul class="submission-list">
        <li v-for="sub in detail.submissions" :key="sub.id">
          {{ sub.提交时间 }} · {{ sub.提交人 }} · {{ sub.处理结果 }} · <code>{{ sub.file_hash.slice(0, 8) }}</code>
        </li>
      </ul>
    </div>

    <!-- 导出弹窗 -->
    <div v-if="showExport" class="modal-backdrop" data-testid="export-modal">
      <form class="modal" @submit.prevent="onExport">
        <h3>导出增量缺口重放清单</h3>
        <label class="create-field">
          <span>来源端点</span>
          <input v-model="exportForm.endpoint" data-testid="export-endpoint" required />
        </label>
        <label class="create-field">
          <span>原游标</span>
          <input v-model="exportForm.origin_cursor" data-testid="export-cursor" required />
        </label>
        <label class="create-field">
          <span>缺口起游标（含）</span>
          <input v-model.number="exportForm.cursor_from" data-testid="export-from" type="number" required />
        </label>
        <label class="create-field">
          <span>缺口止游标（含）</span>
          <input v-model.number="exportForm.cursor_to" data-testid="export-to" type="number" required />
        </label>
        <label class="create-field">
          <span>缺口日志</span>
          <input v-model="exportForm.gap_log" data-testid="export-log" placeholder="增量追踪缺口:游标x-y缺失" />
        </label>
        <label class="create-field">
          <span>已验证观察范围（可选，格式 起-止，多个用逗号分隔）</span>
          <input v-model="exportForm.observations_text" data-testid="export-observations"
                 placeholder="如 20-23,24-28" />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showExport = false">取消</button>
          <button class="btn primary" data-testid="submit-export" type="submit">生成清单文件</button>
        </div>
        <p v-if="lastManifest" class="detail-line">
          已生成清单 {{ lastManifest.handoff_no }}，指纹
          <code>{{ lastManifest.file_hash }}</code>，可直接在下方导入窗口提交。
        </p>
      </form>
    </div>

    <!-- 导入弹窗 -->
    <div v-if="showImport" class="modal-backdrop" data-testid="import-modal">
      <form class="modal" @submit.prevent="onImport">
        <h3>导入增量缺口重放清单</h3>
        <label class="create-field">
          <span>清单文件内容（JSON）</span>
          <textarea v-model="importText" class="json-area" data-testid="import-manifest" rows="9" required></textarea>
        </label>
        <label class="create-field">
          <span>导入时新增的已验证观察范围（可选，格式 起-止）</span>
          <input v-model="importObservation" data-testid="import-observation" placeholder="如 24-28" />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showImport = false">取消</button>
          <button class="btn primary" data-testid="submit-import" type="submit">导入并生成交接记录</button>
        </div>
      </form>
    </div>

    <!-- 回执弹窗 -->
    <div v-if="receiptTarget !== null" class="modal-backdrop" data-testid="receipt-modal">
      <form class="modal" @submit.prevent="onReceipt">
        <h3>提交实际补齐回执 · 清单 #{{ receiptTarget }}</h3>
        <label class="create-field">
          <span>补齐起游标（含）</span>
          <input v-model.number="receiptForm.cursor_from" data-testid="receipt-from" type="number" required />
        </label>
        <label class="create-field">
          <span>补齐止游标（含）</span>
          <input v-model.number="receiptForm.cursor_to" data-testid="receipt-to" type="number" required />
        </label>
        <label class="create-field">
          <span>补齐依据</span>
          <input v-model="receiptForm.basis" data-testid="receipt-basis" placeholder="重放补齐批次回执" />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="receiptTarget = null">取消</button>
          <button class="btn primary" data-testid="submit-receipt" type="submit">按回执缩减区间</button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ handoffs.length }} 份交接清单</span>
      <span v-if="message" :class="messageError ? 'error-text' : 'ok-text'">{{ message }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  claimHandoff,
  exportGapManifest,
  getHandoff,
  importGapManifest,
  listHandoffs,
  reviewHandoff,
  submitReceipts,
  syncReplay,
  type HandoffDetail,
  type HandoffRow,
  type ObservationSpec,
} from '@/api/gap-handoff'

const handoffs = ref<HandoffRow[]>([])
const detail = ref<HandoffDetail | null>(null)
const message = ref('')
const messageError = ref(false)
const showExport = ref(false)
const showImport = ref(false)
const importText = ref('')
const importObservation = ref('')
const lastManifest = ref<Record<string, unknown> | null>(null)
const receiptTarget = ref<number | null>(null)

const exportForm = ref({
  endpoint: '',
  origin_cursor: '',
  cursor_from: 20,
  cursor_to: 40,
  gap_log: '',
  observations_text: '',
})
const receiptForm = ref({ cursor_from: 0, cursor_to: 0, basis: '实际补齐回执' })

const activeCount = computed(() =>
  handoffs.value.filter((row) => row.status === '待认领' || row.status === '补采中' || row.status === '部分完成').length)
const totalPending = computed(() => handoffs.value.reduce((sum, row) => sum + row.待重放条数, 0))

function notify(text: string, isError = false) {
  message.value = text
  messageError.value = isError
}

function parseRanges(text: string): Array<[number, number]> {
  return text
    .split(/[,，]/)
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => {
      const [from, to] = part.split('-').map((value) => Number(value.trim()))
      if (!Number.isFinite(from) || !Number.isFinite(to) || from > to) {
        throw new Error(`区间「${part}」格式无效，应为 起-止`)
      }
      return [from, to] as [number, number]
    })
}

async function reload() {
  handoffs.value = await listHandoffs()
  if (detail.value) {
    detail.value = await getHandoff(detail.value.handoff.id)
  }
}

async function onExport() {
  try {
    const form = exportForm.value
    const observations: ObservationSpec[] = parseRanges(form.observations_text).map(([from, to]) => ({
      source_endpoint: form.endpoint,
      origin_cursor: form.origin_cursor,
      cursor_from: from,
      cursor_to: to,
      basis: '已验证观察范围',
    }))
    const result = await exportGapManifest({
      endpoint: form.endpoint,
      origin_cursor: form.origin_cursor,
      gaps: [{
        source_endpoint: form.endpoint,
        origin_cursor: form.origin_cursor,
        cursor_from: form.cursor_from,
        cursor_to: form.cursor_to,
        gap_log: form.gap_log || `增量追踪缺口:游标${form.cursor_from}-${form.cursor_to}缺失`,
      }],
      observations,
    })
    lastManifest.value = result.manifest
    importText.value = JSON.stringify(result.manifest, null, 2)
    notify(result.message)
    showExport.value = false
  } catch (error) {
    notify(error instanceof Error ? error.message : '导出失败', true)
  }
}

function openImport() {
  if (lastManifest.value) {
    importText.value = JSON.stringify(lastManifest.value, null, 2)
  }
  showImport.value = true
}

async function onImport() {
  try {
    const manifest = JSON.parse(importText.value) as Record<string, unknown>
    const form = exportForm.value
    const observations: ObservationSpec[] = parseRanges(importObservation.value).map(([from, to]) => ({
      source_endpoint: form.endpoint || String(manifest.source_endpoint ?? ''),
      origin_cursor: form.origin_cursor || String(manifest.origin_cursor ?? ''),
      cursor_from: from,
      cursor_to: to,
      basis: '导入时已验证观察范围',
    }))
    const result = await importGapManifest({ manifest, observations })
    notify(result.message)
    showImport.value = false
    await reload()
    await loadDetail(result.handoff.id)
  } catch (error) {
    notify(error instanceof Error ? error.message : '导入失败', true)
  }
}

async function loadDetail(id: number) {
  detail.value = await getHandoff(id)
}

async function onClaim(id: number) {
  try {
    const result = await claimHandoff(id)
    notify(result.message)
    await reload()
  } catch (error) {
    notify(error instanceof Error ? error.message : '认领失败', true)
  }
}

function openReceipt(id: number) {
  const target = handoffs.value.find((row) => row.id === id)
  receiptTarget.value = id
  const match = target?.待重放区间.match(/\[(\d+),(\d+)\]/)
  receiptForm.value = {
    cursor_from: match ? Number(match[1]) : 0,
    cursor_to: match ? Number(match[2]) : 0,
    basis: '实际补齐回执',
  }
}

async function onReceipt() {
  if (receiptTarget.value === null) return
  try {
    const target = handoffs.value.find((row) => row.id === receiptTarget.value)
    const result = await submitReceipts(receiptTarget.value, [{
      source_endpoint: target?.来源端点,
      origin_cursor: target?.原游标,
      cursor_from: receiptForm.value.cursor_from,
      cursor_to: receiptForm.value.cursor_to,
      basis: receiptForm.value.basis,
    }])
    notify(result.message)
    receiptTarget.value = null
    await reload()
  } catch (error) {
    notify(error instanceof Error ? error.message : '回执提交失败', true)
  }
}

async function onSync(id: number) {
  try {
    const result = await syncReplay(id)
    notify(result.message)
    await reload()
  } catch (error) {
    notify(error instanceof Error ? error.message : '同步失败', true)
  }
}

async function onReview(id: number) {
  try {
    const result = await reviewHandoff(id)
    notify(result.message)
    await reload()
  } catch (error) {
    notify(error instanceof Error ? error.message : '复核失败', true)
  }
}

onMounted(reload)
</script>

<style scoped>
.gap-page .detail-card {
  margin-top: 16px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px 16px;
}
.detail-card h3 { margin: 0 0 8px; font-size: 15px; }
.detail-card h4 { margin: 14px 0 6px; font-size: 13px; color: var(--muted); }
.detail-line { font-size: 13px; color: var(--muted); }
.basis-cell { max-width: 260px; font-size: 12px; color: #334155; }
.submission-list { margin: 0; padding-left: 18px; font-size: 12px; color: var(--muted); }
.submission-list li { margin-bottom: 4px; }
.json-area {
  width: 100%;
  box-sizing: border-box;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px;
  font-family: ui-monospace, monospace;
  font-size: 12px;
}
.ok-text { color: #15803d; }
code {
  background: #f1f5f9;
  border-radius: 4px;
  padding: 1px 5px;
  font-size: 12px;
}
</style>
