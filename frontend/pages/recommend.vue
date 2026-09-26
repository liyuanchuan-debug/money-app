<script setup lang="ts">
import type { ChipMode, RecommendResult } from '~/composables/useApi'

const api = useApi()

const mode = ref<ChipMode>('even')
const copied = ref(false)
const copyError = ref('')

const { data: settings } = await useAsyncData('settings', () => api.getSettings())
if (settings.value?.mode) {
  mode.value = settings.value.mode as ChipMode
}

const {
  data: result,
  refresh,
  pending,
  error,
} = await useAsyncData<RecommendResult>('recommend', () =>
  api.recommend({ mode: mode.value }),
)

watch(mode, async (value) => {
  copied.value = false
  await api.updateSettings({ mode: value })
  await refresh()
})

function pad(n: number) {
  return String(n).padStart(2, '0')
}

const waveStyles: Record<string, string> = {
  small: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300',
  normal: 'border-amber-500/40 bg-amber-500/10 text-amber-300',
  big: 'border-rose-500/40 bg-rose-500/10 text-rose-300',
}

const roleStyles: Record<string, string> = {
  primary: 'bg-cyan-500 text-slate-950',
  secondary: 'bg-slate-200 text-slate-900',
  defense: 'bg-slate-700 text-slate-100',
}

const chipModes: Array<{ value: ChipMode; label: string }> = [
  { value: 'even', label: '均注 10/10/10' },
  { value: 'weighted', label: '侧重 20/5/5' },
  { value: 'single', label: '单挑 30' },
]

async function copy() {
  if (!result.value?.copy_text) return
  try {
    await navigator.clipboard.writeText(result.value.copy_text)
    copied.value = true
    copyError.value = ''
    setTimeout(() => (copied.value = false), 2000)
  } catch {
    copyError.value = '复制失败，请手动选中下方文本'
  }
}
</script>

