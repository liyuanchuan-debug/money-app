/* ==========================================================================
 * useAuth —— 全站唯一的登录态来源（SSR 安全 + 跨页面共享）
 * --------------------------------------------------------------------------
 * 三条底线：
 *   1. 状态一律走 Nuxt 的 `useState`，服务端 / 客户端同一份，不各持一个副本；
 *      也因此组件销毁后不丢，页面之间跳转不会重拉 `/api/auth/me`。
 *   2. `ensureLoaded()` 是**幂等引导**：只在需要时打一次 `/api/auth/me`，
 *      401 视为「未登录」这个正常状态，绝不抛错、绝不把用户卡在加载态。
 *   3. 会话是后端下发的 **HttpOnly 持久 Cookie**（`wm_session`，带 Max-Age，
 *      关浏览器再开仍在；等同 localStorage 级持久，不用 sessionStorage）。
 *      浏览器端靠 `credentials: 'include'`；SSR 靠 `useRequestFetch()` 转发 Cookie。
 *      默认有效期很长（后端 `JWT_EXPIRE_DAYS` / 365 天）；清站点数据或点退出才失效。
 *
 * 口径提醒：角色 / 状态判定**只读英文枚举**（USER / VIP / ADMIN、PENDING / …），
 * 汉字只出现在接口给的 `*_label` 与展示用映射表里。
 *
 * 灰度开关：`authEnforced` 反映后端 `AUTH_ENFORCED`（API 是否强制校验）。
 * 前端菜单 / 路由门禁**不依赖**该开关：公开页对访客可见，个人能力按角色收口。
 * `AUTH_ENFORCED=false` 只是后端开发旁路，不会把受保护 UI 放开给访客。
 * ========================================================================== */

import type { AuthConfig, AuthMe, AuthUser, RegisterResult } from '~/composables/useApi'
import { ROLE_RANK } from '~/composables/useApi'
import { invalidateApiCache, withApiCache } from '~/utils/apiCache'
import { computed } from 'vue'

/* -------------------------------------------------------------------------- */
/* 错误归一化：把 $fetch 的异常翻译成页面能区分的种类                            */
/* -------------------------------------------------------------------------- */

/**
 * 登录失败的两种「完全不同的错」：
 *   - `credentials`    口令 / 手机号不对（后端 401，统一文案，不区分两者）
 *   - `account-status` 口令是对的，但账号不可用（后端 **403**：待审批 / 已拒绝 / 已停用）
 * 这两种必须分开呈现：前者用户自己能改，后者只能等管理员。
 */
export type AuthErrorKind
  = | 'credentials'
    | 'account-status'
    | 'forbidden'
    | 'conflict'
    | 'validation'
    | 'not-found'
    | 'network'
    | 'unknown'

export interface AuthErrorInfo {
  kind: AuthErrorKind
  /** HTTP 状态码；0 表示请求根本没发出去（断网 / 跨域被拦） */
  status: number
  /** 能直接给用户看的文案（优先用后端 detail） */
  message: string
}

/** FastAPI 的 detail 可能是字符串，也可能是 422 的 [{loc,msg,type}] 数组 */
function detailText(payload: unknown): string {
  if (!payload || typeof payload !== 'object') return ''
  const detail = (payload as { detail?: unknown }).detail
  if (typeof detail === 'string') return detail.trim()
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (typeof item === 'string') return item
        if (item && typeof item === 'object') {
          const record = item as { msg?: unknown; message?: unknown }
          const text = record.msg ?? record.message
          return typeof text === 'string' ? text : ''
        }
        return ''
      })
      .filter((text): text is string => Boolean(text))
      .join('；')
  }
  return ''
}

export function httpStatusOf(error: unknown): number {
  const record = error as { status?: unknown; statusCode?: unknown } | null
  const raw = record?.statusCode ?? record?.status
  const value = Number(raw)
  return Number.isFinite(value) && value > 0 ? value : 0
}

/**
 * 把任意请求异常翻译成 { kind, status, message }。
 *
 * `context: 'login'` 时 403 会被判成 `account-status`（口令对、账号不可用）；
 * 其余上下文里 403 就是 `forbidden`（权限不足）。
 */
export function authErrorInfo(
  error: unknown,
  context: 'login' | 'admin' | 'register' = 'login',
): AuthErrorInfo {
  const record = error as { data?: unknown; message?: unknown } | null
  const status = httpStatusOf(error)
  const message = detailText(record?.data)

  if (context === 'login') {
    if (status === 401) {
      return { kind: 'credentials', status, message: message || '手机号或密码错误' }
    }
    if (status === 403) {
      return { kind: 'account-status', status, message: message || '账号当前不可用，请联系管理员' }
    }
  }

  if (status === 403) return { kind: 'forbidden', status, message: message || '权限不足' }
  if (status === 409) return { kind: 'conflict', status, message: message || '操作被系统安全规则拒绝' }
  if (status === 422) return { kind: 'validation', status, message: message || '提交的内容不符合要求' }
  if (status === 404) return { kind: 'not-found', status, message: message || '目标不存在（可能已被删除）' }
  if (status === 0) {
    const fallback = typeof record?.message === 'string' && record.message.trim()
      ? record.message.trim()
      : '网络异常，请检查网络后重试'
    return { kind: 'network', status: 0, message: fallback }
  }
  return { kind: 'unknown', status, message: message || `请求失败（HTTP ${status}）` }
}

