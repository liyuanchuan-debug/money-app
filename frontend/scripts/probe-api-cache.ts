/**
 * 探针：验证 in-flight 去重、GET 短缓存、写后失效。
 * 用法：npx --yes jiti scripts/probe-api-cache.ts
 */
import {
  __setApiCacheStoreForTests,
  buildApiCacheKey,
  createApiCacheStore,
  getApiCacheStats,
  invalidateApiCache,
  withApiCache,
} from '../utils/apiCache'

function assert(cond: unknown, msg: string): asserts cond {
  if (!cond) throw new Error(msg)
}

async function main() {
  const store = createApiCacheStore()
  __setApiCacheStoreForTests(store)

  let upstreamCalls = 0
  let settingsValue = { total_amount: 50 }

  const fakeSettings = () =>
    withApiCache('/api/settings', {}, async () => {
      upstreamCalls += 1
      await new Promise(r => setTimeout(r, 40))
      return { ...settingsValue }
    })

  // 1) 并发两次 getSettings → 只打 1 次 upstream
  const [a, b] = await Promise.all([fakeSettings(), fakeSettings()])
  assert(a.total_amount === 50 && b.total_amount === 50, 'settings value mismatch')
  assert(upstreamCalls === 1, `concurrent dedupe expected upstream=1, got ${upstreamCalls}`)
  const afterDedupe = getApiCacheStats()
  assert(afterDedupe.dedupe >= 1, `expected dedupe>=1, got ${afterDedupe.dedupe}`)

  // 2) TTL 内再次 GET → hit，不再打 upstream
  const c = await fakeSettings()
  assert(c.total_amount === 50, 'cached settings mismatch')
  assert(upstreamCalls === 1, `TTL hit expected upstream still 1, got ${upstreamCalls}`)
  const afterHit = getApiCacheStats()
  assert(afterHit.hit >= 1, `expected hit>=1, got ${afterHit.hit}`)

  // 3) PUT settings 后缓存失效，下次 GET 重新打
  await withApiCache('/api/settings', { method: 'PUT', body: { total_amount: 80 } }, async () => {
    upstreamCalls += 1
    settingsValue = { total_amount: 80 }
    return { ...settingsValue }
  })
  const d = await fakeSettings()
  assert(d.total_amount === 80, 'after write expected fresh 80')
  assert(upstreamCalls === 3, `after invalidate expected upstream=3 (1+put+1), got ${upstreamCalls}`)

  // 4) 两份 store 互不串（模拟 SSR per-request）
  const storeA = createApiCacheStore()
  const storeB = createApiCacheStore()
  __setApiCacheStoreForTests(storeA)
  await withApiCache('/api/auth/me', {}, async () => ({ phone: '111' }))
  __setApiCacheStoreForTests(storeB)
  await withApiCache('/api/auth/me', {}, async () => ({ phone: '222' }))
  assert(storeA.cache.size === 1 && storeB.cache.size === 1, 'each store should have its own entry')
  const phoneA = (storeA.cache.values().next().value as { value: { phone: string } }).value.phone
  const phoneB = (storeB.cache.values().next().value as { value: { phone: string } }).value.phone
  assert(phoneA === '111' && phoneB === '222', 'SSR stores must not leak across requests')

  // 5) key 规范化：query 顺序无关
  const k1 = buildApiCacheKey('GET', '/api/draws', { limit: 200, offset: 0 })
  const k2 = buildApiCacheKey('GET', '/api/draws', { offset: 0, limit: 200 })
  assert(k1 === k2, 'sorted query keys must match')

  // 6) invalidate 前缀
  __setApiCacheStoreForTests(store)
  store.cache.clear()
  store.stats = { upstream: 0, hit: 0, dedupe: 0 }
  await withApiCache('/api/draws/latest', {}, async () => ({ id: 1 }))
  await withApiCache('/api/settings', {}, async () => ({ ok: true }))
  invalidateApiCache('draws')
  assert([...store.cache.keys()].every(k => !k.includes('/api/draws')), 'draws prefix cleared')
  assert([...store.cache.keys()].some(k => k.includes('/api/settings')), 'settings kept')

  __setApiCacheStoreForTests(null)

  console.log(JSON.stringify({
    ok: true,
    concurrent_getSettings_upstream: 1,
    ttl_second_get_upstream: 1,
    after_put_get_upstream: 3,
    ssr_isolation: 'pass',
    query_key_stable: true,
    invalidate_prefix: 'pass',
  }, null, 2))
}

main().catch((err) => {
  console.error(err)
  process.exit(1)
})
