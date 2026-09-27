/* ==========================================================================
 * 全局路由门禁 —— 访客可读公开数据页；个人能力页要登录；再按 meta.role 校验。
 * --------------------------------------------------------------------------
 * 公开路径（无需登录，只读浏览）：
 *   /、/login、/register、/legal/*
 *   /zodiac、/history
 *   /stats/trend、/stats/frequency、/stats/zodiac-trend
 *
 * 必须登录（再按角色）：
 *   /settings（USER+）
 *   /recommend、/stats/pnl、/stats/backtest（VIP+）
 *   /draws、/entry、/admin/*（ADMIN）
 *
 * 与后端 `AUTH_ENFORCED` 解耦：
 *   - UI **始终**按登录态 / 角色收口；
 *   - `AUTH_ENFORCED=false` 只是后端 API 开发旁路，不放开前端菜单与路由。
 *
 * 页面声明最低角色（英文枚举 USER < VIP < ADMIN）：
 *   definePageMeta({ role: 'VIP' })
 *   definePageMeta({ role: 'ADMIN' })
 * 未声明 role 的非公开页：只要求已登录。
 * ========================================================================== */

/** 无需登录即可进入的路径前缀 / 精确路径 */
const PUBLIC_EXACT = new Set([
  '/login',
  '/register',
  '/zodiac',
  '/history',
  '/stats/trend',
  '/stats/frequency',
  '/stats/zodiac-trend',
])
const PUBLIC_PREFIXES = ['/legal']

function isPublicPath(path: string): boolean {
  if (path === '/' || PUBLIC_EXACT.has(path)) return true
  return PUBLIC_PREFIXES.some(
    prefix => path === prefix || path.startsWith(`${prefix}/`),
  )
}

export default defineNuxtRouteMiddleware(async (to) => {
  const { ensureLoaded, isLoggedIn, hasRole } = useAuth()

  await ensureLoaded()

  // 公开页：访客可进（不强制登录墙）
  if (isPublicPath(to.path)) return

  if (!isLoggedIn.value) {
    return navigateTo(
      { path: '/login', query: { redirect: to.fullPath } },
      { replace: true },
    )
  }

  const meta = to.meta as Record<string, unknown>
  const minimum = typeof meta.role === 'string' ? meta.role : ''

  if (minimum && !hasRole(minimum)) {
    return abortNavigation(
      createError({
        statusCode: 403,
        statusMessage: '权限不足',
        message: `该功能仅限${minimum}及以上角色使用`,
        fatal: false,
      }),
    )
  }
})
