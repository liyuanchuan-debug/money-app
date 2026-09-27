<script setup lang="ts">
/** 生肖走势：访客可读（只读样本统计） */

/**
 * 生肖走势 —— GET /api/stats/zodiac-trend
 * --------------------------------------------------------------------------
 * 口径铁律：生肖走势只覆盖「本池已导入的 N 期特码样本」（后端 notes 原话）。
 * 连出 / 轮转 / 覆盖都是**描述样本内已经发生的事**，不是对下一期的判断。
 *
 * 最近窗口 recent 由本页的控件传入（后端默认 12，上限 49）；窗口一变，
 * 覆盖生肖数就会变，页面会展示后端回报的 requested / used / present / missing。
 *
 * 权限：本页为公开只读数据；个人投注能力（recommend / pnl）仍须 VIP。
 */
import type { ZodiacTrendStats } from '~/composables/useStats'
import { STATS_DEFAULT_SAMPLE_LIMIT, useStats } from '~/composables/useStats'

const api = useStats()
const sampleLimit = ref(STATS_DEFAULT_SAMPLE_LIMIT)

/** 最近 N 期观察窗口（后端默认 12，上限 49） */
const RECENT_OPTIONS = [6, 12, 24, 36]
const recent = ref(12)

/** 只读：生肖走势不写库 */
const { data, pending, error, refresh } = await useAsyncData<ZodiacTrendStats>(
  'stats-zodiac-trend',
  () => api.zodiacTrend(sampleLimit.value, recent.value),
  { watch: [sampleLimit, recent] },
)

useHead({ title: '生肖走势 · 四叶沙盘' })

const zodiacs = computed(() => data.value?.zodiacs ?? [])
const window_ = computed(() => data.value?.recent_window ?? null)
const leaders = computed(() => data.value?.max_streak_leaders ?? [])

/** 英文码 → 中文标签（后端只给 sequence 的英文码，标签要自己映射） */
const zodiacLabels = computed(
  () => new Map(zodiacs.value.map(row => [row.zodiac, row.zodiac_label])),
)

/** 最近窗口的生肖序列（按时间升序，旧 → 新） */
const windowSequence = computed(() =>
  (window_.value?.sequence ?? []).map(code => ({
    code,
    label: zodiacLabels.value.get(code) ?? code,
  })),
)

/** 最近窗口覆盖度：出现过的生肖 / 全部生肖 */
const coverageValue = computed(() => {
  const info = window_.value
  if (!info || !info.available) return null
  return (info.distinct_count / info.available) * 100
})

/** 12 生肖出现次数柱状图（提示里带上连出口径） */
const zodiacBars = computed(() =>
  zodiacs.value.map(row => ({
    label: row.zodiac_label,
    value: row.appearances,
    hint:
      `最长连出 ${row.max_streak} 期`
      + (row.max_streak_end_period === null ? '' : `（第 ${row.max_streak_end_period} 期结束）`)
      + ` · 当前连出 ${row.current_streak} 期`,
  })),
)
</script>

