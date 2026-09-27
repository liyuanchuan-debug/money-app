import type { ComputedRef, MaybeRefOrGetter, Ref } from 'vue'
import { computed, onBeforeUnmount, onMounted, ref, toValue, watch } from 'vue'

/* ==========================================================================
 * useMotion —— 动效总开关 + 三个原语（数字滚动 / 错开入场 / 命中光环）
 * --------------------------------------------------------------------------
 * 三条底线：
 *   1. prefers-reduced-motion: reduce 时，JS 侧直接短路（不跑 requestAnimationFrame），
 *      并把 HTML 的动画压到终态（见 assets/css/main.css 末尾的媒体查询）；
 *   2. 用户可以手动全局关掉特效，开关落在 <html class="motion-off"> + localStorage；
 *   3. 所有动效只碰 transform / opacity / filter，不碰 layout 属性。
 * ========================================================================== */

const STORAGE_KEY = 'wave-money:motion'
const ROOT_CLASS = 'motion-off'

/** 全局共享单例（客户端）：是否播放特效 */
const motionEnabled = ref(true)
/** 全局共享单例（客户端）：系统是否要求减少动效 */
const systemReduced = ref(false)

let bootstrapped = false

function readStored(): string | null {
  if (!import.meta.client) return null
  try {
    return window.localStorage.getItem(STORAGE_KEY)
  } catch {
    return null
  }
}

function prefersReduced(): boolean {
  if (!import.meta.client || typeof window.matchMedia !== 'function') return false
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches
  } catch {
    return false
  }
}

function syncRootClass() {
  if (!import.meta.client) return
  const root = document.documentElement
  if (!root) return
  root.classList.toggle(ROOT_CLASS, !motionEnabled.value)
}

/** 只在浏览器首次求值时执行一次：读偏好、写 html class、监听系统设置变化 */
function bootstrapOnce() {
  if (bootstrapped || !import.meta.client) return
  bootstrapped = true

  systemReduced.value = prefersReduced()
  const stored = readStored()
  motionEnabled.value
    = stored === 'off' ? false : stored === 'on' ? true : !systemReduced.value
  syncRootClass()

  if (typeof window.matchMedia === 'function') {
    try {
      const mq = window.matchMedia('(prefers-reduced-motion: reduce)')
      mq.addEventListener?.('change', (event: MediaQueryListEvent) => {
        systemReduced.value = event.matches
        // 用户没有显式选择过，就跟随系统
        if (readStored() === null) {
          motionEnabled.value = !event.matches
          syncRootClass()
        }
      })
    } catch {
      /* 忽略：不支持 matchMedia 的环境按「可动效」处理 */
    }
  }
}

export interface UseMotionReturn {
  /** 特效是否开启（用户开关） */
  motionEnabled: ComputedRef<boolean>
  /** 系统是否声明 prefers-reduced-motion: reduce */
  systemReducedMotion: ComputedRef<boolean>
  /** 综合判断：现在到底该不该动（所有动效都必须先问它） */
  shouldAnimate: ComputedRef<boolean>
  toggleMotion: () => void
  setMotion: (value: boolean) => void
  enableMotion: () => void
  disableMotion: () => void
}

/**
 * 动效总开关。无生命周期副作用，可在任意位置调用（含 rAF 回调）。
 * 关闭方式：① 用户在 AppNav 点「特效 关」；② 系统 prefers-reduced-motion: reduce。
 */
export function useMotion(): UseMotionReturn {
  bootstrapOnce()

  const shouldAnimate = computed(() => motionEnabled.value && !systemReduced.value)

  function setMotion(value: boolean) {
    motionEnabled.value = value
    if (import.meta.client) {
      try {
        window.localStorage.setItem(STORAGE_KEY, value ? 'on' : 'off')
      } catch {
        /* localStorage 不可用（隐私模式）时只作用于当前会话 */
      }
    }
    syncRootClass()
  }

  return {
    motionEnabled: computed(() => motionEnabled.value),
    systemReducedMotion: computed(() => systemReduced.value),
    shouldAnimate,
    toggleMotion: () => setMotion(!motionEnabled.value),
    setMotion,
    enableMotion: () => setMotion(true),
    disableMotion: () => setMotion(false),
  }
}

/* -------------------------------------------------------------------------- */
/* 原语 1：数字滚动                                                            */
/* -------------------------------------------------------------------------- */

