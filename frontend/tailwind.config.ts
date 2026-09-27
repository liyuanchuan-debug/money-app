import type { Config } from 'tailwindcss'

/**
 * 四叶沙盘 · 设计令牌（Design Tokens）
 * ---------------------------------------------------------------------------
 * 主题口径：仅深色（dark only），不做浅色主题。
 * 视觉语言：液态琉璃（Liquid Glass）——所有玻璃面必须叠在 AppBackground 的
 * 环境光背景之上，否则 backdrop-blur 只会糊成一片灰。
 *
 * 三层玻璃（与 assets/css/main.css 的 .glass-panel / .glass-card / .glass-control 一一对应）：
 *   L1  .glass-panel   页面分区承载面      低模糊 / 低染色 / 大圆角
 *   L2  .glass-card    可交互卡片          中模糊 + 渐变描边（琉璃边）+ 悬停上浮 + 光带扫过
 *   L3  .glass-control 控件（按钮/输入）    高模糊 + 渐变描边 + 扫光 + 明确的禁用态
 *
 * 注意：content 为空数组时，@nuxtjs/tailwindcss v6 会自动注入扫描路径；
 * 这里显式再列一遍（模块的 merger 会把数组追加到注入的 files 里），
 * 保证新增的 components/charts 等目录一定被扫描到。
 */
