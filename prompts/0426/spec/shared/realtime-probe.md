# Shared SPEC: 子号 + 母号实时探活(force_refresh)

## 0. 元数据 + 引用方

| 字段 | 内容 |
|---|---|
| 名称 | 子号实时探活(`POST /api/accounts/{email}/probe`)+ 模型清单(`GET /api/accounts/{email}/models`)+ 母号 force_refresh(`/api/admin/master-health?force_refresh=1`)|
| 版本 | **v1.0 (2026-04-28 Round 11 — 用户主动绕过 30min 节流 + 5min cache 实时拉取最新可用性)** |
| 主题归属 | 用户在 UI 主动触发的"立即探活"按钮 + 后端 endpoint 契约 + 与 sync_account_states 30min 节流的关系 + 与 master_health 5min cache 的关系 |
| 引用方 | Round 11 task `04-28-round11-master-resub-models-validate` / spec-2-account-lifecycle.md v1.7 / master-subscription-health.md v1.2 §13 / account-state-machine.md v2.1 / quota-classification.md / Round 11 AC1~AC7 |
| 共因 | Round 11 user Q3 实证:子号 / 母号状态不能及时探活,无法及时从失效→有效状态反映;Round 9 sync_account_states 30min 探测节流 + Round 8 master_health 5min cache 在用户视角下 "stale" |
| 不在范围 | 后台自动 retroactive(见 master-subscription-health v1.2 §11) / sync_account_states 自动节流(见 quota-classification.md §3.4)/ 5min cache 自身策略(见 master-subscription-health v1.0 §6) |

---

## 1. 概念定义

| 术语 | 定义 |
|---|---|
| `realtime probe` | 用户在 UI 主动点击"立即探活"按钮触发的一次性同步探测,绕过节流/cache 拿到最新结果 |
| `子号实时探活` | `POST /api/accounts/{email}/probe` — 强制对单个子号调 `cheap_codex_smoke` + `check_codex_quota`,落 `last_quota_check_at`,但**不修改** `status`(只读) |
| `模型清单` | `GET /api/accounts/{email}/models` — 用子号 access_token 调 OpenAI `/backend-api/models` 拿可用模型列表 |
| `母号 force_refresh` | `GET /api/admin/master-health?force_refresh=1` — 绕过 5min cache 实时探一次 master subscription 状态(Round 8 已有,Round 11 文档化) |
| `30min 节流` | `sync_account_states` 内部去重逻辑:同一 email 在 30min 内不重复探测(`last_quota_check_at` 字段记录) |
| `5min cache` | `is_master_subscription_healthy` cache TTL,5 分钟内不重复 GET /backend-api/accounts |

---

## 2. 端点契约

### 2.1 `POST /api/accounts/{email}/probe` — 子号实时探活

**Method / Path**:`POST /api/accounts/{email:str}/probe`

**Auth**:Bearer(同其他 `/api/accounts/*` 端点)

**Request body**:

```json
{
  "force_codex_smoke": true     // 强制调 cheap_codex_smoke 即使 24h cache 命中
}
```

**Response 200 OK**(成功):

```json
{
  "email": "alice@example.com",
  "status_before": "active",                   // probe 调用前的 acc.status (供前端对比 RT-I1)
  "status_after": "active",                    // probe 调用后的 acc.status (RT-I1:与 status_before 一致)
  "quota_status": "ok",                        // ok / exhausted / no_quota / auth_error / network_error
  "quota_info": {                              // check_codex_quota 200 时落盘的 quota 快照,其他状态 null
    "primary_pct": 35,
    "primary_total": 100,
    "primary_remaining": 65,
    "primary_resets_at": 1777702800,
    "weekly_pct": 10,
    "weekly_resets_at": 1778304000,
    "smoke_verified": true,
    "smoke_cache_hit": false
  },
  "smoke_result": "alive",                     // alive / auth_invalid / uncertain
  "smoke_detail": {                            // smoke alive 时为 dict;cache hit / 异常时为 str
    "model": "gpt-5",
    "response_text": "...",
    "raw_event": "response.completed",
    "tokens": 12
  },
  "last_quota_check_at": 1777699300.0          // probe 调用后落盘的 epoch seconds
}
```

**Response 200 OK**(异常归类 / smoke uncertain,RT-I2 — 业务路径永不抛 5xx):

```json
{
  "email": "alice@example.com",
  "status_before": "active",
  "status_after": "active",
  "quota_status": "network_error",
  "quota_info": null,
  "smoke_result": "uncertain",
  "smoke_detail": "exception:RuntimeError",
  "last_quota_check_at": 1777699300.0
}
```

**Response 404 Not Found**(email 不存在):

```json
{ "detail": "账号不存在" }
```

**Response 422 Unprocessable Entity**(`acc.auth_file == None / 文件不存在 / 缺 access_token`):