export interface NumberRollOptions {
  /** 错开延迟（毫秒）：列表第 n 项传 stagger: n * 70 */
  stagger?: number
  /** 滚动总时长（毫秒） */
  duration?: number
  /** 随机值下界 */
  min?: number
  /** 随机值上界 */
  max?: number
}

export interface NumberRollReturn {
  /** 当前应显示的数字（滚动中为随机值，结束后为最终值） */
  display: Ref<number | null>
  /** 是否正在滚动 */
  rolling: Ref<boolean>
  replay: () => void
  stop: () => void
}

/**
 * 数字滚动：先快速跳随机值，后段收敛到最终值。
 * - prefers-reduced-motion / 手动关闭特效时：直接落到最终值，一帧都不跑；
 * - 用 requestAnimationFrame，组件卸载时自动取消（不会留下定时器）。
 */
export function useNumberRoll(
  target: MaybeRefOrGetter<number | null | undefined>,
  options: NumberRollOptions = {},
): NumberRollReturn {
  const stagger = Math.max(0, options.stagger ?? 0)
  const duration = Math.max(120, options.duration ?? 900)
  const min = options.min ?? 1
  const max = options.max ?? 49

  const display = ref<number | null>(toValue(target) ?? null)
  const rolling = ref(false)

  let rafId = 0
  let timerId: ReturnType<typeof setTimeout> | null = null
  let startedAt = 0

  function stop() {
    if (rafId && import.meta.client) cancelAnimationFrame(rafId)
    rafId = 0
    if (timerId) clearTimeout(timerId)
    timerId = null
    rolling.value = false
  }

  function run() {
    stop()
    const final = toValue(target)
    if (final === null || final === undefined || !Number.isFinite(Number(final))) {
      display.value = null
      return
    }

    const value = Number(final)
    const { shouldAnimate } = useMotion()
    if (!shouldAnimate.value || !import.meta.client) {
      // 减少动效：直接渲染终态
      display.value = value
      return
    }

    rolling.value = true
    const tick = (now: number) => {
      if (!startedAt) startedAt = now
      const elapsed = now - startedAt
      if (elapsed >= duration) {
        display.value = value
        rolling.value = false
        rafId = 0
        return
      }
      // 前 70% 快速跳随机值，后 30% 收敛，制造「刹车」感
      display.value
        = elapsed / duration < 0.7 ? min + Math.floor(Math.random() * (max - min + 1)) : value
      rafId = requestAnimationFrame(tick)
    }

    timerId = setTimeout(() => {
      startedAt = 0
      rafId = requestAnimationFrame(tick)
    }, stagger)
  }

  watch(() => toValue(target), () => run())
  onMounted(() => run())
  onBeforeUnmount(() => stop())

  return { display, rolling, replay: run, stop }
}

/* -------------------------------------------------------------------------- */
/* 原语 2：错开入场                                                            */
/* -------------------------------------------------------------------------- */

export interface StaggerOptions {
  /** 每项间隔（毫秒） */
  step?: number
  /** 基础延迟（毫秒） */
  base?: number
  /** 最多错开多少项（超过则复用最后一档，避免长列表等到天荒地老） */
  max?: number
}

/** 错开序列的延迟计算器；配合 <MotionReveal :index> 使用。 */
export function useStagger(options: StaggerOptions = {}) {
  const step = options.step ?? 70
  const base = options.base ?? 0
  const max = options.max ?? 12

  function delayFor(index: number) {
    const slot = Math.min(Math.max(0, index), max)
    return base + slot * step
  }

  return { delayFor, step, base }
}

/* -------------------------------------------------------------------------- */
/* 原语 3：命中光环                                                            */
/* -------------------------------------------------------------------------- */

export interface HitPulseReturn {
  /** 每次触发自增，直接绑给 <PulseRing :trigger> */
  key: Ref<number>
  active: Ref<boolean>
  fire: () => void
}

/** 命中光环：触发一次，向外扩一圈光环；减少动效时完全跳过。 */
export function useHitPulse(options: { hold?: number } = {}): HitPulseReturn {
  const hold = options.hold ?? 1700
  const key = ref(0)
  const active = ref(false)
  let timer: ReturnType<typeof setTimeout> | null = null

  function fire() {
    const { shouldAnimate } = useMotion()
    if (!shouldAnimate.value) return
    key.value += 1
    active.value = true
    if (timer) clearTimeout(timer)
    timer = setTimeout(() => {
      active.value = false
      timer = null
    }, hold)
  }

  onBeforeUnmount(() => {
    if (timer) clearTimeout(timer)
    timer = null
  })

  return { key, active, fire }
}
