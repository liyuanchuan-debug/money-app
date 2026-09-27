<script setup lang="ts">
/**
 * 注册页（提交注册申请）。
 *
 * 三件事必须如实告诉用户，因为这个接口**刻意**不给任何可推断的信息：
 *   1. 提交成功 = 已进入「待审批」，**不是**可以用了；
 *   2. 后端不区分「新号」与「已注册过的号」，回执对两种情况完全一致
 *      （注册接口不能当手机号存在性探针用），所以这里也不能替它「猜」；
 *   3. 因此提交成功后**绝不跳进 App** —— 待审批账号登录必被拒，
 *      假装登录成功就是在骗用户。
 *
 * 页头用 <BrandMark>（品牌名与图形只此一处真源，本页不再硬编码品牌文字）。
 * 本页是站外落地页，刻意不渲染 AppNav，出口只有页脚的「返回首页」。
 */

const { register, isLoggedIn, maskedPhone, roleLabel } = useAuth()

useHead({ title: '注册' })

/** 与后端 ``services/auth.py`` 同源（PHONE_PATTERN / PASSWORD_MIN_LENGTH = 6） */
const PHONE_PATTERN = /^1[3-9]\d{9}$/
const PASSWORD_MIN = 6
const PASSWORD_MAX = 128

const phone = ref('')
const password = ref('')
const confirmPassword = ref('')

const submitting = ref(false)
const formError = ref('')

/** 提交成功后进入「回执态」：只展示后端返回的 message，不跳转 */
const submitted = ref(false)
const serverMessage = ref('')

function resetErrors() {
  formError.value = ''
}

function validate(): string {
  if (!PHONE_PATTERN.test(phone.value.trim())) {
    return '请输入 11 位中国大陆手机号（如 13800138000）'
  }
  if (password.value.length < PASSWORD_MIN) {
    return `密码至少 ${PASSWORD_MIN} 位`
  }
  if (password.value.length > PASSWORD_MAX) {
    return `密码最多 ${PASSWORD_MAX} 位`
  }
  if (password.value !== confirmPassword.value) {
    return '两次输入的密码不一致'
  }
  return ''
}

async function submit() {
  if (submitting.value) return // 防连点

  resetErrors()

  const invalid = validate()
  if (invalid) {
    formError.value = invalid
    return
  }

  submitting.value = true
  try {
    const result = await register(phone.value, password.value)
    serverMessage.value = result?.message || '注册申请已提交。'
    submitted.value = true
    // 回执态里不再保留口令
    password.value = ''
    confirmPassword.value = ''
  } catch (error) {
    formError.value = authErrorInfo(error, 'register').message
  } finally {
    submitting.value = false
  }
}

/** 回执态下想「再提交一次」（例如输错了手机号） */
function restart() {
  submitted.value = false
  serverMessage.value = ''
  resetErrors()
}