```json
// auth_file 缺失或文件不存在
{ "detail": { "error": "auth_file_missing", "message": "账号无可用 auth_file,无法探活" } }

// auth_file 解析失败(JSON 损坏)
{ "detail": { "error": "auth_file_unreadable", "message": "auth_file 解析失败: <ExcType>" } }

// auth_file 中缺 access_token
{ "detail": { "error": "access_token_missing", "message": "auth_file 中缺 access_token,无法探活" } }
```

**Response 400 Bad Request**(对主号调 probe — 主号请用 `/api/admin/master-health`):

```json
{ "detail": "主号不属于子号探活对象,请用 /api/admin/master-health" }
```

**副作用**(写盘,与 `sync_account_states` 接口对齐):
- `acc.last_quota_check_at = time.time()`(始终落盘,绕过 30min 节流 — RT-I3)
- `acc.last_codex_smoke_at = time.time()`(始终落盘,绕过 24h cache 时机判定)
- `acc.last_smoke_result = "alive"|"auth_invalid"|"uncertain"`(quota-classification.md §4.4 字段,Round 7 引入)
- `acc.last_quota = QuotaSnapshot(...)`(quota status="ok" 时落盘)
- **`acc.status` 不修改**(RT-I1)— probe 是只读探活,不改状态机;若 probe 发现 auth_invalid,由后续 sync_account_states / cmd_check 路径在 30min 后自然消化(届时 last_quota_check_at 仍生效,会跳过节流)

### 2.2 `GET /api/accounts/{email}/models` — 子号可用模型清单

**Method / Path**:`GET /api/accounts/{email:str}/models`

**Auth**:Bearer

**Response 200 OK**:

```json
{
  "email": "alice@example.com",
  "plan_type": "team",                              // 从 auth_file id_token JWT 解析
  "models": [
    {
      "slug": "gpt-5-thinking",                      // OpenAI 内部 slug
      "title": "GPT-5 Thinking",
      "description": "Latest reasoning model",
      "tags": ["team-only"],                         // 可选,team-only / general 等标签
      "max_tokens": 200000
    },
    {
      "slug": "gpt-5",
      "title": "GPT-5",
      "tags": ["general"]
    }
  ],
  "raw_response_preview": "{...}"                   // 可选,/backend-api/models 原始响应前 500 字
}
```

**Response 401 Unauthorized**(上游 token 401/403 透传):

```json
{ "detail": { "error": "auth_invalid", "http_status": 401 } }
```

**Response 502 Bad Gateway**(上游非 200 / JSON 解析失败 / 网络错):

```json
// upstream 5xx / 4xx (非 401/403)
{ "detail": { "error": "upstream_status_500", "body_preview": "..." } }

// 网络错
{ "detail": { "error": "network_error", "message": "<ExcType>" } }

// JSON 解析失败
{ "detail": { "error": "json_parse_error", "message": "<ExcType>" } }
```

**Response 503 Service Unavailable**(上游 timeout):

```json
{ "detail": { "error": "timeout" } }
```

**Response 404 / 422**:同 §2.1。

**副作用**:无(纯读 endpoint)

### 2.3 `GET /api/admin/master-health?force_refresh=1` — 母号 force_refresh

**已存在**(Round 8 落地,本 spec 仅文档化 Round 11 引用)。

| 入参 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `force_refresh` | bool query | `false` | true 时绕过 5min cache 实时探一次,触发 `is_master_subscription_healthy(force_refresh=True)` |

**Response 200 OK**(grace 期内,Round 11 v1.2):

```json
{
  "healthy": true,
  "reason": "subscription_grace",
  "evidence": {
    "current_user_role": "account-owner",
    "raw_item": {...},
    "grace_until": 1778304000.0,
    "grace_remain_seconds": 604700.0,
    "probed_at": 1777699300.0,
    "cache_hit": false
  }
}
```

**守恒**(M-I1 + §13 endpoint 守恒):**永不**返回 5xx,任何异常映射到 `(healthy=false, reason in {"auth_invalid", "network_error"}, evidence.detail=...)`。

---

## 3. 不变量

### 3.1 子号 probe 不变量

- **RT-I1**:`POST /api/accounts/{email}/probe` **不修改 `acc.status`**;只读探活,只落 `last_*` 字段。状态变更走 sync_account_states / cmd_check 路径(在 30min 节流后)
- **RT-I2**:**业务路径永不抛 5xx**(`cheap_codex_smoke` / `check_codex_quota` 异常都被吞为 `smoke_result="uncertain"` 或 `quota_status="network_error"` + 200 OK 返回);仅前置参数校验失败抛 4xx(404 邮箱不存在 / 422 auth_file_missing / 400 主号 probe)
- **RT-I3**:`acc.last_quota_check_at` 落盘后,`sync_account_states` 在 30min 节流窗口内**不**重复 probe(保持原 spec quota-classification §3.4 节流契约不变 — probe 是用户主动绕过,但写盘后下游路径继续遵守节流)
- **RT-I4**:**单 email 串行化** — 同一 email 同时只允许 1 个 probe in flight;并发请求返回 409 Conflict 或排队等待(后端实现自决,二选一)
- **RT-I5**:probe 的 `last_quota_check_at` 落盘必须先于响应返回(保证客户端拿到响应后立即 GET /api/status 能看到新值,UI 不会渲染 stale)

