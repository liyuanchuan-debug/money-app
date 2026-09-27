<script setup lang="ts">
import type { DrawImportResult, DrawItem } from '~/composables/useApi'
import { scopeLabel } from '~/composables/useDraws'

definePageMeta({ role: 'ADMIN' })

const api = useApi()

const text = ref('')
const importing = ref(false)
const clearing = ref(false)
const result = ref<DrawImportResult | null>(null)
const errorMessage = ref('')
const infoMessage = ref('')

// 用户真实数据格式（期号在前 + 全角冒号 + 旧装饰）；(波色) 与 /五行 会被自动忽略
const SAMPLE_LINE
  = '269期 2026-09-26：19(红)鼠/火、40(红)兔/火、36(蓝)羊/土、05(绿)虎/金、'
  + '38(绿)蛇/木、16(绿)兔/木 +22(绿)鸡/水'

/**
 * 一次把池子取满（后端 limit 上限 500），统计与分页都基于这一份数据。
 * 后端 GET /api/draws 只返回裸数组、没有 total 字段，只有把整池取回来，
 * 「共 N 期 / 第 X / Y 页」才算得准；顺带翻页不再打网络。
 */
const POOL_LIMIT = 500

/** 每页条数：手机上 20 行 ≈ 两屏（表格本身横向可滚，行数只影响纵向长度） */
const PAGE_SIZE = 20

const { data: draws, refresh: refreshDraws, pending } = await useAsyncData<DrawItem[]>(
  'draws',
  () => api.listDraws(POOL_LIMIT, 0),
)

/** 当前页码（1 起）；越界由 GlassPager 负责夹紧 */
const page = ref(1)
/** 只渲染当前一页的行：DOM 里永远只有一页，不再一次铺满 200 行 */
const pagedDraws = computed(() =>
  (draws.value ?? []).slice((page.value - 1) * PAGE_SIZE, page.value * PAGE_SIZE),
)

/** 池子超过上限时，列表与统计只覆盖最近 POOL_LIMIT 期（如实说明，不假装全量） */
const poolTruncated = computed(() => (draws.value?.length ?? 0) >= POOL_LIMIT)

/** 删除 / 清空后把页码夹回最后一页，绝不把用户留在空白页 */
function clampPage() {
  const pages = Math.max(1, Math.ceil((draws.value?.length ?? 0) / PAGE_SIZE))
  if (page.value > pages) page.value = pages
}

function pad(n: number | null | undefined) {
  if (n === null || n === undefined) return '—'
  return String(n).padStart(2, '0')
}

/* ---------------- 统计：全部只基于本池已导入的开奖记录 ---------------- */
const importHit = useHitPulse()

const counts = computed(() => {
  const map = new Map<number, number>()
  for (const draw of draws.value ?? []) {
    map.set(draw.special_number, (map.get(draw.special_number) ?? 0) + 1)
  }
  return map
})

/** 出现次数排行（升序排名，次数多的在前；次数相同按号码排） */
const frequency = computed(() =>
  [...counts.value.entries()]
    .map(([number, count]) => ({ label: pad(number), value: count }))
    .sort((a, b) => b.value - a.value || a.label.localeCompare(b.label)),
)

const topFrequency = computed(() => frequency.value.slice(0, 10))

const heatCells = computed(() =>
  Array.from({ length: 49 }, (_, index) => {
    const number = index + 1
    return {
      number: pad(number),
      value: counts.value.get(number) ?? 0,
      hint: '本池已导入的记录里出现次数',
    }
  }),
)

const distinctCount = computed(() => counts.value.size)
const totalDraws = computed(() => draws.value?.length ?? 0)

/** 号码覆盖度：本池已导入数据里出现过的号码占 01-49 的比例 */
const coverage = computed(() => (totalDraws.value ? (distinctCount.value / 49) * 100 : null))

