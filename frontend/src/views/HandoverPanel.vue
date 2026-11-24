<template>
  <section class="handover-panel" data-testid="handover-panel">
    <header class="handover-head">
      <div>
        <h3>事件日交接与按日迁冷</h3>
        <p class="page-desc">
          按实际采样日核对旧日已确认覆盖与待重写末段，并直接从冷热分层读回同一发布结果。
        </p>
      </div>
      <div class="page-actions">
        <label class="filter-item">
          <span>测点</span>
          <input v-model="point" data-testid="handover-point" placeholder="按测点筛选" @change="load" />
        </label>
        <button class="btn" data-testid="handover-refresh" type="button" @click="load">刷新</button>
      </div>
    </header>

    <p v-if="errorMessage" class="error-text" data-testid="handover-error">{{ errorMessage }}</p>

    <table class="data-table" data-testid="handover-table">
      <thead>
        <tr>
          <th>交接号</th>
          <th>事件日</th>
          <th>交接状态</th>
          <th>层级</th>
          <th>前段</th>
          <th>末段</th>
          <th>整日合计</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in items" :key="item.handover_no" :data-testid="`handover-${item.handover_no}`">
          <td>{{ item.handover_no }}</td>
          <td>{{ item.event_day }}</td>
          <td>{{ item.status }}</td>
          <td>{{ tierLabel(item.tier) }}</td>
          <td>{{ segmentLabel(item, '前段') }}</td>
          <td>{{ segmentLabel(item, '末段') }}</td>
          <td>{{ item.day_total }}</td>
          <td class="row-actions">
            <button
              class="link"
              type="button"
              :data-testid="`readback-${item.handover_no}`"
              @click="readBack(item.handover_no)"
            >
              读回核对
            </button>
            <button
              class="link"
              type="button"
              :data-testid="`reconcile-${item.handover_no}`"
              @click="reconcile(item.handover_no)"
            >
              整日核对
            </button>
            <button
              class="link"
              type="button"
              :data-testid="`evict-${item.handover_no}`"
              @click="evict(item.handover_no)"
            >
              迁冷
            </button>
          </td>
        </tr>
        <tr v-if="!items.length">
          <td colspan="8" class="empty-state">暂无事件日交接记录</td>
        </tr>
      </tbody>
    </table>

    <section v-if="activeReadback" class="handover-result" data-testid="handover-readback">
      <h4>
        读回 {{ activeReadback.handover_no }} · {{ activeReadback.event_day }} ·
        {{ activeReadback.published_version }}
      </h4>
      <table class="data-table">
        <thead>
          <tr><th>分段</th><th>实际读源</th><th>层级</th><th>窗口值</th><th>小计</th><th>与发布一致</th><th>说明</th></tr>
        </thead>
        <tbody>
          <tr v-for="(seg, name) in activeReadback.segments" :key="name" :data-testid="`readback-segment-${name}`">
            <td>{{ name }}</td>
            <td>{{ sourceLabel(seg.source) }}</td>
            <td>{{ tierLabel(seg.tier) }}</td>
            <td>{{ seg.values.join('、') || '—' }}</td>
            <td>{{ seg.total ?? '—' }}</td>
            <td>{{ seg.matches_published ? '一致' : '待重写' }}</td>
            <td>{{ seg.reason ?? '—' }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section v-if="activeReconcile" class="handover-result" data-testid="handover-reconcile">
      <h4>
        整日核对 {{ activeReconcile.event_day }} · {{ activeReconcile.tier }} ·
        {{ activeReconcile.reconciled ? '账实一致' : '存在差异' }}
      </h4>
      <p class="status-legend">
        <span>待重写分段：{{ activeReconcile.pending_segments.join('、') || '无' }}</span>
      </p>
    </section>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  evictEventDay,
  listHandovers,
  readBackHandover,
  reconcileHandover,
} from '@/api/backend-service'
import type { HandoverSummary, Readback, Reconcile } from '@/data/handover'

const point = ref('')
const items = ref<HandoverSummary[]>([])
const activeReadback = ref<Readback | null>(null)
const activeReconcile = ref<Reconcile | null>(null)
const errorMessage = ref('')

const TIER_LABELS: Record<string, string> = {
  hot: '热区',
  cold: '冷区',
  partial_cold: '部分冷区',
}
const SOURCE_LABELS: Record<string, string> = { cold: '冷区', hot: '热区', none: '未归并' }

function tierLabel(tier: string): string {
  return TIER_LABELS[tier] ?? tier
}

function sourceLabel(source: string): string {
  return SOURCE_LABELS[source] ?? source
}

function segmentLabel(item: HandoverSummary, name: string): string {
  const seg = item.segments[name]
  if (!seg?.recorded) return '—'
  const value = seg.published_value ?? '—'
  return `${value}（${seg.state}）`
}

async function load() {
  errorMessage.value = ''
  try {
    const payload = await listHandovers(point.value.trim() || undefined)
    items.value = payload.items
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '交接列表读取失败'
  }
}

async function readBack(handoverNo: string) {
  errorMessage.value = ''
  activeReconcile.value = null
  try {
    activeReadback.value = await readBackHandover(handoverNo)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '读回失败'
  }
}

async function reconcile(handoverNo: string) {
  errorMessage.value = ''
  activeReadback.value = null
  try {
    activeReconcile.value = await reconcileHandover(handoverNo)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '整日核对失败'
  }
}

async function evict(handoverNo: string) {
  errorMessage.value = ''
  try {
    await evictEventDay(handoverNo, { actor: '值班员' })
    await load()
    await readBack(handoverNo)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '迁冷失败'
  }
}

onMounted(load)
</script>
