<script setup lang="ts">
import type { ZodiacTable, ZodiacYear } from '~/composables/useApi'
import { zodiacIdentity, zodiacTint } from '~/composables/useZodiac'

/** 生肖表：访客可读，无登录门禁 */

/**
 * 生肖表（农历年口径的 49 号码关联表，只读）。
 *
 * 三点口径，页面文案与此保持一致：
 *   1. 表按**农历年（春节当日）**切换，不是自然年；同一个号码在不同农历年属于不同生肖
 *      （01 号 2026 丙午马年是「马」，2025 乙巳蛇年是「蛇」）——这是本页最容易看错的一点；
 *   2. 结构来自算术：号码 12 个一循环（1 = 13 = 25 = 37 = 49 同肖），且 49 = 12 × 4 + 1，
 *      所以 12 组里必然只有 1 组是 5 个号码（含 01 与 49），其余 11 组各 4 个；
 *   3. 本页只做映射展示，不读开奖记录、不统计出现次数、不推导任何开奖结果。
 *
 * 视觉：沿用全站液态琉璃（AppBackground + GlassPanel / GlassCard / SpecialBall /
 * MotionReveal / StatChip），生肖专属字形与 12 色板集中在 components/ZodiacGlyph.vue
 * 与 composables/useZodiac.ts（色值只有那一处来源）。
 */
const api = useApi()

const selectedYear = ref<number | null>(null)
const table = ref<ZodiacTable | null>(null)
const loading = ref(true)
const errorMessage = ref('')

const { data: years } = await useAsyncData<ZodiacYear[]>(
  'zodiac-years',
  () => api.zodiacYears(),
)

const yearMap = computed(
  () => new Map((years.value ?? []).map(item => [item.lunar_year, item])),
)

function pad(n: number) {
  return String(n).padStart(2, '0')
}

async function loadTable(lunarYear?: number | null) {
  loading.value = true
  errorMessage.value = ''
  try {
    const year = lunarYear ?? selectedYear.value
    // 用该农历年的春节当日去取表：生肖表按农历年（春节）切换
    const startsOn = year != null ? yearMap.value.get(year)?.starts_on : undefined
    const data = await api.zodiacTable(startsOn)
    table.value = data
    selectedYear.value = data.lunar_year
  } catch (err: any) {
    errorMessage.value = err?.data?.detail || err?.message || '加载生肖表失败'
  } finally {
    loading.value = false
  }
}

await loadTable()

function onYearChange() {
  // 出错后再选回同一年也要能重试，所以错误态同样触发重新拉取
  if (selectedYear.value !== table.value?.lunar_year || errorMessage.value) {
    loadTable(selectedYear.value)
  }
}

/* ------------------------------------------------------------------ */
/* 12 色板（唯一来源在 composables/useZodiac.ts，此处只做取用）          */
/* ------------------------------------------------------------------ */
const identityOf = (code?: string | null) => zodiacIdentity(code)

/**
 * 今年拿到 5 个号码的那一组。
 * 49 = 12 × 4 + 1 → 12 组里必然只有 1 组是 5 个（同时含 01 与 49），其余各 4 个；
 * 这里从接口数据里找出来，不写死是哪个生肖（换年份会换成另一个）。
 */
const fiveGroup = computed(
  () => (table.value?.zodiacs ?? []).find(group => group.numbers.length > 4) ?? null,
)

const fiveGroupNumbersText = computed(() =>
  (fiveGroup.value?.numbers ?? []).map(pad).join('、'),
)

function isFiveNumber(code?: string | null) {
  return !!code && code === fiveGroup.value?.code
}

/** 号码片 / 号码格：同肖同色（淡染 + 同色描边）；5 个号码的那一组更亮一档 */
function tintStyle(code?: string | null, strong = false) {
  return {
    borderColor: zodiacTint(code, strong ? 0.62 : 0.32),
    backgroundColor: zodiacTint(code, strong ? 0.18 : 0.1),
    boxShadow: 'inset 0 1px 0 0 rgba(255, 255, 255, 0.12)',
  }
}

function dotStyle(code?: string | null) {
  return {
    backgroundColor: zodiacTint(code, 0.95),
    boxShadow: `0 0 10px -2px ${zodiacTint(code, 0.9)}`,
  }
}

