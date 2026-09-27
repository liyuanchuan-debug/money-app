<script setup lang="ts">
/**
 * AppNav —— 全局导航。
 *
 * 两套形态，同一份入口：
 *   - ≥ sm：顶部琉璃胶囊导航（沿用原设计，允许换行，不做横向滚动）；
 *   - < sm：移动优先的**底部 Tab 栏**（固定底部 + env(safe-area-inset-bottom) 安全区）。
 *
 * 可见性（与后端访问矩阵对齐，**不看 AUTH_ENFORCED**）：
 *   - 未登录：公开只读入口可见（首页 / 生肖表 / 历史）+ 醒目登录 CTA；
 *   - USER+：设置；
 *   - VIP+：波浪买入法、收益仪表；
 *   - ADMIN：开奖导入、开奖号录入、用户管理。
 *   特码/冷热/生肖走势等公开统计入口在首页卡片，不占主导航。
 *
 * 入口排序（「波浪买入法」是主推业务入口）：
 *   - 顶栏：首页 → **波浪买入法（重点样式）** → 公开只读 → 个人能力 / 管理；
 *   - 底栏：**波浪买入法（实心主 CTA，最左）** → 首页 → 模拟收益 → 更多。
 *   重点样式只改外观，不放开门控：未登录 / 非 VIP 时该入口**不渲染**，
 *   由「登录」CTA 与首页登录引导承担文案，不出现点了才知道没权限的诱饵入口。
 */

interface NavLink {
  to: string
  label: string
  /** 移动端「更多」面板里的右侧小字说明 */
  hint?: string
  /**
   * 入口要求的最低角色（英文枚举；层级 USER < VIP < ADMIN）。
   * 留空 = 访客也可看见（公开只读）。
   */
  role?: string
  /**
   * 重点入口（当前只有「波浪买入法」）：顶栏描边高亮、底栏实心主 CTA。
   * 只改**样式**，不改可见性 —— 门控仍然只看 `role`。
   */
  accent?: boolean
}

/**
 * 桌面 / 平板：全部入口平铺（再按登录态 / 角色过滤）。
 *
 * 顺序即「重要性顺序」：首页 → 波浪买入法（重点入口，紧随首页，不再吊在末尾）
 * → 公开只读 → 其余个人能力 / 管理入口。
 */
const desktopLinks: NavLink[] = [
  { to: '/', label: '首页' },
  { to: '/recommend', label: '波浪买入法', hint: '需 VIP', role: 'VIP', accent: true },
  { to: '/zodiac', label: '生肖表' },
  { to: '/history', label: '历史' },
  { to: '/stats/pnl', label: '模拟收益', hint: '模拟买入 / 模拟盈亏', role: 'VIP' },
  { to: '/settings', label: '设置', role: 'USER' },
  { to: '/draws', label: '开奖导入', hint: '仅管理员', role: 'ADMIN' },
  { to: '/entry', label: '开奖号录入', hint: '仅管理员', role: 'ADMIN' },
  { to: '/admin/users', label: '用户管理', hint: '仅管理员', role: 'ADMIN' },
]

/**
 * 移动端底部 Tab：主入口 + 「更多」；未登录时公开入口 + 登录 CTA。
 *
 * 「波浪买入法」占**首屏最左**（拇指最先够到、视觉第一落点）并做成实心主 CTA，
 * 不再混在末尾普通 Tab 里；未登录 / 非 VIP 时该 Tab 不渲染（走登录引导，不做诱饵入口）。
 */
const mobileTabs: NavLink[] = [
  { to: '/recommend', label: '波浪买入法', role: 'VIP', accent: true },
  { to: '/', label: '首页' },
  { to: '/stats/pnl', label: '模拟收益', role: 'VIP' },
]

/** 访客底栏保留的公开入口（其余公开链仍可从首页卡片进入） */
const GUEST_MOBILE_TABS = new Set(['/', '/history', '/zodiac'])

