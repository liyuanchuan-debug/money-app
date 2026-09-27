// https://nuxt.com/docs/api/configuration/nuxt-config

/**
 * 本地默认走同源 `/api`（由 Nitro 反代到后端），避免：
 *   - 浏览器开 `127.0.0.1:3000` 而 API 写 `localhost:8000` → CORS / Cookie 主机不一致；
 *   - 跨端口直连时会话 Cookie 看起来「登录成功、刷新就失效」。
 * 生产跨域部署时在环境变量里设 `NUXT_PUBLIC_API_URL=https://api.example.com`，
 * 并同步后端 `CORS_ORIGINS` + `SESSION_COOKIE_SAMESITE=none`。
 */
const backendOrigin = (
  process.env.NUXT_API_PROXY_TARGET
  || process.env.NUXT_PUBLIC_API_URL
  || 'http://127.0.0.1:8000'
).replace(/\/$/, '')

const publicApiBase = (process.env.NUXT_PUBLIC_API_URL || '').replace(/\/$/, '')

export default defineNuxtConfig({
  compatibilityDate: '2024-11-01',
  devtools: { enabled: true },

  modules: ['@nuxtjs/tailwindcss'],

  // 组件命名：不按目录加前缀，这样 components/charts/LineChart.vue 直接是 <LineChart>
  components: [{ path: '~/components', pathPrefix: false }],

  // 让 tailwind 模块把 assets/css/main.css 当作 Tailwind 入口
  // （模块解析到同一个绝对路径后会自动去重，不会重复注入）
  tailwindcss: {
    cssPath: '~/assets/css/main.css',
  },

  // 全局基底样式：深色底、字体栈、细滚动条、琉璃组件类
  css: ['~/assets/css/main.css'],

  runtimeConfig: {
    public: {
      // 空串 = 浏览器 / SSR 都打同源 `/api/*`（经下面的 proxy）
      apiBase: publicApiBase,
    },
  },

  nitro: {
    // 开发态 Vite 中间件代理（HMR 热更时也生效）
    devProxy: {
      '/api': {
        target: `${backendOrigin}/api`,
        changeOrigin: true,
        cookieDomainRewrite: '',
      },
    },
  },

  // SSR / preview / 生产同构：把 `/api/**` 反代到后端，会话 Cookie 落在前端域名上
  routeRules: {
    '/api/**': {
      proxy: `${backendOrigin}/api/**`,
    },
  },

  app: {
    head: {
      title: '四叶沙盘',
      meta: [
        // viewport-fit=cover：否则 iOS 上 env(safe-area-inset-*) 恒为 0，底栏会贴 Home 条
        { name: 'viewport', content: 'width=device-width, initial-scale=1, viewport-fit=cover' },
        { name: 'description', content: '四叶沙盘 —— 本池开奖样本的波动观察与统计工具：特码走势、遗漏、冷热与走步回测，所有数字只描述本池已导入的样本。' },
        { name: 'theme-color', content: '#05060f' },
        { name: 'color-scheme', content: 'dark' },
      ],
      // 品牌标记 = public/ 下手绘四叶草；Nuxt 不会自动注入 favicon，这里显式挂上。
      // SVG 优先；PNG/ICO 兜底（Chrome 标签栏 / 书签 / 旧缓存场景更稳）。
      // ?v=2 逼浏览器丢掉旧坏 SVG 的顽固 favicon 缓存
      link: [
        { rel: 'icon', type: 'image/svg+xml', href: '/favicon.svg?v=2' },
        { rel: 'icon', type: 'image/png', sizes: '32x32', href: '/favicon-32.png?v=2' },
        { rel: 'icon', href: '/favicon.ico?v=2', sizes: 'any' },
        { rel: 'apple-touch-icon', href: '/apple-touch-icon.png?v=2' },
      ],
    },
  },
})
