/**
 * 传输层 in-flight 去重 + 短时 GET 内存缓存。
 *
 * SSR 铁律：服务端缓存挂在当前 NuxtApp 上（每请求一份），禁止模块级 Map 跨请求串数据。
 * 浏览器端可用模块级 store（单页 = 单用户）。
 */

export type ApiFetchOptions = Parameters<typeof $fetch>[1]

interface CacheEntry {
  expiresAt: number
  value: unknown
}

export interface ApiCacheStore {
  cache: Map<string, CacheEntry>
  inflight: Map<string, Promise<unknown>>
  /** 探针计数：upstream = 真正发出去的次数；hit / dedupe 分别是缓存命中与合并 */
  stats: {
    upstream: number
    hit: number
    dedupe: number
  }
}

const CLIENT_STORE_KEY = '__waveApiCache'

export function createApiCacheStore(): ApiCacheStore {
  return {
    cache: new Map(),
    inflight: new Map(),
    stats: { upstream: 0, hit: 0, dedupe: 0 },
  }
}

/** 浏览器端单例（模块级只在 client 用，绝不在服务端跨请求共享） */
let clientStore: ApiCacheStore | null = null

/** 探针 / 单测可注入；生产路径保持 null */
let storeOverride: ApiCacheStore | null = null

/** @internal 仅探针与单测 */
export function __setApiCacheStoreForTests(store: ApiCacheStore | null): void {
  storeOverride = store
  if (!store) clientStore = null
}

/**
 * 取当前请求/页面对应的 cache store。
 * - client：模块级单例
 * - server：挂在 NuxtApp（per-request）；拿不到上下文时退回一次性 ephemeral store（不串数据）
 */
export function getApiCacheStore(): ApiCacheStore {
  if (storeOverride) return storeOverride

  if (import.meta.client) {
    if (!clientStore) clientStore = createApiCacheStore()
    return clientStore
  }

  try {
    const app = useNuxtApp() as unknown as Record<string, unknown>
    const existing = app[CLIENT_STORE_KEY] as ApiCacheStore | undefined
    if (existing) return existing
    const created = createApiCacheStore()
    app[CLIENT_STORE_KEY] = created
    return created
  } catch {
    // 没有 Nuxt 上下文：不做跨调用共享，避免误用成模块级全局
    return createApiCacheStore()
  }
}

/** 稳定序列化：对象键排序，保证同一 query/body 得到同一 key */
function stableStringify(value: unknown): string {
  if (value === undefined) return ''
  if (value === null) return 'null'
  if (typeof value !== 'object') return JSON.stringify(value)
  if (Array.isArray(value)) {
    return `[${value.map(item => stableStringify(item)).join(',')}]`
  }
  const record = value as Record<string, unknown>
  const keys = Object.keys(record).sort()
  return `{${keys.map(key => `${JSON.stringify(key)}:${stableStringify(record[key])}`).join(',')}}`
}

function normalizePath(path: string): string {
  const trimmed = String(path || '').trim()
  if (!trimmed) return '/'
  // 去掉 origin，只留 pathname + 已有 search（query 对象另算）
  try {
    if (/^https?:\/\//i.test(trimmed)) {
      const url = new URL(trimmed)
      return `${url.pathname}${url.search}` || '/'
    }
  } catch {
    /* 非 URL 字符串按 path 处理 */
  }
  return trimmed.startsWith('/') ? trimmed : `/${trimmed}`
}