### 3.2 模型清单不变量

- **RT-I6**:`GET /api/accounts/{email}/models` **完全只读**,不写盘,不修改任何 acc 字段
- **RT-I7**:模型清单失败按 HTTP 状态分级映射:401/403 上游 → 401 + `detail.error="auth_invalid"`;5xx / 4xx 非 401/403 → 502 + `detail.error="upstream_status_<code>"`;timeout → 503;网络错 → 502 + `detail.error="network_error"`;JSON 解析失败 → 502 + `detail.error="json_parse_error"`。前端调用方应优雅处理这些 4xx/5xx,而非依赖固定 200 OK

### 3.3 母号 force_refresh 不变量

- **RT-I8**:`force_refresh=1` 时 `is_master_subscription_healthy(force_refresh=True)`,绕过 5min cache,但仍落 cache(下次 cache_hit=True 反映这次实测)
- **RT-I9**:`force_refresh=1` 仍遵守 master-subscription-health.md §13 endpoint 守恒(永不返回 5xx)

---

## 4. UI 集成

### 4.1 子号"立即探活"按钮

**位置**:`web/src/components/Dashboard.vue` 表格每行操作列(`<td>` 内,与"补登录" / "移出" / "导出" / "删除"按钮同级)

**显示条件**(Round 11 实施 — `canProbe(acc)`):
- `!acc.is_main_account`(主号有自己的"立即重测"按钮在 banner)
- `acc.auth_file != None`(orphan / pending 无 token,探不动)
- `acc.status != "pending"`(注册中无意义)

**交互**:
1. 点击 → 调 `api.probeAccount(email, true)`(`force_codex_smoke=true`)
2. loading 期间禁用 + 显示 spinner(同其他按钮的 `:loading` props)
3. 完成后:
   - `smoke_result="alive"` → success toast `"探活成功 · {email} · {time} · alive"`
   - `smoke_result="auth_invalid"` → warn toast `"探活完成 · token 失效 · {email}"`
   - `smoke_result="uncertain"` → info toast(默认)
4. emit 父组件 `refresh` → 父组件重新 GET /api/status 拉到最新 `last_quota_check_at` + UsabilityCell 自动重渲

**错误处理**:任何抛错(包括 422 no_auth_file / 404 not_found / 网络断)→ `toast.error('探活失败', err.message)`

### 4.2 母号"立即重测"按钮(已有,Round 8 落地)

**位置**:`web/src/components/MasterHealthBanner.vue`(banner 右上"立即重测"按钮)

**交互**:点击 → emit `refresh` → 父组件调 `api.getMasterHealth(true)`(force_refresh=1)。

**Round 11 文案对齐**:Round 11 后 master_health 在 grace 期内可能返回 `(True, "subscription_grace")`,banner severity 自动从 critical 切换为 warning(useStatus.js `masterHealthSeverity` 路由)。

### 4.3 模型清单 UI(可选 / Round 11 backlog)

`GET /api/accounts/{email}/models` 是 Round 11 实测自验工具(AC7),不强制要求 UI(后端 dev tool 可直接 curl 验证)。如未来加 UI,推荐位置:Dashboard 表格"导出"按钮旁加"模型"按钮 → 弹窗显示 plan_type + 可用模型列表。

---

## 5. 测试要点

`tests/unit/test_round11_realtime_probe.py` 应包含的 case(≥6):

| # | case | 验证点 |
|---|---|---|
| 1 | `test_probe_endpoint_returns_smoke_alive` | mock cheap_codex_smoke → "alive",断言 200 + smoke_result="alive" + last_quota_check_at 落盘 |
| 2 | `test_probe_endpoint_returns_smoke_auth_invalid` | mock cheap_codex_smoke → "auth_invalid",断言 200 + smoke_result="auth_invalid" + acc.status 不变(RT-I1)|
| 3 | `test_probe_endpoint_exception_returns_uncertain` | mock cheap_codex_smoke 抛 RuntimeError,断言 200 + smoke_result="uncertain" + error 字段(RT-I2)|
| 4 | `test_probe_endpoint_no_auth_file_returns_422` | acc.auth_file=None,断言 422 + detail.error ∈ {"auth_file_missing", "access_token_missing", "auth_file_unreadable"} |
| 5 | `test_probe_endpoint_account_not_found_returns_404` | email 不存在,断言 404 + detail="账号不存在" |
| 6 | `test_probe_endpoint_status_unchanged` | 任何分支 probe 后 acc.status 字段不被修改(RT-I1)|
| 7 | `test_models_endpoint_returns_team_models` | mock /backend-api/models 200,断言 response.models[] 非空 + plan_type 解析正确 |
| 8 | `test_models_endpoint_401_returns_401_auth_invalid` | mock /backend-api/models 401,断言 raise HTTPException(401) + detail.error="auth_invalid"(RT-I7 — 实现选择透传 401 而非吞为 200,与 spec §2.2 401 响应一致)|

