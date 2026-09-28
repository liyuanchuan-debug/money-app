import { withApiCache } from '~/utils/apiCache'

export interface LotterySettings {
  small_max: number
  normal_max: number
  /** 最大投注金额：**唯一的预算真值**，所有模式都从它取预算 */
  total_amount: number
  /** 金额最小单位（注码粒度）：所有模式下各注金额必须是它的正整数倍（默认 5） */
  amount_unit: number
  /** 只读派生（deprecated）：均分后再向下对齐到 amount_unit；不可写 */
  bet_unit: number
  mode: string
  pick_count: number
  /** 特码兑付倍数（用户设定；默认 47，不是收益承诺） */
  odds: number
  /** 避开重肖：true=候选池排除最新同肖；默认 false */
  exclude_repeat_zodiac: boolean
  /**
   * 近期走势加权：neutral|hot|cold|mid。
   * 读取口径：没在设置页手动设置过就是 neutral（不加权），
   * 旧版本残留的 hot 也不会自动生效。
   */
  trend_bias: TrendBias
  /** 近窗期数；0=全部样本；默认 30 */
  trend_window: number
  /**
   * 避冷加权：True=距上次出现超过 avoid_cold_days 天的号权重递减、金额递减；
   * 默认 True（旧后端缺字段时同样按开启处理）。
   */
  avoid_cold_enabled: boolean
  /** 避冷阈值（自然日）：距上次出现 ≤ 该值不惩罚；默认 60，范围 1..999 */
  avoid_cold_days: number
  /** 只读派生字段：恒为 normal_max + 1 */
  big_min: number
}

/** PUT /api/settings 的**可写**字段（bet_unit / big_min 是只读派生值，不在此列） */
export interface LotterySettingsPatch {
  small_max?: number
  normal_max?: number
  total_amount?: number
  amount_unit?: number
  mode?: ChipMode
  pick_count?: number
  odds?: number
  exclude_repeat_zodiac?: boolean
  trend_bias?: TrendBias
  trend_window?: number
  avoid_cold_enabled?: boolean
  avoid_cold_days?: number
}

export interface Pick {
  number: number
  diff: number
  wave_type: 'small' | 'normal' | 'big'
  wave_label: string
  role: 'primary' | 'secondary' | 'defense'
  role_label: string
  amount: number
  is_repeat_zodiac: boolean
  /**
   * 号码 → 生肖的固定映射（英文码 + 汉字标签），随农历年（春节）轮转。
   * 与 `latest_zodiac` 指向同一组号码（农历年只决定这一组叫什么名字）。
   * null = 后端拿不到开奖参照日 / 参照日不在已知农历年表内 —— 不猜年份，前端照实留空。
   */
  zodiac?: string | null
  zodiac_label?: string | null
  /** 近窗内出现次数（走势加权依据） */
  trend_count?: number
  trend_note?: string
  /**
   * 距本池样本内最近一次出现的自然日数（后端 compute_days_since_last）。
   * null = 本池样本内从未出现（按最冷处理，不编造天数）。
   */
  days_since_last?: number | null
  /** 该注是否被避冷加权压低（只降不升；关闭避冷时恒为 false） */
  avoid_cold_penalized?: boolean
  /** 避冷加权（1.0=不惩罚；越久越低；样本内未出现为 0） */
  avoid_cold_weight?: number
}

export interface TrendNumberStat {
  number: number
  count: number
  rate: number
  diff?: number
  /**
   * 近窗 count=0 时附带：距样本内最近一次出现的自然日数。
   * null = 本池样本内从未出现（前端写「样本内未出现」，勿编造）。
   */
  days_since_last?: number | null
}

export interface TrendWaveRoles {
  type: string
  label: string
  primary: TrendNumberStat[]
  secondary: TrendNumberStat[]
  defense: TrendNumberStat[]
}

export interface TrendDistributions {
  window: number
  used_window: number
  bias: TrendBias | string
  bias_label: string
  rule: string
  waves: Record<string, TrendWaveRoles>
}

