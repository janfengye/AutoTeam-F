<template>
  <div class="min-h-screen flex items-center justify-center p-4">
    <div class="bg-gray-900 border border-gray-800 rounded-xl p-6 w-full max-w-lg">
      <h1 class="text-xl font-bold text-white text-center mb-2">AutoTeam 初始配置</h1>
      <p class="text-sm text-gray-400 text-center mb-6">首次使用请填写以下配置项</p>

      <div v-if="message" class="mb-4 px-4 py-3 rounded-lg text-sm border" :class="messageClass">
        {{ message }}
      </div>

      <div class="space-y-4">
        <div v-for="field in fields" :key="field.key">
          <label class="block text-sm text-gray-400 mb-1">
            {{ field.prompt }}
            <span v-if="!field.optional" class="text-red-400">*</span>
            <span v-if="field.key === 'API_KEY'" class="text-gray-500 text-xs ml-1">（留空自动生成）</span>
          </label>
          <input
            v-model="form[field.key]"
            :type="field.key.includes('PASSWORD') || field.key.includes('KEY') ? 'password' : 'text'"
            :placeholder="field.default || ''"
            class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white focus:outline-none focus:border-blue-500"
          />
        </div>
      </div>

      <button @click="save" :disabled="saving"
        class="w-full mt-6 px-4 py-2.5 bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium rounded-lg transition disabled:opacity-50">
        {{ saving ? '验证并保存中...' : '保存配置' }}
      </button>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, reactive } from 'vue'
import { api, setApiKey } from '../api.js'

const emit = defineEmits(['configured'])

const fields = ref([])
const form = reactive({})
const saving = ref(false)
const message = ref('')
const messageClass = ref('')

onMounted(async () => {
  try {
    const result = await api.getSetupStatus()
    fields.value = result.fields
    for (const f of result.fields) {
      form[f.key] = f.default || ''
    }
  } catch (e) {
    message.value = '获取配置状态失败: ' + e.message
    messageClass.value = 'bg-red-500/10 text-red-400 border-red-500/20'
  }
})

async function save() {
  saving.value = true
  message.value = ''
  try {
    const result = await api.saveSetup({ ...form })
    if (result.api_key) {
      setApiKey(result.api_key)
    }
    message.value = result.message
    messageClass.value = 'bg-green-500/10 text-green-400 border-green-500/20'
    setTimeout(() => emit('configured'), 1000)
  } catch (e) {
    message.value = e.message
    messageClass.value = 'bg-red-500/10 text-red-400 border-red-500/20'
  } finally {
    saving.value = false
  }
}
</script>
