<script setup lang="ts">
import type { ChipMode, LotterySettings, RecordItem } from '~/composables/useApi'

const router = useRouter()
const api = useApi()

const input = ref('')
const submitting = ref(false)
const errorMessage = ref('')
const infoMessage = ref('')

const {
  data: records,
  refresh: refreshRecords,
  pending: recordsPending,
} = await useAsyncData('records', () => api.listRecords(10))

const { data: settings, refresh: refreshSettings } = await useAsyncData<LotterySettings>(
  'settings',
  () => api.getSettings(),
)

const rules = computed(() => ({
  small: settings.value?.small_max ?? 10,
  normal: settings.value?.normal_max ?? 30,
}))

function pad(n: number) {
  return String(n).padStart(2, '0')
}

function formatTime(value: string) {
  const d = new Date(value)
  return Number.isNaN(d.getTime())
    ? value
    : d.toLocaleString('zh-CN', { hour12: false })
}

async function submit() {
  errorMessage.value = ''
  infoMessage.value = ''

  const raw = input.value.trim()
  const value = Number(raw)
  if (!raw || !Number.isInteger(value) || value < 1 || value > 49) {
    errorMessage.value = '请输入 1-49 之间的整数号码'
    return
  }

  submitting.value = true
  try {
    await api.addRecord(value)
    input.value = ''
    infoMessage.value = `已保存开奖号 ${pad(value)}`
    await refreshRecords()
  } catch (err: any) {
    errorMessage.value = err?.data?.detail || err?.message || '保存失败'
  } finally {
    submitting.value = false
  }
}

async function updateMode(mode: ChipMode) {
  await api.updateSettings({ mode })
  await refreshSettings()
}

async function clearHistory() {
  if (!confirm('确定清空全部历史开奖号？此操作不可恢复。')) return
  const result = await api.clearRecords()
  infoMessage.value = `已清空 ${result.deleted} 条记录`
  await refreshRecords()
}

async function exportJson() {
  const data = await api.exportData()
  const blob = new Blob([JSON.stringify(data, null, 2)], {
    type: 'application/json;charset=utf-8',
  })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `wave-money-${new Date().toISOString().slice(0, 10)}.json`
  a.click()
  URL.revokeObjectURL(url)
}

const fileInput = ref<HTMLInputElement | null>(null)

function pickFile() {
  fileInput.value?.click()
}

async function onFileChange(event: Event) {
  const target = event.target as HTMLInputElement
  const file = target.files?.[0]
  if (!file) return

  errorMessage.value = ''
  try {
    const text = await file.text()
    const payload = JSON.parse(text)
    const result = await api.importData(payload, true)
    infoMessage.value = `已导入 ${result.imported_records} 条记录`
    await refreshRecords()
  } catch (err: any) {
    errorMessage.value = err?.data?.detail || err?.message || '导入失败：JSON 格式不正确'
  } finally {
    target.value = ''
  }
}

function goRecommend() {
  router.push('/recommend')
}
</script>

