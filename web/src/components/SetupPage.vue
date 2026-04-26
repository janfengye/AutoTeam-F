<template>
  <div class="min-h-screen flex items-center justify-center p-4">
    <div class="bg-gray-900 border border-gray-800 rounded-xl p-6 w-full max-w-2xl">
      <h1 class="text-xl font-bold text-white text-center mb-2">AutoTeam 初始配置</h1>
      <p class="text-sm text-gray-400 text-center mb-6">首次使用请按步骤填写以下配置</p>

      <div v-if="message" class="mb-4 px-4 py-3 rounded-lg text-sm border" :class="messageClass">
        {{ message }}
      </div>

      <!-- 卡片 1:Mail Provider 选择 -->
      <div class="mb-4 p-4 bg-gray-800/40 border border-gray-800 rounded-lg">
        <div class="flex items-center justify-between mb-3">
          <h2 class="text-sm font-semibold text-white">1. 邮箱后端</h2>
          <span class="text-xs" :class="state === 'PROVIDER' ? 'text-yellow-400' : 'text-green-400'">
            {{ state === 'PROVIDER' ? '请选择' : '已选 ' + form.MAIL_PROVIDER }}
          </span>
        </div>
        <div class="flex gap-2">
          <button
            v-for="opt in providerOptions" :key="opt.value"
            @click="selectProvider(opt.value)"
            :class="form.MAIL_PROVIDER === opt.value
              ? 'bg-blue-600 border-blue-500 text-white'
              : 'bg-gray-800 border-gray-700 text-gray-300'"
            class="flex-1 px-3 py-2 border rounded text-sm transition">
            <div class="font-medium">{{ opt.label }}</div>
            <div class="text-xs opacity-75 mt-0.5">{{ opt.desc }}</div>
          </button>
        </div>
      </div>

      <!-- 卡片 2:连接 + 凭据 -->
      <div class="mb-4 p-4 rounded-lg border" :class="cardClass('CONNECTION')">
        <div class="flex items-center justify-between mb-3">
          <h2 class="text-sm font-semibold text-white">2. 服务器连接</h2>
          <span class="text-xs" :class="connectionStatusClass">{{ connectionStatus }}</span>
        </div>
        <div class="space-y-3" :class="state === 'PROVIDER' ? 'opacity-40 pointer-events-none' : ''">
          <div>
            <label class="block text-xs text-gray-400 mb-1">
              {{ form.MAIL_PROVIDER === 'maillab' ? 'MAILLAB_API_URL' : 'CLOUDMAIL_BASE_URL' }} *
            </label>
            <input
              v-model="form[baseUrlKey]"
              type="text"
              :placeholder="form.MAIL_PROVIDER === 'maillab' ? 'https://your-maillab.example.com' : 'https://example.com/api'"
              class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded text-sm text-white" />
          </div>
          <div v-if="form.MAIL_PROVIDER === 'maillab'">
            <label class="block text-xs text-gray-400 mb-1">MAILLAB_USERNAME *</label>
            <input
              v-model="form.MAILLAB_USERNAME"
              type="text"
              placeholder="admin@example.com"
              class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded text-sm text-white" />
          </div>
          <div>
            <label class="block text-xs text-gray-400 mb-1">{{ passwordKey }} *</label>
            <input
              v-model="form[passwordKey]"
              type="password"
              class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded text-sm text-white" />
          </div>
          <button
            @click="testConnection"
            :disabled="testing || !canTestConnection"
            class="px-4 py-2 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white text-sm rounded transition">
            {{ testing ? '测试中...' : '测试连接' }}
          </button>
        </div>
      </div>

      <!-- 卡片 3:Domain 验证 -->
      <div class="mb-4 p-4 rounded-lg border" :class="cardClass('DOMAIN')">
        <div class="flex items-center justify-between mb-3">
          <h2 class="text-sm font-semibold text-white">3. 邮箱域名</h2>
          <span class="text-xs" :class="domainStatusClass">{{ domainStatus }}</span>
        </div>
        <div class="space-y-3" :class="!canEnterDomain ? 'opacity-40 pointer-events-none' : ''">
          <div>
            <label class="block text-xs text-gray-400 mb-1">{{ domainKey }} *</label>
            <select
              v-if="domainList && domainList.length"
              v-model="selectedDomain"
              class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded text-sm text-white">
              <option v-for="d in domainList" :key="d" :value="stripAt(d)">{{ d }}</option>
            </select>
            <input
              v-else
              v-model="selectedDomain"
              type="text"
              placeholder="example.com"
              class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded text-sm text-white" />
          </div>
          <button
            @click="verifyDomain"
            :disabled="verifying || !selectedDomain"
            class="px-4 py-2 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white text-sm rounded transition">
            {{ verifying ? '验证中...' : '验证归属' }}
          </button>
        </div>
      </div>

      <!-- 卡片 4:其他配置 -->
      <div class="mb-4 p-4 rounded-lg border" :class="cardClass('SAVE')">
        <h2 class="text-sm font-semibold text-white mb-3">4. 其他配置</h2>
        <div class="space-y-3" :class="state !== 'SAVE' ? 'opacity-40 pointer-events-none' : ''">
          <div v-for="field in otherFields" :key="field.key">
            <label class="block text-xs text-gray-400 mb-1">
              {{ field.prompt }}
              <span v-if="!field.optional" class="text-red-400">*</span>
              <span v-if="field.key === 'API_KEY'" class="text-gray-500 text-[10px] ml-1">(留空自动生成)</span>
            </label>
            <input
              v-model="form[field.key]"
              :type="field.key.includes('PASSWORD') || field.key.includes('KEY') ? 'password' : 'text'"
              :placeholder="field.default || ''"
              class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded text-sm text-white" />
          </div>
        </div>
      </div>

      <button
        @click="save"
        :disabled="saving || state !== 'SAVE'"
        class="w-full px-4 py-2.5 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white text-sm font-medium rounded-lg transition">
        {{ saving ? '验证并保存中...' : '保存配置' }}
      </button>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { api, setApiKey } from '../api.js'