async function goLogin() {
  await navigateTo('/login')
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
        <h1 class="text-gradient text-3xl font-semibold tracking-tight sm:text-4xl">申请注册</h1>
        <p class="text-sm text-slate-400">
          填写手机号与密码提交申请；账号由管理员审批，通过后即可登录。
        </p>
      </MotionReveal>

      <!-- 已登录的人不需要再注册 -->
      <MotionReveal v-if="isLoggedIn" :index="1">
        <GlassPanel variant="soft" padding="md" rounded="2xl">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <div class="min-w-0 space-y-0.5">
              <p class="text-sm font-medium text-white">你当前已登录</p>
              <p class="num truncate text-[11px] text-slate-400">
                {{ maskedPhone }} · {{ roleLabel }}
              </p>
            </div>
            <NuxtLink
              to="/"
              class="inline-flex min-h-[44px] shrink-0 items-center justify-center px-3 text-sm text-aqua-200 underline underline-offset-4"
            >
              去首页
            </NuxtLink>
          </div>
        </GlassPanel>
      </MotionReveal>

      <!-- 提交成功的回执：后端 message 原样呈现 + 明确的下一步 -->
      <MotionReveal v-if="submitted" :index="2">
        <GlassPanel variant="strong" padding="lg" rounded="3xl" glow tone="aqua">
          <div class="flex flex-col gap-4">
            <StatChip tone="amber" size="md" dot>待审批</StatChip>

            <div class="space-y-2">
              <h2 class="text-xl font-semibold text-white">注册申请已提交</h2>
              <p class="text-sm leading-relaxed text-slate-300">
                {{ serverMessage }}
              </p>
            </div>

            <hr class="glass-hairline">

            <ul class="space-y-2 text-sm text-slate-400">
              <li class="flex gap-2">
                <span class="text-aqua-300" aria-hidden="true">·</span>
                <span>
                  下一步：等管理员审批通过后再回来登录。审批前登录会提示
                  「账号待管理员审批，暂时无法登录」，这不是密码错误。
                </span>
              </li>
              <li class="flex gap-2">
                <span class="text-aqua-300" aria-hidden="true">·</span>
                <span>
                  如果这个手机号之前已经注册过，重复提交不会覆盖已有账号，
                  直接去登录即可。
                </span>
              </li>
            </ul>

            <div class="flex flex-col gap-2 sm:flex-row">
              <GlassButton
                variant="primary"
                size="lg"
                class="min-h-[48px] w-full"
                @click="goLogin"
              >
                去登录
              </GlassButton>
              <GlassButton
                variant="glass"
                size="lg"
                class="min-h-[48px] w-full"
                @click="restart"
              >
                再提交一个申请
              </GlassButton>
            </div>
          </div>
        </GlassPanel>
      </MotionReveal>

      <!-- 注册表单 -->
      <MotionReveal v-else :index="3">
        <form @submit.prevent="submit">
          <GlassPanel variant="strong" padding="lg" rounded="3xl" glow tone="aqua">
            <div class="flex flex-col gap-5">
              <div class="space-y-2">
                <label for="register-phone" class="block text-sm font-medium text-slate-200">
                  手机号
                </label>
                <input
                  id="register-phone"
                  v-model="phone"
                  type="tel"
                  inputmode="numeric"
                  maxlength="11"
                  autocomplete="username"
                  placeholder="请输入手机号"
                  class="glass-input num h-12 w-full px-4 text-base text-white"
                >
                <p class="text-[11px] text-slate-500">
                  手机号就是登录账号，请填常用号码。
                </p>
              </div>

              <div class="space-y-2">
                <label for="register-password" class="block text-sm font-medium text-slate-200">
                  密码
                </label>
                <input
                  id="register-password"
                  v-model="password"
                  type="password"
                  autocomplete="new-password"
                  :placeholder="`至少 ${PASSWORD_MIN} 位`"
                  class="glass-input h-12 w-full px-4 text-base text-white"
                >
              </div>

              <div class="space-y-2">
                <label for="register-confirm" class="block text-sm font-medium text-slate-200">
                  确认密码
                </label>
                <input
                  id="register-confirm"
                  v-model="confirmPassword"
                  type="password"
                  autocomplete="new-password"
                  placeholder="再输入一次"
                  class="glass-input h-12 w-full px-4 text-base text-white"
                >
              </div>

              <!-- 最关键的预期管理，放在提交按钮正上方，不做脚注 -->
              <div class="rounded-xl border border-amber-400/40 bg-amber-400/10 px-4 py-3">
                <StatChip tone="amber" size="sm" dot>提交后不会立刻可用</StatChip>
                <p class="mt-2 text-sm leading-relaxed text-amber-100">
                  新账号默认是「待审批」状态，
                  <strong class="font-semibold">必须等管理员审批通过后才能登录</strong>。
                </p>
              </div>

              <p v-if="formError" class="text-sm text-bloom-300" role="alert">
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
                  {{ submitting ? '提交中…' : '提交注册申请' }}
                </GlassButton>

                <p class="text-center text-xs text-slate-400">
                  已经有账号了？
                  <NuxtLink
                    to="/login"
                    class="font-medium text-aqua-200 underline underline-offset-4"
                  >
                    直接登录
                  </NuxtLink>
                  <span class="text-slate-500">（已提交过的申请不用重复提交）</span>
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
