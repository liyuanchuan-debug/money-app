<script setup lang="ts">
/**
 * 用户管理（审批 / 角色 / 状态）—— 仅 ADMIN。
 *
 * 设计重点是**待审批队列**：这是 owner 每天真正要干的活，
 * 所以「待审批」是默认视图而不是埋在筛选后面的一个选项，队列里直接给主行动。
 *
 * 三件必须如实说明的事：
 *   1. 路由与菜单已按 ADMIN 收口；后端 `AUTH_ENFORCED=true` 时接口也会拦。
 *      若本地显式关掉 API 旁路，页面仍会提示「仅管理员」；
 *   2. 后端有「防管理员自我锁死」护栏，会返回 409 —— 原样呈现给用户，
 *      不吞掉、也不改写成「操作失败」；
 *   3. 判定一律读英文枚举（USER / VIP / ADMIN、PENDING / …），
 *      `role_label` / `status_label` 只用来显示。
 */

import type { AuthErrorInfo } from '~/composables/useAuth'
import type { AuthUser } from '~/composables/useApi'
import { authErrorInfo, roleLabelOf, statusLabelOf } from '~/composables/useAuth'
import { toLocalDate } from '~/composables/useDraws'

definePageMeta({ role: 'ADMIN' })

type FilterValue = 'ALL' | string

const { api, ensureLoaded, authEnforced, hasRole, isLoggedIn, roles: authRoles, statuses: authStatuses } = useAuth()

useHead({ title: '用户管理' })

/** 每页条数：手机上 10 张卡片差不多两屏，再长就不叫分页了 */
const PAGE_SIZE = 10

const statusFilter = ref<FilterValue>('PENDING')
const users = ref<AuthUser[]>([])
const pendingCount = ref(0)
const loading = ref(true)
const page = ref(1)

/** 列表加载失败（含 403「不是管理员」） */
const loadError = ref<AuthErrorInfo | null>(null)
/** 单个动作的失败（409 锁死护栏 / 422 非法枚举 / 403） */
const actionError = ref('')
const actionInfo = ref('')
const busyKey = ref('')
/** 展开「角色与审批状态」的用户 */
const expanded = ref('')

/**
 * 待确认的动作。放在 `shallowRef` 里：对象里带一个函数（run），
 * 不需要深层响应式，也不希望 Vue 去代理这个函数。
 */
interface PendingAction {
  /** `${phone}:${action}`，同时用作按钮的 busy key */
  key: string
  phone: string
  prompt: string
  confirmLabel: string
  done: string
  dangerous: boolean
  run: () => Promise<unknown>
}
const pending = shallowRef<PendingAction | null>(null)

/** 非管理员直接不给看（路由中间件也会拦；此处防灰态闪一下） */
const blocked = computed(() => !hasRole('ADMIN'))
const confirmingPhone = computed(() => pending.value?.phone ?? '')

const pagedUsers = computed(() =>
  users.value.slice((page.value - 1) * PAGE_SIZE, page.value * PAGE_SIZE),
)

/* ---------------- 展示映射（只影响显示，不参与判定） ---------------- */

type ChipTone = 'aqua' | 'nebula' | 'bloom' | 'emerald' | 'amber' | 'neutral'

const ROLE_TONE: Record<string, ChipTone> = { ADMIN: 'nebula', VIP: 'aqua', USER: 'neutral' }
const STATUS_TONE: Record<string, ChipTone> = {
  PENDING: 'amber',
  APPROVED: 'emerald',
  REJECTED: 'bloom',
  DISABLED: 'neutral',
}

function roleTone(value: string): ChipTone {
  return ROLE_TONE[value] ?? 'neutral'
}

function statusTone(value: string): ChipTone {
  return STATUS_TONE[value] ?? 'neutral'
}

/** ISO 时间 → 本地 `YYYY-MM-DD HH:mm`；拿不到就显示占位符 */
function dateTimeText(value: string | null | undefined): string {
  if (!value) return '—'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return String(value).replace('T', ' ').slice(0, 16)
  const pad = (input: number) => String(input).padStart(2, '0')
  return `${toLocalDate(parsed)} ${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`
}

