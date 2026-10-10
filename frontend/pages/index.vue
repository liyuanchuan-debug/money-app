<script setup lang="ts">
import type { DrawItem } from '~/composables/useApi'
import { normalizeDraw, normalizeDraws, periodText, scopeLabel, zodiacText } from '~/composables/useDraws'

/**
 * 首页：
 *   - 访客与已登录均可看本池只读总览（最新特码 + 走势 + 公开功能入口）；
 *   - 个人能力（波浪买入法 / 收益 / 设置）按角色过滤；未登录时 CTA 引导登录 / VIP。
 */
const api = useApi()
const { ensureLoaded, isLoggedIn, hasRole, loaded } = useAuth()

await ensureLoaded()

/** 一次取回本池最近 N 期：既用于统计口径，也够画走势（后端 limit 上限 500） */
const SAMPLE_LIMIT = 200
/** 均值 / 差值窗口：样本速览里写明了「近 N 期」，与走势的滑动窗口无关 */
const STATS_WINDOW = 30
/** 走势默认一屏显示多少期；可以左右滑动看更早的期数 */
const TREND_WINDOW_DEFAULT = 30
/** 走势「一屏多少期」档位（0 = 全部），直接透传给 LineChart 的 window-options */
const TREND_WINDOW_OPTIONS = [30, 60, 100, 0]

/**
 * 公开只读：访客也拉开奖样本（对应后端匿名开放的 GET /api/draws*）。
 */
const [
  { data: latestRaw, pending: latestPending },
  { data: rawDraws, pending: drawsPending },
] = await Promise.all([
  useAsyncData<DrawItem | null>('draws-latest', async () => {
    try {
      return await api.latestDraw()
    } catch {
      const fallback = await api.listDraws(1, 0)
      return fallback?.[0] ?? null
    }
  }),
  useAsyncData<DrawItem[]>('draws-sample', async () => {
    return api.listDraws(SAMPLE_LIMIT, 0)
  }),
])

/** 本池样本（新 → 旧） */
const sample = computed(() => normalizeDraws(rawDraws.value))
const sampleSize = computed(() => sample.value.length)
/** 取满了上限 → 本池可能还有更早的期数没读进来，必须在页面上说明 */
const truncated = computed(() => sampleSize.value >= SAMPLE_LIMIT)
/** 统计口径文案：本页每个数字旁边都要出现 */
const scope = computed(() => scopeLabel(sampleSize.value))

const latest = computed(
  () => normalizeDraw(latestRaw.value) ?? normalizeDraw(rawDraws.value?.[0]),
)

/** 走势：倒序取回的新→旧先翻成旧→新再画；滑动模式下一屏只显示最近 trendWindow 期 */
const trend = computed(() => {
  const ordered = sample.value.slice().reverse()
  return {
    values: ordered.map(item => item.special_number),
    labels: ordered.map(item => (item.period !== null ? `第${item.period}期` : item.draw_date)),
    count: ordered.length,
  }
})

const trendWindow = ref(TREND_WINDOW_DEFAULT)

const trendSeries = computed(() =>
  trend.value.count >= 2 ? [{ name: '特码', values: trend.value.values as Array<number | null> }] : [],
)

const stats = computed(() => {
  const window = sample.value.slice(0, STATS_WINDOW)
  if (!window.length) return { count: 0, mean: null as number | null, diff: null as number | null }
  const values = window.map(item => item.special_number)
  return {
    count: values.length,
    mean: values.reduce((total, value) => total + value, 0) / values.length,
    diff: sample.value.length >= 2
      ? Math.abs(sample.value[0].special_number - sample.value[1].special_number)
      : null,
  }
})

const loading = computed(() => latestPending.value || drawsPending.value)

/* ---------------- 功能入口（按角色过滤；与 AppNav / 后端矩阵对齐） ---------------- */
interface NavCard {
  to: string
  title: string
  desc: string
  edge: 'mixed' | 'aqua' | 'nebula' | 'bloom'
  /**
   * 最低角色；与后端 Depends 一致。
   * 省略 = 访客可读公开数据。
   */
  role?: 'USER' | 'VIP' | 'ADMIN'
}

