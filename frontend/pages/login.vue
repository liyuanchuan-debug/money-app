<script setup lang="ts">
/**
 * 登录页。
 *
 * 两种**必须分开讲**的失败（后端契约，见 routers/auth.py）：
 *   1. 手机号 / 密码错 → HTTP **401**，文案恒为「手机号或密码错误」（不区分两者）；
 *   2. 口令对但账号不可用 → HTTP **403**，文案是「待审批 / 已拒绝 / 已停用」这类
 *      **本来就写给用户看**的句子。
 * 第 1 种用户自己能改（重输密码），第 2 种只能等管理员 —— 混成一句「登录失败」
 * 会把「我的密码错了」和「我还得等审批」搅在一起，是最容易劝退新用户的地方。
 *
 * 会话是后端下发的 HttpOnly Cookie，前端拿不到也不需要拿；
 * 登录成功后只把 /api/auth/login 返回的 user 写进 useAuth 的共享状态。
 *
 * 页头用 <BrandMark>（品牌名与图形只此一处真源，本页不再硬编码品牌文字）。
 * 本页是站外落地页，刻意不渲染 AppNav，出口只有页脚的「返回首页」。
 */

const { login, isLoggedIn, maskedPhone, roleLabel } = useAuth()
const route = useRoute()

useHead({ title: '登录' })

/** 与后端 ``services/auth.py`` 的 PHONE_PATTERN 同源：快速给出友好提示，
 *  但服务端仍会再校验一次 —— 前端校验只是体验，不是关卡。 */
const PHONE_PATTERN = /^1[3-9]\d{9}$/

const phone = ref('')
const password = ref('')

const submitting = ref(false)
/** 口令错（401）—— 用户自己能改 */
const credentialError = ref('')
/** 账号状态问题（403）—— 口令是对的，只能等管理员 */
const statusError = ref('')
const statusHint = ref('')
/** 表单级 / 其它错误（本地校验、422、网络…） */
const formError = ref('')

/**
 * 状态问题的下一步指引。只按后端文案里的关键词给方向，
 * 认不出来就退回「联系管理员」，绝不猜一个具体结论。
 */
function hintForStatusProblem(message: string): string {
  if (message.includes('审批')) return '审批通过后即可登录，不需要重复提交注册申请。'
  if (message.includes('拒绝')) return '如需重新申请，请联系管理员。'
  if (message.includes('停用')) return '如需恢复使用，请联系管理员。'
  return '如需帮助，请联系管理员。'
}

function resetErrors() {
  credentialError.value = ''
  statusError.value = ''
  statusHint.value = ''
  formError.value = ''
}

/** 只接受站内绝对路径，挡掉 //evil.com 这类开放重定向 */
function redirectTarget(): string {
  const raw = route.query.redirect
  const value = Array.isArray(raw) ? raw[0] : raw
  if (typeof value === 'string' && value.startsWith('/') && !value.startsWith('//')) return value
  return '/'
}

async function submit() {
  if (submitting.value) return // 防连点：一次只发一个登录请求

  resetErrors()

  if (!PHONE_PATTERN.test(phone.value.trim())) {
    formError.value = '请输入 11 位中国大陆手机号（如 13800138000）'
    return
  }
  if (!password.value) {
    formError.value = '请输入密码'
    return
  }

  submitting.value = true
  try {
    await login(phone.value, password.value)
    // 登录态已写进共享状态，导航区会立刻变成「手机号 + 角色 + 退出」
    await navigateTo(redirectTarget(), { replace: true })
  } catch (error) {
    const info = authErrorInfo(error, 'login')
    if (info.kind === 'credentials') {
      credentialError.value = info.message
    } else if (info.kind === 'account-status') {
      statusError.value = info.message
      statusHint.value = hintForStatusProblem(info.message)
    } else {
      formError.value = info.message
    }
  } finally {
    submitting.value = false
  }
}

async function goHome() {
  await navigateTo('/')
}
</script>