/* -------------------------------------------------------------------------- */
/* 展示助手（纯函数，页面里也可以直接用）                                        */
/* -------------------------------------------------------------------------- */

/** 手机号中间打码：13800138000 → 138****8000（只用于展示，不改变实际数据） */
export function maskPhone(phone: string | null | undefined): string {
  const text = String(phone ?? '').trim()
  if (!text) return ''
  if (text.length < 7) return text
  return `${text.slice(0, 3)}****${text.slice(-4)}`
}

/** 角色展示映射（仅在后端没给 role_label 时兜底） */
export const ROLE_LABELS: Record<string, string> = {
  USER: '普通用户',
  VIP: 'VIP用户',
  ADMIN: '管理员',
}

/** 状态展示映射（后端不返回状态列表的 label，这里与 services/auth.py 的 STATUS_LABELS 对齐） */
export const STATUS_LABELS: Record<string, string> = {
  PENDING: '待审批',
  APPROVED: '已通过',
  REJECTED: '已拒绝',
  DISABLED: '已停用',
}

export function roleLabelOf(role: string | null | undefined): string {
  const value = String(role ?? '')
  return ROLE_LABELS[value] ?? value
}

export function statusLabelOf(status: string | null | undefined): string {
  const value = String(status ?? '')
  return STATUS_LABELS[value] ?? value
}

/* -------------------------------------------------------------------------- */
/* 每个 Nuxt 实例一份的运行时槽位                                              */
/* -------------------------------------------------------------------------- */
/**
 * 用 `useNuxtApp()` 上的槽位而不是模块级变量来放「正在进行的引导 Promise」：
 * 服务端的模块作用域是**跨请求共享**的，用模块级变量会把一个请求的 Promise
 * 串给另一个请求（甚至在请求间泄漏登录态）。挂在 NuxtApp 上则服务端每请求一份、
 * 浏览器端每页一份，正好是我们要的粒度。
 */
interface AuthRuntimeSlot {
  inflight: Promise<void> | null
}

function authRuntimeSlot(): AuthRuntimeSlot {
  const app = useNuxtApp() as unknown as Record<string, unknown>
  const key = '$authRuntime'
  const existing = app[key] as AuthRuntimeSlot | undefined
  if (existing) return existing
  const created: AuthRuntimeSlot = { inflight: null }
  app[key] = created
  return created
}

/* -------------------------------------------------------------------------- */
/* 主入口                                                                      */
/* -------------------------------------------------------------------------- */