export interface RecommendResult {
  latest: number
  latest_zodiac: number[]
  /**
   * 「最新同肖」这一组的生肖（与 `latest_zodiac` 恒为同一组号码）；
   * null = 农历年未知（参照日缺失或不在已知表内）。
   */
  latest_zodiac_code?: string | null
  latest_zodiac_label?: string | null
  /** 生肖口径溯源：参照日 = 本池最新一期开奖日（YYYY-MM-DD）与其农历年；未知时 null */
  zodiac_date?: string | null
  zodiac_year?: number | null
  previous: number | null
  prev_wave: { number: number; diff: number; type: string; label: string } | null
  settings: LotterySettings
  mode: string
  mode_label: string
  /** 派生展示（deprecated）：均分后向下对齐到 amount_unit */
  bet_unit: number
  total_amount: number
  /** 本次分配使用的金额最小单位（注码粒度） */
  amount_unit: number
  /** 实际分配到各注的合计（避冷加权压低后可能 < total_amount） */
  staked_total?: number
  /** 避冷加权本次生效摘要（只增不改；penalized_picks=0 表示未压低任何注） */
  avoid_cold?: {
    enabled: boolean
    /** 避冷阈值（自然日） */
    days: number
    /** 保本金额 = 1 个金额最小单位 */
    cap_amount: number
    penalized_picks: number
    budget_total: number
    /** 被避冷省下、未再分配的金额 */
    reduced_total: number
  }
  focus_order: Array<{ type: string; label: string }>
  picks: Pick[]
  missing_waves: Array<{ type: string; label: string; note: string }>
  notes: string[]
  copy_text: string
  trend_window?: number
  trend_bias?: TrendBias | string
  trend_bias_label?: string
  number_frequency?: TrendNumberStat[]
  trend_distributions?: TrendDistributions
  /** 本次推荐按模拟买入自动采用后的快照摘要（假设号码预览时没有） */
  adopted_round?: {
    id?: number
    period: number
    status: string
    /** 英文枚举：SIMULATED | REAL | SKIPPED（当前默认 SIMULATED） */
    stake_mode?: string
    cost: number
    odds: number
    hit?: boolean | null
    payout?: number
    profit?: number | null
  }
}

/**
 * 筹码模式。取值必须与后端 ``services/lottery.py`` 的 ``MODE_EVEN`` /
 * ``MODE_WEIGHTED`` / ``MODE_SINGLE`` / ``MODE_RANDOM`` 一致
 * （``PUT /api/settings`` 与 ``POST /api/recommend`` 都按 `^(even|weighted|single|random)$` 校验）。
 */
export type ChipMode = 'even' | 'weighted' | 'single' | 'random'

/** 走势加权偏好；与后端 TREND_BIAS_* 一致 */
export type TrendBias = 'neutral' | 'hot' | 'cold' | 'mid'

export interface ChipModeOption {
  value: ChipMode
  label: string
}

export interface TrendBiasOption {
  value: TrendBias
  label: string
}

/**
 * 筹码模式的唯一取值表（默认值在「设置」页持久化，临场切换在「波浪买入法」页）。
 * 只放 value + 汉字 label，两个页面共用同一份，避免取值漂移。
 */
export const CHIP_MODE_OPTIONS: ChipModeOption[] = [
  { value: 'even', label: '均注' },
  { value: 'weighted', label: '侧重' },
  { value: 'single', label: '单挑' },
  { value: 'random', label: '随机分配' },
]

/**
 * 均注金额预览（与后端 ``_distribute_units_even`` 同源）：
 * 先把最大投注换成单位个数，再尽量均分；余数补给前几注。
 * 例：total=50 / unit=5 / count=6 → ``[10, 10, 10, 10, 5, 5]``。
 */
export function previewEvenAmounts(
  total: number,
  count: number,
  unit: number,
): number[] {
  const u = Math.max(1, Math.floor(Number(unit) || 1))
  const n = Math.max(1, Math.floor(Number(count) || 1))
  const unitsTotal = Math.floor(Math.max(0, Number(total) || 0) / u)
  if (unitsTotal <= 0) return []
  const noteCount = Math.min(n, unitsTotal)
  const base = Math.floor(unitsTotal / noteCount)
  const rem = unitsTotal % noteCount
  return Array.from({ length: noteCount }, (_, i) => (base + (i < rem ? 1 : 0)) * u)
}

/** 走势加权取值表（设置页持久化；财富密码页可临时预览）；默认 neutral */
export const TREND_BIAS_OPTIONS: TrendBiasOption[] = [
  { value: 'neutral', label: '不加权' },
  { value: 'hot', label: '热号偏好' },
  { value: 'mid', label: '中频优先' },
  { value: 'cold', label: '冷号偏好' },
]