function sortedQueryString(query: unknown): string {
  if (!query || typeof query !== 'object') return ''
  const record = query as Record<string, unknown>
  const parts: string[] = []
  for (const key of Object.keys(record).sort()) {
    const raw = record[key]
    if (raw === undefined || raw === null) continue
    parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(String(raw))}`)
  }
  return parts.join('&')
}

export function buildApiCacheKey(
  method: string,
  path: string,
  query?: unknown,
  body?: unknown,
): string {
  const m = (method || 'GET').toUpperCase()
  const p = normalizePath(path)
  const q = sortedQueryString(query)
  const b = body === undefined ? '' : stableStringify(body)
  return `${m}|${p}|${q}|${b}`
}

/** 只读 GET 的 TTL（ms）；0 = 不缓存结果（仍可 in-flight 去重） */
export function resolveGetTtlMs(path: string): number {
  const p = normalizePath(path).split('?')[0] || ''

  if (p === '/api/auth/config') return 60_000
  if (p === '/api/auth/me') return 15_000

  if (p === '/api/settings') return 20_000
  if (p.startsWith('/api/draws')) return 20_000

  // stats 只读端点；backtest 是 POST，不会走到这里
  if (p.startsWith('/api/stats/')) return 20_000

  if (p.startsWith('/api/zodiac')) return 60_000
  if (p === '/api/health') return 10_000
  if (p.startsWith('/api/admin/')) return 10_000

  return 0
}

/** POST recommend / backtest：只去重不缓存 */
export function isDedupeOnlyWrite(method: string, path: string): boolean {
  const m = (method || 'GET').toUpperCase()
  if (m !== 'POST') return false
  const p = normalizePath(path).split('?')[0] || ''
  return p === '/api/recommend' || p === '/api/stats/backtest'
}

/**
 * 写成功后按前缀失效。
 * prefix 匹配 cache key 里的 path 段（如 'draws' → `/api/draws...`）。
 */
export function invalidateApiCache(prefix?: string): void {
  const store = getApiCacheStore()
  if (!prefix) {
    store.cache.clear()
    return
  }
  const needle = prefix.startsWith('/') ? prefix : `/api/${prefix}`
  for (const key of store.cache.keys()) {
    // key = METHOD|path|query|body
    const pathPart = key.split('|')[1] || ''
    if (pathPart === needle || pathPart.startsWith(`${needle}/`) || pathPart.startsWith(`${needle}?`)) {
      store.cache.delete(key)
    }
  }
}

/** 根据写接口路径决定失效哪些前缀 */
export function invalidateAfterWrite(method: string, path: string): void {
  const m = (method || 'GET').toUpperCase()
  if (m === 'GET' || m === 'HEAD') return
  if (isDedupeOnlyWrite(m, path)) return

  const p = normalizePath(path).split('?')[0] || ''

  if (p.startsWith('/api/draws')) {
    invalidateApiCache('draws')
    invalidateApiCache('stats')
    invalidateApiCache('history')
    return
  }
  if (p.startsWith('/api/settings')) {
    invalidateApiCache('settings')
    invalidateApiCache('stats')
    return
  }
  if (p.startsWith('/api/auth') || p.startsWith('/api/admin')) {
    invalidateApiCache('auth')
    invalidateApiCache('admin')
  }
}

export function dedupedFetch<T>(key: string, factory: () => Promise<T>): Promise<T> {
  const store = getApiCacheStore()
  const existing = store.inflight.get(key)
  if (existing) {
    store.stats.dedupe += 1
    return existing as Promise<T>
  }

  const promise = (async () => {
    store.stats.upstream += 1
    return await factory()
  })().finally(() => {
    if (store.inflight.get(key) === promise) {
      store.inflight.delete(key)
    }
  })

  store.inflight.set(key, promise)
  return promise
}

export function cachedGet<T>(key: string, ttlMs: number, factory: () => Promise<T>): Promise<T> {
  const store = getApiCacheStore()
  if (ttlMs > 0) {
    const hit = store.cache.get(key)
    if (hit && hit.expiresAt > Date.now()) {
      store.stats.hit += 1
      return Promise.resolve(hit.value as T)
    }
  }

  return dedupedFetch(key, async () => {
    const value = await factory()
    if (ttlMs > 0) {
      store.cache.set(key, { value, expiresAt: Date.now() + ttlMs })
    }
    return value
  })
}

/**
 * 统一入口：GET 短缓存 + 全方法 in-flight 去重；写成功后自动失效相关前缀。
 * `factory` 必须是真正打后端的那段（含 credentials / useRequestFetch）。
 */
export function withApiCache<T>(
  path: string,
  options: ApiFetchOptions | undefined,
  factory: () => Promise<T>,
): Promise<T> {
  const method = String((options as { method?: string } | undefined)?.method || 'GET').toUpperCase()
  const query = (options as { query?: unknown } | undefined)?.query
  const body = (options as { body?: unknown } | undefined)?.body
  const key = buildApiCacheKey(method, path, query, body)

  if (method === 'GET' || method === 'HEAD') {
    const ttl = resolveGetTtlMs(path)
    if (ttl > 0) return cachedGet(key, ttl, factory)
    return dedupedFetch(key, factory)
  }

  // recommend / backtest：只合并并发，不落结果缓存
  if (isDedupeOnlyWrite(method, path)) {
    return dedupedFetch(key, factory)
  }

  // 其它写操作：去重 + 成功后失效
  return dedupedFetch(key, async () => {
    const value = await factory()
    invalidateAfterWrite(method, path)
    return value
  })
}

/** 探针用：读计数并可选重置 */
export function getApiCacheStats(reset = false): ApiCacheStore['stats'] {
  const store = getApiCacheStore()
  const snapshot = { ...store.stats }
  if (reset) {
    store.stats.upstream = 0
    store.stats.hit = 0
    store.stats.dedupe = 0
  }
  return snapshot
}