<template>
  <main class="relative px-5 py-10 text-slate-100 sm:py-14">
    <div class="mx-auto flex w-full max-w-md flex-col gap-6">
      <!-- 页头：品牌标记（四叶草 + 字标）替代原先的纯文字占位「账号」 -->
      <MotionReveal :index="0" class="space-y-2.5">
        <!-- 包一层块级 div：space-y 只对块级兄弟生效，inline-flex 直接当兄弟会少一段间距 -->
        <div>
          <BrandMark size="sm" />
        </div>
        <h1 class="text-gradient text-3xl font-semibold tracking-tight sm:text-4xl">登录</h1>
        <p class="text-sm text-slate-400">
          用手机号 + 密码登录。登录后即可使用已开放的统计分析功能。
        </p>
      </MotionReveal>

      <!-- 已登录：不做任何自动跳转（避免和上一页来回弹），只给一个明确出口 -->
      <MotionReveal v-if="isLoggedIn" :index="1">
        <GlassPanel variant="soft" padding="md" rounded="2xl">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <div class="min-w-0 space-y-0.5">
              <p class="text-sm font-medium text-white">当前已是登录状态</p>
              <p class="num truncate text-[11px] text-slate-400">
                {{ maskedPhone }} · {{ roleLabel }}
              </p>
            </div>
            <GlassButton class="min-h-[44px] shrink-0" @click="goHome">去首页</GlassButton>
          </div>
        </GlassPanel>
      </MotionReveal>

      <!-- 新用户最容易踩的坑：账号不是「注册完就能用」，要等管理员审批。
           所以放在表单**之前**、用琥珀色整体提示，不当脚注写。 -->
      <MotionReveal :index="2">
        <GlassPanel variant="soft" padding="md" rounded="2xl">
          <div class="flex flex-col gap-2">
            <StatChip tone="amber" size="sm" dot>新账号需管理员审批</StatChip>
            <p class="text-sm leading-relaxed text-slate-300">
              新注册的账号默认是「待审批」状态，
              <strong class="font-semibold text-amber-200">必须等管理员审批通过后才能登录</strong>。
              审批前登录会提示「账号待管理员审批，暂时无法登录」——这不是账号或密码的问题。
            </p>
          </div>
        </GlassPanel>
      </MotionReveal>

      <!-- 登录表单 -->
      <MotionReveal :index="3">
        <form @submit.prevent="submit">
          <GlassPanel variant="strong" padding="lg" rounded="3xl" glow tone="aqua">
            <div class="flex flex-col gap-5">
              <div class="space-y-2">
                <label for="login-phone" class="block text-sm font-medium text-slate-200">
                  手机号
                </label>
                <input
                  id="login-phone"
                  v-model="phone"
                  type="tel"
                  inputmode="numeric"
                  maxlength="11"
                  autocomplete="username"
                  placeholder="请输入手机号"
                  class="glass-input num h-12 w-full px-4 text-base text-white"
                >
              </div>

              <div class="space-y-2">
                <label for="login-password" class="block text-sm font-medium text-slate-200">
                  密码
                </label>
                <input
                  id="login-password"
                  v-model="password"
                  type="password"
                  autocomplete="current-password"
                  placeholder="请输入密码"
                  class="glass-input h-12 w-full px-4 text-base text-white"
                >
              </div>

              <!-- 两类失败分开呈现：先口令错（玫色），再账号状态（琥珀色） -->
              <div
                v-if="credentialError"
                role="alert"
                class="rounded-xl border border-bloom-400/40 bg-bloom-400/10 px-4 py-3 text-sm text-bloom-200"
              >
                <p class="font-medium">{{ credentialError }}</p>
                <p class="mt-1 text-[11px] text-bloom-300">
                  请核对手机号与密码后重试；忘记密码请联系管理员重置。
                </p>
              </div>

              <div
                v-else-if="statusError"
                role="alert"
                class="rounded-xl border border-amber-400/40 bg-amber-400/10 px-4 py-3 text-sm text-amber-100"
              >
                <p class="font-medium">{{ statusError }}</p>
                <p class="mt-1 text-[11px] text-amber-200">{{ statusHint }}</p>
              </div>

              <p v-else-if="formError" class="text-sm text-bloom-300" role="alert">
                {{ formError }}
              </p>

              <div class="space-y-3">
                <GlassButton
                  type="submit"
                  variant="primary"
                  size="lg"
                  block
                  class="min-h-[48px]"
                  :loading="submitting"
                  :disabled="submitting"
                >
                  {{ submitting ? '登录中…' : '登录' }}
                </GlassButton>

                <p class="text-center text-xs text-slate-400">
                  还没有账号？
                  <NuxtLink
                    to="/register"
                    class="font-medium text-aqua-200 underline underline-offset-4"
                  >
                    申请注册
                  </NuxtLink>
                  <span class="text-slate-500">（需管理员审批）</span>
                </p>
              </div>
            </div>
          </GlassPanel>
        </form>
      </MotionReveal>

      <MotionReveal :index="4" class="text-center">
        <NuxtLink
          to="/"
          class="inline-flex min-h-[44px] items-center justify-center px-3 text-xs text-slate-400 underline underline-offset-4"
        >
          返回首页
        </NuxtLink>
      </MotionReveal>
    </div>
  </main>
</template>