/** 走势近窗档位（0 = 全部） */
export const TREND_WINDOW_OPTIONS = [
  { value: 30, label: '近 30 期' },
  { value: 60, label: '近 60 期' },
  { value: 100, label: '近 100 期' },
  { value: 0, label: '全部样本' },
] as const

/* ---------------------------------------------------------------------- */
/* 避冷加权（冷号排后 + 金额封顶，只降不升）                                */
/* ---------------------------------------------------------------------- */
/**
 * 避冷加权：与「近期走势加权」是**两个独立维度**（可同时生效）——
 *   1）冷号排后：距上次出现超过阈值的号（含样本内从未出现）排到候选队列末尾；
 *   2）金额封顶：权重随天数递减，金额最多给「保本金额」（1 个金额最小单位）。
 * 取值 / 口径与后端 ``services/lottery.py`` 的 ``avoid_cold_weight`` /
 * ``is_avoid_cold_number`` 同源；这里的汉字只用于展示，不参与任何落库判定。
 * 这是样本内偏好，不是概率，也不承诺提高命中率或收益。
 */
export const AVOID_COLD_LABEL = '避冷加权'
/** 避冷阈值默认值（自然日） */
export const AVOID_COLD_DEFAULT_DAYS = 60
export const AVOID_COLD_DAYS_MIN = 1
export const AVOID_COLD_DAYS_MAX = 999

/** 避冷加权展示文案（0 = 样本内从未出现 → 金额归 0；勿写成概率） */
export function avoidColdWeightText(weight: number | null | undefined): string {
  if (weight == null || !Number.isFinite(weight)) return '—'
  if (weight >= 1) return '不降权'
  if (weight <= 0) return '样本内未出现 · 金额归 0'
  return `权重 ${Math.round(weight * 100)}%`
}

/** 距上次出现展示文案：null = 本池样本内从未出现（不编造天数） */
export function avoidColdDaysText(days: number | null | undefined): string {
  if (days == null || !Number.isFinite(days)) return '样本内未出现'
  return `${Math.trunc(days)} 天前`
}

/* ---------------------------------------------------------------------- */
/* 开奖总表（每期只保留特码）                                               */
/* ---------------------------------------------------------------------- */
export interface DrawItem {
  id: number
  draw_date: string
  period: number
  special_number: number
  /** 生肖英文码（HORSE / RAT …）；后端可能只给中文标签，故一律按可选处理 */
  zodiac?: string | null
  /** 生肖中文标签（马 / 鼠 …） */
  zodiac_label?: string | null
}

/**
 * GET /api/draws/next-period 的返回体：快捷录入表单的预填值。
 * 期号 / 日期的推导口径与 ``add_draw`` 的缺省值同源（期号 = max(period)+1、
 * 日期 = 今天 UTC+8），所以前端直接用服务端的建议值即可，不必自己在浏览器里算。
 * 本池还没有开奖记录时 ``has_draws`` 为 false，``suggested_period`` 为 1。
 */
export interface NextPeriod {
  suggested_period: number
  /** YYYY-MM-DD（Asia/Shanghai 口径） */
  suggested_draw_date: string
  timezone: string
  has_draws: boolean
  latest_draw: DrawItem | null
}

/**
 * POST /api/draws/quick 的请求体。
 * period / draw_date 留空（null / undefined / 空串）时由后端自动补全：
 * period → max(period) + 1；draw_date → 今天。
 * 同一 period 重复提交后端按 upsert 处理（用于修正录错的期号）。
 */
export interface QuickDrawPayload {
  period?: number | null
  draw_date?: string | null
  special_number: number
}

/** POST /api/draws/{id}/correct 请求体 */
export interface DrawCorrectPayload {
  special_number: number
  draw_date?: string | null
}

/** 纠正开奖回执：含新旧特码与重算条数 */
export interface DrawCorrectResult {
  draw: DrawItem
  action: string
  action_label: string
  period: number
  old_special_number: number
  new_special_number: number
  old_draw_date: string
  new_draw_date: string
  resettled_rounds: number
  correction_id?: number | null
}

