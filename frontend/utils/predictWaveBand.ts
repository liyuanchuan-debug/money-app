/**
 * 与后端 ``services.lottery.predict_wave_band`` / ``replay_predict_wave_band`` 同口径：
 * 最新在前；window=0 用全部；不足两期返回 null。
 */

export interface WaveBand {
  low: number
  high: number
  center: number
  samples: number
}

export interface WaveBandReplay {
  /** 最新在前的目标期下标（被预测、已有实开的那一期） */
  targetIndex: number
  anchor: number
  actual: number
  diff: number
  hit: boolean
  band: WaveBand
  targetPeriod: number
  anchorPeriod: number
}

function quantile(sorted: number[], q: number): number {
  if (!sorted.length) return Number.NaN
  const pos = (sorted.length - 1) * q
  const base = Math.floor(pos)
  const rest = pos - base
  const next = sorted[base + 1]
  if (next === undefined) return sorted[base]!
  return sorted[base]! + rest * (next - sorted[base]!)
}

/** ``historyNewestFirst``：下标 0 = 最新一期特码 */
export function predictWaveBand(
  historyNewestFirst: number[],
  window = 0,
): WaveBand | null {
  let series = historyNewestFirst.map(n => Math.trunc(n)).filter(n => Number.isFinite(n))
  const limit = Math.trunc(window || 0)
  if (limit > 0) series = series.slice(0, limit)
  if (series.length < 2) return null
  const diffs: number[] = []
  for (let i = 1; i < series.length; i += 1) {
    diffs.push(Math.abs(series[i]! - series[i - 1]!))
  }
  if (!diffs.length) return null
  diffs.sort((a, b) => a - b)
  return {
    center: Math.round(quantile(diffs, 0.5) * 100) / 100,
    low: Math.round(quantile(diffs, 0.25) * 100) / 100,
    high: Math.round(quantile(diffs, 0.75) * 100) / 100,
    samples: diffs.length,
  }
}

/**
 * 回放「预测第 ``targetIndex`` 期」当时的带（最新在前）。
 * 只用 ``history[targetIndex+1:]``；锚点 = ``history[targetIndex+1]``；实开 = ``history[targetIndex]``。
 */
export function replayWaveBandAt(
  numbersNewestFirst: number[],
  periodsNewestFirst: number[],
  targetIndex: number,
  window = 0,
): WaveBandReplay | null {
  const index = Math.trunc(targetIndex)
  if (index < 0) return null
  if (numbersNewestFirst.length < index + 3 || periodsNewestFirst.length < index + 3) return null
  const actual = Math.trunc(numbersNewestFirst[index]!)
  const anchor = Math.trunc(numbersNewestFirst[index + 1]!)
  const band = predictWaveBand(numbersNewestFirst.slice(index + 1), window)
  if (!band) return null
  const diff = Math.abs(actual - anchor)
  const hit = band.low <= diff && diff <= band.high
  return {
    targetIndex: index,
    anchor,
    actual,
    diff,
    hit,
    band,
    targetPeriod: Math.trunc(periodsNewestFirst[index]!),
    anchorPeriod: Math.trunc(periodsNewestFirst[index + 1]!),
  }
}

/**
 * 可回放的最大「往前步数」：offset=1 看最近已开奖期，offset 越大越早。
 * 需要目标期 + 锚点 + 至少再 1 期估带 → 最多 length-2 步。
 */
export function maxReplayOffset(sampleLength: number): number {
  return Math.max(0, Math.trunc(sampleLength) - 2)
}

export interface LatticeBandHitRow {
  targetIndex: number
  period: number | null
  anchor: number
  actual: number
  diff: number
  hit: boolean
  low: number
  high: number
}

export interface LatticeBandHitStats {
  dataStatus: 'OK' | 'INSUFFICIENT'
  sampleSize: number
  evaluated: number
  hits: number
  misses: number
  hitRate: number | null
  latticeWindow: number
  latest: LatticeBandHitRow | null
  recent: LatticeBandHitRow[]
}

/** 本池 walk-forward：实开差值落在当时预测带的命中统计（与后端同口径）。 */
export function latticeBandHitStats(
  numbersNewestFirst: number[],
  periodsNewestFirst: Array<number | null | undefined> = [],
  window = 0,
  recentLimit = 20,
): LatticeBandHitStats {
  const series = numbersNewestFirst.map(n => Math.trunc(n))
  const periods = series.map((_, index) => {
    const raw = periodsNewestFirst[index]
    return raw == null || !Number.isFinite(Number(raw)) ? null : Math.trunc(Number(raw))
  })
  const recentCap = Math.max(0, Math.trunc(recentLimit))
  const rows: LatticeBandHitRow[] = []
  for (let targetIndex = 0; targetIndex < Math.max(0, series.length - 2); targetIndex += 1) {
    const available = series.slice(targetIndex + 1)
    if (available.length < 2) continue
    const band = predictWaveBand(available, window)
    if (!band) continue
    const anchor = available[0]!
    const actual = series[targetIndex]!
    const diff = Math.abs(actual - anchor)
    const hit = band.low <= diff && diff <= band.high
    rows.push({
      targetIndex,
      period: periods[targetIndex] ?? null,
      anchor,
      actual,
      diff,
      hit,
      low: band.low,
      high: band.high,
    })
  }
  const evaluated = rows.length
  const hits = rows.reduce((sum, row) => sum + (row.hit ? 1 : 0), 0)
  return {
    dataStatus: evaluated > 0 ? 'OK' : 'INSUFFICIENT',
    sampleSize: series.length,
    evaluated,
    hits,
    misses: evaluated - hits,
    hitRate: evaluated > 0 ? hits / evaluated : null,
    latticeWindow: Math.trunc(window || 0),
    latest: rows[0] ?? null,
    recent: rows.slice(0, recentCap),
  }
}

export function formatBandHitRate(rate: number | null | undefined): string {
  if (rate == null || !Number.isFinite(rate)) return '—'
  return `${(rate * 100).toFixed(1)}%`
}

/** @deprecated 等价于 ``replayWaveBandAt(..., 0, window)`` */
export function replayPreviousWaveBand(
  numbersNewestFirst: number[],
  periodsNewestFirst: number[],
  window = 0,
): WaveBandReplay | null {
  return replayWaveBandAt(numbersNewestFirst, periodsNewestFirst, 0, window)
}
