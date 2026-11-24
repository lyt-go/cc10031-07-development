import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'
import type {
  HandoverList,
  HandoverSummary,
  Readback,
  Reconcile,
} from '@/data/handover'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

async function handoverJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await request(path, init)
  if (!response.ok) {
    const detail = await response.json().catch(() => null)
    const message =
      typeof detail?.detail === 'string'
        ? detail.detail
        : detail?.detail?.message ?? '交接状态读取失败'
    throw new Error(message)
  }
  return (await response.json()) as T
}

export async function listHandovers(point?: string): Promise<HandoverList> {
  const query = point ? `?point=${encodeURIComponent(point)}` : ''
  return await handoverJson<HandoverList>(`/api/handovers${query}`)
}

export async function getHandover(handoverNo: string): Promise<{ handover: HandoverSummary }> {
  return await handoverJson(`/api/handovers/${encodeURIComponent(handoverNo)}`)
}

export async function readBackHandover(
  handoverNo: string,
  segment?: string,
): Promise<Readback> {
  const query = segment ? `?segment=${encodeURIComponent(segment)}` : ''
  return await handoverJson(`/api/handovers/${encodeURIComponent(handoverNo)}/readback${query}`)
}

export async function reconcileHandover(handoverNo: string): Promise<Reconcile> {
  return await handoverJson(`/api/handovers/${encodeURIComponent(handoverNo)}/reconcile`, {
    method: 'POST',
  })
}

export async function evictEventDay(
  handoverNo: string,
  options: { failAfter?: number; actor?: string } = {},
): Promise<{ tier: string; archived_segments: string[]; skipped_segments: Record<string, string> }> {
  const body: Record<string, unknown> = {}
  if (options.failAfter !== undefined) body.fail_after = options.failAfter
  if (options.actor) body.actor = options.actor
  return await handoverJson(`/api/handovers/${encodeURIComponent(handoverNo)}/evict`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}