const emit = defineEmits(['configured'])

// 状态机:PROVIDER → CONNECTION → DOMAIN → SAVE (SPEC-1 §4.1)
const state = ref('PROVIDER')
const form = reactive({
  MAIL_PROVIDER: 'cf_temp_email',
  CLOUDMAIL_BASE_URL: '',
  CLOUDMAIL_PASSWORD: '',
  CLOUDMAIL_DOMAIN: '',
  MAILLAB_API_URL: '',
  MAILLAB_USERNAME: '',
  MAILLAB_PASSWORD: '',
  MAILLAB_DOMAIN: '',
  CPA_URL: 'http://127.0.0.1:8317',
  CPA_KEY: '',
  PLAYWRIGHT_PROXY_URL: '',
  PLAYWRIGHT_PROXY_BYPASS: '',
  API_KEY: '',
})

const fields = ref([])
const testing = ref(false)
const verifying = ref(false)
const saving = ref(false)
const message = ref('')
const messageClass = ref('')

const domainList = ref(null)
const selectedDomain = ref('')
const detectedProvider = ref(null)

const providerOptions = [
  { value: 'cf_temp_email', label: 'cf_temp_email', desc: 'dreamhunter2333/cloudflare_temp_email' },
  { value: 'maillab', label: 'maillab', desc: 'maillab/cloud-mail (skymail.ink)' },
]

const baseUrlKey = computed(() => form.MAIL_PROVIDER === 'maillab' ? 'MAILLAB_API_URL' : 'CLOUDMAIL_BASE_URL')
const passwordKey = computed(() => form.MAIL_PROVIDER === 'maillab' ? 'MAILLAB_PASSWORD' : 'CLOUDMAIL_PASSWORD')
const domainKey = computed(() => form.MAIL_PROVIDER === 'maillab' ? 'MAILLAB_DOMAIN' : 'CLOUDMAIL_DOMAIN')

const canTestConnection = computed(() => {
  const baseOk = form[baseUrlKey.value]
  const pwdOk = form[passwordKey.value]
  if (form.MAIL_PROVIDER === 'maillab') {
    return baseOk && form.MAILLAB_USERNAME && pwdOk
  }
  return baseOk && pwdOk
})

const canEnterDomain = computed(() => state.value === 'DOMAIN' || state.value === 'SAVE')

const otherFields = computed(() => fields.value.filter(f =>
  !['MAIL_PROVIDER', 'CLOUDMAIL_BASE_URL', 'CLOUDMAIL_EMAIL', 'CLOUDMAIL_PASSWORD', 'CLOUDMAIL_DOMAIN',
    'MAILLAB_API_URL', 'MAILLAB_USERNAME', 'MAILLAB_PASSWORD', 'MAILLAB_DOMAIN'].includes(f.key)
))

const connectionStatus = computed(() => {
  if (state.value === 'PROVIDER') return '待选 provider'
  if (testing.value) return '测试中...'
  if (state.value === 'CONNECTION') return '请测试连接'
  return detectedProvider.value ? `已通过 (${detectedProvider.value})` : '已通过'
})
const connectionStatusClass = computed(() => {
  if (state.value === 'PROVIDER' || state.value === 'CONNECTION') return 'text-yellow-400'
  return 'text-green-400'
})

const domainStatus = computed(() => {
  if (!canEnterDomain.value) return '需先通过测试连接'
  if (verifying.value) return '验证中...'
  if (state.value === 'DOMAIN') return '请验证域名归属'
  return '已通过'
})
const domainStatusClass = computed(() => state.value === 'SAVE' ? 'text-green-400' : 'text-yellow-400')

