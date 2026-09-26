// https://nuxt.com/docs/api/configuration/nuxt-config
export default defineNuxtConfig({
  compatibilityDate: '2024-11-01',
  devtools: { enabled: true },

  modules: ['@nuxtjs/tailwindcss'],

  runtimeConfig: {
    public: {
      // Override with NUXT_PUBLIC_API_URL in .env / Vercel
      apiBase: process.env.NUXT_PUBLIC_API_URL || 'http://localhost:8000',
    },
  },

  app: {
    head: {
      title: 'Wave Money',
      meta: [
        { name: 'description', content: 'Wave Money — Nuxt + FastAPI starter' },
      ],
    },
  },
})