const coverageCaption = computed(() =>
  totalDraws.value
    ? `已出现 ${distinctCount.value} / 49 个号码（${scopeLabel(totalDraws.value)}）`
    : '还没有可统计的导入记录',
)

const latestSpecial = computed(() => draws.value?.[0]?.special_number ?? null)

function fillSample() {
  text.value = SAMPLE_LINE
}

async function submit() {
  errorMessage.value = ''
  infoMessage.value = ''
  result.value = null

  if (!text.value.trim()) {
    errorMessage.value = '请先粘贴开奖总表文本'
    return
  }

  importing.value = true
  try {
    const summary = await api.importDraws(text.value)
    result.value = summary
    infoMessage.value = `导入完成：新增 / 覆盖 ${summary.imported} 期，跳过 ${summary.skipped} 期`
    if (summary.imported > 0) importHit.fire()
    await refreshDraws()
    // 新导入的期次排在最前，直接回到第 1 页让用户看到结果
    page.value = 1
  } catch (err: any) {
    errorMessage.value = err?.data?.detail || err?.message || '导入失败'
  } finally {
    importing.value = false
  }
}

async function removeDraw(draw: DrawItem) {
  if (!confirm(`确定删除 ${draw.draw_date} 第 ${draw.period} 期？此操作不可恢复。`)) return
  errorMessage.value = ''
  infoMessage.value = ''
  try {
    await api.deleteDraw(draw.id)
    infoMessage.value = `已删除 ${draw.draw_date} 第 ${draw.period} 期`
    await refreshDraws()
    // 删到当前页空了就把页码夹回最后一页（GlassPager 也会兜底夹紧）
    clampPage()
  } catch (err: any) {
    errorMessage.value = err?.data?.detail || err?.message || '删除失败'
  }
}

/* ---------------- 纠正开奖（覆盖特码 + 重算该期盈亏） ---------------- */
const correctingId = ref<number | null>(null)
const correctNumber = ref('')
const correctSubmitting = ref(false)

function openCorrect(draw: DrawItem) {
  correctingId.value = draw.id
  correctNumber.value = String(draw.special_number)
  errorMessage.value = ''
  infoMessage.value = ''
}

function cancelCorrect() {
  correctingId.value = null
  correctNumber.value = ''
}

async function submitCorrect(draw: DrawItem) {
  if (correctSubmitting.value) return
  const raw = correctNumber.value.trim()
  const number = Number(raw)
  if (!Number.isInteger(number) || number < 1 || number > 49) {
    errorMessage.value = '纠正特码必须是 1-49 之间的整数'
    return
  }
  if (number === draw.special_number) {
    errorMessage.value = '新特码与原特码相同，无需纠正'
    return
  }

  const ok = confirm(
    `纠正开奖：第 ${draw.period} 期特码将从 ${pad(draw.special_number)} 改为 ${pad(number)}。\n`
    + '会覆盖原号码，并重算该期已采用推荐的命中与盈亏。确定继续？',
  )
  if (!ok) return

  correctSubmitting.value = true
  errorMessage.value = ''
  infoMessage.value = ''
  try {
    const result = await api.correctDraw(draw.id, { special_number: number })
    infoMessage.value
      = `已纠正第 ${result.period} 期：特码 ${pad(result.old_special_number)} → ${pad(result.new_special_number)}`
      + `；重算采用快照 ${result.resettled_rounds} 条`
    correctingId.value = null
    correctNumber.value = ''
    await refreshDraws()
    try {
      await Promise.all([
        refreshNuxtData('draws-sample'),
        refreshNuxtData('draws-latest'),
        refreshNuxtData('stats-pnl'),
        refreshNuxtData('recommend'),
      ])
    } catch {
      /* 下游缓存刷新失败不影响纠正事实 */
    }
  } catch (err: any) {
    errorMessage.value = err?.data?.detail || err?.message || '纠正失败'
  } finally {
    correctSubmitting.value = false
  }
}