function cardClass(targetState) {
  const order = ['PROVIDER', 'CONNECTION', 'DOMAIN', 'SAVE']
  const cur = order.indexOf(state.value)
  const tgt = order.indexOf(targetState)
  if (tgt > cur) return 'bg-gray-800/20 border-gray-800'
  if (tgt < cur) return 'bg-green-900/10 border-green-900/40'
  return 'bg-blue-900/10 border-blue-700/40'
}

function stripAt(d) {
  return (d || '').replace(/^@/, '').trim()
}

function selectProvider(value) {
  form.MAIL_PROVIDER = value
  // SPEC-1 §4.1 — 切换 provider 时后续状态全部重置
  state.value = 'CONNECTION'
  detectedProvider.value = null
  domainList.value = null
  selectedDomain.value = ''
  message.value = ''
}

function showError(resp) {
  message.value = `${resp.error_code || 'ERROR'}: ${resp.message || '未知错误'}` +
    (resp.hint ? ` — ${resp.hint}` : '')
  messageClass.value = 'bg-red-500/10 text-red-400 border-red-500/20'
}

async function testConnection() {
  testing.value = true
  message.value = ''
  try {
    const fp = await api.probeMailProvider({
      provider: form.MAIL_PROVIDER,
      step: 'fingerprint',
      base_url: form[baseUrlKey.value],
    })
    if (!fp.ok) { showError(fp); return }
    detectedProvider.value = fp.detected_provider
    domainList.value = fp.domain_list
    if (fp.warnings && fp.warnings.length) {
      message.value = '⚠️ ' + fp.warnings.join('; ')
      messageClass.value = 'bg-yellow-500/10 text-yellow-400 border-yellow-500/20'
    }

    const cred = await api.probeMailProvider({
      provider: form.MAIL_PROVIDER,
      step: 'credentials',
      base_url: form[baseUrlKey.value],
      username: form.MAILLAB_USERNAME,
      password: form.MAILLAB_PASSWORD,
      admin_password: form.CLOUDMAIL_PASSWORD,
    })
    if (!cred.ok) { showError(cred); return }
    state.value = 'DOMAIN'

    if (domainList.value && domainList.value.length === 1) {
      selectedDomain.value = stripAt(domainList.value[0])
    }
  } catch (e) {
    message.value = '请求失败: ' + e.message
    messageClass.value = 'bg-red-500/10 text-red-400 border-red-500/20'
  } finally {
    testing.value = false
  }
}

async function verifyDomain() {
  verifying.value = true
  message.value = ''
  try {
    const own = await api.probeMailProvider({
      provider: form.MAIL_PROVIDER,
      step: 'domain_ownership',
      base_url: form[baseUrlKey.value],
      username: form.MAILLAB_USERNAME,
      password: form.MAILLAB_PASSWORD,
      admin_password: form.CLOUDMAIL_PASSWORD,
      domain: selectedDomain.value,
    })
    if (!own.ok) { showError(own); return }
    form[domainKey.value] = '@' + selectedDomain.value
    if (form.MAIL_PROVIDER === 'maillab') {
      form.CLOUDMAIL_DOMAIN = form.CLOUDMAIL_DOMAIN || ('@' + selectedDomain.value)
    }
    state.value = 'SAVE'
    if (own.cleaned === false && own.leaked_probe) {
      message.value = `⚠️ 探测邮箱回收失败,请到后台手动删除: ${own.leaked_probe.email}`
      messageClass.value = 'bg-yellow-500/10 text-yellow-400 border-yellow-500/20'
    }
  } catch (e) {
    message.value = '请求失败: ' + e.message
    messageClass.value = 'bg-red-500/10 text-red-400 border-red-500/20'
  } finally {
    verifying.value = false
  }
}

async function save() {
  saving.value = true
  message.value = ''
  try {
    const result = await api.saveSetup({ ...form })
    if (result.api_key) setApiKey(result.api_key)
    message.value = result.message || '保存成功'
    messageClass.value = 'bg-green-500/10 text-green-400 border-green-500/20'
    setTimeout(() => emit('configured'), 1000)
  } catch (e) {
    message.value = e.message
    messageClass.value = 'bg-red-500/10 text-red-400 border-red-500/20'
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  try {
    const result = await api.getSetupStatus()
    fields.value = result.fields
    for (const f of result.fields) {
      if (form[f.key] === '' || form[f.key] === undefined) {
        form[f.key] = f.default || ''
      }
    }
    if (result.provider) form.MAIL_PROVIDER = result.provider
  } catch (e) {
    message.value = '获取配置状态失败: ' + e.message
    messageClass.value = 'bg-red-500/10 text-red-400 border-red-500/20'
  }
})
</script>
