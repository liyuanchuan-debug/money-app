<script setup lang="ts">
import type { DrawItem } from '~/composables/useApi'
import { normalizeDraws, periodText, scopeLabel, zodiacText } from '~/composables/useDraws'

/**
 * 历史记录 —— 本池已导入的开奖记录，只读（访客可读）。

 *
 * 原先依赖已废弃的「快捷录入记录 / 历史」两个接口，现改为 GET /api/draws。
 * 「删除 / 清空」不在这里做：写入与批量管理统一收在「开奖导入」页（/draws），
 * 单号补录在「开奖号录入」页（/entry）。本页没有任何写操作。
 *
 * 分页：一次把池子取满（limit 上限 500），再在浏览器里切片。
 * 后端 GET /api/draws 只返回裸数组、没有 total 字段，客户端切片才能给出精确的
 * 「共 N 期 / 第 X / Y 页」，翻页也不必再打网络。本页只读，列表在会话内不会变。
 */
const api = useApi()

/** 一次最多列出的期数（后端 limit 上限 500；够覆盖当前池子） */
const HISTORY_LIMIT = 500

/** 每页条数：手机上 20 行 ≈ 两屏，再长就不叫分页了 */
const PAGE_SIZE = 20

const { data: rawDraws, pending, error, refresh } = await useAsyncData<DrawItem[]>(
  'history-draws',
  () => api.listDraws(HISTORY_LIMIT, 0),
)

const draws = computed(() => normalizeDraws(rawDraws.value))
const total = computed(() => draws.value.length)
/** 统计口径：本池已导入 N 期数据内 */
const scope = computed(() => scopeLabel(total.value))
/** 取满了上限说明可能还有更早的记录没列出来 */
const truncated = computed(() => total.value >= HISTORY_LIMIT)

/** 当前页码（1 起）；越界由 GlassPager 负责夹紧 */
const page = ref(1)
/** 只渲染当前一页的行：DOM 里永远只有一页，不再一次铺 200 行 */
const pagedDraws = computed(() =>
  draws.value.slice((page.value - 1) * PAGE_SIZE, page.value * PAGE_SIZE),
)

/** 重试 / 刷新后回到第 1 页（GlassPager 也会夹紧越界页码，这是双保险） */
async function reload() {
  page.value = 1
  await refresh()
}
</script>

<template>
  <main class="relative px-5 py-10 text-slate-100 sm:py-14">
    <div class="mx-auto flex max-w-4xl flex-col gap-7">
      <header class="flex flex-wrap items-start justify-between gap-4">
        <div class="space-y-2.5">
          <div>
            <BrandMark size="sm" />
          </div>
          <h1 class="text-gradient text-3xl font-semibold tracking-tight sm:text-4xl">
            历史记录
          </h1>
          <p class="max-w-xl text-sm text-slate-400">
            口径：{{ scope }}（按日期倒序，最新在前）。本页只读；纠正录错特码请到
            <NuxtLink to="/entry" class="text-aqua-300 underline-offset-2 hover:underline">开奖号录入 · 纠正开奖</NuxtLink>
            或
            <NuxtLink to="/draws" class="text-aqua-300 underline-offset-2 hover:underline">开奖导入</NuxtLink>
            列表里的「纠正」。
          </p>
          <StatChip tone="neutral" size="sm" dot>只读页 · 录入 / 导入请到对应功能页</StatChip>
        </div>
        <AppNav />
      </header>

      <section class="space-y-3">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <h2 class="text-lg font-medium text-white">全部开奖记录</h2>
          <span class="num text-xs text-slate-500">共 {{ total }} 期</span>
        </div>

        <p v-if="pending" class="text-sm text-slate-400">加载中…</p>

        <p
          v-else-if="error"
          class="rounded-xl border border-bloom-400/40 bg-bloom-400/10 px-4 py-3 text-sm text-bloom-200"
        >
          {{ (error as any)?.data?.detail || error.message || '加载失败' }}
          <button type="button" class="ml-2 underline underline-offset-2" @click="reload()">
            重试
          </button>
        </p>

        <!-- 空态：数据不足就说数据不足 -->
        <GlassPanel v-else-if="!total" variant="soft" padding="lg">
          <p class="text-center text-sm text-slate-500">
            数据不足：本池已导入 0 期数据内，还没有可列出的开奖记录。
          </p>
        </GlassPanel>

        <GlassPanel v-else variant="soft" padding="none" rounded="2xl" class="overflow-hidden">
          <div id="history-table" class="overflow-x-auto">
            <table class="w-full min-w-[460px] text-left text-sm">
              <thead class="glass-table-head text-xs text-slate-400">
                <tr class="border-b border-white/10">
                  <th class="px-4 py-3 font-medium">期号</th>
                  <th class="px-4 py-3 font-medium">日期</th>
                  <th class="px-4 py-3 font-medium">特码</th>
                  <th class="px-4 py-3 font-medium">生肖</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="draw in pagedDraws"
                  :key="draw.id ?? `${draw.draw_date}-${draw.period ?? draw.special_number}`"
                  class="border-b border-white/5 last:border-b-0"
                >
                  <td class="num px-4 py-3 text-slate-300">{{ periodText(draw) }}</td>
                  <td class="num px-4 py-3 text-slate-400">{{ draw.draw_date || '—' }}</td>
                  <td class="px-4 py-3">
                    <SpecialBall :number="draw.special_number" size="sm" :glow="false" />
                  </td>
                  <td class="px-4 py-3">
                    <StatChip tone="neutral" size="sm">{{ zodiacText(draw) || '—' }}</StatChip>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <!-- 分页：手机优先，每页 20 行；总数 / 当前区间 / 悬浮页码都在这 -->
          <GlassPager
            v-model:page="page"
            :page-size="PAGE_SIZE"
            :total="total"
            label="全部开奖记录分页"
            scroll-target="#history-table"
            class="border-t border-white/10 px-4 py-3"
          />
        </GlassPanel>

        <p v-if="truncated" class="text-xs text-slate-500">
          只列出了最近的 {{ HISTORY_LIMIT }} 期，更早的记录未显示。
        </p>
      </section>
    </div>
  </main>
</template>