export function useAuth() {
  const config = useRuntimeConfig()
  const baseURL = config.public.apiBase as string
  const api = useApi()

  /**
   * 服务端渲染时，用 `useRequestFetch()` 把浏览器送来的 Cookie 转发给后端
   * （服务端的 `$fetch` 不会自动带 Cookie）。浏览器端返回 null，
   * 交给 `credentials: 'include'` 自己带。
   */
  let serverForwardingFetch: typeof $fetch | null = null
  if (!import.meta.client) {
    try {
      serverForwardingFetch = useRequestFetch() as unknown as typeof $fetch
    } catch {
      // 没有 h3 event（例如静态渲染）时退回普通 $fetch：只是拿不到登录态，不影响渲染
      serverForwardingFetch = null
    }
  }

  function rawFetch<T>(path: string, options: Parameters<typeof $fetch>[1] = {}) {
    // 与 useApi 共用同一 cache store：config/me 短缓存，login/logout 写成功后失效 auth。
    return withApiCache(path, options, () => {
      const fetcher = serverForwardingFetch ?? $fetch
      return fetcher<T>(path, { baseURL, credentials: 'include', ...options })
    })
  }

  /* ---------------- 共享状态（useState ⇒ SSR 安全 & 跨页共享） ---------------- */

  const user = useState<AuthUser | null>('auth:user', () => null)
  const roles = useState<string[]>('auth:roles', () => [])
  const statuses = useState<string[]>('auth:statuses', () => [])
  /** 后端 API 是否强制鉴权；UI 门禁不读此值（未登录始终当访客） */
  const authEnforced = useState<boolean>('auth:enforced', () => true)
  /** 引导是否已跑完（跑完之前导航区显示占位，避免先闪一个「登录」再变成手机号） */
  const loaded = useState<boolean>('auth:loaded', () => false)
  const loading = useState<boolean>('auth:loading', () => false)

  /* ---------------- 派生 ---------------- */

  const isLoggedIn = computed(() => Boolean(user.value?.phone))
  const role = computed(() => user.value?.role ?? null)
  const roleLabel = computed(() => user.value?.role_label || roleLabelOf(user.value?.role))
  const status = computed(() => user.value?.status ?? null)
  const statusLabel = computed(() => user.value?.status_label || statusLabelOf(user.value?.status))
  const maskedPhone = computed(() => maskPhone(user.value?.phone))
  const isAdmin = computed(() => hasRole('ADMIN'))

  /**
   * 角色层级判定：USER < VIP < ADMIN，ADMIN 同时满足 VIP / USER。
   * 未登录一律 false（不存在「匿名也满足」的分支）。
   */
  function hasRole(minimum: string): boolean {
    const current = user.value?.role
    if (!current) return false
    return (ROLE_RANK[current] ?? 0) >= (ROLE_RANK[minimum] ?? 99)
  }

  /**
   * UI 门禁：必须已登录且角色达标。
   * 不再因 `authEnforced=false` 而放行 —— 后端旁路不等于前端放开菜单。
   */
  function canUse(minimum: string): boolean {
    return hasRole(minimum)
  }

  /* ---------------- 引导 ---------------- */

  /** 拉公开配置（角色 / 状态枚举 + 灰度开关）。失败静默：不能因为配置抖动就锁死全站。 */
  async function fetchConfig(): Promise<AuthConfig | null> {
    try {
      const payload = await rawFetch<AuthConfig>('/api/auth/config')
      authEnforced.value = Boolean(payload?.auth_enforced)
      if (Array.isArray(payload?.roles)) roles.value = payload.roles
      if (Array.isArray(payload?.statuses)) statuses.value = payload.statuses
      return payload
    } catch {
      return null
    }
  }

  /**
   * 拉当前用户。
   * - 200 → 写入状态，顺便同步一次灰度开关；
   * - 401 → 视为「未登录」这个**正常状态**，清空 user，不报错；
   * - 其它（网络 / 5xx）→ 保留现状，不清空已登录用户，也不抛错。
   * 任何情况下都不 throw —— 引导路径抛错会把页面卡在加载态。
   */
  async function fetchMe(): Promise<AuthUser | null> {
    try {
      const me = await rawFetch<AuthMe>('/api/auth/me')
      user.value = me
      authEnforced.value = Boolean(me?.auth_enforced)
      return me
    } catch (error) {
      if (httpStatusOf(error) === 401) {
        user.value = null
        return null
      }
      return user.value
    }
  }

  /**
   * 幂等引导：全站只跑一次，配置与当前用户并发拉取。
   * 多处同时调用时共享同一个 Promise（并发只有一次真实请求）；
   * 引导完成（含失败）后 `loaded` 一定为 true，不会永远停在加载态。
   */
  async function ensureLoaded(force = false): Promise<void> {
    if (loaded.value && !force) return
    const slot = authRuntimeSlot()
    if (slot.inflight && !force) return slot.inflight

    loading.value = true
    const task = (async () => {
      await Promise.allSettled([fetchConfig(), fetchMe()])
      loaded.value = true
    })()
    slot.inflight = task

    try {
      await task
    } finally {
      loading.value = false
      if (slot.inflight === task) slot.inflight = null
    }
  }

  /* ---------------- 动作（失败一律抛出，由页面归一化后展示） ---------------- */

  async function login(phone: string, password: string): Promise<AuthUser> {
    const result = await rawFetch<{ ok: boolean; user: AuthUser }>('/api/auth/login', {
      method: 'POST',
      body: { phone: String(phone ?? '').trim(), password },
    })
    // 会话已变：强制丢掉旧 me/config，避免短 TTL 内读到登录前的匿名态
    invalidateApiCache('auth')
    user.value = result.user
    loaded.value = true
    return result.user
  }

  async function register(phone: string, password: string): Promise<RegisterResult> {
    return await rawFetch<RegisterResult>('/api/auth/register', {
      method: 'POST',
      body: { phone: String(phone ?? '').trim(), password },
    })
  }

  /** 登出：后端接口是幂等的；即便请求失败也要清掉本地状态，不能把人「登而不出」 */
  async function logout(): Promise<void> {
    try {
      await rawFetch<{ ok: boolean }>('/api/auth/logout', { method: 'POST' })
    } catch {
      /* 静默：本地状态照清 */
    } finally {
      invalidateApiCache('auth')
      user.value = null
      loaded.value = true
    }
  }

  /** 手动刷新当前用户（页面需要「刚刚被管理员改了角色」立刻生效时用） */
  async function refresh(): Promise<AuthUser | null> {
    invalidateApiCache('auth')
    return await fetchMe()
  }

  return {
    /* 状态 */
    user,
    loaded,
    loading,
    /* 派生 */
    isLoggedIn,
    isAdmin,
    role,
    roleLabel,
    status,
    statusLabel,
    maskedPhone,
    authEnforced,
    roles,
    statuses,
    /* 判定 */
    hasRole,
    canUse,
    /* 动作 */
    ensureLoaded,
    fetchMe,
    fetchConfig,
    refresh,
    login,
    register,
    logout,
    /* 传输层（管理后台等页面直接用） */
    api,
  }
}