const navCards: NavCard[] = [
  { to: '/zodiac', title: '生肖表', desc: '农历年口径的 49 号码归表', edge: 'mixed' },
  { to: '/history', title: '历史记录', desc: '本池已导入的全部开奖记录', edge: 'mixed' },
  { to: '/stats/trend', title: '特码走势', desc: '本池样本内特码走势与波动分布', edge: 'aqua' },
  { to: '/stats/frequency', title: '冷热统计', desc: '样本内出现次数与偏差（不做热号结论）', edge: 'mixed' },
  { to: '/stats/zodiac-trend', title: '生肖走势', desc: '生肖连出、最近窗口覆盖与轮转', edge: 'aqua' },
  { to: '/stats/pnl', title: '模拟收益仪表', desc: '模拟买入试算：成本、兑付与累计模拟盈亏', edge: 'aqua', role: 'VIP' },
  { to: '/stats/backtest', title: '策略回测', desc: '走步回测：每期只用该期之前的数据', edge: 'bloom', role: 'VIP' },
  { to: '/settings', title: '设置', desc: '波动阈值、赔率与下注默认值', edge: 'mixed', role: 'USER' },
  { to: '/draws', title: '开奖导入', desc: '整期开奖总表批量粘贴导入', edge: 'nebula', role: 'ADMIN' },
  { to: '/entry', title: '开奖号录入', desc: '单号录入与纠正（仅管理员）', edge: 'bloom', role: 'ADMIN' },
  { to: '/admin/users', title: '用户管理', desc: '审批账号、调整角色与状态', edge: 'nebula', role: 'ADMIN' },
]

/**
 * 「波浪买入法」不放在这张网格里：它是主推业务入口，单独做成首页顶部的
 * 大卡（见模板 VIP 主入口），比只读数据类卡片更靠前也更显眼。
 */

function cardVisible(card: NavCard): boolean {
  if (!card.role) return true
  return isLoggedIn.value && hasRole(card.role)
}

const visibleNavCards = computed(() => navCards.filter(cardVisible))

/** VIP 及以上（ADMIN 也满足）：首页主入口与顶栏 / 底栏重点入口同口径 */
const canUseRecommend = computed(() => hasRole('VIP'))
</script>

