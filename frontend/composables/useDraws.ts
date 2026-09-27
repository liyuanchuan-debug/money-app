/* ==========================================================================
 * 开奖记录的展示层归一化（防御性解析）
 * --------------------------------------------------------------------------
 * GET /api/draws、GET /api/draws/latest、POST /api/draws/quick 返回的是同一个
 * 「装饰后的开奖对象」，但：
 *   - 生肖可能只给中文标签（zodiac_label）或只给英文码（zodiac）；
 *   - draw_date 可能是 'YYYY-MM-DD'，也可能是完整 ISO 串；
 *   - 理论上不该缺的字段一旦缺失，页面不能白屏。
 * 所以页面只消费这里归一化后的 NormalizedDraw：缺字段一律降级为占位符，不抛异常。
 *
 * 口径提醒：本文件里的数字全部来自「本池已导入」的开奖记录，
 * 任何统计展示都必须带上 scopeLabel(n)，禁止升格成越界口径。
 * ========================================================================== */

export interface NormalizedDraw {
  id: number | null
  /** YYYY-MM-DD；拿不到时为空串 */
  draw_date: string
  period: number | null
  /** 1 - 49 */
  special_number: number
  /** 英文生肖码（可能为空串） */
  zodiac: string
  /** 中文生肖标签（可能为空串） */
  zodiac_label: string
}

/** 号码补零（默认 2 位，即 01 - 49）；非法值返回占位符 */
export function padNumber(value: number | string | null | undefined, width = 2): string {
  if (value === null || value === undefined || value === '') return '—'
  const numeric = Number(value)
  if (!Number.isFinite(numeric)) return '—'
  const text = String(Math.trunc(Math.abs(numeric)))
  return width > 0 ? text.padStart(width, '0') : text
}

/** Date → 本地时区的 YYYY-MM-DD（不能用 toISOString：会按 UTC 差一天） */
export function toLocalDate(value: Date): string {
  const year = value.getFullYear()
  const month = String(value.getMonth() + 1).padStart(2, '0')
  const day = String(value.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

/** 今天（本地时区）的 YYYY-MM-DD，用作录入页的默认日期 */
export function todayLocal(): string {
  return toLocalDate(new Date())
}

/** 日期归一化：'YYYY-MM-DD' / 'YYYY-MM-DDTHH:mm:ss' / ISO / Date → 'YYYY-MM-DD' */
export function normalizeDate(raw: unknown): string {
  if (raw instanceof Date) {
    return Number.isNaN(raw.getTime()) ? '' : toLocalDate(raw)
  }
  if (typeof raw !== 'string' || !raw.trim()) return ''
  const text = raw.trim()
  const matched = text.match(/^(\d{4}-\d{2}-\d{2})/)
  if (matched) return matched[1]
  const parsed = new Date(text)
  return Number.isNaN(parsed.getTime()) ? text : toLocalDate(parsed)
}

/**
 * 生肖展示文案：优先中文标签 zodiac_label，退到英文码 zodiac，都没有则空串。
 * 页面不要直接读 zodiac_label，否则字段缺失时会渲染出 "undefined"。
 */
export function zodiacText(draw: {
  zodiac?: unknown
  zodiac_label?: unknown
} | null | undefined): string {
  if (!draw) return ''
  const label = typeof draw.zodiac_label === 'string' ? draw.zodiac_label.trim() : ''
  if (label) return label
  return typeof draw.zodiac === 'string' ? draw.zodiac.trim() : ''
}

/** 把可能缺失 / 为空串的字段转成整数；拿不到就返回 null（不要把 null 变成 0） */
function toIntOrNull(raw: unknown): number | null {
  if (raw === null || raw === undefined || raw === '') return null
  if (typeof raw !== 'number' && typeof raw !== 'string') return null
  const value = Number(raw)
  return Number.isFinite(value) ? Math.trunc(value) : null
}

/** 把任意后端开奖对象归一化；号码非法（缺失 / 越界 / 类型不对）时返回 null */
export function normalizeDraw(raw: unknown): NormalizedDraw | null {
  if (!raw || typeof raw !== 'object') return null
  const source = raw as Record<string, unknown>

  const special = source.special_number
  if (typeof special !== 'number' && typeof special !== 'string') return null
  const number = Number(special)
  if (!Number.isFinite(number) || number < 1 || number > 49) return null

  return {
    id: toIntOrNull(source.id),
    draw_date: normalizeDate(source.draw_date),
    period: toIntOrNull(source.period),
    special_number: Math.trunc(number),
    zodiac: typeof source.zodiac === 'string' ? source.zodiac : '',
    zodiac_label: typeof source.zodiac_label === 'string' ? source.zodiac_label : '',
  }
}

/** 批量归一化，并丢掉无法识别的条目（不因为一条脏数据整页崩掉） */
export function normalizeDraws(raw: unknown): NormalizedDraw[] {
  if (!Array.isArray(raw)) return []
  const result: NormalizedDraw[] = []
  for (const item of raw) {
    const draw = normalizeDraw(item)
    if (draw) result.push(draw)
  }
  return result
}

/**
 * 统计口径文案（全站唯一允许的口径措辞）。
 * 任何统计图形 / 数字旁边都必须出现它。
 */
export function scopeLabel(sampleSize: number): string {
  const size = Number.isFinite(Number(sampleSize)) ? Math.max(0, Math.trunc(Number(sampleSize))) : 0
  return `本池已导入 ${size} 期数据内`
}

/** 期号展示：第 269 期；拿不到期号时退化成日期或占位符 */
export function periodText(draw: NormalizedDraw | null | undefined): string {
  if (!draw) return '—'
  if (draw.period !== null) return `第 ${draw.period} 期`
  return draw.draw_date || '—'
}
