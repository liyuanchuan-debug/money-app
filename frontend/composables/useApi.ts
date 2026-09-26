export interface RecordItem {
  id: number
  number: number
  created_at: string
}

export interface LotterySettings {
  small_max: number
  normal_max: number
  bet_unit: number
  mode: string
}

export interface Pick {
  number: number
  diff: number
  wave_type: 'small' | 'normal' | 'big'
  wave_label: string
  role: 'primary' | 'secondary' | 'defense'
  role_label: string
  amount: number
  is_repeat_zodiac: boolean
}

export interface RecommendResult {
  latest: number
  latest_zodiac: number[]
  previous: number | null
  prev_wave: { number: number; diff: number; type: string; label: string } | null
  settings: LotterySettings
  mode: string
  mode_label: string
  bet_unit: number
  total_amount: number
  focus_order: Array<{ type: string; label: string }>
  picks: Pick[]
  missing_waves: Array<{ type: string; label: string; note: string }>
  notes: string[]
  copy_text: string
}

export type ChipMode = 'even' | 'weighted' | 'single'

export function useApi() {
  const config = useRuntimeConfig()
  const baseURL = config.public.apiBase as string

  async function apiFetch<T>(path: string, options: Parameters<typeof $fetch>[1] = {}) {
    return $fetch<T>(path, {
      baseURL,
      ...options,
    })
  }

  return {
    baseURL,
    apiFetch,

    health: () => apiFetch<{ status: string }>('/api/health'),

    listRecords: (limit?: number) =>
      apiFetch<RecordItem[]>('/api/records', {
        query: limit ? { limit } : undefined,
      }),

    addRecord: (number: number) =>
      apiFetch<RecordItem>('/api/records', {
        method: 'POST',
        body: { number },
      }),

    clearRecords: () =>
      apiFetch<{ deleted: number }>('/api/records', { method: 'DELETE' }),

    getSettings: () => apiFetch<LotterySettings>('/api/settings'),

    updateSettings: (patch: Partial<LotterySettings>) =>
      apiFetch<LotterySettings>('/api/settings', {
        method: 'PUT',
        body: patch,
      }),

    recommend: (options: { mode?: ChipMode; number?: number } = {}) =>
      apiFetch<RecommendResult>('/api/recommend', {
        method: 'POST',
        body: options,
      }),

    exportData: () =>
      apiFetch<Record<string, unknown>>('/api/export'),

    importData: (payload: unknown, replace = true) =>
      apiFetch<{ imported_records: number; replaced: boolean }>('/api/import', {
        method: 'POST',
        query: { replace },
        body: payload,
      }),
  }
}
