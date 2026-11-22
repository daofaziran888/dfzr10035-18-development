/** 增量缺口重放清单专用接口：导出清单文件、导入认领、回执缩减、重放队列同步与复核。 */
import { request } from './client'

export type GapSpec = {
  gap_no?: string
  source_endpoint?: string
  origin_cursor?: string
  cursor_from: number
  cursor_to: number
  gap_log?: string
  coverage_basis?: string
}

export type ObservationSpec = {
  source_endpoint: string
  origin_cursor: string
  cursor_from: number
  cursor_to: number
  basis?: string
  verified_by?: string
}

export type ReceiptSpec = {
  source_endpoint?: string
  origin_cursor?: string
  cursor_from: number
  cursor_to: number
  basis?: string
  source?: string
}

export type HandoffRow = {
  id: number
  清单编号: string
  来源端点: string
  原游标: string
  缺口区间: string
  缺口条数: number
  待重放区间: string
  待重放条数: number
  已补齐条数: number
  值班班组: string
  导出时间: string
  status: string
  file_hash: string
}

export type SegmentRow = {
  id: number
  handoff_id: number
  段编号: string
  来源端点: string
  原游标: string
  起始游标: number
  结束游标: number
  条数: number
  覆盖依据: string
  缺口日志: string
  status: string
  retry_task_id: number | null
  回执记录: Array<Record<string, string | number>>
  不派发说明: string
}

export type HandoffDetail = {
  handoff: HandoffRow
  segments: SegmentRow[]
  submissions: Array<{
    id: number
    handoff_id: number
    file_hash: string
    提交人: string
    提交时间: string
    处理结果: string
  }>
  dispatch_notes: Array<Record<string, unknown>>
  original_gap: string
  current_pending: string
}

type ApiResult<T> = { ok: boolean; message: string } & T

async function call<T>(path: string, body?: unknown): Promise<ApiResult<T>> {
  const response = await request(path, body === undefined ? undefined : {
    method: 'POST',
    body: JSON.stringify(body),
  })
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  return (await response.json()) as ApiResult<T>
}

export function exportGapManifest(payload: {
  tracker_id?: number
  endpoint?: string
  origin_cursor?: string
  operator?: string
  shift_team?: string
  gaps: GapSpec[]
  observations?: ObservationSpec[]
}) {
  return call<{ manifest: Record<string, unknown>; file_hash: string }>(
    '/api/incr-gap/export', payload)
}

export function importGapManifest(payload: {
  manifest: Record<string, unknown>
  observations?: ObservationSpec[]
  operator?: string
  shift_team?: string
}) {
  return call<{ handoff: HandoffRow; detail: HandoffDetail; file_hash: string }>(
    '/api/incr-gap/import', payload)
}

export async function listHandoffs(): Promise<HandoffRow[]> {
  const response = await request('/api/incr-gap/handoffs')
  if (!response.ok) throw new Error('交接记录读取失败')
  const data = (await response.json()) as ApiResult<{ items: HandoffRow[] }>
  return data.items
}

export async function getHandoff(handoffId: number): Promise<HandoffDetail> {
  const response = await request(`/api/incr-gap/handoffs/${handoffId}`)
  if (!response.ok) throw new Error('交接详情读取失败')
  const data = (await response.json()) as ApiResult<{ detail: HandoffDetail }>
  return data.detail
}

export function claimHandoff(handoffId: number, operator = '接班值班员') {
  return call<{ handoff: HandoffRow; detail: HandoffDetail }>(
    `/api/incr-gap/handoffs/${handoffId}/claim`, { operator })
}

export function submitReceipts(handoffId: number, receipts: ReceiptSpec[], operator = '接班值班员') {
  return call<{ handoff: HandoffRow; detail: HandoffDetail }>(
    `/api/incr-gap/handoffs/${handoffId}/receipts`, { operator, receipts })
}

export function syncReplay(handoffId: number) {
  return call<{ handoff: HandoffRow; detail: HandoffDetail }>(
    `/api/incr-gap/handoffs/${handoffId}/sync-replay`)
}

export function reviewHandoff(handoffId: number) {
  return call<{ handoff: HandoffRow; detail: HandoffDetail }>(
    `/api/incr-gap/handoffs/${handoffId}/review`)
}