<template>
  <main class="min-h-screen bg-slate-950 text-slate-100">
    <div class="mx-auto flex max-w-3xl flex-col gap-7 px-5 py-12">
      <header class="flex items-start justify-between gap-4">
        <div class="space-y-2">
          <p class="text-xs font-semibold tracking-[0.3em] text-cyan-400 uppercase">
            Wave Money
          </p>
          <h1 class="text-3xl font-semibold text-white">推荐结果</h1>
        </div>
        <NuxtLink
          to="/"
          class="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-300 hover:border-slate-500"
        >
          返回录入
        </NuxtLink>
      </header>

      <p v-if="pending" class="text-sm text-slate-400">计算中…</p>
      <p v-else-if="error" class="rounded-xl border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-300">
        {{ (error as any)?.data?.detail || error.message }}
      </p>

      <template v-else-if="result">
        <!-- 上期信息 -->
        <section class="rounded-2xl border border-slate-800 bg-slate-900/60 px-5 py-4">
          <div class="flex flex-wrap items-center gap-x-6 gap-y-3 text-sm">
            <div>
              <p class="text-xs text-slate-500">最新开奖号</p>
              <p class="text-2xl font-semibold text-white">{{ pad(result.latest) }}</p>
            </div>
            <div>
              <p class="text-xs text-slate-500">上期号码</p>
              <p class="text-2xl font-semibold text-slate-300">
                {{ result.previous === null ? '—' : pad(result.previous) }}
              </p>
            </div>
            <div>
              <p class="text-xs text-slate-500">上期波动（与上上期）</p>
              <span
                v-if="result.prev_wave"
                class="mt-1 inline-block rounded-lg border px-3 py-1 text-sm"
                :class="waveStyles[result.prev_wave.type]"
              >
                {{ result.prev_wave.label }} · 差值 {{ result.prev_wave.diff }}
              </span>
              <span v-else class="mt-1 inline-block text-sm text-slate-500">数据不足</span>
            </div>
            <div>
              <p class="text-xs text-slate-500">本期侧重</p>
              <p class="mt-1 text-sm font-medium text-cyan-300">
                {{ result.focus_order.map((f) => f.label).join(' → ') }}
              </p>
            </div>
          </div>
          <p class="mt-3 text-xs text-slate-500">
            重肖（需避开）：{{ result.latest_zodiac.map(pad).join(' / ') }}
          </p>
        </section>

        <!-- 无解波动提示 -->
        <section
          v-if="result.missing_waves.length"
          class="space-y-1 rounded-xl border border-amber-500/50 bg-amber-500/10 px-4 py-3"
        >
          <p
            v-for="item in result.missing_waves"
            :key="item.type"
            class="text-sm font-medium text-amber-300"
          >
            ⚠ {{ item.note }}
          </p>
        </section>

        <!-- 筹码模式 -->
        <section class="space-y-2">
          <div class="flex flex-wrap gap-2">
            <button
              v-for="option in chipModes"
              :key="option.value"
              type="button"
              class="rounded-lg border px-3 py-2 text-sm transition"
              :class="mode === option.value
                ? 'border-cyan-500 bg-cyan-500/15 text-cyan-300'
                : 'border-slate-700 bg-slate-900 text-slate-300 hover:border-slate-500'"
              @click="mode = option.value"
            >
              {{ option.label }}
            </button>
          </div>
          <p class="text-xs text-slate-500">
            模式：{{ result.mode_label }} · 每注 {{ result.bet_unit }} 元 · 总金额
            {{ result.total_amount }} 元
          </p>
        </section>

        <!-- 推荐号 -->
        <section class="grid gap-4 sm:grid-cols-3">
          <article
            v-for="pick in result.picks"
            :key="pick.number"
            class="flex flex-col gap-3 rounded-2xl border border-slate-800 bg-slate-900/60 p-5"
          >
            <div class="flex items-center justify-between">
              <span
                class="rounded-md px-2 py-0.5 text-xs font-semibold"
                :class="roleStyles[pick.role]"
              >
                {{ pick.role_label }}
              </span>
              <span class="text-sm font-medium text-slate-300">{{ pick.amount }} 元</span>
            </div>

            <p class="text-center text-5xl font-bold tracking-tight text-white">
              {{ pad(pick.number) }}
            </p>

            <div class="space-y-1.5 text-sm">
              <div class="flex items-center justify-between">
                <span class="text-slate-500">差值</span>
                <span class="text-slate-200">{{ pick.diff }}</span>
              </div>
              <div class="flex items-center justify-between">
                <span class="text-slate-500">波动</span>
                <span
                  class="rounded border px-1.5 py-0.5 text-xs"
                  :class="waveStyles[pick.wave_type]"
                >
                  {{ pick.wave_label }}
                </span>
              </div>
              <div class="flex items-center justify-between">
                <span class="text-slate-500">重肖</span>
                <span :class="pick.is_repeat_zodiac ? 'text-rose-400' : 'text-emerald-400'">
                  {{ pick.is_repeat_zodiac ? '是' : '否' }}
                </span>
              </div>
            </div>
          </article>
        </section>

        <!-- 复制 -->
        <section class="space-y-2">
          <button
            type="button"
            class="w-full rounded-xl bg-cyan-500 px-5 py-3 font-medium text-slate-950 transition hover:bg-cyan-400"
            @click="copy"
          >
            {{ copied ? '已复制 ✓' : '一键复制投注串' }}
          </button>
          <p class="text-center font-mono text-sm text-slate-300">
            {{ result.copy_text }}
          </p>
          <p v-if="copyError" class="text-center text-xs text-rose-400">{{ copyError }}</p>
        </section>

        <!-- 说明 -->
        <section v-if="result.notes.length" class="space-y-1 border-t border-slate-800 pt-4">
          <p v-for="note in result.notes" :key="note" class="text-xs text-slate-500">
            · {{ note }}
          </p>
        </section>
      </template>
    </div>
  </main>
</template>