<template>
  <main class="relative px-5 py-10 text-slate-100 sm:py-14">
    <div class="mx-auto flex max-w-3xl flex-col gap-7">
      <!-- 头部 -->
      <MotionReveal :index="0" class="flex flex-wrap items-start justify-between gap-4">
        <div class="space-y-2.5">
          <div>
            <BrandMark size="sm" />
          </div>
          <h1 class="text-gradient text-3xl font-semibold tracking-tight sm:text-4xl">
            {{ isLoggedIn ? '开奖数据总览' : '四叶沙盘' }}
          </h1>
          <p class="max-w-xl text-sm text-slate-400">
            <template v-if="isLoggedIn">
              只读仪表盘：展示本池已导入的最新特码与近段走势；波浪买入法与模拟收益仪表需 VIP。
            </template>
            <template v-else>
              开奖历史、生肖表与走势可直接浏览。波浪买入法、模拟收益仪表等个人能力需登录且为 VIP。
            </template>
          </p>
        </div>
        <AppNav />
      </MotionReveal>

      <!-- 未登录：醒目登录 CTA（不挡公开数据） -->
      <MotionReveal v-if="loaded && !isLoggedIn" :index="1">
        <GlassPanel variant="strong" padding="md" rounded="3xl" glow tone="aqua">
          <div class="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
            <div class="space-y-1.5">
              <StatChip tone="aqua" size="sm" dot>登录解锁个人能力</StatChip>
              <p class="text-sm font-medium text-white">
                波浪买入法、模拟收益仪表与个人设置需登录（VIP / 管理员）
              </p>
              <p class="text-xs leading-relaxed text-slate-400">
                下方开奖总览与公开统计无需登录即可查看。新账号需管理员审批后才能登录。
              </p>
            </div>
            <div class="flex w-full shrink-0 flex-col gap-2 sm:w-auto sm:flex-row">
              <NuxtLink
                to="/login"
                class="glass-control glass-control-primary glass-edge inline-flex min-h-[48px] items-center justify-center rounded-xl px-5 py-3 text-sm font-semibold tracking-wide"
              >
                立即登录
              </NuxtLink>
              <NuxtLink
                to="/register"
                class="glass-control glass-edge inline-flex min-h-[48px] items-center justify-center rounded-xl px-5 py-3 text-sm font-medium text-slate-100"
              >
                申请注册
              </NuxtLink>
            </div>
          </div>
        </GlassPanel>
      </MotionReveal>

      <!--
        VIP：波浪买入法主入口。
        刻意排在**所有只读数据卡之前**（最新特码 / 样本速览 / 走势都靠后），
        并用实心主 CTA 做视觉重心 —— 让「打开首页第一眼」就能点到；
        未登录 / 非 VIP 不渲染（门控与路由 meta.role='VIP' 一致，不做诱饵入口）。
      -->
      <MotionReveal v-if="loaded && canUseRecommend" :index="1">
        <GlassPanel variant="strong" padding="md" rounded="3xl" glow tone="aqua">
          <div class="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
            <div class="space-y-2">
              <StatChip tone="aqua" size="sm" dot>VIP 主入口</StatChip>
              <h2 class="text-gradient text-2xl font-semibold tracking-tight">
                波浪买入法
              </h2>
              <p class="max-w-xl text-sm leading-relaxed text-slate-300">
                按本池波动模式生成财富密码：走势加权、筹码分配与采用快照一次看完。
              </p>
            </div>
            <NuxtLink
              to="/recommend"
              class="glass-control glass-control-primary glass-edge inline-flex min-h-[48px] shrink-0 items-center justify-center rounded-xl px-5 py-3 text-sm font-semibold tracking-wide"
            >
              进入波浪买入法 <span aria-hidden="true" class="ml-1">→</span>
            </NuxtLink>
          </div>
        </GlassPanel>
      </MotionReveal>

      <!-- 公开只读总览（访客与登录用户共用） -->
      <!-- Hero：最新特码 -->
      <MotionReveal :index="2">
        <GlassPanel variant="strong" padding="lg" rounded="3xl" glow tone="aqua">
          <p v-if="loading" class="text-center text-sm text-slate-400">加载中…</p>

          <div v-else-if="!latest" class="flex flex-col items-center gap-3 py-4 text-center">
            <StatChip tone="neutral" size="sm" dot>{{ scope }}</StatChip>
            <p class="text-base font-medium text-slate-300">数据不足</p>
            <p class="max-w-md text-xs text-slate-500">
              本池还没有开奖记录。
              <template v-if="isLoggedIn && hasRole('ADMIN')">
                请从下方「开奖导入」批量粘贴开奖总表，或用「开奖号录入」补单号。
              </template>
              <template v-else>
                请等待管理员导入开奖数据后再查看。
              </template>
            </p>
          </div>

          <div
            v-else
            class="flex flex-col items-center gap-5 text-center sm:flex-row sm:items-center sm:justify-between sm:text-left"
          >
            <div class="space-y-3">
              <StatChip tone="aqua" size="sm" dot>最新特码</StatChip>
              <p class="num text-lg font-medium text-white">
                {{ periodText(latest) }}
                <span v-if="latest.draw_date" class="ml-2 text-sm font-normal text-slate-400">
                  {{ latest.draw_date }}
                </span>
              </p>
              <div class="flex flex-wrap items-center justify-center gap-2 sm:justify-start">
                <StatChip tone="nebula" size="md">
                  生肖 {{ zodiacText(latest) || '—' }}
                </StatChip>
                <StatChip tone="neutral" size="sm">{{ scope }}</StatChip>
              </div>
            </div>

            <div class="shrink-0">
              <SpecialBall
                :number="latest.special_number"
                size="xl"
                tone="aqua"
                label="最新一期"
                :glow="true"
              />
            </div>
          </div>
        </GlassPanel>
      </MotionReveal>

      <!-- 样本速览 -->
      <MotionReveal :index="3">
        <GlassPanel padding="md" rounded="3xl">
          <div class="grid grid-cols-3 gap-3 text-center sm:text-left">
            <div class="space-y-1">
              <p class="text-[11px] text-slate-500">样本期数</p>
              <p class="num text-xl font-semibold text-white">
                <AnimatedNumber :value="sampleSize" :pad="0" :stagger="0" />
              </p>
            </div>
            <div class="space-y-1">
              <p class="text-[11px] text-slate-500">近 {{ STATS_WINDOW }} 期均值</p>
              <p class="num text-xl font-semibold text-white">
                {{ stats.mean === null ? '—' : stats.mean.toFixed(1) }}
              </p>
            </div>
            <div class="space-y-1">
              <p class="text-[11px] text-slate-500">最新差值（与上期）</p>
              <p class="num text-xl font-semibold text-white">
                {{ stats.diff === null ? '—' : stats.diff }}
              </p>
            </div>
          </div>
          <p class="mt-3 text-[11px] text-slate-500">
            口径：{{ scope }}<template v-if="truncated">（本次只读取最近 {{ SAMPLE_LIMIT }} 期）</template>；均值与差值只描述本池样本，样本不足时不做分布结论。
          </p>
        </GlassPanel>
      </MotionReveal>

      <!-- 近期走势 -->
      <MotionReveal :index="4">
        <GlassPanel padding="lg" rounded="3xl">
          <div class="mb-4 flex flex-wrap items-end justify-between gap-2">
            <div>
              <h2 class="text-lg font-medium text-white">最近特码走势</h2>
              <p class="text-xs text-slate-500">
                口径：{{ scope }}；旧 → 新排列，共画 {{ trend.count }} 期；
                左右滑动移动号码光标（默认一屏 {{ TREND_WINDOW_DEFAULT }} 期）。
              </p>
            </div>
            <span class="num text-xs text-slate-500">{{ trend.count }} 期</span>
          </div>
          <LineChart
            :series="trendSeries"
            :labels="trend.labels"
            :height="230"
            :y-min="1"
            :y-max="49"
            scrollable
            v-model:window-size="trendWindow"
            :window-options="TREND_WINDOW_OPTIONS"
            empty-text="数据不足"
            empty-hint="本池至少要 2 期开奖记录才能连成走势"
          />
        </GlassPanel>
      </MotionReveal>

      <!-- 功能入口 -->
      <section class="space-y-3">
        <div>
          <h2 class="text-lg font-medium text-white">功能入口</h2>
          <p class="text-xs text-slate-500">
            公开数据对访客可见；波浪买入法 / 模拟收益仪表需 VIP；录入与用户管理仅管理员。
          </p>
        </div>
        <div class="grid grid-cols-2 gap-3 sm:grid-cols-3">
          <MotionReveal
            v-for="(card, index) in visibleNavCards"
            :key="card.to"
            :index="index"
            :stagger="55"
          >
            <NuxtLink :to="card.to" class="block h-full rounded-2xl active:opacity-80">
              <GlassCard padding="md" class="flex h-full flex-col gap-2" :edge="card.edge">
                <p class="text-sm font-medium text-white">{{ card.title }}</p>
                <p class="text-xs leading-relaxed text-slate-400">{{ card.desc }}</p>
                <StatChip
                  v-if="card.role === 'ADMIN'"
                  tone="bloom"
                  size="xs"
                  class="mt-auto"
                >
                  仅管理员
                </StatChip>
                <StatChip
                  v-else-if="card.role === 'VIP'"
                  tone="aqua"
                  size="xs"
                  class="mt-auto"
                >
                  VIP
                </StatChip>
              </GlassCard>
            </NuxtLink>
          </MotionReveal>
        </div>
      </section>
    </div>
  </main>
</template>