---

## 6. 与 Round 9 5 触发点 retroactive 的关系

Round 9 master-subscription-health v1.1 §11 引入 5 触发点 retroactive helper(RT-1~RT-5 + RT-6),由后台自动驱动:

- **RT-1**:`app_lifespan` 启动时
- **RT-2**:`_auto_check_loop` 每个 interval
- **RT-3**:`cmd_check / _reconcile_team_members` 末尾
- **RT-4**:`sync_account_states` 末尾
- **RT-5**:`cmd_rotate` 末尾
- RT-6(已有):`cmd_reconcile` 末尾

**realtime probe ≠ retroactive**:

| 维度 | realtime probe(本 spec) | retroactive 5 触发点(Round 9 §11) |
|---|---|---|
| 触发方 | 用户在 UI 主动点击 | 后端自动周期 / 启动时 / 巡检完后 |
| 范围 | 单个 email(子号)或单次 master_health | 整个 workspace 内所有候选子号 |
| 目的 | 拿到最新 last_quota_check_at + smoke_result | 把 master cancelled 期内的 active 子号批量改 GRACE / 母号 healthy 时撤回 ACTIVE |
| 副作用 | **不**修改 acc.status(RT-I1) | **修改** acc.status(GRACE 进入 / 退出) |
| 节流 | 用户每次点击都生效(无节流) | 走 master_health 5min cache;helper 内 dry_run 可跳过 |

**两者互补**:
- 用户实时 probe 拿到 token 失效信号 → 提示用户去重登录(UI 出现"补登录"按钮)
- 后台 retroactive 在母号 cancel/续费/grace 切换时整体调整子号状态机
- realtime probe 完成后,下次 retroactive(下个 interval / 下次 sync)读到新的 last_quota_check_at 不重复 probe(节流复用)

---

## 7. 参考资料

- `master-subscription-health.md` v1.2 §13(endpoint 守恒,master_health force_refresh 已有)+ §14(subscription_grace)
- `quota-classification.md` §3.4 / §4.4(30min 节流 + 24h smoke cache)
- `account-state-machine.md` v2.1 §4.6(母号 × 子号联动表)
- Round 11 task PRD `04-28-round11-master-resub-models-validate/prd.md`(用户 Q3 实证 + Approach A 决策)

---

## 附录 A:修订记录

| 版本 | 时间 | 变更 |
|---|---|---|
| v1.0 | 2026-04-28 Round 11 | 初版 — 子号 `POST /probe` + `GET /models` + 母号 force_refresh 文档化。9 不变量(RT-I1~I9)+ 8 测试 case + UI 集成位置 + 与 Round 9 5 触发点 retroactive 的关系。配套 Round 11 task `04-28-round11-master-resub-models-validate` AC5 落地。 |
| v1.0.1 | 2026-04-28 Round 11 check 阶段 | 文档与实现对齐 — (1) §2.1 200 OK 字段名改为实现实际返回的 `status_before/status_after/quota_status/quota_info/smoke_result/smoke_detail/last_quota_check_at`(与 spec 草稿的 `status/quota/quota.info` 不同);(2) §2.1 Request body 移除 `force_quota_check`(实现仅 force_codex_smoke);(3) §2.1 404/422/400 错误响应字段对齐实现命名(`auth_file_missing/auth_file_unreadable/access_token_missing` + 404 `detail="账号不存在"` + 400 主号 probe);(4) §2.2 401/502/503 改为透传上游 HTTPException(non-200,与 RT-I7 重写一致);(5) §3.1 RT-I2 修正为"业务路径永不抛 5xx",前置参数校验 4xx 仍允许;(6) §3.2 RT-I7 重写为按 HTTP 状态分级映射;(7) §5 测试要点 case 4/5/8 命名对齐实现。无功能性改动,仅 spec 文本与实现行为对齐。 |

---

**文档结束。** 工程师据此可直接编写两个新 endpoint(`POST /api/accounts/{email}/probe` + `GET /api/accounts/{email}/models`)、补充 master_health endpoint 的 force_refresh 文档、UI 子号实时探活按钮接入,无需额外决策。