const route = useRoute()
const { motionEnabled, toggleMotion } = useMotion()
const {
  hasRole,
  loaded,
  isLoggedIn,
  maskedPhone,
  roleLabel,
  role,
  ensureLoaded,
  logout,
} = useAuth()
const moreOpen = ref(false)
const loggingOut = ref(false)

onMounted(() => {
  void ensureLoaded()
})

/** 入口可见：无 role = 公开；有 role = 必须已登录且角色达标 */
function linkVisible(link: NavLink): boolean {
  if (!link.role) return true
  return isLoggedIn.value && hasRole(link.role)
}

const visibleDesktopLinks = computed(() => desktopLinks.filter(linkVisible))
const visibleMobileTabs = computed(() => {
  if (!isLoggedIn.value) {
    return desktopLinks.filter(link => !link.role && GUEST_MOBILE_TABS.has(link.to))
  }
  return mobileTabs.filter(linkVisible)
})

/** 「更多」面板收纳的入口（访客也可看到公开但未进底栏的项） */
const visibleMoreLinks = computed(() => {
  return visibleDesktopLinks.value.filter(
    link => !visibleMobileTabs.value.some(tab => tab.to === link.to),
  )
})

function isActive(to: string) {
  if (to === '/') return route.path === '/'
  return route.path === to || route.path.startsWith(`${to}/`)
}

const moreActive = computed(() => visibleMoreLinks.value.some(link => isActive(link.to)))

const tabClass
  = 'flex min-w-0 flex-1 flex-col items-center justify-center gap-1 rounded-xl px-1 py-2 '
  + 'text-[11px] font-medium transition-colors duration-200 select-none active:bg-white/10'

/**
 * 重点入口 Tab：同一套骨架，字号提一档、占位稍宽（flex-[1.3]）——
 * 底栏里唯一「填充」的 Tab，不靠颜色深浅去猜。
 */
const tabAccentClass
  = 'flex min-w-0 flex-[1.3] flex-col items-center justify-center gap-1 rounded-xl px-1 py-2 '
  + 'text-[12px] transition-transform duration-200 select-none active:scale-[0.97]'

watch(
  () => route.path,
  () => {
    moreOpen.value = false
  },
)

/** 退出后回首页（访客可读），避免整站墙回登录页 */
async function onLogout() {
  if (loggingOut.value) return
  loggingOut.value = true
  try {
    await logout()
    moreOpen.value = false
    await navigateTo('/')
  } finally {
    loggingOut.value = false
  }
}
</script>

