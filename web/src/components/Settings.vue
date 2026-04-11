<template>
  <div class="mt-6 bg-gray-900 border border-gray-800 rounded-xl p-4">
    <div class="flex items-center justify-between mb-4">
      <h2 class="text-lg font-semibold text-white">巡检设置</h2>
      <span v-if="saved" class="text-xs text-green-400 transition">已保存</span>
    </div>

    <div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
      <div>
        <label class="block text-sm text-gray-400 mb-1">巡检间隔</label>
        <div class="flex items-center gap-2">
          <input v-model.number="form.interval" type="number" min="1"
            class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white focus:outline-none focus:border-blue-500" />
          <span class="text-sm text-gray-500 shrink-0">分钟</span>
        </div>
      </div>
      <div>
        <label class="block text-sm text-gray-400 mb-1">额度阈值</label>
        <div class="flex items-center gap-2">
          <input v-model.number="form.threshold" type="number" min="1" max="100"
            class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white focus:outline-none focus:border-blue-500" />
          <span class="text-sm text-gray-500 shrink-0">%</span>
        </div>
      </div>
      <div>
        <label class="block text-sm text-gray-400 mb-1">触发账号数</label>
        <div class="flex items-center gap-2">
          <input v-model.number="form.min_low" type="number" min="1"
            class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white focus:outline-none focus:border-blue-500" />
          <span class="text-sm text-gray-500 shrink-0">个</span>
        </div>
      </div>
    </div>

    <div class="mt-3 flex items-center justify-between">
      <p class="text-xs text-gray-500">
        每 {{ form.interval }} 分钟检查一次，{{ form.min_low }} 个以上账号剩余低于 {{ form.threshold }}% 时自动轮转
      </p>
      <button @click="save" :disabled="saving"
        class="px-4 py-1.5 bg-blue-600 hover:bg-blue-500 text-white text-sm rounded-lg transition disabled:opacity-50">
        {{ saving ? '保存中...' : '保存' }}
      </button>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api.js'

const form = ref({ interval: 5, threshold: 10, min_low: 2 })
const saving = ref(false)
const saved = ref(false)

onMounted(async () => {
  try {
    const cfg = await api.getAutoCheckConfig()
    form.value = {
      interval: Math.round(cfg.interval / 60),
      threshold: cfg.threshold,
      min_low: cfg.min_low,
    }
  } catch (e) {
    console.error('加载巡检配置失败:', e)
  }
})

async function save() {
  saving.value = true
  saved.value = false
  try {
    const cfg = await api.setAutoCheckConfig({
      interval: form.value.interval * 60,
      threshold: form.value.threshold,
      min_low: form.value.min_low,
    })
    form.value = {
      interval: Math.round(cfg.interval / 60),
      threshold: cfg.threshold,
      min_low: cfg.min_low,
    }
    saved.value = true
    setTimeout(() => { saved.value = false }, 3000)
  } catch (e) {
    console.error('保存失败:', e)
  } finally {
    saving.value = false
  }
}
</script>