/** 角色 / 状态取值表来自 GET /api/auth/config，拿不到时退回后端同源的四个枚举 */
const roleOptions = computed(() => {
  const list = authRoles.value.length ? authRoles.value : ['USER', 'VIP', 'ADMIN']
  return list.map(value => ({ value, label: roleLabelOf(value) }))
})

const statusOptions = computed(() => {
  const list = authStatuses.value.length
    ? authStatuses.value
    : ['PENDING', 'APPROVED', 'REJECTED', 'DISABLED']
  // 待审批永远排第一：它是这个页面的主任务
  const ordered = ['PENDING', ...list.filter(value => value !== 'PENDING')]
  return ordered.map(value => ({ value, label: statusLabelOf(value) }))
})

const filterOptions = computed<Array<{ value: FilterValue, label: string }>>(() => [
  ...statusOptions.value.map(option => ({ value: option.value as FilterValue, label: option.label })),
  { value: 'ALL', label: '全部' },
])

/* ---------------- 数据 ---------------- */

/**
 * 拉列表。
 * 待审批视图只打一个请求；其它视图并发多打一个「待审批」用于顶部的队列提示
 * （后端没有 count 接口，队列数量只能这样拿）。
 */
async function load() {
  if (blocked.value) {
    users.value = []
    pendingCount.value = 0
    loading.value = false
    return
  }

  loading.value = true
  loadError.value = null
  try {
    if (statusFilter.value === 'PENDING') {
      users.value = await api.adminListUsers('PENDING')
      pendingCount.value = users.value.length
    } else {
      const [list, pendingList] = await Promise.all([
        api.adminListUsers(statusFilter.value === 'ALL' ? undefined : statusFilter.value),
        api.adminListUsers('PENDING'),
      ])
      users.value = list
      pendingCount.value = pendingList.length
    }
  } catch (error) {
    loadError.value = authErrorInfo(error, 'admin')
    users.value = []
  } finally {
    loading.value = false
  }
}

function setFilter(value: FilterValue) {
  if (statusFilter.value === value) return
  statusFilter.value = value
  page.value = 1
  expanded.value = ''
  pending.value = null
  actionError.value = ''
  actionInfo.value = ''
  void load()
}

function toggleExpanded(phone: string) {
  expanded.value = expanded.value === phone ? '' : phone
}

/** 取消行内二次确认 */
function cancelConfirm() {
  pending.value = null
}

/* ---------------- 动作 ---------------- */

/** 执行（已确认的）动作；409 / 422 / 403 一律把后端原话呈现出来 */
async function execute(action: PendingAction) {
  if (pending.value?.key === action.key) pending.value = null
  if (busyKey.value) return // 同时只允许一个动作在飞

  busyKey.value = action.key
  actionError.value = ''
  actionInfo.value = ''
  try {
    await action.run()
    actionInfo.value = action.done
    if (expanded.value === action.phone) expanded.value = ''
    await load()
  } catch (error) {
    actionError.value = authErrorInfo(error, 'admin').message
  } finally {
    busyKey.value = ''
  }
}

/**
 * 所有会改变账号可用性的操作都要二次确认（行内确认条，确认文案里带上手机号，
 * 防止在「通过 / 拒绝」相邻按钮之间误点）。
 */
function askApprove(user: AuthUser) {
  pending.value = {
    key: `${user.phone}:approve`,
    phone: user.phone,
    prompt: `确认通过 ${user.phone} 的注册申请？通过后该账号立即可登录。`,
    confirmLabel: '确认通过',
    done: `已通过 ${user.phone}`,
    dangerous: false,
    run: () => api.adminApproveUser(user.phone),
  }
}

function askReject(user: AuthUser) {
  pending.value = {
    key: `${user.phone}:reject`,
    phone: user.phone,
    prompt: `确认拒绝 ${user.phone} 的注册申请？拒绝后该账号无法登录。`,
    confirmLabel: '确认拒绝',
    done: `已拒绝 ${user.phone}`,
    dangerous: true,
    run: () => api.adminRejectUser(user.phone),
  }
}