<template>
  <main class="min-h-screen bg-slate-950 text-slate-100">
    <div class="mx-auto flex max-w-3xl flex-col gap-8 px-5 py-12">
      <header class="space-y-2">
        <p class="text-xs font-semibold tracking-[0.3em] text-cyan-400 uppercase">
          Wave Money
        </p>
        <h1 class="text-3xl font-semibold text-white">开奖号录入</h1>
        <p class="text-sm text-slate-400">
          输入最新一期开奖号（1-49），回车即保存。号码 1-49 每 12 个为同一生肖。
        </p>
      </header>

      <!-- 规则 -->
      <section class="flex flex-wrap gap-3 text-sm">
        <span class="rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-3 py-1.5 text-emerald-300">
          小波动 ≤ {{ rules.small }}
        </span>
        <span class="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-1.5 text-amber-300">
          常规 {{ rules.small }} &lt; 差值 ≤ {{ rules.normal }}
        </span>
        <span class="rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-1.5 text-rose-300">
          大跳 &gt; {{ rules.normal }}
        </span>
      </section>

      <!-- 输入 -->
      <section class="space-y-3">
        <label for="lottery-input" class="block text-sm font-medium text-slate-300">
          最新开奖号
        </label>
        <div class="flex gap-3">
          <input
            id="lottery-input"
            v-model="input"
            type="text"
            inputmode="numeric"
            maxlength="2"
            placeholder="1 - 49"
            class="w-32 rounded-xl border border-slate-700 bg-slate-900 px-4 py-3 text-center text-2xl font-semibold text-white outline-none focus:border-cyan-500"
            @keyup.enter="submit"
          />
          <button
            type="button"
            class="rounded-xl bg-cyan-500 px-5 py-3 font-medium text-slate-950 transition hover:bg-cyan-400 disabled:opacity-50"
            :disabled="submitting"
            @click="submit"
          >
            {{ submitting ? '保存中…' : '保存' }}
          </button>
        </div>

        <p v-if="errorMessage" class="text-sm text-rose-400">{{ errorMessage }}</p>
        <p v-else-if="infoMessage" class="text-sm text-emerald-400">{{ infoMessage }}</p>
      </section>

      <!-- 最近 10 期 -->
      <section class="space-y-3">
        <div class="flex items-center justify-between">
          <h2 class="text-lg font-medium text-white">最近 10 期</h2>
          <span class="text-xs text-slate-500">
            {{ records?.length ?? 0 }} / 10 条
          </span>
        </div>

        <p v-if="recordsPending" class="text-sm text-slate-400">加载中…</p>
        <p v-else-if="!records?.length" class="text-sm text-slate-500">
          还没有记录，输入第一个开奖号开始。
        </p>
        <ul v-else class="grid grid-cols-2 gap-2 sm:grid-cols-5">
          <li
            v-for="(record, index) in records"
            :key="record.id"
            class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-3 text-center"
          >
            <p class="text-2xl font-semibold text-white">{{ pad(record.number) }}</p>
            <p class="mt-1 text-[11px] text-slate-500">
              {{ index === 0 ? '最新' : `-${index}期` }}
            </p>
            <p class="text-[11px] text-slate-600">{{ formatTime(record.created_at) }}</p>
          </li>
        </ul>
      </section>

      <!-- 筹码模式 -->
      <section class="space-y-3">
        <h2 class="text-lg font-medium text-white">筹码模式</h2>
        <div class="flex flex-wrap gap-2">
          <button
            v-for="option in [
              { value: 'even', label: '均注 10/10/10' },
              { value: 'weighted', label: '侧重 20/5/5' },
              { value: 'single', label: '单挑 30' },
            ]"
            :key="option.value"
            type="button"
            class="rounded-lg border px-3 py-2 text-sm transition"
            :class="settings?.mode === option.value
              ? 'border-cyan-500 bg-cyan-500/15 text-cyan-300'
              : 'border-slate-700 bg-slate-900 text-slate-300 hover:border-slate-500'"
            @click="updateMode(option.value as ChipMode)"
          >
            {{ option.label }}
          </button>
        </div>
      </section>

      <!-- 生成推荐 -->
      <button
        type="button"
        class="rounded-2xl bg-gradient-to-r from-cyan-500 to-blue-500 px-6 py-4 text-lg font-semibold text-slate-950 transition hover:opacity-90 disabled:opacity-40"
        :disabled="!records?.length"
        @click="goRecommend"
      >
        生成推荐
      </button>
      <p v-if="!records?.length" class="-mt-4 text-center text-xs text-slate-500">
        至少需要 1 期记录才能生成推荐
      </p>

      <!-- 数据管理 -->
      <section class="space-y-3 border-t border-slate-800 pt-6">
        <h2 class="text-sm font-medium text-slate-400">数据管理</h2>
        <div class="flex flex-wrap gap-2">
          <button
            type="button"
            class="rounded-lg border border-slate-700 bg-slate-900 px-4 py-2 text-sm text-slate-200 hover:border-slate-500"
            @click="exportJson"
          >
            导出 JSON
          </button>
          <button
            type="button"
            class="rounded-lg border border-slate-700 bg-slate-900 px-4 py-2 text-sm text-slate-200 hover:border-slate-500"
            @click="pickFile"
          >
            导入 JSON
          </button>
          <button
            type="button"
            class="rounded-lg border border-rose-700/60 bg-rose-950/40 px-4 py-2 text-sm text-rose-300 hover:border-rose-500"
            @click="clearHistory"
          >
            清空历史
          </button>
          <input
            ref="fileInput"
            type="file"
            accept="application/json,.json"
            class="hidden"
            @change="onFileChange"
          />
        </div>
      </section>
    </div>
  </main>
</template>
