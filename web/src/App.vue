<template>
  <!-- 登录页 -->
  <div v-if="!authenticated" class="min-h-screen flex items-center justify-center">
    <div class="bg-gray-900 border border-gray-800 rounded-xl p-8 w-full max-w-sm">
      <h1 class="text-xl font-bold text-white text-center mb-2">AutoTeam</h1>
      <p class="text-sm text-gray-400 text-center mb-6">请输入 API Key 登录</p>
      <div v-if="authError" class="mb-4 px-4 py-3 rounded-lg text-sm bg-red-500/10 text-red-400 border border-red-500/20">
        {{ authError }}
      </div>
      <input
        v-model.trim="inputKey"
        type="password"
        placeholder="API Key"
        @keyup.enter="doLogin"
        class="w-full px-4 py-2.5 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white focus:outline-none focus:border-blue-500 mb-4"
      />
      <button @click="doLogin" :disabled="!inputKey || authLoading"
        class="w-full px-4 py-2.5 bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium rounded-lg transition disabled:opacity-50">
        {{ authLoading ? '验证中...' : '登录' }}
      </button>
    </div>
  </div>

  <!-- 主面板 -->
  <div v-else class="flex min-h-screen">
    <!-- 侧边栏 -->
    <Sidebar :active="currentPage" @navigate="currentPage = $event" />

    <!-- 主内容区 -->
    <div class="flex-1 p-4 md:p-6 overflow-y-auto pb-20 md:pb-6">
      <!-- 顶部状态栏 -->
      <div class="flex items-center justify-end gap-3 mb-6">
        <span v-if="busyTask" class="flex items-center gap-2 text-sm text-yellow-400">
          <span class="animate-spin inline-block w-4 h-4 border-2 border-yellow-400 border-t-transparent rounded-full"></span>
          {{ busyTask.command === 'admin-login'
            ? '管理员登录中...'
            : busyTask.command === 'main-codex-sync'
              ? '主号 Codex 同步中...'
              : `${busyTask.command} 执行中...` }}
        </span>
        <button @click="refresh" :disabled="loading"
          class="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 text-sm rounded-lg border border-gray-700 transition disabled:opacity-50">
          {{ loading ? '刷新中...' : '刷新' }}
        </button>
        <button v-if="authRequired" @click="doLogout"
          class="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 text-sm rounded-lg border border-gray-700 transition text-gray-400 hover:text-white">
          登出
        </button>
      </div>

      <!-- 页面内容 -->
      <Dashboard v-if="currentPage === 'dashboard'"
        :status="status" :loading="loading" :running-task="busyTask" :admin-status="adminStatus" @refresh="refresh" />

      <TeamMembers v-else-if="currentPage === 'team'" />

      <TasksPage v-else-if="currentPage === 'tasks'"
        :running-task="busyTask" :admin-status="adminStatus" :tasks="tasks"
        @task-started="onTaskStarted" @refresh="refresh" />

      <LogViewer v-else-if="currentPage === 'logs'" />

      <Settings v-else-if="currentPage === 'settings'"
        :admin-status="adminStatus" :codex-status="codexStatus" @refresh="refresh" @admin-progress="onAdminProgress" />
    </div>
  </div>
</template>

<script setup>
import { computed, ref, onMounted, onUnmounted } from 'vue'
import { api, setApiKey, clearApiKey } from './api.js'
import Sidebar from './components/Sidebar.vue'
import Dashboard from './components/Dashboard.vue'
import TeamMembers from './components/TeamMembers.vue'
import TasksPage from './components/TasksPage.vue'
import LogViewer from './components/LogViewer.vue'
import TaskPanel from './components/TaskPanel.vue'
import TaskHistory from './components/TaskHistory.vue'
import Settings from './components/Settings.vue'

const authenticated = ref(false)
const authRequired = ref(false)
const authLoading = ref(false)
const authError = ref('')
const inputKey = ref('')
const currentPage = ref('dashboard')

const status = ref(null)
const adminStatus = ref(null)
const codexStatus = ref(null)
const tasks = ref([])
const loading = ref(false)
const runningTask = ref(null)
const busyTask = computed(() => {
  if (adminStatus.value?.login_in_progress) {
    return { command: 'admin-login' }
  }
  if (codexStatus.value?.in_progress) {
    return { command: 'main-codex-sync' }
  }
  return runningTask.value
})

let pollTimer = null

async function checkAuth() {
  try {
    const result = await api.checkAuth()
    authenticated.value = result.authenticated
    authRequired.value = result.auth_required
    return result.authenticated
  } catch (e) {
    if (e.status === 401) {
      authenticated.value = false
      authRequired.value = true
      return false
    }
    authenticated.value = true
    authRequired.value = false
    return true
  }
}

async function doLogin() {
  authError.value = ''
  authLoading.value = true
  try {
    setApiKey(inputKey.value)
    const ok = await checkAuth()
    if (!ok) {
      clearApiKey()
      authError.value = 'API Key 无效'
    } else {
      inputKey.value = ''
      refresh()
      startPolling(600000)
    }
  } catch (e) {
    clearApiKey()
    authError.value = e.message
  } finally {
    authLoading.value = false
  }
}

function doLogout() {
  clearApiKey()
  authenticated.value = false
  stopPolling()
}

async function refresh() {
  loading.value = true
  try {
    const [s, t, admin, codex] = await Promise.all([
      api.getStatus(),
      api.getTasks(),
      api.getAdminStatus(),
      api.getMainCodexStatus(),
    ])
    status.value = s
    tasks.value = t
    adminStatus.value = admin
    codexStatus.value = codex
    runningTask.value = t.find(t => t.status === 'running' || t.status === 'pending') || null
  } catch (e) {
    if (e.status === 401) {
      authenticated.value = false
      return
    }
    console.error('刷新失败:', e)
  } finally {
    loading.value = false
  }
}

function onTaskStarted() {
  startPolling(10000)
  refresh()
}

function onAdminProgress() {
  startPolling(10000)
  refresh()
}

function startPolling(interval = 600000) {
  stopPolling()
  pollTimer = setInterval(async () => {
    await refresh()
    if (!busyTask.value && interval < 600000) {
      startPolling(600000)
    }
  }, interval)
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

onMounted(async () => {
  const ok = await checkAuth()
  if (ok) {
    refresh()
    startPolling(600000)
  }
})

onUnmounted(() => {
  stopPolling()
})
</script>