<template>
  <StatsPageFrame
    v-model:sample-limit="sampleLimit"
    title="生肖走势"
    subtitle="本池已导入样本内，12 生肖的出现次数与连出（最长 / 当前），以及最近观察窗口的覆盖与轮转情况。连出与覆盖只描述样本内已发生的事，不是对下一期的判断。"
    required-role="PUBLIC"
    :envelope="data"
    :pending="pending"
    :error="error"
    @retry="refresh()"
  >
    <div class="flex flex-col gap-6">
      <MotionReveal :index="2">
        <!-- 最近窗口：观察范围可调 -->
        <GlassPanel variant="soft" padding="sm" rounded="2xl" as="div" class="space-y-2.5">
          <div class="flex items-baseline justify-between gap-2">
            <p class="text-sm font-medium text-slate-200">最近观察窗口</p>
            <span class="num text-[11px] text-slate-500">
              实际取到 {{ window_?.used ?? '—' }} 期 / 请求 {{ window_?.requested ?? recent }} 期
            </span>
          </div>
          <div class="flex flex-wrap gap-2" role="group" aria-label="最近观察窗口">
            <button
              v-for="option in RECENT_OPTIONS"
              :key="option"
              type="button"
              class="num min-h-[44px] min-w-[64px] rounded-xl border px-4 text-base font-medium transition-colors duration-200 select-none active:scale-[0.98]"
              :class="option === recent
                ? 'border-nebula-400/60 bg-nebula-400/15 text-nebula-100'
                : 'border-white/10 bg-white/5 text-slate-300 active:bg-white/15'"
              :aria-pressed="option === recent"
              @click="recent = option"
            >
              {{ option }}
            </button>
          </div>
          <p class="text-[11px] leading-relaxed text-slate-500">
            窗口越长越平滑，越短越受单期影响；覆盖率是描述性的，不具可比性时后端会标注「数据不足」。
          </p>
        </GlassPanel>
      </MotionReveal>
      <MotionReveal :index="3">
        <!-- 覆盖度 + 轮转 -->
        <GlassPanel padding="lg" rounded="3xl" class="space-y-4">
          <div class="space-y-1">
            <h2 class="text-lg font-medium text-white">最近 {{ window_?.used ?? recent }} 期覆盖</h2>
            <p class="text-xs text-slate-500">口径：本池样本内最近窗口里出现过的生肖数量 / 全部 12 生肖。</p>
          </div>

          <div class="flex flex-col items-center gap-4 sm:flex-row sm:items-center sm:justify-around">
            <RingGauge
              :value="coverageValue"
              :size="164"
              tone="nebula"
              :decimals="0"
              label="窗口覆盖生肖"
              :caption="`${window_?.distinct_count ?? 0} / ${window_?.available ?? 12} 个生肖`"
              empty-text="数据不足"
            />

            <div class="w-full space-y-3 sm:w-auto sm:min-w-[240px]">
              <div class="space-y-1.5">
                <p class="text-[11px] text-slate-500">窗口内出现过</p>
                <div class="flex flex-wrap gap-1.5">
                  <StatChip
                    v-for="label in window_?.present_labels ?? []"
                    :key="`present-${label}`"
                    tone="aqua"
                    size="xs"
                  >
                    {{ label }}
                  </StatChip>
                  <span v-if="!(window_?.present_labels ?? []).length" class="text-xs text-slate-500">
                    数据不足
                  </span>
                </div>
              </div>
              <div class="space-y-1.5">
                <p class="text-[11px] text-slate-500">窗口内未出现</p>
                <div class="flex flex-wrap gap-1.5">
                  <StatChip
                    v-for="label in window_?.missing_labels ?? []"
                    :key="`missing-${label}`"
                    tone="neutral"
                    size="xs"
                    outline
                  >
                    {{ label }}
                  </StatChip>
                  <span v-if="!(window_?.missing_labels ?? []).length" class="text-xs text-slate-500">
                    12 个生肖在窗口内都出现过
                  </span>
                </div>
              </div>
              <p v-if="data?.rotation?.note" class="text-[11px] leading-relaxed text-slate-500">
                {{ data.rotation.note }}
              </p>
            </div>
          </div>

          <!-- 窗口序列：旧 → 新 -->
          <div class="space-y-1.5">
            <p class="text-[11px] text-slate-500">窗口序列（旧 → 新）</p>
            <div class="flex flex-wrap gap-1.5">
              <span
                v-for="(item, index) in windowSequence"
                :key="`${item.code}-${index}`"
                class="rounded-lg border border-nebula-400/30 bg-nebula-400/10 px-2 py-1 text-xs text-nebula-100"
              >
                {{ item.label }}
              </span>
              <span v-if="!windowSequence.length" class="text-xs text-slate-500">数据不足</span>
            </div>
          </div>
        </GlassPanel>
      </MotionReveal>
      <MotionReveal :index="4">
        <!-- 出现次数 -->
        <GlassPanel padding="lg" rounded="3xl" class="space-y-3">
          <div class="space-y-1">
            <h2 class="text-lg font-medium text-white">12 生肖出现次数</h2>
            <p class="text-xs text-slate-500">柱高 = 样本内出现次数；提示里带上该生肖的最长连出与当前连出。</p>
          </div>
          <BarChart
            :bars="zodiacBars"
            :height="230"
            value-suffix=" 次"
            empty-text="数据不足"
            empty-hint="本池还没有可用于统计的开奖记录"
          />
        </GlassPanel>
      </MotionReveal>
      <MotionReveal :index="5">
        <!-- 连出榜 -->
        <GlassPanel variant="soft" padding="sm" rounded="2xl" as="div" class="space-y-3">
          <div class="space-y-1">
            <h3 class="text-sm font-medium text-slate-200">最长连出榜</h3>
            <p class="text-[11px] text-slate-500">
              连出 = 连续多期特码落在同一生肖；按最长连出降序取前 {{ leaders.length }} 名。
            </p>
          </div>

          <p v-if="!leaders.length" class="text-xs text-slate-500">数据不足，无法排名。</p>

          <ul v-else class="space-y-1.5">
            <li
              v-for="row in leaders"
              :key="row.zodiac"
              class="flex min-h-[44px] items-center gap-3 rounded-xl border border-white/5 bg-white/5 px-3 py-2"
            >
              <StatChip tone="nebula" size="sm">{{ row.zodiac_label }}</StatChip>
              <span class="num text-[11px] text-slate-500">
                样本内出现 {{ row.appearances }} 次
                <template v-if="row.max_streak_end_period !== null">
                  · 第 {{ row.max_streak_end_period }} 期结束
                </template>
              </span>
              <span class="num ml-auto shrink-0 text-base font-semibold text-nebula-200">
                连出 {{ row.max_streak }}<span class="ml-0.5 text-[11px] font-normal">期</span>
              </span>
            </li>
          </ul>
        </GlassPanel>
      </MotionReveal>
      <MotionReveal :index="6">
        <!-- 12 生肖明细 -->
        <GlassPanel variant="soft" padding="sm" rounded="2xl" as="div" class="space-y-3">
          <h3 class="text-sm font-medium text-slate-200">12 生肖明细</h3>
          <p v-if="!zodiacs.length" class="text-xs text-slate-500">数据不足。</p>
          <div v-else class="overflow-x-auto">
            <table class="w-full min-w-[380px] text-left text-sm">
              <thead class="glass-table-head text-xs text-slate-400">
                <tr class="border-b border-white/10">
                  <th class="px-2 py-2 font-medium">生肖</th>
                  <th class="px-2 py-2 text-right font-medium">出现</th>
                  <th class="px-2 py-2 text-right font-medium">最长连出</th>
                  <th class="px-2 py-2 text-right font-medium">当前连出</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="row in zodiacs"
                  :key="row.zodiac"
                  class="border-b border-white/5 last:border-b-0"
                >
                  <td class="px-2 py-2 text-slate-200">{{ row.zodiac_label }}</td>
                  <td class="num px-2 py-2 text-right text-slate-300">{{ row.appearances }}</td>
                  <td class="num px-2 py-2 text-right text-slate-300">{{ row.max_streak }}</td>
                  <td
                    class="num px-2 py-2 text-right"
                    :class="row.current_streak > 0 ? 'text-aqua-200' : 'text-slate-500'"
                  >
                    {{ row.current_streak }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </GlassPanel>
      </MotionReveal>
    </div>
  </StatsPageFrame>
</template>
