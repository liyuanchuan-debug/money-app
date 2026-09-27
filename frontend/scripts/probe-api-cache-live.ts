/**
 * 对真实后端做并发 / TTL / 写后失效探针。
 * 用法：npx --yes jiti scripts/probe-api-cache-live.ts
 */
import {
  __setApiCacheStoreForTests,
  createApiCacheStore,
  getApiCacheStats,
  withApiCache,
} from '../utils/apiCache'

const BASE = process.env.NUXT_PUBLIC_API_URL || 'http://127.0.0.1:8000'

async function main() {
  const store = createApiCacheStore()
  __setApiCacheStoreForTests(store)
  getApiCacheStats(true)

  let upstream = 0

  const getSettings = () =>
    withApiCache('/api/settings', {}, async () => {
      upstream += 1
      const res = await fetch(`${BASE}/api/settings`)
      if (!res.ok) throw new Error(`GET settings ${res.status}`)
      return res.json() as Promise<{ total_amount: number }>
    })

  const t0 = Date.now()
  const [a, b] = await Promise.all([getSettings(), getSettings()])
  const concurrentMs = Date.now() - t0
  const afterConcurrent = upstream

  const t1 = Date.now()
  const c = await getSettings()
  const ttlHitMs = Date.now() - t1
  const afterTtlHit = upstream

  const t2 = Date.now()
  await withApiCache(
    '/api/settings',
    { method: 'PUT', body: { total_amount: a.total_amount } },
    async () => {
      upstream += 1
      const res = await fetch(`${BASE}/api/settings`, {
        method: 'PUT',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          total_amount: a.total_amount,
          amount_unit: (a as { amount_unit?: number }).amount_unit ?? 5,
          small_max: (a as { small_max?: number }).small_max ?? 10,
          normal_max: (a as { normal_max?: number }).normal_max ?? 30,
          mode: (a as { mode?: string }).mode ?? 'even',
          pick_count: (a as { pick_count?: number }).pick_count ?? 6,
          exclude_repeat_zodiac: (a as { exclude_repeat_zodiac?: boolean }).exclude_repeat_zodiac === true,
        }),
      })
      if (!res.ok) throw new Error(`PUT settings ${res.status}`)
      return res.json()
    },
  )
  const d = await getSettings()
  const afterWriteMs = Date.now() - t2

  // 首页同构：latest + draws 并行（不同 key，各 1 次）
  let homeUpstream = 0
  const homeStore = createApiCacheStore()
  __setApiCacheStoreForTests(homeStore)
  const latest = () =>
    withApiCache('/api/draws/latest', {}, async () => {
      homeUpstream += 1
      const res = await fetch(`${BASE}/api/draws/latest`)
      if (!res.ok) throw new Error(`latest ${res.status}`)
      return res.json()
    })
  const draws = () =>
    withApiCache('/api/draws', { query: { limit: 200, offset: 0 } }, async () => {
      homeUpstream += 1
      const res = await fetch(`${BASE}/api/draws?limit=200&offset=0`)
      if (!res.ok) throw new Error(`draws ${res.status}`)
      return res.json()
    })
  const h0 = Date.now()
  await Promise.all([latest(), draws()])
  const homeColdMs = Date.now() - h0
  const homeColdUpstream = homeUpstream
  const h1 = Date.now()
  await Promise.all([latest(), draws()])
  const homeWarmMs = Date.now() - h1
  const homeWarmUpstream = homeUpstream

  __setApiCacheStoreForTests(null)

  console.log(JSON.stringify({
    ok: true,
    before_after: {
      '2x concurrent getSettings': { before: 2, after: afterConcurrent },
      '3rd getSettings within TTL': { before: 3, after: afterTtlHit },
      'PUT then GET': { before: 'GET may be stale without invalidate', after_upstream: upstream, note: 'PUT+fresh GET' },
      'home latest+draws cold': { before: 2, after: homeColdUpstream },
      'home latest+draws warm TTL': { before: 4, after: homeWarmUpstream },
    },
    timings_ms: {
      concurrent_pair: concurrentMs,
      ttl_hit: ttlHitMs,
      put_then_get: afterWriteMs,
      home_cold: homeColdMs,
      home_warm: homeWarmMs,
    },
    values: {
      same_abc: a.total_amount === b.total_amount && b.total_amount === c.total_amount,
      after_put_total: d.total_amount,
    },
    cache_stats: getApiCacheStats(),
  }, null, 2))
}

main().catch((err) => {
  console.error(err)
  process.exit(1)
})