async function clearAll() {
  if (!confirm('确定清空全部开奖总表记录？此操作不可恢复。')) return
  errorMessage.value = ''
  infoMessage.value = ''
  clearing.value = true
  try {
    const res = await api.clearDraws()
    result.value = null
    infoMessage.value = `已清空 ${res.deleted} 期开奖记录`
    await refreshDraws()
    page.value = 1
  } catch (err: any) {
    errorMessage.value = err?.data?.detail || err?.message || '清空失败'
  } finally {
    clearing.value = false
  }
}
</script>

<template>
  <main class="relative px-5 py-10 text-slate-100 sm:py-14">
    <div class="mx-auto flex max-w-4xl flex-col gap-7">
      <!-- 头部：明确「整期开奖总表批量导入」，与 /entry 的单号录入页区分 -->
      <MotionReveal :index="0" class="flex flex-wrap items-start justify-between gap-4">
        <div class="space-y-2.5">
          <div>
            <BrandMark size="sm" />
          </div>
          <StatChip tone="nebula" size="sm" dot>
            整期开奖总表批量导入｜一次粘贴多期，每期只保留 1 个特码
          </StatChip>
          <h1 class="text-gradient text-3xl font-semibold tracking-tight sm:text-4xl">
            开奖总表导入
          </h1>
          <p class="max-w-2xl text-sm text-slate-400">
            每行一整期（日期 / 期号 + 6 正码 + 1 特码），`+` 标记的那枚是特码。<strong class="font-medium text-slate-200">系统只保留特码</strong>，
            正码会被丢弃；(波色) 与 生肖/五行 这类旧装饰照常粘贴即可，程序会自动忽略它们。
          </p>
          <p class="text-xs text-slate-500">
            单号补录请走「开奖号录入」页（/entry）；本页只做整期开奖总表的批量导入。
          </p>
        </div>
        <AppNav />
      </MotionReveal>

      <!-- 粘贴导入 -->
      <MotionReveal :index="1">
        <GlassPanel variant="strong" padding="lg" rounded="3xl" glow tone="nebula">
          <div class="space-y-3">
            <div class="flex flex-wrap items-center justify-between gap-2">
              <h2 class="text-lg font-medium text-white">粘贴开奖总表</h2>
              <GlassButton size="sm" @click="fillSample">载入示例行</GlassButton>
            </div>

            <textarea
              v-model="text"
              rows="6"
              class="glass-input num block w-full px-4 py-3 font-mono text-sm text-slate-100"
              placeholder="每行一期，例如：269期 2026-09-26：19(红)鼠/火、40(红)兔/火、… +22(绿)鸡/水"
            />

            <div class="flex flex-wrap gap-2.5">
              <div class="relative inline-flex rounded-[14px]">
                <GlassButton
                  variant="primary"
                  :loading="importing"
                  @click="submit"
                >
                  {{ importing ? '导入中…' : '导入' }}
                </GlassButton>
                <PulseRing :trigger="importHit.key" tone="nebula" :rings="2" />
              </div>
              <GlassButton
                variant="danger"
                :loading="clearing"
                :disabled="clearing || !draws?.length"
                @click="clearAll"
              >
                {{ clearing ? '清空中…' : '清空全部' }}
              </GlassButton>
            </div>

            <p v-if="errorMessage" class="text-sm text-bloom-300">{{ errorMessage }}</p>
            <p v-else-if="infoMessage" class="text-sm text-emerald-300">{{ infoMessage }}</p>
          </div>
        </GlassPanel>
      </MotionReveal>

      <!-- 导入结果：成功 / 解析失败 / 警告 分栏展示 -->
      <section v-if="result" class="space-y-3">
        <div class="glass-panel rounded-2xl border border-emerald-400/40 px-4 py-3 text-sm text-emerald-200">
          导入成功：新增 / 覆盖 {{ result.imported }} 期，跳过 {{ result.skipped }} 期。
        </div>

        <div
          v-if="result.errors.length"
          class="glass-panel space-y-2 rounded-2xl border border-bloom-400/40 px-4 py-3 text-sm text-bloom-200"
        >
          <p class="font-medium">解析失败 {{ result.errors.length }} 行（其余合法行已正常导入）</p>
          <ul class="space-y-1 text-xs">
            <li v-for="(item, index) in result.errors" :key="index">
              第 {{ item.line }} 行：{{ item.message }}｜原文：{{ item.text }}
            </li>
          </ul>
        </div>

        <div
          v-if="result.warnings.length"
          class="glass-panel space-y-2 rounded-2xl border border-amber-400/40 px-4 py-3 text-sm text-amber-200"
        >
          <p class="font-medium">
            警告 {{ result.warnings.length }} 条（已按生肖权威表记录，不影响导入）
          </p>
          <ul class="space-y-1 text-xs">
            <li v-for="(item, index) in result.warnings" :key="index">{{ item }}</li>
          </ul>
        </div>
      </section>

      <!-- 已导入记录 -->
      <section class="space-y-3">
        <div class="flex flex-wrap items-end justify-between gap-2">
          <div>
            <h2 class="text-lg font-medium text-white">已导入开奖记录</h2>
            <p class="text-xs text-slate-500">
              只保留每期特码；下表与统计只基于本池已导入的开奖记录。录错可用「纠正」覆盖特码并重算盈亏。
            </p>
          </div>
          <span class="num text-xs text-slate-500">共 {{ totalDraws }} 期</span>
        </div>

        <p v-if="pending" class="text-sm text-slate-400">加载中…</p>
        <GlassPanel v-else-if="!draws?.length" variant="soft" padding="lg">
          <p class="text-center text-sm text-slate-500">
            还没有开奖记录，粘贴开奖总表后点「导入」。
          </p>
        </GlassPanel>
        <GlassPanel v-else variant="soft" padding="none" rounded="2xl" class="overflow-hidden">
          <div id="draws-table" class="overflow-x-auto">
            <table class="w-full min-w-[520px] text-left text-sm">
              <thead class="glass-table-head text-xs text-slate-400">
                <tr class="border-b border-white/10">
                  <th class="px-4 py-3 font-medium">期号</th>
                  <th class="px-4 py-3 font-medium">日期</th>
                  <th class="px-4 py-3 font-medium">特码</th>
                  <th class="px-4 py-3 font-medium">生肖</th>
                  <th class="px-4 py-3 text-right font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="draw in pagedDraws"
                  :key="draw.id"
                  class="border-b border-white/5 transition-colors last:border-b-0 hover:bg-white/5"
                >
                  <td class="num px-4 py-3 text-slate-300">{{ draw.period }}</td>
                  <td class="num px-4 py-3 text-slate-400">{{ draw.draw_date }}</td>
                  <td class="px-4 py-3">
                    <SpecialBall
                      :number="draw.special_number"
                      size="sm"
                      :tone="draw.special_number === latestSpecial ? 'aqua' : 'nebula'"
                      :glow="draw.special_number === latestSpecial"
                    />
                  </td>
                  <td class="px-4 py-3">
                    <StatChip tone="neutral" size="sm">
                      {{ draw.zodiac_label ?? draw.zodiac ?? '—' }}
                    </StatChip>
                  </td>
                  <td class="px-4 py-3 text-right">
                    <div class="flex flex-col items-end gap-2">
                      <div class="flex flex-wrap justify-end gap-1.5">
                        <GlassButton
                          size="sm"
                          variant="glass"
                          :disabled="correctSubmitting"
                          @click="openCorrect(draw)"
                        >
                          纠正
                        </GlassButton>
                        <GlassButton size="sm" variant="ghost" @click="removeDraw(draw)">
                          <span class="text-bloom-300">删除</span>
                        </GlassButton>
                      </div>
                      <div
                        v-if="correctingId === draw.id"
                        class="flex w-full max-w-[220px] flex-col gap-2 rounded-xl border border-aqua-400/30 bg-aqua-400/5 p-2 text-left"
                      >
                        <p class="text-[11px] text-slate-400">
                          原特码 {{ pad(draw.special_number) }} → 新特码
                        </p>
                        <input
                          v-model="correctNumber"
                          type="number"
                          min="1"
                          max="49"
                          inputmode="numeric"
                          class="glass-input num h-10 w-full px-3 text-base text-white"
                          aria-label="纠正后的特码"
                        >
                        <div class="flex flex-wrap gap-1.5">
                          <GlassButton
                            size="sm"
                            variant="primary"
                            class="min-h-[40px]"
                            :loading="correctSubmitting"
                            @click="submitCorrect(draw)"
                          >
                            确认纠正
                          </GlassButton>
                          <GlassButton
                            size="sm"
                            variant="glass"
                            class="min-h-[40px]"
                            :disabled="correctSubmitting"
                            @click="cancelCorrect"
                          >
                            取消
                          </GlassButton>
                        </div>
                      </div>
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <!-- 分页：手机优先，每页 20 行；删除后页码会被夹回最后一页，不会停在空白页 -->
          <GlassPager
            v-model:page="page"
            :page-size="PAGE_SIZE"
            :total="totalDraws"
            label="已导入开奖记录分页"
            scroll-target="#draws-table"
            class="border-t border-white/10 px-4 py-3"
          />
        </GlassPanel>
      </section>

      <!-- 频次 / 热度 / 覆盖度：全部基于本池已导入数据 -->
      <MotionReveal :index="3">
        <GlassPanel padding="lg" rounded="3xl">
          <div class="mb-4">
            <h2 class="text-lg font-medium text-white">特码出现频次</h2>
            <p class="text-xs text-slate-500">
              口径：{{ scopeLabel(totalDraws) }}；同一青色系内柱子越高越深，样本不足时不做任何分布推断。
            </p>
            <p v-if="poolTruncated" class="mt-1 text-xs text-amber-200">
              本池记录已超过 {{ POOL_LIMIT }} 期：列表与以下统计只覆盖最近 {{ POOL_LIMIT }} 期。
            </p>
          </div>
          <BarChart
            :bars="topFrequency"
            :height="220"
            tone="aqua"
            value-suffix=" 次"
            empty-text="数据不足"
            empty-hint="导入整期开奖总表后这里才会出现频次"
          />
        </GlassPanel>
      </MotionReveal>

      <MotionReveal :index="4">
        <div class="grid gap-5 lg:grid-cols-3">
          <GlassPanel padding="lg" rounded="3xl" class="lg:col-span-2">
            <div class="mb-4">
              <h2 class="text-lg font-medium text-white">01 - 49 出现热度</h2>
              <p class="text-xs text-slate-500">
                同一个青色系里，颜色越深表示在本池已导入的记录里出现越多；
                中性灰格表示暂时没有出现过。下方色阶标出每一档对应的出现次数。
              </p>
            </div>
            <HeatGrid
              :cells="heatCells"
              :columns="7"
              tone="aqua"
              :highlight="latestSpecial === null ? [] : [pad(latestSpecial)]"
              legend
              value-unit="次"
              :legend-scope="scopeLabel(totalDraws)"
              empty-text="数据不足"
            />
          </GlassPanel>

          <GlassPanel padding="lg" rounded="3xl" class="flex flex-col items-center justify-center gap-4">
            <RingGauge
              :value="coverage"
              :size="168"
              tone="nebula"
              label="号码覆盖度"
              :caption="coverageCaption"
            />
            <p class="text-center text-[11px] text-slate-500">
              覆盖度 = 本池已导入记录中出现过的号码个数 / 49。
            </p>
          </GlassPanel>
        </div>
      </MotionReveal>
    </div>
  </main>
</template>