/**
 * SpecialBall 只有三档色调（aqua / nebula / bloom），按强调色家族挑最接近的一档；
 * 线条字形与号码片仍然用各自生肖的专属强调色。
 */
const BLOOM_CODES = new Set(['OX', 'TIGER', 'RABBIT', 'HORSE', 'MONKEY', 'ROOSTER'])
const NEBULA_CODES = new Set(['DRAGON', 'DOG', 'PIG'])
function ballTone(code?: string | null): 'aqua' | 'nebula' | 'bloom' {
  if (code && BLOOM_CODES.has(code)) return 'bloom'
  if (code && NEBULA_CODES.has(code)) return 'nebula'
  return 'aqua'
}
</script>

<template>
  <main class="relative px-5 py-10 text-slate-100 sm:py-14">
    <div class="mx-auto flex max-w-3xl flex-col gap-6 sm:gap-7">
      <!-- 头部 -->
      <MotionReveal :index="0" class="flex flex-wrap items-start justify-between gap-4">
        <div class="space-y-2.5">
          <div>
            <BrandMark size="sm" />
          </div>
          <h1 class="text-gradient text-3xl font-semibold tracking-tight sm:text-4xl">
            生肖表
          </h1>
          <p class="max-w-xl text-sm text-slate-400">
            49 个号码在每个农历年各归属一个生肖。本页只是这张归属表：只读、不统计开奖、不推导任何开奖结果。
          </p>
          <StatChip tone="aqua" size="sm" dot>只读映射表 · 农历年（春节）口径</StatChip>
        </div>
        <AppNav />
      </MotionReveal>

      <!-- 农历年轮转说明（本页最容易看错的一点，保持醒目） -->
      <MotionReveal :index="1">
        <GlassPanel
          variant="soft"
          padding="md"
          rounded="3xl"
          class="border-amber-300/35 bg-amber-400/5"
        >
          <div class="flex flex-col gap-2.5">
            <div class="flex flex-wrap items-center gap-2.5">
              <StatChip tone="amber" size="sm" dot>随春节重排</StatChip>
              <p class="text-sm font-semibold text-amber-100">
                生肖表随农历年（春节）重排，不是固定不变的。
              </p>
            </div>
            <p class="text-sm leading-relaxed text-amber-200/90">
              同一个号码在不同农历年属于<strong class="font-semibold text-amber-100">不同生肖</strong>：01 号在
              2026 丙午马年是「马」，在 2025 乙巳蛇年却是「蛇」——生肖随春节整体轮转一圈。
              本页只是这 49 个号码的归属表，不据此推导任何开奖结果。
            </p>
          </div>
        </GlassPanel>
      </MotionReveal>

      <!-- 年份切换 -->
      <MotionReveal :index="2">
        <GlassPanel variant="strong" padding="lg" rounded="3xl" glow tone="aqua">
          <div class="flex flex-col gap-4">
            <div class="flex flex-col gap-2">
              <label for="zodiac-year" class="text-sm font-medium text-slate-200">农历年</label>
              <div class="relative w-full sm:w-64">
                <select
                  id="zodiac-year"
                  v-model.number="selectedYear"
                  class="glass-input num h-12 w-full appearance-none px-4 pr-11 text-base text-white"
                  @change="onYearChange"
                >
                  <option v-for="year in years" :key="year.lunar_year" :value="year.lunar_year">
                    {{ year.lunar_year }}（{{ year.animal_of_01_label }}年）
                  </option>
                </select>
                <svg
                  class="pointer-events-none absolute top-1/2 right-4 h-4 w-4 -translate-y-1/2 text-slate-400"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="2"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                  aria-hidden="true"
                >
                  <path d="M6 9l6 6 6-6" />
                </svg>
              </div>
              <p class="text-[11px] text-slate-500">
                切换年份按该农历年的春节当日重新取表（表按春节切换，不按自然年）。
              </p>
            </div>

            <div v-if="table" class="flex flex-wrap items-center gap-2">
              <StatChip tone="aqua" size="sm" dot>春节 {{ table.starts_on }} 起</StatChip>
              <StatChip tone="nebula" size="sm">
                01 号属 {{ table.animal_of_01_label ?? '—' }}
              </StatChip>
              <StatChip tone="neutral" size="sm">
                本表
                <AnimatedNumber
                  :value="table.numbers.length"
                  :stagger="0"
                  :min="40"
                  :max="49"
                />
                个号码
              </StatChip>
              <StatChip v-if="loading" tone="amber" size="sm" dot>正在切换…</StatChip>
            </div>
          </div>
        </GlassPanel>
      </MotionReveal>

      <!-- 首次加载：还没有可渲染的表 -->
      <MotionReveal v-if="loading && !table" :index="3">
        <GlassPanel padding="lg" rounded="3xl" class="flex items-center justify-center gap-3">
          <span
            class="h-4 w-4 shrink-0 animate-spin rounded-full border-2 border-aqua-300 border-t-transparent"
            aria-hidden="true"
          />
          <p class="text-sm text-slate-400">加载中…</p>
        </GlassPanel>
      </MotionReveal>

      <!-- 错误态：原样给出接口的 detail，并给一条重试路径 -->
      <MotionReveal v-else-if="errorMessage" :index="3">
        <GlassPanel
          variant="soft"
          padding="lg"
          rounded="3xl"
          class="border-rose-400/35 bg-rose-500/5"
        >
          <div class="flex flex-col gap-3">
            <div class="flex flex-wrap items-center gap-2.5">
              <StatChip tone="bloom" size="sm" dot>加载失败</StatChip>
              <p class="text-sm text-rose-200" role="alert">{{ errorMessage }}</p>
            </div>
            <GlassButton variant="glass" size="lg" @click="loadTable(selectedYear)">
              重新加载
            </GlassButton>
          </div>
        </GlassPanel>
      </MotionReveal>

      <!-- 空态：接口正常但没有可渲染的表 → 直说数据不足 -->
      <MotionReveal v-else-if="!table" :index="3">
        <GlassPanel
          variant="soft"
          padding="lg"
          rounded="3xl"
          class="flex flex-col items-center gap-2 text-center"
        >
          <StatChip tone="neutral" size="sm" dot>数据不足</StatChip>
          <p class="text-sm text-slate-400">
            没有拿到该农历年的生肖表，请换一个年份或稍后重试。
          </p>
        </GlassPanel>
      </MotionReveal>

      <template v-else>
        <!-- 12 生肖 → 号码（每个生肖一个专属字形 + 专属强调色） -->
        <section class="space-y-3">
          <MotionReveal :index="3">
            <h2 class="text-lg font-medium text-white">
              {{ table.lunar_year }} 年：12 生肖对应号码
            </h2>
            <p class="mt-0.5 text-xs text-slate-500">
              每个生肖有自己的字形与强调色；这套色板与下方映射图是同一套。
            </p>
          </MotionReveal>

          <p v-if="!table.zodiacs.length" class="text-sm text-slate-400">
            数据不足：没有拿到生肖分组。
          </p>
          <div v-else class="grid grid-cols-2 gap-3 sm:grid-cols-3">
            <MotionReveal
              v-for="(group, index) in table.zodiacs"
              :key="group.code"
              :index="index"
              :stagger="45"
              class="h-full"
            >
              <GlassCard as="div" padding="md" class="flex h-full flex-col gap-3" :interactive="false">
                <div class="flex items-start gap-3">
                  <ZodiacGlyph :code="group.code" size="md" />
                  <div class="min-w-0">
                    <p class="text-base font-semibold text-white">
                      {{ group.label ?? group.code }}
                    </p>
                    <p class="num text-[11px]" :style="{ color: identityOf(group.code).text }">
                      {{ group.numbers.length }} 个号码<template v-if="isFiveNumber(group.code)">
                        · 含 01 与 49</template>
                    </p>
                  </div>
                </div>
                <div class="mt-auto flex flex-wrap gap-1.5">
                  <span
                    v-for="number in group.numbers"
                    :key="number"
                    class="num inline-flex h-8 w-8 items-center justify-center rounded-lg border text-xs font-semibold"
                    :style="{ ...tintStyle(group.code, isFiveNumber(group.code)), color: identityOf(group.code).text }"
                  >
                    {{ pad(number) }}
                  </span>
                </div>
              </GlassCard>
            </MotionReveal>
          </div>
        </section>

        <!-- 结构：同肖链条 + 7 列映射图（让 period 12 与 5 个号码那一组肉眼可见） -->
        <MotionReveal :index="4">
          <GlassPanel padding="lg" rounded="3xl">
            <div class="flex flex-col gap-5">
              <div>
                <h2 class="text-lg font-medium text-white">49 号码映射图</h2>
                <p class="mt-0.5 text-xs leading-relaxed text-slate-400">
                  按 01 → 49 排成 7 列：同一个生肖的号码每隔
                  <span class="num text-slate-300">12</span>
                  个数出现一次，所以同色格子在网格里连成一条条向左下斜的条纹——这就是生肖表的结构。
                </p>
              </div>

              <!-- 同肖链条：今年拿到 5 个号码的那一组（49 = 12 × 4 + 1） -->
              <div v-if="fiveGroup" class="rounded-2xl border border-white/10 bg-white/5 px-4 py-4">
                <div class="flex items-start gap-3">
                  <ZodiacGlyph :code="fiveGroup.code" size="md" />
                  <div class="min-w-0">
                    <p class="text-sm font-medium text-white">
                      同肖链条：{{ fiveGroup.label ?? fiveGroup.code }}（{{ fiveGroup.numbers.length }} 个号码）
                    </p>
                    <p class="mt-0.5 text-xs leading-relaxed text-slate-400">
                      49 = 12 × 4 + 1，所以今年只有这一个生肖拿到 5 个号码（它同时含 01 与 49），
                      其余 11 个生肖各 4 个。
                    </p>
                  </div>
                </div>
                <div class="mt-4 flex flex-wrap items-center gap-1">
                  <template v-for="(number, index) in fiveGroup.numbers" :key="number">
                    <span v-if="index" class="px-0.5 text-[11px] text-slate-500" aria-hidden="true">→</span>
                    <SpecialBall :number="number" size="sm" :tone="ballTone(fiveGroup.code)" :glow="true" />
                  </template>
                </div>
              </div>

              <!-- 12 色图例：同色 = 同生肖，右侧数字是该生肖今年的号码个数（4 或 5） -->
              <div class="flex flex-wrap gap-x-3 gap-y-1.5">
                <span
                  v-for="group in table.zodiacs"
                  :key="`legend-${group.code}`"
                  class="inline-flex items-center gap-1.5 text-[11px] text-slate-300"
                >
                  <span class="h-2 w-2 rounded-full" :style="dotStyle(group.code)" aria-hidden="true" />
                  {{ group.label ?? group.code }}
                  <span class="num text-slate-500">{{ group.numbers.length }}</span>
                </span>
              </div>

              <!-- 49 格（7 × 7）：一格一号码，底色 = 当年生肖 -->
              <div class="grid grid-cols-7 gap-1.5 sm:gap-2">
                <div
                  v-for="item in table.numbers"
                  :key="item.number"
                  class="flex min-h-[46px] flex-col items-center justify-center gap-1 rounded-lg border px-0.5 py-1"
                  :style="tintStyle(item.zodiac, isFiveNumber(item.zodiac))"
                >
                  <span
                    class="num text-[13px] leading-none font-semibold"
                    :style="{ color: identityOf(item.zodiac).text }"
                  >
                    {{ pad(item.number) }}
                  </span>
                  <span
                    class="text-[10px] leading-none"
                    :style="{ color: zodiacTint(item.zodiac, 0.95) }"
                  >
                    {{ item.zodiac_label ?? '—' }}
                  </span>
                </div>
              </div>

              <p class="text-[11px] leading-relaxed text-slate-500">
                边框更亮的那一格就是今年拿到 5 个号码的生肖（<span class="num">{{ fiveGroupNumbersText }}</span>）。
                本页与开奖记录无关：不读取本池已导入的期数、不统计出现次数，也不做任何范围判断。
              </p>
            </div>
          </GlassPanel>
        </MotionReveal>
      </template>
    </div>
  </main>
</template>