export interface DrawImportError {
  line: number
  text: string
  message: string
}

export interface DrawImportResult {
  imported: number
  skipped: number
  warnings: string[]
  errors: DrawImportError[]
}

/* ---------------------------------------------------------------------- */
/* 生肖表（农历年口径的 49 号码关联表，只读）                                */
/* ---------------------------------------------------------------------- */
export interface ZodiacGroup {
  code: string
  label: string | null
  numbers: number[]
}

export interface ZodiacNumberItem {
  number: number
  zodiac: string | null
  zodiac_label: string | null
}

export interface ZodiacTable {
  date: string
  lunar_year: number
  starts_on: string
  animal_of_01: string
  animal_of_01_label: string | null
  zodiacs: ZodiacGroup[]
  numbers: ZodiacNumberItem[]
}

export interface ZodiacYear {
  lunar_year: number
  starts_on: string
  animal_of_01: string
  animal_of_01_label: string | null
}

export interface ZodiacYearNumber {
  lunar_year: number
  number: number
  zodiac: string
  zodiac_label: string | null
}

/* ---------------------------------------------------------------------- */
/* 认证与权限（RBAC）                                                       */
/* ---------------------------------------------------------------------- */
/**
 * 角色 / 状态枚举：**英文口径与后端 services/auth.py 完全一致**。
 * 落库与判定只用这里的英文值；汉字一律走接口返回的 `*_label` 或前端的展示映射表。
 */
export const ROLE_USER = 'USER'
export const ROLE_VIP = 'VIP'
export const ROLE_ADMIN = 'ADMIN'

/** 角色层级：数值越大权限越高。ADMIN 同时满足 VIP / USER 的要求。 */
export const ROLE_RANK: Record<string, number> = {
  [ROLE_USER]: 1,
  [ROLE_VIP]: 2,
  [ROLE_ADMIN]: 3,
}

export const STATUS_PENDING = 'PENDING'
export const STATUS_APPROVED = 'APPROVED'
export const STATUS_REJECTED = 'REJECTED'
export const STATUS_DISABLED = 'DISABLED'

/** 落库 / 判定用的英文枚举 */
export type Role = 'USER' | 'VIP' | 'ADMIN'
export type UserStatus = 'PENDING' | 'APPROVED' | 'REJECTED' | 'DISABLED'

/** GET /api/auth/config —— 公开接口，前端据此决定是否上锁 */
export interface AuthConfig {
  auth_enforced: boolean
  roles: string[]
  statuses: string[]
}

/**
 * 用户信息（`GET /api/auth/me`、`GET /api/admin/users` 返回体同形）。
 * 判定一律读英文 `role` / `status`；`role_label` / `status_label` 只用于展示。
 */
export interface AuthUser {
  id: number
  phone: string
  role: string
  role_label: string
  status: string
  status_label: string
  created_at: string | null
  approved_at: string | null
  last_login_at: string | null
}

/** `GET /api/auth/me` 在 UserOut 之上多带一个灰度开关 */
export interface AuthMe extends AuthUser {
  auth_enforced: boolean
}

export interface LoginResult {
  ok: boolean
  user: AuthUser
}

/**
 * `POST /api/auth/register` 回执。
 * 后端**刻意**不区分「新号」与「已注册」——所以这里也没有 status / created 字段，
 * 前端不许自己推断手机号是否已存在。
 */
export interface RegisterResult {
  ok: boolean
  message: string
}

