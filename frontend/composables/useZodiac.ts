/**
 * 生肖视觉身份表（12 色板 + 汉字标签 + 字形兜底）。
 * ---------------------------------------------------------------------------
 * 与 useChartTheme.ts 同一思路：这里只描述「色板 / 身份」，不描述任何业务口径。
 * 后端 services/mark_six.py 的 ZODIAC_ORDER（RAT…PIG）是唯一权威顺序，
 * 前端按同一顺序给出 12 组身份，避免各页面各写一套颜色（禁止散落 hex）。
 *
 * 色板约定（与 useChartTheme 一致）：
 *   - rgb  —— '95, 184, 232' 三元组，按需拼 rgba() 做淡染 / 描边 / 光晕；
 *   - text —— 同色相在深色琉璃底上的浅色端，用于文字与线条字形。
 * 12 色刻意在色环上拉开（相邻生肖可分辨），并配以明度 / 饱和度差异，
 * 让「同肖 = 同色」在 7 列映射网格里连成可追踪的条纹。
 *
 * 口径提醒：本文件只服务「农历年 49 号码 → 生肖」这张只读映射表的展示，
 * 不含任何开奖结果推断，也不做任何范围（市场）判断。
 */

/** 与后端 ZODIAC_ORDER 同序的 12 生肖英文码（唯一权威顺序） */
export type ZodiacCode =
  | 'RAT'
  | 'OX'
  | 'TIGER'
  | 'RABBIT'
  | 'DRAGON'
  | 'SNAKE'
  | 'HORSE'
  | 'GOAT'
  | 'MONKEY'
  | 'ROOSTER'
  | 'DOG'
  | 'PIG'

export interface ZodiacIdentity {
  /** 英文码（未知码走 ZODIAC_FALLBACK 时为 'UNKNOWN'） */
  code: string
  /** 汉字标签（鼠 / 牛 / 虎…） */
  label: string
  /** 字形兜底：SVG 线条字形不可用（未知码）时的备选 */
  emoji: string
  /** RGB 三元组，形如 '95, 184, 232'，用 zodiacTint() 拼 rgba() */
  rgb: string
  /** 深色底上的浅色文字 / 线条色（同一色相） */
  text: string
}

/**
 * 12 色板（唯一来源，禁止在别处再写生肖颜色）。
 * 选色说明：冷青（鼠）/ 牛皮棕（牛）/ 焰橙（虎）/ 樱粉（兔）/ 紫晶（龙）/
 * 翡翠（蛇）/ 朱砂（马）/ 羊绒米白（羊）/ 橄榄青绿（猴）/ 玉米金（鸡）/
 * 靛蓝（狗）/ 藕荷玫紫（猪）——相邻生肖在色环上都不撞色。
 */
export const ZODIAC_IDENTITIES: Record<ZodiacCode, ZodiacIdentity> = {
  RAT: { code: 'RAT', label: '鼠', emoji: '🐭', rgb: '95, 184, 232', text: '#bfe6ff' },
  OX: { code: 'OX', label: '牛', emoji: '🐂', rgb: '181, 116, 76', text: '#efcbaa' },
  TIGER: { code: 'TIGER', label: '虎', emoji: '🐯', rgb: '255, 128, 64', text: '#ffd5b8' },
  RABBIT: { code: 'RABBIT', label: '兔', emoji: '🐰', rgb: '255, 179, 217', text: '#ffdcec' },
  DRAGON: { code: 'DRAGON', label: '龙', emoji: '🐉', rgb: '157, 123, 255', text: '#d8ccff' },
  SNAKE: { code: 'SNAKE', label: '蛇', emoji: '🐍', rgb: '63, 211, 154', text: '#b9f2dc' },
  HORSE: { code: 'HORSE', label: '马', emoji: '🐴', rgb: '255, 107, 122', text: '#ffc9d0' },
  GOAT: { code: 'GOAT', label: '羊', emoji: '🐐', rgb: '236, 220, 178', text: '#f4ecd2' },
  MONKEY: { code: 'MONKEY', label: '猴', emoji: '🐵', rgb: '168, 214, 96', text: '#dcefb8' },
  ROOSTER: { code: 'ROOSTER', label: '鸡', emoji: '🐔', rgb: '255, 210, 70', text: '#ffedb0' },
  DOG: { code: 'DOG', label: '狗', emoji: '🐶', rgb: '143, 168, 255', text: '#d2dcff' },
  PIG: { code: 'PIG', label: '猪', emoji: '🐷', rgb: '224, 95, 208', text: '#f8c9f2' },
}

/** 未知 / 缺失生肖码时的中性身份（灰蓝色，不做任何生肖断言） */
export const ZODIAC_FALLBACK: ZodiacIdentity = {
  code: 'UNKNOWN',
  label: '未知',
  emoji: '❔',
  rgb: '148, 163, 184',
  text: '#e2e8f0',
}

/** 是否为已知的 12 生肖码（用于决定走线条字形还是兜底 emoji） */
export function isZodiacCode(code?: string | null): code is ZodiacCode {
  return !!code && Object.prototype.hasOwnProperty.call(ZODIAC_IDENTITIES, code)
}

/** 取生肖身份；未知 / 缺失回落中性身份，绝不抛错 */
export function zodiacIdentity(code?: string | null): ZodiacIdentity {
  if (!isZodiacCode(code)) return ZODIAC_FALLBACK
  return ZODIAC_IDENTITIES[code]
}

/** 取生肖强调色的 rgba()，alpha 缺省 1 */
export function zodiacTint(code: string | null | undefined, alpha = 1): string {
  return `rgba(${zodiacIdentity(code).rgb}, ${alpha})`
}