<template>
  <!-- 桌面 / 平板：顶部胶囊导航 -->
  <nav class="glass-panel hidden flex-wrap items-center gap-1 p-1.5 sm:flex" aria-label="主导航">
    <NuxtLink
      v-for="link in visibleDesktopLinks"
      :key="link.to"
      :to="link.to"
      class="nav-link"
      :class="[
        isActive(link.to) ? 'nav-link-active' : '',
        link.accent ? 'nav-link-accent' : '',
      ]"
      :aria-current="isActive(link.to) ? 'page' : undefined"
    >
      <span
        v-if="link.accent"
        class="h-1.5 w-1.5 rounded-full bg-aqua-300 shadow-glow-aqua"
        aria-hidden="true"
      />
      {{ link.label }}
    </NuxtLink>

    <template v-if="isLoggedIn">
      <span class="mx-1 hidden h-5 w-px bg-white/10 lg:block" aria-hidden="true" />

      <button
        type="button"
        class="nav-link gap-1.5"
        :aria-pressed="motionEnabled"
        :title="motionEnabled ? '关闭全部动效' : '开启全部动效'"
        @click="toggleMotion"
      >
        <span
          class="h-1.5 w-1.5 rounded-full transition-colors"
          :class="motionEnabled ? 'bg-aqua-300 shadow-glow-aqua' : 'bg-slate-500'"
          aria-hidden="true"
        />
        特效{{ motionEnabled ? '开' : '关' }}
      </button>
    </template>

    <span class="mx-1 hidden h-5 w-px bg-white/10 lg:block" aria-hidden="true" />

    <span
      v-if="!loaded"
      class="ml-1 h-4 w-20 animate-pulse rounded-full bg-white/10"
      aria-hidden="true"
    />

    <template v-else-if="isLoggedIn">
      <span class="flex items-center gap-2 px-2 py-1.5">
        <span class="num text-xs text-slate-300">{{ maskedPhone }}</span>
        <StatChip size="xs" :tone="role === 'ADMIN' ? 'nebula' : role === 'VIP' ? 'aqua' : 'neutral'">
          {{ roleLabel }}
        </StatChip>
      </span>
      <button
        type="button"
        class="nav-link"
        :disabled="loggingOut"
        @click="onLogout"
      >
        {{ loggingOut ? '退出中…' : '退出' }}
      </button>
    </template>

    <NuxtLink
      v-else
      to="/login"
      class="nav-link nav-link-cta"
      :class="isActive('/login') ? 'nav-link-active' : ''"
    >
      登录
    </NuxtLink>
  </nav>

  <ClientOnly>
    <!-- 移动端：底部 Tab 栏 -->
    <Teleport to="body">
      <nav class="fixed inset-x-0 bottom-0 z-50 sm:hidden" aria-label="主导航（移动端）">
        <!--
          安全区垫在琉璃底栏内部（不是透明外层）：Home 条区域仍是玻璃底，
          Tab 文案抬高；与 layouts/default 的 pb calc 各计一次，不双倍。
          pb-1.5 = 0.375rem，再加 env(safe-area-inset-bottom)。
        -->
        <div
          class="glass-panel-strong flex items-stretch gap-1 border-t border-white/15 px-2 pt-1.5 pb-[calc(0.375rem+env(safe-area-inset-bottom,0px))]"
        >
          <NuxtLink
            v-for="tab in visibleMobileTabs"
            :key="tab.to"
            :to="tab.to"
            :class="[
              tab.accent ? tabAccentClass : tabClass,
              tab.accent
                ? 'nav-tab-accent'
                : (isActive(tab.to) ? 'bg-white/10 text-aqua-200' : 'text-slate-400'),
            ]"
            :aria-current="isActive(tab.to) ? 'page' : undefined"
          >
            <span
              class="h-1.5 w-1.5 rounded-full"
              :class="tab.accent
                ? 'bg-ink-900/70'
                : (isActive(tab.to) ? 'bg-aqua-300 shadow-glow-aqua' : 'bg-slate-600')"
              aria-hidden="true"
            />
            <span class="max-w-full truncate">{{ tab.label }}</span>
          </NuxtLink>

          <!-- 未登录：底栏右侧主色登录 CTA（比普通 Tab 更醒目） -->
          <NuxtLink
            v-if="loaded && !isLoggedIn"
            to="/login"
            class="nav-link-cta flex min-w-0 flex-[1.35] flex-col items-center justify-center gap-1 !rounded-xl !px-2 !py-2 !text-[12px] select-none active:opacity-90"
            :aria-current="isActive('/login') ? 'page' : undefined"
          >
            <span class="max-w-full truncate">登录</span>
          </NuxtLink>

          <button
            v-if="isLoggedIn || visibleMoreLinks.length"
            type="button"
            :class="[
              tabClass,
              moreOpen || moreActive ? 'bg-white/10 text-aqua-200' : 'text-slate-400',
            ]"
            :aria-expanded="moreOpen"
            aria-haspopup="dialog"
            @click="moreOpen = !moreOpen"
          >
            <span
              class="h-1.5 w-1.5 rounded-full"
              :class="moreOpen || moreActive ? 'bg-aqua-300 shadow-glow-aqua' : 'bg-slate-600'"
              aria-hidden="true"
            />
            <span class="max-w-full truncate">更多</span>
          </button>
        </div>
      </nav>
    </Teleport>

    <!-- 移动端：「更多」面板（访客可见公开入口；已登录含账号区） -->
    <Teleport to="body">
      <div
        v-if="moreOpen && (isLoggedIn || visibleMoreLinks.length)"
        class="fixed inset-0 z-[60] sm:hidden"
        role="dialog"
        aria-modal="true"
        aria-label="更多功能"
      >
        <div class="absolute inset-0 bg-ink-950/70" @click="moreOpen = false" />

        <div class="absolute inset-x-0 bottom-0">
          <div
            class="glass-panel-strong rounded-t-3xl border-t border-white/15 px-4 pt-4 pb-[calc(1rem+env(safe-area-inset-bottom,0px))]"
          >
            <div class="flex items-center justify-between gap-3">
              <h2 class="text-sm font-medium text-white">更多功能</h2>
              <button
                type="button"
                class="glass-control rounded-lg px-3 py-1.5 text-xs text-slate-300"
                @click="moreOpen = false"
              >
                关闭
              </button>
            </div>

            <div v-if="isLoggedIn" class="mt-3">
              <span
                v-if="!loaded"
                class="block h-14 animate-pulse rounded-xl bg-white/10"
                aria-hidden="true"
              />

              <div
                v-else
                class="flex min-h-[52px] items-center justify-between gap-3 rounded-xl border border-white/10 bg-white/5 px-4 py-3"
              >
                <div class="min-w-0 space-y-1">
                  <p class="num truncate text-sm font-medium text-white">{{ maskedPhone }}</p>
                  <StatChip
                    size="xs"
                    :tone="role === 'ADMIN' ? 'nebula' : role === 'VIP' ? 'aqua' : 'neutral'"
                  >
                    {{ roleLabel }}
                  </StatChip>
                </div>
                <GlassButton
                  size="sm"
                  variant="glass"
                  class="min-h-[44px] shrink-0"
                  :loading="loggingOut"
                  :disabled="loggingOut"
                  @click="onLogout"
                >
                  退出
                </GlassButton>
              </div>
            </div>

            <div
              v-else-if="loaded"
              class="mt-3 rounded-xl border border-aqua-400/25 bg-aqua-400/10 px-4 py-3"
            >
              <p class="text-sm text-aqua-100">登录后可使用波浪买入法、模拟收益仪表与个人设置。</p>
              <NuxtLink
                to="/login"
                class="mt-2 inline-flex min-h-[44px] items-center text-sm font-medium text-aqua-200"
              >
                去登录 →
              </NuxtLink>
            </div>

            <hr class="glass-hairline my-3">

            <ul class="space-y-1.5">
              <li v-for="link in visibleMoreLinks" :key="link.to">
                <NuxtLink
                  :to="link.to"
                  class="flex min-h-[52px] items-center justify-between gap-3 rounded-xl border px-4 py-3 transition-colors active:bg-white/15"
                  :class="isActive(link.to)
                    ? 'border-aqua-400/40 bg-aqua-400/10 text-aqua-200'
                    : 'border-white/10 bg-white/5 text-slate-200'"
                  :aria-current="isActive(link.to) ? 'page' : undefined"
                >
                  <span class="text-sm font-medium">{{ link.label }}</span>
                  <span class="text-[11px] text-slate-500">{{ link.hint }}</span>
                </NuxtLink>
              </li>
            </ul>

            <button
              v-if="isLoggedIn"
              type="button"
              class="mt-3 flex min-h-[52px] w-full items-center justify-between rounded-xl border border-white/10 bg-white/5 px-4 py-3 text-left transition-colors active:bg-white/15"
              :aria-pressed="motionEnabled"
              @click="toggleMotion"
            >
              <span class="text-sm font-medium text-slate-200">全部动效</span>
              <span
                class="text-[11px]"
                :class="motionEnabled ? 'text-aqua-200' : 'text-slate-500'"
              >
                {{ motionEnabled ? '已开启' : '已关闭' }}
              </span>
            </button>
          </div>
        </div>
      </div>
    </Teleport>
  </ClientOnly>
</template>