function askRole(user: AuthUser, role: string) {
  if (role === user.role) return
  const label = roleLabelOf(role)
  pending.value = {
    key: `${user.phone}:role:${role}`,
    phone: user.phone,
    prompt: `确认把 ${user.phone} 的角色改为「${label}」？`,
    confirmLabel: `改为${label}`,
    done: `已把 ${user.phone} 的角色改为${label}`,
    dangerous: role === 'ADMIN',
    run: () => api.adminSetUserRole(user.phone, role),
  }
}

function askStatus(user: AuthUser, status: string) {
  if (status === user.status) return
  const label = statusLabelOf(status)
  pending.value = {
    key: `${user.phone}:status:${status}`,
    phone: user.phone,
    prompt: `确认把 ${user.phone} 的审批状态改为「${label}」？`,
    confirmLabel: `改为${label}`,
    done: `已把 ${user.phone} 的状态改为${label}`,
    dangerous: status !== 'APPROVED',
    run: () => api.adminSetUserStatus(user.phone, status),
  }
}

/* ---------------- 生命周期 ---------------- */

onMounted(async () => {
  // 复用全站引导（幂等）：拿到灰度开关与角色、状态枚举。
  // 引导本身不抛错，这里再兜一层：引导异常也不能连带把列表卡住。
  try {
    await ensureLoaded()
  } catch {
    /* 忽略：列表照样拉，权限结论交给接口的 403 */
  }
  await load()
})

async function goLogin() {
  await navigateTo('/login')
}

async function goHome() {
  await navigateTo('/')
}
</script>