export function useApi() {
  const config = useRuntimeConfig()
  const baseURL = config.public.apiBase as string

  /**
   * SSR 专用的「带请求上下文的 fetch」。
   *
   * 会话是浏览器与后端之间的 HttpOnly Cookie；服务端渲染时那次 `/api/*` 请求是
   * **服务器自己发的**，浏览器不会替它带 Cookie（`credentials: 'include'` 只管浏览器）。
   * 所以 SSR 阶段必须换用 `useRequestFetch()` 返回的 fetch —— 它绑定当前 h3 event，
   * 会把浏览器送来的 `cookie` 头一并转发给后端。浏览器端返回 null，交回
   * `credentials: 'include'` 自己带，行为与改动前完全一致。
   *
   * 为什么在这里（`useApi()` 调用时）捕获一次，而不是在 `apiFetch` 里现取：
   * `useRequestFetch()` 依赖当前 Nuxt 实例的 `ssrContext.event`，**只能在 setup /
   * 请求上下文里调用**。`apiFetch` 完全可能在 setup 之外被调用（事件回调、
   * `watch` / `setInterval` 回调、`useAsyncData` 的 handler 等），那时再调就取不到了。
   * 在 `useApi()` 时把实例钉住，之后无论从哪个回调里调 `apiFetch` 都能用上。
   *
   * 拿不到请求上下文（例如 `useApi()` 本身就在 setup 之外被调用、或静态渲染）
   * 时静默回退到普通 `$fetch`：只是这一次没有登录态，绝不抛错、也绝不改变调用方
   * 能观察到的行为（`$fetch` 的异常照旧向上抛）。
   */
  let requestFetch: typeof $fetch | null = null
  if (!import.meta.client) {
    try {
      requestFetch = useRequestFetch() as unknown as typeof $fetch
    } catch {
      requestFetch = null
    }
  }

  async function apiFetch<T>(path: string, options: Parameters<typeof $fetch>[1] = {}) {
    // 签名 / 返回类型 / 异常行为均不变：SSR 用带上下文的 fetch，浏览器用 $fetch。
    // 外层 withApiCache：同 key in-flight 合并 + 只读短缓存；写成功后按前缀失效。
    return withApiCache(path, options, () => {
      const fetcher = requestFetch ?? $fetch
      return fetcher<T>(path, {
        baseURL,
        // 会话是后端下发的 HttpOnly Cookie：不带 credentials 的话登录「看起来成功」，
        // 但后续每个请求仍是匿名。放在 ...options 之前，调用方显式传值时仍可覆盖。
        credentials: 'include',
        ...options,
      })
    })
  }

  return {
    baseURL,
    apiFetch,

    health: () => apiFetch<{ status: string }>('/api/health'),

    getSettings: () => apiFetch<LotterySettings>('/api/settings'),

    updateSettings: (patch: LotterySettingsPatch) =>
      apiFetch<LotterySettings>('/api/settings', {
        method: 'PUT',
        body: patch,
      }),

    /**
     * 生成候选号码。
     * - ``mode``：临场切换筹码模式（不写默认值）；
     * - ``bet_count``：临场注数（1–10，不落库；覆盖 settings.pick_count）；
     * - ``amount_seed``：随机分配的**重掷种子**（不传时后端按「期」+ 参数确定性派生，
     *   所以同一期重复请求结果一致；传了才得到另一份分配）；
     * - ``total_amount`` / ``amount_unit``：临场试算覆盖项（可选，不落库）；
     * - ``trend_bias`` / ``trend_window``：走势加权临场预览（可选，不落库）。
     */
    recommend: (
      options: {
        mode?: ChipMode
        number?: number
        bet_count?: number
        amount_seed?: number | string
        total_amount?: number
        amount_unit?: number
        trend_bias?: TrendBias
        trend_window?: number
      } = {},
    ) =>
      apiFetch<RecommendResult>('/api/recommend', {
        method: 'POST',
        body: options,
      }),

    // 开奖总表（整期批量导入，落库只保留特码）
    importDraws: (text: string, replaceExisting = true) =>
      apiFetch<DrawImportResult>('/api/draws/import', {
        method: 'POST',
        body: { text, replace_existing: replaceExisting },
      }),

    listDraws: (limit?: number, offset?: number) =>
      apiFetch<DrawItem[]>('/api/draws', {
        query: {
          ...(limit ? { limit } : {}),
          ...(offset ? { offset } : {}),
        },
      }),

    latestDraw: () => apiFetch<DrawItem | null>('/api/draws/latest'),

    /**
     * 下一期的建议期号 / 日期（录入页预填用）。
     * 与 POST /api/draws/quick 的缺省口径同源：期号 = max(period)+1、日期 = 今天 UTC+8。
     */
    nextPeriod: () => apiFetch<NextPeriod>('/api/draws/next-period'),

    /**
     * 单号录入（开奖号录入页专用）。
     * period / draw_date 留空即交给后端自动补全；同 period 重复提交 = upsert（修正录错）。
     */
    quickAddDraw: (payload: QuickDrawPayload) =>
      apiFetch<DrawItem>('/api/draws/quick', {
        method: 'POST',
        body: {
          ...(payload.period ? { period: payload.period } : {}),
          ...(payload.draw_date ? { draw_date: payload.draw_date } : {}),
          special_number: payload.special_number,
        },
      }),

    /**
     * 纠正已保存开奖：覆盖特码（可改日期），并重算该期采用快照盈亏。
     * 回执含新旧特码与 resettled_rounds。
     */
    correctDraw: (drawId: number, payload: DrawCorrectPayload) =>
      apiFetch<DrawCorrectResult>(`/api/draws/${drawId}/correct`, {
        method: 'POST',
        body: {
          special_number: payload.special_number,
          ...(payload.draw_date ? { draw_date: payload.draw_date } : {}),
        },
      }),

    deleteDraw: (id: number) =>
      apiFetch<{ deleted: number }>(`/api/draws/${id}`, { method: 'DELETE' }),

    clearDraws: () =>
      apiFetch<{ deleted: number }>('/api/draws', { method: 'DELETE' }),

    // 生肖表（农历年口径的 49 号码关联表，只读）
    zodiacTable: (date?: string) =>
      apiFetch<ZodiacTable>('/api/zodiac/table', {
        query: date ? { date } : undefined,
      }),

    zodiacYears: () => apiFetch<ZodiacYear[]>('/api/zodiac/years'),

    zodiacYearNumbers: (lunarYear: number) =>
      apiFetch<ZodiacYearNumber[]>(`/api/zodiac/years/${lunarYear}/numbers`),

    /* ---------------- 认证（会话走 HttpOnly Cookie，见 apiFetch 的 credentials） ---------------- */

    /** 公开配置：灰度开关 + 角色 / 状态枚举来源 */
    authConfig: () => apiFetch<AuthConfig>('/api/auth/config'),

    /**
     * 提交注册申请。**恒返回 200 + 同一句话**（不暴露手机号是否已注册），
     * 所以前端不许拿返回值判断「这个号是不是已经有过」。
     */
    authRegister: (payload: { phone: string; password: string }) =>
      apiFetch<RegisterResult>('/api/auth/register', {
        method: 'POST',
        body: payload,
      }),

    /** 登录成功会下发会话 Cookie；失败 401（口令错）/ 403（账号状态问题） */
    authLogin: (payload: { phone: string; password: string }) =>
      apiFetch<LoginResult>('/api/auth/login', {
        method: 'POST',
        body: payload,
      }),

    /** 当前登录用户；未登录 → 401（这是正常状态，调用方按「已登出」处理） */
    authMe: () => apiFetch<AuthMe>('/api/auth/me'),

    /** 登出（幂等：未登录调用同样 200） */
    authLogout: () => apiFetch<{ ok: boolean }>('/api/auth/logout', { method: 'POST' }),

    /* ---------------- 管理后台（仅 ADMIN） ---------------- */

    /** 用户列表（最新注册在前）；status 留空 = 全部。取值见 AuthConfig.statuses */
    adminListUsers: (status?: string) =>
      apiFetch<AuthUser[]>('/api/admin/users', {
        query: status ? { status } : undefined,
      }),

    /** 审批通过（任意状态 → APPROVED，幂等） */
    adminApproveUser: (phone: string) =>
      apiFetch<AuthUser>(`/api/admin/users/${encodeURIComponent(phone)}/approve`, {
        method: 'POST',
      }),

    /** 拒绝注册申请（任意状态 → REJECTED，幂等） */
    adminRejectUser: (phone: string) =>
      apiFetch<AuthUser>(`/api/admin/users/${encodeURIComponent(phone)}/reject`, {
        method: 'POST',
      }),

    /** 变更角色（USER / VIP / ADMIN），受「防管理员自我锁死」护栏约束（可能 409） */
    adminSetUserRole: (phone: string, role: Role | string) =>
      apiFetch<AuthUser>(`/api/admin/users/${encodeURIComponent(phone)}/role`, {
        method: 'POST',
        body: { role },
      }),

    /** 通用状态变更（PENDING / APPROVED / REJECTED / DISABLED），同样可能 409 */
    adminSetUserStatus: (phone: string, status: UserStatus | string) =>
      apiFetch<AuthUser>(`/api/admin/users/${encodeURIComponent(phone)}/status`, {
        method: 'POST',
        body: { status },
      }),
  }
}