export default {
  content: [
    './app.vue',
    './error.vue',
    './app.config.{js,ts}',
    './layouts/**/*.{vue,js,ts}',
    './components/**/*.{vue,js,ts}',
    './pages/**/*.vue',
    './composables/**/*.{js,ts}',
    './plugins/**/*.{js,ts}',
  ],
  theme: {
    extend: {
      /* ------------------------------------------------------------------ */
      /* 深空底色：靛蓝 / 紫的近黑基底                                       */
      /* ------------------------------------------------------------------ */
      colors: {
        ink: {
          50: '#eef1fb',
          100: '#dbdff5',
          200: '#b8c0e6',
          300: '#8d97d0',
          400: '#616bb0',
          500: '#434b8e',
          600: '#333a72',
          700: '#282e5b',
          800: '#191d3f',
          900: '#0c0e26',
          950: '#05060f',
        },
        /* 强调色阶：青（主）/ 紫（次）/ 玫（警示 & 大波动） */
        aqua: {
          50: '#ecfdff',
          100: '#cff9ff',
          200: '#a1f1ff',
          300: '#63e6fb',
          400: '#22d3ee',
          500: '#0bb6d6',
          600: '#0592b0',
          700: '#0a7490',
          800: '#115e75',
          900: '#144e63',
          950: '#063344',
        },
        nebula: {
          50: '#f4f1ff',
          100: '#eae5ff',
          200: '#d6ccff',
          300: '#b8a5ff',
          400: '#9a7bff',
          500: '#7c4dff',
          600: '#6a2ff0',
          700: '#5820cc',
          800: '#481ca6',
          900: '#3b1a85',
          950: '#1e0b4d',
        },
        bloom: {
          50: '#fff1f4',
          100: '#ffe1e7',
          200: '#ffc7d3',
          300: '#ff9db2',
          400: '#ff6a8d',
          500: '#fb3f6e',
          600: '#e81d57',
          700: '#c41348',
          800: '#a0133f',
          900: '#84133a',
          950: '#4a0520',
        },
        /* 琉璃面色（半透明白染色 / 边框高光），用 bg-glass-tint 之类直接取用 */
        glass: {
          tint: 'rgba(255, 255, 255, 0.06)',
          'tint-strong': 'rgba(255, 255, 255, 0.10)',
          'tint-soft': 'rgba(255, 255, 255, 0.03)',
          border: 'rgba(255, 255, 255, 0.14)',
          'border-soft': 'rgba(255, 255, 255, 0.08)',
          highlight: 'rgba(255, 255, 255, 0.55)',
          sheen: 'rgba(255, 255, 255, 0.20)',
          shadow: 'rgba(3, 4, 12, 0.92)',
        },
      },

      /* 高级感字体：纯系统栈（离线可用），刻意把拉丁字形排在 CJK 之前 */
      fontFamily: {
        sans: [
          '-apple-system',
          'BlinkMacSystemFont',
          '"Segoe UI Variable Display"',
          '"Segoe UI"',
          '"PingFang SC"',
          '"Hiragino Sans GB"',
          '"Microsoft YaHei UI"',
          '"Microsoft YaHei"',
          '"Noto Sans SC"',
          '"Source Han Sans SC"',
          '"Helvetica Neue"',
          'Arial',
          'sans-serif',
        ],
        mono: [
          '"SF Mono"',
          '"JetBrains Mono"',
          '"Cascadia Mono"',
          'Consolas',
          '"Liberation Mono"',
          'Menlo',
          'monospace',
        ],
      },

      borderRadius: {
        '4xl': '2rem',
        '5xl': '2.5rem',
      },

      /* 模糊三档：同时作用于 blur-* 与 backdrop-blur-* */
      blur: {
        'glass-sm': '10px',
        'glass-md': '18px',
        'glass-lg': '30px',
      },

      /* 分层阴影：深外阴影 + 内侧顶部高光 */
      boxShadow: {
        'glass-l1':
          'inset 0 1px 0 0 rgba(255, 255, 255, 0.14), 0 24px 60px -30px rgba(3, 4, 12, 0.92)',
        'glass-l2':
          'inset 0 1px 0 0 rgba(255, 255, 255, 0.16), 0 26px 60px -30px rgba(3, 4, 12, 0.95), 0 10px 30px -22px rgba(124, 77, 255, 0.55)',
        'glass-l3':
          'inset 0 1px 0 0 rgba(255, 255, 255, 0.28), 0 16px 36px -20px rgba(3, 4, 12, 0.92)',
        'glass-inner': 'inset 0 1px 0 0 rgba(255, 255, 255, 0.30)',
        'glow-aqua': '0 0 28px -6px rgba(34, 211, 238, 0.60)',
        'glow-nebula': '0 0 30px -6px rgba(124, 77, 255, 0.60)',
        'glow-bloom': '0 0 30px -6px rgba(251, 63, 110, 0.55)',
      },

      backgroundImage: {
        'glass-edge':
          'conic-gradient(from 140deg, rgba(34,211,238,0.55), rgba(124,77,255,0.50), rgba(251,63,110,0.42), rgba(34,211,238,0.55))',
        'glass-sheen':
          'linear-gradient(100deg, transparent 0%, rgba(255,255,255,0.16) 45%, transparent 100%)',
        'glass-top':
          'linear-gradient(180deg, rgba(255,255,255,0.09) 0%, rgba(255,255,255,0.02) 55%, rgba(255,255,255,0.01) 100%)',
        'text-shimmer':
          'linear-gradient(100deg, rgba(203,213,225,0.55) 22%, rgba(255,255,255,1) 44%, rgba(203,213,225,0.55) 66%)',
        'cta-aqua': 'linear-gradient(135deg, #22d3ee 0%, #7c4dff 100%)',
        'cta-bloom': 'linear-gradient(135deg, #fb3f6e 0%, #7c4dff 100%)',
      },

      transitionTimingFunction: {
        silk: 'cubic-bezier(0.22, 1, 0.36, 1)',
      },

      /* ------------------------------------------------------------------ */
      /* 关键帧与动画注册                                                    */
      /* ------------------------------------------------------------------ */
      keyframes: {
        /* 极光漂移（慢、长时长） */
        'aurora-drift': {
          '0%': { transform: 'translate3d(0, 0, 0) scale(1)' },
          '50%': { transform: 'translate3d(5%, -4%, 0) scale(1.12)' },
          '100%': { transform: 'translate3d(-6%, 5%, 0) scale(1.04)' },
        },
        /* 金属流光扫过（多用于文字 / 骨架） */
        shimmer: {
          '0%': { backgroundPosition: '-160% 0' },
          '100%': { backgroundPosition: '260% 0' },
        },
        /* 光带横扫（卡片 / 按钮悬停） */
        sweep: {
          '0%': { transform: 'translateX(-150%) skewX(-14deg)', opacity: '0' },
          '30%': { opacity: '1' },
          '100%': { transform: 'translateX(240%) skewX(-14deg)', opacity: '0' },
        },
        /* 上下悬浮 */
        float: {
          '0%, 100%': { transform: 'translateY(0)' },
          '50%': { transform: 'translateY(-7px)' },
        },
        /* 中奖式扩散光环 */
        'pulse-ring': {
          '0%': { transform: 'scale(0.55)', opacity: '0.85' },
          '70%': { opacity: '0.26' },
          '100%': { transform: 'scale(1.85)', opacity: '0' },
        },
        /* 卡片错开入场：透明度 + 位移 + 模糊 */
        'reveal-up': {
          '0%': { opacity: '0', transform: 'translateY(16px) scale(0.985)', filter: 'blur(10px)' },
          '60%': { opacity: '1' },
          '100%': { opacity: '1', transform: 'translateY(0) scale(1)', filter: 'blur(0)' },
        },
        /* 数字滚动落位 */
        roll: {
          '0%': { transform: 'translateY(-58%)', opacity: '0', filter: 'blur(6px)' },
          '45%': { opacity: '1' },
          '100%': { transform: 'translateY(0)', opacity: '1', filter: 'blur(0)' },
        },
        /* 琉璃球体高光旋转 */
        'glass-spin': {
          '0%': { transform: 'rotate(0deg)' },
          '100%': { transform: 'rotate(360deg)' },
        },
        /* 呼吸光晕 */
        breathe: {
          '0%, 100%': { opacity: '0.6', transform: 'scale(1)' },
          '50%': { opacity: '1', transform: 'scale(1.06)' },
        },
      },
      animation: {
        'aurora-1': 'aurora-drift 26s ease-in-out infinite alternate',
        'aurora-2': 'aurora-drift 38s ease-in-out infinite alternate-reverse',
        'aurora-3': 'aurora-drift 46s ease-in-out infinite alternate',
        shimmer: 'shimmer 3.6s linear infinite',
        sweep: 'sweep 1.4s cubic-bezier(0.22, 1, 0.36, 1) both',
        float: 'float 6.5s ease-in-out infinite',
        'pulse-ring': 'pulse-ring 1.5s cubic-bezier(0.24, 0.8, 0.32, 1) both',
        'reveal-up': 'reveal-up 0.72s cubic-bezier(0.22, 1, 0.36, 1) both',
        roll: 'roll 0.5s cubic-bezier(0.22, 1, 0.36, 1) both',
        'glass-spin': 'glass-spin 16s linear infinite',
        'ball-spin': 'glass-spin 1.1s linear infinite',
        breathe: 'breathe 4.5s ease-in-out infinite',
      },
    },
  },
  plugins: [],
} satisfies Config