<template>
  <main class="relative px-5 py-10 text-slate-100 sm:py-14">
    <div class="mx-auto flex w-full max-w-2xl flex-col gap-6">
      <MotionReveal :index="0" class="flex flex-wrap items-start justify-between gap-4">
        <div class="min-w-0 space-y-2.5">
          <div>
            <BrandMark size="sm" />
          </div>
          <h1 class="text-gradient text-3xl font-semibold tracking-tight sm:text-4xl">用户管理</h1>
          <p class="max-w-xl text-sm text-slate-400">
            审批新注册账号、调整角色与账号状态。新账号默认「待审批」，通过后才能登录。
          </p>
          <StatChip tone="bloom" size="sm" dot>
            仅管理员 · {{ authEnforced ? 'API 已鉴权' : 'API 旁路开启（AUTH_ENFORCED=false）' }}
          </StatChip>
          <p v-if="!authEnforced" class="text-[11px] text-slate-500">
            后端旁路已开：接口暂不强制登录，但本页与菜单仍只对管理员可见。
          </p>
        </div>
        <AppNav />
      </MotionReveal>

      <!-- 灰度期之外的非管理员：直接说清楚，不去打接口 -->
      <GlassPanel v-if="blocked" variant="soft" padding="lg" rounded="2xl">
        <div class="space-y-3">
          <StatChip tone="bloom" size="sm" dot>权限不足</StatChip>
          <p class="text-sm leading-relaxed text-slate-300">
            用户管理仅限管理员使用。当前账号
            {{ isLoggedIn ? '不是管理员' : '未登录' }}，因此这里不展示任何用户数据。
          </p>
          <div class="flex flex-wrap gap-2">
            <GlassButton
              v-if="!isLoggedIn"
              variant="primary"
              class="min-h-[44px]"
              @click="goLogin"
            >
              去登录
            </GlassButton>
            <GlassButton v-else variant="glass" class="min-h-[44px]" @click="goHome">
              返回首页
            </GlassButton>
          </div>
        </div>
      </GlassPanel>

      <template v-else>
        <!-- 不是待审批视图时，仍然把队列规模顶在最上面，一键切回去 -->
        <MotionReveal v-if="statusFilter !== 'PENDING' && pendingCount > 0" :index="1">
          <GlassPanel variant="soft" padding="md" rounded="2xl">
            <div class="flex flex-wrap items-center justify-between gap-3">
              <div class="flex items-center gap-2">
                <StatChip tone="amber" size="sm" dot>待审批队列</StatChip>
                <p class="num text-sm text-slate-300">
                  还有 <strong class="font-semibold text-amber-200">{{ pendingCount }}</strong> 个账号等你审批
                </p>
              </div>
              <GlassButton
                variant="primary"
                size="sm"
                class="min-h-[44px] shrink-0"
                @click="setFilter('PENDING')"
              >
                立即处理
              </GlassButton>
            </div>
          </GlassPanel>
        </MotionReveal>

        <!-- 筛选：待审批排第一，是默认视图 -->
        <MotionReveal :index="2" class="space-y-2">
          <p id="user-status-filter" class="text-xs font-medium text-slate-400">审批状态筛选</p>
          <div
            role="radiogroup"
            aria-labelledby="user-status-filter"
            class="flex flex-wrap gap-2"
          >
            <GlassButton
              v-for="option in filterOptions"
              :key="option.value"
              role="radio"
              :aria-checked="statusFilter === option.value"
              :variant="statusFilter === option.value ? 'primary' : 'glass'"
              class="min-h-[44px] px-4 text-sm"
              @click="setFilter(option.value)"
            >
              {{ option.label }}
              <template v-if="option.value === 'PENDING' && pendingCount > 0">
                （{{ pendingCount }}）
              </template>
            </GlassButton>
          </div>
        </MotionReveal>

        <!-- 列表 -->
        <MotionReveal :index="3">
          <GlassPanel variant="strong" padding="lg" rounded="3xl" glow tone="aqua">
            <div class="space-y-4">
              <div class="space-y-1.5">
                <h2 class="text-lg font-medium text-white">
                  {{ statusFilter === 'PENDING' ? '待审批队列' : '用户列表' }}
                </h2>
                <p v-if="statusFilter === 'PENDING'" class="text-sm text-slate-400">
                  这是新注册账号的待办队列 —— 每天只需要在这里逐条「通过」或「拒绝」。
                </p>
                <p v-else class="text-sm text-slate-400">
                  共 <span class="num">{{ users.length }}</span> 个账号；展开「角色与审批状态」可调整权限。
                </p>
              </div>

              <p v-if="loading" class="text-sm text-slate-400">加载中…</p>

              <div
                v-else-if="loadError"
                role="alert"
                class="rounded-xl border border-bloom-400/40 bg-bloom-400/10 px-4 py-3 text-sm text-bloom-200"
              >
                <p class="font-medium">{{ loadError.message }}</p>
                <button
                  type="button"
                  class="mt-1 underline underline-offset-2"
                  @click="load()"
                >
                  重试
                </button>
              </div>

              <template v-else>
                <p v-if="actionError" role="alert" class="text-sm text-bloom-300">
                  {{ actionError }}
                </p>
                <p v-else-if="actionInfo" role="status" class="text-sm text-emerald-300">
                  {{ actionInfo }}
                </p>

                <p v-if="!users.length" class="text-sm text-slate-500">
                  <template v-if="statusFilter === 'PENDING'">
                    待审批队列是空的：当前没有新注册申请需要处理。
                  </template>
                  <template v-else>这个状态下没有账号。</template>
                </p>

                <ul v-else class="space-y-3">
                  <GlassCard
                    v-for="user in pagedUsers"
                    :key="user.phone"
                    as="li"
                    padding="md"
                    rounded="2xl"
                    :interactive="false"
                    :sweep="false"
                  >
                    <div class="flex flex-wrap items-start justify-between gap-2">
                      <div class="min-w-0 space-y-1">
                        <p class="num text-base font-semibold text-white">{{ user.phone }}</p>
                        <p class="num text-[11px] text-slate-500">
                          注册 {{ dateTimeText(user.created_at) }}
                          · 最近登录 {{ dateTimeText(user.last_login_at) }}
                        </p>
                      </div>
                      <div class="flex flex-wrap items-center gap-1.5">
                        <StatChip :tone="roleTone(user.role)" size="sm">
                          {{ user.role_label || roleLabelOf(user.role) }}
                        </StatChip>
                        <StatChip :tone="statusTone(user.status)" size="sm" dot>
                          {{ user.status_label || statusLabelOf(user.status) }}
                        </StatChip>
                      </div>
                    </div>

                    <!-- 待审批的主行动：直接暴露在卡面上 -->
                    <div
                      v-if="user.status === 'PENDING' && confirmingPhone !== user.phone"
                      class="mt-3 flex flex-wrap gap-2"
                    >
                      <GlassButton
                        variant="primary"
                        class="min-h-[44px] min-w-0 flex-1"
                        :loading="busyKey === `${user.phone}:approve`"
                        @click="askApprove(user)"
                      >
                        通过
                      </GlassButton>
                      <GlassButton
                        variant="danger"
                        class="min-h-[44px] min-w-0 flex-1"
                        @click="askReject(user)"
                      >
                        拒绝
                      </GlassButton>
                    </div>

                    <!-- 行内二次确认条 -->
                    <div
                      v-if="pending && pending.phone === user.phone"
                      class="mt-3 rounded-xl border border-aqua-400/40 bg-aqua-400/10 px-4 py-3"
                    >
                      <p class="text-sm text-aqua-100">{{ pending.prompt }}</p>
                      <div class="mt-3 flex flex-wrap gap-2">
                        <GlassButton
                          :variant="pending.dangerous ? 'danger' : 'primary'"
                          class="min-h-[44px] min-w-0 flex-1"
                          :loading="busyKey === pending.key"
                          @click="execute(pending)"
                        >
                          {{ pending.confirmLabel }}
                        </GlassButton>
                        <GlassButton
                          variant="glass"
                          class="min-h-[44px] min-w-0 flex-1"
                          @click="cancelConfirm"
                        >
                          取消
                        </GlassButton>
                      </div>
                    </div>

                    <button
                      type="button"
                      class="mt-3 flex min-h-[44px] w-full items-center justify-between gap-3 rounded-xl border border-white/10 bg-white/5 px-4 text-left text-sm text-slate-200 transition-colors active:bg-white/15"
                      :aria-expanded="expanded === user.phone"
                      @click="toggleExpanded(user.phone)"
                    >
                      <span>角色与审批状态</span>
                      <span class="text-[11px] text-slate-500">
                        {{ expanded === user.phone ? '收起' : '展开' }}
                      </span>
                    </button>

                    <div
                      v-if="expanded === user.phone"
                      class="mt-3 space-y-4 border-t border-white/10 pt-3"
                    >
                      <div class="space-y-2">
                        <p :id="`role-label-${user.phone}`" class="text-xs font-medium text-slate-400">
                          角色
                        </p>
                        <div
                          role="radiogroup"
                          :aria-labelledby="`role-label-${user.phone}`"
                          class="flex flex-wrap gap-2"
                        >
                          <GlassButton
                            v-for="option in roleOptions"
                            :key="option.value"
                            role="radio"
                            :aria-checked="user.role === option.value"
                            :variant="user.role === option.value ? 'primary' : 'glass'"
                            class="min-h-[44px] px-3 text-sm"
                            @click="askRole(user, option.value)"
                          >
                            {{ option.label }}
                          </GlassButton>
                        </div>
                      </div>

                      <div class="space-y-2">
                        <p :id="`status-label-${user.phone}`" class="text-xs font-medium text-slate-400">
                          审批状态
                        </p>
                        <div
                          role="radiogroup"
                          :aria-labelledby="`status-label-${user.phone}`"
                          class="flex flex-wrap gap-2"
                        >
                          <GlassButton
                            v-for="option in statusOptions"
                            :key="option.value"
                            role="radio"
                            :aria-checked="user.status === option.value"
                            :variant="user.status === option.value ? 'primary' : 'glass'"
                            class="min-h-[44px] px-3 text-sm"
                            @click="askStatus(user, option.value)"
                          >
                            {{ option.label }}
                          </GlassButton>
                        </div>
                      </div>

                      <p class="text-[11px] text-slate-500">
                        后端有「不能把自己降权 / 停用」与「至少保留一个已通过的管理员」两条护栏，
                        触发时会返回 409 并在这里显示原话。
                      </p>
                    </div>
                  </GlassCard>
                </ul>

                <GlassPager
                  v-model:page="page"
                  :page-size="PAGE_SIZE"
                  :total="users.length"
                  unit="个"
                  label="用户列表分页"
                />
              </template>
            </div>
          </GlassPanel>
        </MotionReveal>
      </template>
    </div>
  </main>
</template>
