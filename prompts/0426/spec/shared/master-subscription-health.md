# Shared SPEC: Master 母号订阅健康度探针

## 0. 元数据 + 引用方

| 字段 | 内容 |
|---|---|
| 名称 | Master ChatGPT Team 母号订阅降级探针(三层判定 + 5min 缓存 + 触发位点矩阵 + retroactive 重分类 + grace 期 healthy=True) |
| 版本 | **v1.4.1 (2026-04-29 Round 11 五轮 spec-update — §15.6 关系表新增 `OAUTH_SUBPROCESS_TIMEOUT_S` 行,显式标注与 M-OA-backoff 维度区别(单次上界 vs 失败间隔下界);引用方加 `oauth-subprocess-timeout.md` v1.0.0 与 `account-state-machine.md` v2.1.2 §4.7;纯 spec 增量,无代码改动)** |
| 主题归属 | `is_master_subscription_healthy()` 函数契约 + 三层探针 + 缓存策略 + 5 个触发位点 + 5 个误判缓解 + retroactive helper 5 触发点 + grace 期 JWT 解析(双路径:OAuth id_token + web access_token)+ endpoint 守恒 + **subscription_grace healthy=True 状态(Round 11)** |
| 引用方 | PRD-7(Round 8) / Round 9 task `04-28-account-usability-state-correction` / **Round 11 task `04-28-round11-master-resub-models-validate`(一轮 + 二轮 + 五轮)** / spec-2-account-lifecycle.md **v1.7** §3.7 / `./account-state-machine.md` **v2.1.2** §4.4 / §4.7 / `./oauth-workspace-selection.md` **v1.5.0** §4.4 / `./oauth-subprocess-timeout.md` **v1.0.0** / `./realtime-probe.md` v1.0 / FR-M1~M4 / AC-B1~AC-B8 / **Round 11 AC1~AC4** |
| 共因 | Round 8 PRD §1 外部根因 — `eligible_for_auto_reactivation: true` 在 grace 期内立即为 true(**Round 11 user Q1 实证**:ChatGPT 网页 team 权限仍可用),v1.0/v1.1 误把它等价于 "cancel_at_period_end=true 且 period 已过";Round 9 共因 — retroactive helper 仅挂 cmd_reconcile 一处导致 stale-active;**Round 11 二轮共因** — v1.2 `_load_admin_id_token` 仅读 `accounts/codex-main-*.json`(OAuth 重登路径),用户走 web session 路径时永远拿不到 id_token → grace_until 解不出 → fallback 误判 cancelled。实证 dump 显示 web access_token JWT 不含 `chatgpt_subscription_active_until` 但含 `chatgpt_plan_type`,需用此字段做 fallback grace 判定。 |
| 不在范围 | OAuth 显式选 personal workspace(见 [`./oauth-workspace-selection.md`](./oauth-workspace-selection.md)) / wham/usage 配额分类(见 [`./quota-classification.md`](./quota-classification.md)) / 自动续费 / 多母号支持(超 PRD-7 Out of Scope) |

---

## 1. 概念定义

| 术语 | 定义 |
|---|---|
| `master account` | AutoTeam 主号(workspace owner / admin),即被 admin_state cookies 登录、持有 ChatGPT Team 订阅的"母号" |
| `master subscription` | master account 在 OpenAI 后端持有的 ChatGPT Team 订阅(由 Stripe 计费),其状态决定子号 invite 后能否拿到 `plan_type=team` |
| `subscription healthy` | 订阅处于 `active` 状态,可继续生产子号 |
| `subscription degraded` | 订阅 cancel 但 workspace 实体仍存在,`/backend-api/accounts` items[*].`eligible_for_auto_reactivation == True`;子号 invite 后必拿 `plan_type=free` |
| `eligible_for_auto_reactivation` | OpenAI 内部字段,**Round 11 修正**:对应 Stripe `subscription.cancel_at_period_end=true`(grace 期内**立即**为 true,不是 "period 已过"才置位)。Round 11 user Q1 实证:该字段为 true 时 ChatGPT 网页 team 权限仍有效,新 invite 在 grace 期内仍能拿 plan_type=team。详见 §14 subscription_grace 状态。 |
| `subscription_grace` | **(Round 11 healthy=True 状态;v1.3 双信号源)** master 母号 `eligible_for_auto_reactivation=true` 且**任一**条件成立:(a) admin id_token JWT 的 `chatgpt_subscription_active_until > now()`(OAuth 路径,有倒计时);(b) admin access_token JWT 的 `chatgpt_plan_type ∈ {team, business, enterprise, edu}`(web session 路径,无倒计时但权益仍生效)。订阅在 grace 期内,新 invite 仍能拿 plan_type=team,fail-fast 入口对此状态放行(因 healthy=True) |
| `三层探针` | (L1) `eligible_for_auto_reactivation` authoritative 主探针 / (L2) 新邀请子号 plan_type corroborating 反推 / (L3) `/billing` 401/404 + workspace `/settings.plan != "team"` safety net |

---

## 2. 完整数据契约

### 2.1 类型定义(置于 `src/autoteam/chatgpt_api.py` 末尾或新模块 `master_health.py` 顶部)

```python
from typing import Literal, Optional, TypedDict

MasterHealthReason = Literal[
    "active",                  # 健康 — 可继续生产子号
    "subscription_grace",      # (Round 11 v1.3) eligible_for_auto_reactivation=True 且任一信号有效:
                               # (a) JWT chatgpt_subscription_active_until > now (有倒计时,OAuth id_token);
                               # (b) JWT chatgpt_plan_type ∈ {team, business, enterprise, edu} (无倒计时,web access_token);
                               # healthy=True,可继续生产子号 (新 invite 仍 plan_type=team)
    "subscription_cancelled",  # eligible_for_auto_reactivation=True 且双信号都失败:
                               # grace_until 缺失/已过期 + plan_type ∈ {free, None}
    "workspace_missing",       # /accounts 找不到目标 workspace(实体已删 / account_id 漂移)
    "role_not_owner",          # current_user_role != account-owner / admin
    "auth_invalid",             # /accounts 401/403,master session 已失效
    "network_error",           # DNS / timeout / SSL / 5xx,需上层决定保守路径
]


class MasterHealthEvidence(TypedDict, total=False):
    """is_master_subscription_healthy 返回的 evidence dict 字段集。"""
    raw_account_item: dict        # /backend-api/accounts items[i] 完整原始记录(便于事后排查)
    http_status: int              # /backend-api/accounts 响应状态码
    probed_at: float              # epoch seconds,本次 probe 开始时间(非 cache 命中时间)
    cache_hit: bool               # 本次返回是否来自 5min cache
    cache_age_seconds: Optional[float]  # cache 命中时距上次实测时间;cache miss 为 None
    detail: Optional[str]         # 失败原因纯文本(network_error / workspace_missing 时填)
    items_count: Optional[int]    # /accounts 返回的 items[] 总数(workspace_missing 调试用)
    account_id: Optional[str]     # 实际比对使用的 master account_id
    current_user_role: Optional[str]  # role_not_owner 时记录原始字面量
    # —— 副探针字段(L2/L3 命中时填,主探针命中时缺省) ——
    plan_field: Optional[str]     # /accounts/{wid}/settings.plan(若 fetch 成功,L3)
    billing_status: Optional[int] # /backend-api/billing/* 探测的 HTTP 状态(L3 兜底)


# 函数返回类型
MasterHealthResult = tuple[bool, MasterHealthReason, MasterHealthEvidence]
```

### 2.2 函数签名(实施期目标)

```python
def is_master_subscription_healthy(
    chatgpt_api: "ChatGPTTeamAPI",
    *,
    account_id: Optional[str] = None,
    timeout: float = 10.0,
    cache_ttl: float = 300.0,
    force_refresh: bool = False,
) -> MasterHealthResult:
    """判定 master 母号 ChatGPT Team 订阅是否健康。

    三层探针(优先级从高到低):
      L1 (authoritative) — /backend-api/accounts items[].eligible_for_auto_reactivation
      L2 (corroborating) — 新邀请子号 OAuth bundle.plan_type 反推(由 _run_post_register_oauth 的
                            既有 plan_drift 路径承担,本函数 *不* 主动触发 invite,只读 cache 旁证)
      L3 (safety net)    — workspace /settings.plan + /billing 401/404 兜底(当 L1 字段缺失时)

    参数:
      chatgpt_api: 已 start() 的 ChatGPTTeamAPI 实例(主号 session)
      account_id: 目标 master workspace account_id;None → 内部从 get_chatgpt_account_id() 取
      timeout: 单次 HTTP 探测超时(秒)
      cache_ttl: 5 分钟内不重复探测;0 表示禁用 cache
      force_refresh: True 表示忽略 cache 强制实测(用于 /api/admin/master-health 手动刷新)

    返回:
      (healthy, reason, evidence)
      healthy True  仅当 reason == "active"
      healthy False 当 reason ∈ {subscription_cancelled, workspace_missing, role_not_owner, auth_invalid, network_error}

    不变量:
      - 函数永不抛异常(所有 Exception 转 network_error,与 check_codex_quota 对齐)
      - cache 命中不产生 HTTP 调用(cache_hit=True + raw_account_item 来自上次实测,
        cache_age_seconds 反映距上次实测的秒数)
      - cache miss 触发 1 次 GET /backend-api/accounts;不触发 invite / settings / billing(L3 仅
        在 L1 字段缺失时执行,且至多 1 次额外 HTTP)
    """
```

### 2.3 缓存落盘契约(`accounts/.master_health_cache.json`)

```json
{
  "schema_version": 2,
  "cache": {
    "<master_account_id_uuid>": {
      "healthy": true,
      "reason": "subscription_grace",
      "probed_at": 1777357190.0,
      "evidence": {
        "raw_account_item": { "...": "..." },
        "http_status": 200,
        "current_user_role": "account-owner",
        "plan_type_jwt": "team",
        "grace_until": 1777699200.0
      }
    }
  }
}
```

**字段约束**:

| 字段 | 类型 | 约束 |
|---|---|---|
| `schema_version` | int | **当前 2(v1.3 升级)**;v1 仅支持 grace_until 解析,v1.3 加 plan_type fallback 后旧 v1 cache 持久化的 cancelled 误判必须作废。`_load_cache` 检测到 `data.get("schema_version") != CACHE_SCHEMA_VERSION` 时整体丢弃返回空 cache(treat as miss),触发新一轮 force_refresh。**未来 schema 不兼容时继续 +1**。 |
| `cache` | dict[str, entry] | key 是 master account_id(UUID);允许多 master 共存(将来 E3 多母号扩展) |
| `entry.healthy` | bool | True/False,与函数返回 healthy 一致 |
| `entry.reason` | str | 7 个 MasterHealthReason 字面量之一 |
| `entry.probed_at` | float | 实测 epoch seconds;cache_age = `time.time() - probed_at` |
| `entry.evidence` | dict | 写盘前裁剪敏感字段(token / cookie 不入盘);默认仅写 `raw_account_item` 子集 + http_status + (v1.3 新增)`plan_type_jwt` + `grace_until`(诊断字段,JWT 解出值,grace_until 可能 None) |

**裁剪规则**:`raw_account_item` 落盘时只保留 `id / structure / current_user_role / eligible_for_auto_reactivation / name / workspace_name`,丢弃 OpenAI 后端可能附带的 token/email/seat 列表等(避免把 token 写入磁盘)。

---

## 3. 行为契约(三层探针执行规则)

### 3.1 前置条件

- `chatgpt_api` 必须已 `start()` 成功(主号 session cookie 可用);否则 L1 直接 `auth_invalid`
- `account_id` 优先级:函数参数 > `get_chatgpt_account_id()` > admin_state cookies / `.env` `CHATGPT_ACCOUNT_ID`
- 调用方在并发场景下不需要自己加锁 — 本函数内部读写 `.master_health_cache.json` 走 `accounts.json` 同款 file-lock

### 3.2 后置条件

- 返回元组永远 3 元素;第 2 元素必为 6 个 `MasterHealthReason` 字面量之一
- `healthy == True ⇔ reason == "active"`(双向蕴含,严格)
- cache 命中 → `evidence["cache_hit"] == True` + `cache_age_seconds` 不为 None;
  cache miss → `cache_hit` 缺省或 `False`,`cache_age_seconds` 为 None,且 `probed_at` 是本次实测时间
- 函数永不抛异常;任何 Exception 归为 `("network_error", evidence)` 返回

### 3.3 三层探针执行顺序

```
┌──────────────────────────────────────────────────────────────────┐
│ Step 0: cache lookup                                              │
│   if (not force_refresh) and cache_age < cache_ttl:               │
│     → 直接返回 cache 中的 (healthy, reason, evidence|cache_hit)    │
├──────────────────────────────────────────────────────────────────┤
│ Step 1: L1 主探针 — GET /backend-api/accounts                       │
│   ├─ 401/403            → ("auth_invalid", ..)                    │
│   ├─ 5xx / network      → ("network_error", ..)                   │
│   ├─ 200 + items[] 中找不到目标 account_id  → ("workspace_missing")  │
│   ├─ 200 + 找到目标 + role 不在 {account-owner, admin, org-admin,   │
│   │       workspace-owner}  → ("role_not_owner")                  │
│   ├─ 200 + 找到目标 + eligible_for_auto_reactivation == True       │
│   │       → ("subscription_cancelled")  ← 主判定命中                │
│   └─ 200 + 上述都不命中  → 进入 Step 2 L3 副判定                    │
├──────────────────────────────────────────────────────────────────┤
│ Step 2: L3 副判定 — workspace /settings.plan(可选,仅当 L1 字段缺失) │
│   背景:OpenAI 后端可能去除 eligible_for_auto_reactivation 字段(误判 │
│   缓解 §5.1 场景 A)。L3 在 L1 没拒判但又拿不到该字段时再确认一次:    │
│   ├─ GET /backend-api/accounts/{account_id}/settings              │
│   ├─ settings.plan ∉ {"team", "business", "enterprise"}           │
│   │       → ("subscription_cancelled", evidence with plan_field)  │
│   ├─ 字段缺失 / 200 但 plan 字段不存在  → 视作 active(不能反向认定)  │
│   └─ 401/403  → ("auth_invalid")                                   │
│   注:L3 不主动调 /backend-api/billing(老 API 已 410/redirect),由   │
│         研究 §2 表 1 确认;仅在未来字段失效时新增 1 处 HTTP。        │
├──────────────────────────────────────────────────────────────────┤
│ Step 3: 落盘 cache + 返回                                          │
│   write_cache({account_id, healthy, reason, probed_at, evidence})  │
│   return (healthy, reason, evidence)                                │
└──────────────────────────────────────────────────────────────────┘
```

### 3.4 异常映射

| 实际异常 | 归类 |
|---|---|
| `requests.exceptions.ConnectionError` | network_error |
| `requests.exceptions.Timeout` | network_error |
| `requests.exceptions.SSLError` | network_error |
| `requests.exceptions.RequestException`(其他) | network_error |
| HTTP 401 / 403 | auth_invalid |
| HTTP 5xx / 429 / 其他 4xx | network_error |
| JSON 解析失败 | network_error |
| 任何未识别 `Exception` 兜底 | network_error |

**对齐**:与 `check_codex_quota`(`shared/quota-classification.md §3.3`)异常映射对齐 — `auth_*` 与 `network_*` 必须严格区分,网络抖动绝不能落入 `auth_invalid`(否则上层会触发"主号重登"等破坏性流程)。

---

## 4. 触发位点矩阵(5 处)

| # | 文件:函数 | 同步/异步 | 失败行为 | 备注 |
|---|---|---|---|---|
| M-T1 | `manager.py:_run_post_register_oauth` 入口(`leave_workspace=True` personal 分支,~L1528 之前) | 同步 | `subscription_cancelled` → `record_failure(category="master_subscription_degraded", stage="run_post_register_oauth_personal_precheck")` + `update_account(email, status=STATUS_STANDBY)` + 不进 OAuth 流程 + `_record_outcome("master_degraded")` | **PRD-7 R1 入口** — fail-fast,避免浪费 2 分钟跑出 plan_drift |
| M-T2 | `manager.py:_run_post_register_oauth` Team 分支入口(`leave_workspace=False`,~L1462 之前) | 同步 | 同 M-T1,但 stage="run_post_register_oauth_team_precheck",且子号已 invite → 走 `_cleanup_team_leftover` 不直接 STANDBY | 母号降级时 Team invite 也会拿 `plan_type=free`(已实测 28 条 plan_drift),对称拦截 |
| M-T3 | `api.py:fill_team_task` / `fill_personal_task` 任务起点(`/api/tasks/fill` handler 入口) | 同步 | `subscription_cancelled` / `auth_invalid` → 直接 HTTP 503,body `{"error": "master_subscription_degraded", "reason": "<reason>", "evidence": <裁剪后 evidence>}` | 让前端直接显示告警横幅,而非等 2 分钟拿到失败 |
| M-T4 | `api.py:get_admin_diagnose`(`/api/admin/diagnose` 现有 4-probe 实现旁挂) | 同步 | 任何 reason 都返回(放在 response body 新增 `master_subscription_state` 字段) | 给 UI Settings 页面横幅数据;支持 `?force_refresh=1` query param |
| M-T5 | `manager.py:cmd_reconcile` 启动前(reconcile entry,~L161-471 入口) | 同步 | `subscription_cancelled` → reconcile 仅做"扫描不动作",日志告警 + 不执行 KICK / state 改写;其他不健康 reason → 跳过 reconcile 这一轮 | 防止母号降级时 reconcile 错误 KICK 健康账号(因为 wham 401 假阳性) |

**T6(可选,Round 8 不实施)**:`api.py` background task 定时器,每 5 分钟主动 probe 1 次推 UI banner 推送 — 留 E1 演进项,Round 8 暂不引入新后台任务。

### 4.1 触发顺序与依赖

```
用户点 fill (前端)
      │
      ▼
M-T3 api.fill_*_task 入口  ──── 母号降级 → 503 ──→ 前端横幅 (不进后端)
      │ healthy
      ▼
manager 启动
      │
      ├── Team 分支:M-T2 → 入 invite → OAuth → bundle 检查
      │
      └── personal 分支:M-T1 → 入 leave_workspace → OAuth (workspace/select)
                                                    └── 见 oauth-workspace-selection.md
```

---

## 5. 误判分析(5 个场景 + 缓解)

### 5.1 False Negative — 应判 degraded 却判 healthy

| # | 场景 | 风险 | 缓解 |
|---|---|---|---|
| FN-A | OpenAI 改字段名 / 移除 `eligible_for_auto_reactivation`(后端字段稳定性无 SLA,research §1.3 已列为开放问题) | 高 — 整套 fail-fast 失效,回到 28 条 plan_drift 状态 | (1) L3 副判定兜底:`/settings.plan != "team"` 也能命中;(2) `register_failures.json` `plan_drift` 持续观测,1-2 周内 0 命中视为字段失效预警 |
| FN-B | `/backend-api/accounts` 被 Cloudflare challenge / 5xx | 中 — `network_error` 时缺省**保守失败**(不视作 healthy) | 调用方按 §5.3 处置:`network_error` 不 fail-fast,记 warning + 走原 OAuth 流程,失败由现有 plan_drift 拦截 |
| FN-C | 字段返回 `null` 而非 `True`(OpenAI 字段为可空) | 低 — 严格按 `is True` 比对,`null/false/missing` 都视作 active | 仅当字段值严格 `True`(boolean)时判 degraded;实施代码必须用 `target.get("eligible_for_auto_reactivation") is True`,不能用 `target.get(...) or False` |
| FN-D | Master 续费 webhook 未到 OpenAI(用户刚续费 30 秒内) | 低 — 短暂窗口仍判 degraded | (1) cache_ttl 5min 让用户主动 force_refresh / `/api/admin/master-health?force_refresh=1`;(2) 失败横幅文案明确"如已续费请 1 分钟后刷新" |
| FN-E | 缓存窗口内 master 已降级 | 中 — cache 5min 内仍判 healthy | 保持 5min TTL 不延长(业务可接受 5min 延迟,延长 cache 反而扩大此窗口);加 force_refresh 入口 |

### 5.2 False Positive — 应判 healthy 却判 degraded

| # | 场景 | 风险 | 缓解 |
|---|---|---|---|
| FP-A | `eligible_for_auto_reactivation` 同时表示 trial / past_due(字段语义不止 cancel) | 中 — research §1.3 暂定为 cancel 强信号,但 OpenAI 未发布字段语义文档 | 实施期 1-2 周内 `register_failures` 抽样:degraded 命中且子号实际拿 `plan_type=team` 的反例 → 字段语义比预期宽,需补充判定条件(例如 `subscription_status` 字段) |
| FP-B | Master 暂时被踢出 owner role(权限漂移) | 低 — `role_not_owner` 单独分类,UI 提示重新接管 | reason 区分,UI 文案不同(degraded 是订阅问题,role_not_owner 是权限问题),用户能定位具体动作 |
| FP-C | 多 workspace 误取错 account(account_id 漂移) | 中 — `.env CHATGPT_ACCOUNT_ID` 与 admin_state 实际登录不一致(PRD §1 已观测) | strict 比对 `target.id == account_id` 字符串相等;不命中 → `workspace_missing`(明确分类,不混入 cancelled) |
| FP-D | cache 过期前 user 已手动 reactivate | 低 — 5min TTL 可接受 | 提供 `/api/admin/master-health?force_refresh=1` 入口,UI 横幅旁加"立即重测"按钮 |
| FP-E | 非 owner 角色不暴露该字段(research §7 Q4 未决) | 中 — 字段对 user/member 可能为空 → `null`,与 cancelled 字段缺失同形 | (1) 先判 role_not_owner;(2) FP-C 强相关 — 如果实测发现 owner 也读不到该字段,降级 L3 副判定 |

### 5.3 调用方推荐处置(对应 §4 触发位点)

```python
# 通用模板(M-T1 / M-T2)
healthy, reason, evidence = is_master_subscription_healthy(chatgpt_api)
if not healthy:
    if reason == "subscription_cancelled":
        # P0:fail-fast,不进 OAuth
        record_failure(
            email,
            category="master_subscription_degraded",
            reason=f"master {get_admin_email()} 订阅已取消(eligible_for_auto_reactivation=true)",
            stage=<stage 名,见 §4 矩阵>,
            master_account_id=evidence.get("raw_account_item", {}).get("id"),
            master_role=evidence.get("current_user_role"),
        )
        update_account(email, status=STATUS_STANDBY)
        _record_outcome("master_degraded", reason="master subscription cancelled")
        return None

    if reason in ("network_error", "auth_invalid"):
        # 保守路径:走原 OAuth,失败由现有 plan_drift / oauth_failed 兜底
        logger.warning("[注册] master health probe 不确定 (%s),按既有逻辑放行", reason)

    elif reason in ("workspace_missing", "role_not_owner"):
        # 中度异常:记录但放行(reconcile 会处理 workspace 漂移 / 权限掉线)
        logger.warning("[注册] master 异常 (%s, role=%s),仍尝试 OAuth",
                       reason, evidence.get("current_user_role"))
```

---

## 6. 缓存策略

### 6.1 TTL 选择理由

- **5 分钟 TTL**:research §4.3 + PRD-7 默认建议
  - master 订阅状态变更通常需要用户手动 cancel / reactivate,**不会高频抖动**
  - 5min 内若用户产生 4-6 个并发任务,可全部复用同一 cache,减少 API 调用噪声
  - 5min 也是 OpenAI 后端字段最终一致性的合理上限(research §1.3 推测)
- **不延长 TTL 的理由**:延长会扩大 FN-E 窗口(降级后仍误判 healthy)
- **不缩短 TTL 的理由**:`/backend-api/accounts` 本身 200ms,但 ChatGPTTeamAPI 上下文切换 + Cloudflare 等 ~3-8s,过短会拖慢 fill 链路

### 6.2 缓存失效时机(invalidation triggers)

| 时机 | 行为 |
|---|---|
| `cache_age >= cache_ttl` | 自然过期,下次 probe 实测 + 重写 cache |
| `force_refresh=True` 调用 | 忽略 cache,实测后重写 cache |
| `/api/admin/master-health?force_refresh=1` | 同上;UI Settings 页"立即重测"按钮触发 |
| schema_version 不一致 | 整体丢弃 cache 文件,作 miss 处理 |
| **不**触发失效 | 子号 OAuth 拿到 plan_drift / kick 单个子号 / reconcile 单轮 — 这些不是 master 订阅状态变化的可靠信号 |

### 6.3 并发安全

- 读写 `.master_health_cache.json` 走 `load_accounts / save_accounts` 同款 file-lock(避免并发 fill 任务读到半写状态)
- 不在内存维护 cache singleton(进程重启后从盘读起,与 admin_state 持久化机制对齐)

---

## 7. 不变量(Invariants)

- **M-I1**:`is_master_subscription_healthy` 永不抛异常(任何 Exception 归为 `network_error` 返回)
- **M-I2**:`auth_invalid` 与 `network_error` 严格区分;**401/403 是 auth_invalid 唯一来源**;5xx / Timeout / Connection 必落 network_error
- **M-I3**:**(Round 11 v1.2 BREAKING 扩展)** `healthy == True ⇔ reason ∈ {"active", "subscription_grace"}`;`healthy == False ⇔ reason ∈ {"subscription_cancelled", "workspace_missing", "role_not_owner", "auth_invalid", "network_error"}`。**v1.0/v1.1** 旧约束 `healthy == True ⇔ reason == "active"` 仅在 v1.2 之前有效;Round 11 引入 subscription_grace 后扩展为两枚字面量。任何代码路径不能让 `healthy=True` 配上面"healthy=False"反集合中的 reason,反之亦然。
- **M-I4**:cache 命中时**不发起任何 HTTP 调用**(L1 / L3 / billing 都不调);命中后 evidence 中 `cache_hit=True` + `cache_age_seconds is not None`
- **M-I5**:cache miss 时**最多发 2 次 HTTP**(L1 + 可选 L3);L2(invite 反推)由 OAuth 既有路径承担,本函数不主动 invite
- **M-I6**:落盘 evidence **不含** access_token / refresh_token / cookie / `__Secure-next-auth.session-token` 等敏感字段;只允许 `raw_account_item` 的白名单子集(§2.3 裁剪规则)
- **M-I7**:`eligible_for_auto_reactivation` 严格 `is True` 比对,**不**用 truthy 判断(防止 `null / "true" / 1` 等假信号触发误判)
- **M-I8**:M-T1 / M-T2 触发位点的 `record_failure` 必须使用 category=`master_subscription_degraded`(spec-2 v1.5 register_failures schema 新增枚举);`stage` 必须明确 OAuth 分支(team/personal),便于日后按分支统计命中率
- **M-I9**:reason=`subscription_cancelled` 是**唯一**触发 fail-fast 的分支;其他 5 个 reason 都走"保守放行 + 记录"路径(避免一次抖动让所有 fill 全瘫)
- **M-I10**:M-T5 reconcile 入口若 master 不健康,**禁止**执行 KICK / state flip 改写动作;只允许 read-only 扫描和日志输出 — 防止误踢真活号

---

## 8. 单元测试 fixture 与样本数据

### 8.1 `/backend-api/accounts` 响应样本

```json
// tests/fixtures/master_accounts_responses.json
{
  "active_team": {
    "status_code": 200,
    "body": {
      "items": [
        {
          "id": "b328bd37-aaaa-bbbb-cccc-16d08e98a0b5",
          "structure": "workspace",
          "current_user_role": "account-owner",
          "name": "Master Team",
          "workspace_name": "Master Team",
          "eligible_for_auto_reactivation": false
        },
        {
          "id": "personal-uuid-1111-2222-3333-444455556666",
          "structure": "personal",
          "current_user_role": "account-owner"
        }
      ]
    }
  },
  "subscription_cancelled": {
    "status_code": 200,
    "body": {
      "items": [
        {
          "id": "b328bd37-aaaa-bbbb-cccc-16d08e98a0b5",
          "structure": "workspace",
          "current_user_role": "account-owner",
          "name": "Master Team",
          "eligible_for_auto_reactivation": true
        }
      ]
    }
  },
  "field_missing_treat_as_active": {
    "status_code": 200,
    "body": {
      "items": [
        {
          "id": "b328bd37-aaaa-bbbb-cccc-16d08e98a0b5",
          "structure": "workspace",
          "current_user_role": "account-owner"
        }
      ]
    }
  },
  "field_null_treat_as_active": {
    "status_code": 200,
    "body": {
      "items": [
        {
          "id": "b328bd37-aaaa-bbbb-cccc-16d08e98a0b5",
          "structure": "workspace",
          "current_user_role": "account-owner",
          "eligible_for_auto_reactivation": null
        }
      ]
    }
  },
  "workspace_missing": {
    "status_code": 200,
    "body": {
      "items": [
        {
          "id": "different-uuid-not-master",
          "structure": "workspace",
          "current_user_role": "user"
        }
      ]
    }
  },
  "role_not_owner": {
    "status_code": 200,
    "body": {
      "items": [
        {
          "id": "b328bd37-aaaa-bbbb-cccc-16d08e98a0b5",
          "structure": "workspace",
          "current_user_role": "user"
        }
      ]
    }
  },
  "auth_invalid_401": {
    "status_code": 401,
    "body": {"error": {"code": "invalid_token"}}
  },
  "network_error_500": {
    "status_code": 500,
    "body": {"error": "Internal Server Error"}
  }
}
```

### 8.2 推荐单测代码

```python
# tests/unit/test_master_subscription_probe.py
import json
import time
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from autoteam.chatgpt_api import is_master_subscription_healthy  # 或 master_health 模块

FIXTURE_PATH = Path("tests/fixtures/master_accounts_responses.json")
FIXTURE = json.loads(FIXTURE_PATH.read_text())
TARGET_WID = "b328bd37-aaaa-bbbb-cccc-16d08e98a0b5"


def _mock_chatgpt_api(sample_name: str):
    sample = FIXTURE[sample_name]
    api = MagicMock()
    api._api_fetch.return_value = {
        "status": sample["status_code"],
        "body": json.dumps(sample["body"]),
    }
    return api


@pytest.mark.parametrize("name,expected_healthy,expected_reason", [
    ("active_team",                   True,  "active"),
    ("subscription_cancelled",        False, "subscription_cancelled"),
    ("field_missing_treat_as_active", True,  "active"),  # FN-C 缓解
    ("field_null_treat_as_active",    True,  "active"),
    ("workspace_missing",             False, "workspace_missing"),
    ("role_not_owner",                False, "role_not_owner"),
    ("auth_invalid_401",              False, "auth_invalid"),
    ("network_error_500",             False, "network_error"),
])
def test_master_health_classification(name, expected_healthy, expected_reason, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # 隔离 cache 文件
    api = _mock_chatgpt_api(name)
    healthy, reason, evidence = is_master_subscription_healthy(
        api, account_id=TARGET_WID, cache_ttl=0,
    )
    assert healthy is expected_healthy
    assert reason == expected_reason
    if reason == "active":
        assert healthy is True
    else:
        assert healthy is False  # M-I3 双向蕴含


def test_cache_hit_no_http(tmp_path, monkeypatch):
    """M-I4:cache 命中不发起 HTTP."""
    monkeypatch.chdir(tmp_path)
    api = _mock_chatgpt_api("subscription_cancelled")
    healthy1, reason1, evidence1 = is_master_subscription_healthy(api, account_id=TARGET_WID)
    assert evidence1.get("cache_hit") is False
    api._api_fetch.reset_mock()
    healthy2, reason2, evidence2 = is_master_subscription_healthy(api, account_id=TARGET_WID)
    assert evidence2.get("cache_hit") is True
    assert evidence2.get("cache_age_seconds") is not None
    assert api._api_fetch.call_count == 0  # 关键不变量
    assert (healthy2, reason2) == (healthy1, reason1)


def test_force_refresh_bypasses_cache(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    api = _mock_chatgpt_api("active_team")
    is_master_subscription_healthy(api, account_id=TARGET_WID)
    api._api_fetch.reset_mock()
    is_master_subscription_healthy(api, account_id=TARGET_WID, force_refresh=True)
    assert api._api_fetch.call_count == 1


def test_field_strict_is_true_only(tmp_path, monkeypatch):
    """M-I7:严格 `is True`,不接受 truthy."""
    monkeypatch.chdir(tmp_path)
    api = MagicMock()
    api._api_fetch.return_value = {
        "status": 200,
        "body": json.dumps({"items": [{
            "id": TARGET_WID, "structure": "workspace",
            "current_user_role": "account-owner",
            "eligible_for_auto_reactivation": "true",  # 字符串 truthy
        }]}),
    }
    healthy, reason, _ = is_master_subscription_healthy(api, account_id=TARGET_WID, cache_ttl=0)
    assert healthy is True and reason == "active"


def test_evidence_no_token_leak(tmp_path, monkeypatch):
    """M-I6:落盘 evidence 不含敏感字段."""
    monkeypatch.chdir(tmp_path)
    api = MagicMock()
    api._api_fetch.return_value = {
        "status": 200,
        "body": json.dumps({"items": [{
            "id": TARGET_WID, "structure": "workspace",
            "current_user_role": "account-owner",
            "eligible_for_auto_reactivation": True,
            "session_token": "SHOULD_NOT_PERSIST",
            "access_token": "SHOULD_NOT_PERSIST",
        }]}),
    }
    is_master_subscription_healthy(api, account_id=TARGET_WID, cache_ttl=300)
    cache_path = Path(".") / "accounts" / ".master_health_cache.json"
    if cache_path.exists():
        text = cache_path.read_text()
        assert "SHOULD_NOT_PERSIST" not in text
        assert "session_token" not in text
        assert "access_token" not in text
```

---

## 9. 与既有 spec / FR 的关系

| 关系对象 | 说明 |
|---|---|
| `spec-2 v1.5 §3.6` | 引用本 spec — 定义 master health 在 `_run_post_register_oauth` / `cmd_reconcile` / `api.fill_*` 的接入位置 |
| [`./oauth-workspace-selection.md`](./oauth-workspace-selection.md) | 互补 — master health=`active` 时,personal OAuth 走 workspace/select 主动选 personal;不健康时不进 OAuth |
| [`./quota-classification.md`](./quota-classification.md) | 异常映射对齐(`auth_*` / `network_*` 严格区分);本 spec 不复用其 5 分类(因为语义不同) |
| [`./plan-type-whitelist.md`](./plan-type-whitelist.md) | L2 反推路径相关 — bundle.plan_type=`free` 是母号降级的旁证,但反推由 plan_drift 路径承担,本函数不主动 invite |
| [`./account-state-machine.md`](./account-state-machine.md) | M-T1 / M-T2 处置使用 `STATUS_STANDBY`(子号回 standby 等用户处理),不引入新状态 |
| `register_failures.json schema` | 新增 category=`master_subscription_degraded`(spec-2 v1.5 RegisterFailureRecord enum 扩) |

---

## 10. 参考资料

### 10.1 内部研究

- `.trellis/tasks/04-27-master-team-degrade-oauth-rejoin/research/master-subscription-probe.md`
  - §1.1-1.2 字段所属 / 语义分析(Stripe 对照)
  - §2 可用 API 端点矩阵(★★★★★ 主探针选择依据)
  - §3 反推法(L2 副判定语义)
  - §4 推荐探针方案(本 spec 函数签名直接源自此处)
  - §5 误判分析(本 spec §5 的来源)
  - §7 后续未决(本 spec §5.1 FN-A / FP-A 的开放问题对应)

### 10.2 内部代码引用(实施期目标位置)

- `src/autoteam/api.py:927-995` — `/api/admin/diagnose` 现有 4-probe 实现,本 spec M-T4 在此扩展
- `src/autoteam/chatgpt_api.py:920-952` — `_list_real_workspaces` 已读 `items[*].structure / current_user_role`,本 spec 函数复用相同 `_api_fetch`
- `src/autoteam/chatgpt_api.py:1097-1124` — admin role 选择逻辑(role 白名单参考)
- `src/autoteam/chatgpt_api.py:1277-1302` — `_api_fetch` 通用封装
- `src/autoteam/manager.py:1513-1656` — `_run_post_register_oauth` 全流程(M-T1 / M-T2 接入点)
- `src/autoteam/manager.py:161-471` — reconcile 入口(M-T5)

### 10.3 OpenAI 官方源(交叉验证)

- `openai/codex codex-rs/protocol/src/auth.rs` — `KnownPlan` 枚举(`Team / Free / ...`)
- `openai/codex codex-rs/login/src/token_data.rs` — `IdTokenInfo` JWT claims(`chatgpt_plan_type` 字段)

### 10.4 Stripe 字段对照

- Stripe `Subscription.cancel_at_period_end` / `status="canceled"` — 与 `eligible_for_auto_reactivation: true` 语义最近似映射
- 文档:<https://docs.stripe.com/api/subscriptions/object>

---

## 11. Retroactive 触发位点矩阵(v1.1 Round 9 新增)

### 11.1 背景

Round 8 落地 `_reconcile_master_degraded_subaccounts(*, dry_run)` 时**仅挂在 `cmd_reconcile`**(独立对账命令 / `POST /api/admin/reconcile`)路径,导致 server 启动 / sync / 后台巡检 / cmd_check / cmd_rotate 都不命中,UI 状态永远 stale。Round 9 task `04-28-account-usability-state-correction` 实测 4 个 xsuuhfn 子号在母号已 cancel 状态下仍标 active 即此根因。详见 `.trellis/tasks/04-28-account-usability-state-correction/research/account-live-probe.md` §3 + §5。

### 11.2 helper 抽象(实施期目标)

```python
# src/autoteam/manager.py 或 src/autoteam/master_health.py
def _apply_master_degraded_classification(
    workspace_id: Optional[str] = None,
    grace_until: Optional[float] = None,
    *,
    chatgpt_api: "ChatGPTTeamAPI" = None,
    dry_run: bool = False,
) -> dict:
    """retroactive helper:把已降级母号 workspace 内子号重分类。

    - 入参:
        workspace_id  : 已降级母号 account_id;None → 内部从 master_health 探测
        grace_until   : 子号 grace 期截止 epoch;None → 内部从 子号 JWT 解析
        chatgpt_api   : 复用调用方实例,None 时按需 spawn(失败 silent)
        dry_run       : True 时不写盘,只返回候选 list

    - 行为:
        1. 调 is_master_subscription_healthy(chatgpt_api)
        2. reason == "subscription_cancelled" → 进入"前进"路径(ACTIVE/EXHAUSTED → GRACE / GRACE → STANDBY)
        3. reason == "active"                  → 进入"撤回"路径(GRACE → ACTIVE,母号续费场景)
        4. 其他 reason                          → return skipped
        5. JWT 解析失败 / save_accounts 失败    → logger.warning + return partial,不抛(M-I1 守恒延伸)

    - 返回:
        {
          "skipped_reason": Optional[str],
          "marked_grace":   List[email],   # ACTIVE/EXHAUSTED → GRACE
          "marked_standby": List[email],   # GRACE → STANDBY (grace 到期)
          "reverted_active": List[email],  # GRACE → ACTIVE  (母号续费撤回)
          "errors":         List[dict],    # 单 email 失败的明细,不传播异常
        }
    """
```

**与 Round 8 `_reconcile_master_degraded_subaccounts` 关系**:Round 9 实施期把后者改为 `_apply_master_degraded_classification` 的薄 wrapper(`cmd_reconcile` 的 RT-6 入口仍用旧名透传),不破坏 round-8 既有调用契约。

### 11.3 5 触发点矩阵(完整)

| # | 入口 | 文件:函数 | 调用时机 | 失败行为 | 对应 AC |
|---|---|---|---|---|---|
| **RT-1** | `app_lifespan` | `api.py:app_lifespan` | `ensure_auth_file_permissions()` 之后 / `_auto_check_loop` 启动之前 / 后台线程,失败不阻塞 yield | logger.warning,继续启动;不抛 | AC-B1 |
| **RT-2** | `_auto_check_loop` 末尾 | `api.py:_auto_check_loop` | 每个 interval 循环末尾(在 cmd_rotate / 巡检计算后)| 同上 | AC-B1 / AC-B2 |
| **RT-3** | `cmd_check` / `_reconcile_team_members` 末尾 | `manager.py:_reconcile_team_members` | return result 之前,复用同一 chatgpt_api | logger.warning + result["master_degraded_retroactive_error"]=str(exc),不让对账主流程失败 | AC-B1 / AC-B2 |
| **RT-4** | `sync_account_states` 末尾 | `manager.py:sync_account_states` | save_accounts 之后(无论 changed 与否),复用 chatgpt_api | 同上 | AC-B1 / AC-B2 |
| **RT-5** | `cmd_rotate` 末尾 | `manager.py:cmd_rotate` 5/5 步之后 | 主巡检 sync→check→fill→quota_recovery→rotate 完成后 | 同上 | AC-B2 |
| RT-6(已有) | `cmd_reconcile` 末尾 | `manager.py:cmd_reconcile` | round-8 既有,改为复用 helper | (现行不变) | (Round-8 已覆盖) |

### 11.4 retroactive helper 与 master_health cache 联动

- helper **必须**走 `is_master_subscription_healthy(...)` cache(默认 5min TTL)以避免每个触发点都打一次 `/backend-api/accounts`;
- `cache_ttl=300` 默认即可,**不需要** `force_refresh=True` 除非:
  - `/api/admin/master-health?force_refresh=1` 显式传入(Round 9 不变);
  - 母号续费 webhook 落地后(Round 9 暂不实施)。
- helper 内部对 `chatgpt_api` 实例的态度:
  - 调用方传入 → 直接复用,**不**主动 stop;
  - 调用方未传入 → spawn 一次 ChatGPTTeamAPI,probe 完调 `stop()`(失败吞掉)。

### 11.5 串行/并发约束

| 约束 | 说明 |
|---|---|
| RT-1 启动时机 | lifespan **不**与 `_auto_check_loop` 抢 `_playwright_lock`;RT-1 后台线程内部 `try/except` 包死 |
| RT-2~RT-5 复用 chatgpt_api | 调用方拿到的 chatgpt_api 已 start();helper 不再 start/stop;失败仅 warning |
| RT-1 与 RT-3/RT-4 重叠场景 | server 重启后 30s 内同时落 RT-1 与首次 sync RT-4 — 第二次会命中 master_health cache,不发 HTTP,无重复 KICK 风险(I12 也保 GRACE 不被 KICK) |
| 多线程写 accounts.json | `_apply_master_degraded_classification` 内每个 `update_account` 都走 `save_accounts` 同款 file-lock,不会 race condition |

### 11.6 单元测试覆盖期望

| 测试 | 说明 |
|---|---|
| `test_retroactive_helper_lifespan_hook` | mock master_health 为 subscription_cancelled,启动 lifespan,等后台线程跑完,断言 4 个 xsuuhfn 状态从 ACTIVE → DEGRADED_GRACE |
| `test_retroactive_helper_grace_expiry` | 设 acc.grace_until = past_epoch,调 helper,断言 GRACE → STANDBY |
| `test_retroactive_helper_master_recovered_revert` | mock master_health 从 cancelled 转 active,且 acc 是 GRACE,断言 GRACE → ACTIVE |
| `test_retroactive_helper_no_kick_on_grace` | reconcile 路径下断言 GRACE 子号不会被 KICK / state flip(I12) |
| `test_retroactive_helper_jwt_decode_failure_silent` | mock JWT decode 抛异常,断言 helper 返回 partial 不传播异常 |
| `test_retroactive_helper_save_accounts_failure_silent` | mock save_accounts 抛 IOError,断言不影响调用方主流程 |

---

## 12. Grace 期处理(v1.1 Round 9 新增)

### 12.1 grace_until 的来源 — 子号 JWT id_token

子号 OAuth bundle 的 `id_token`(JWT)payload 内含 `https://api.openai.com/auth.chatgpt_subscription_active_until` 字段(实测 Round 9 §1.2),格式 ISO-8601 UTC 字符串(如 `"2026-05-25T12:06:23+00:00"`)。该字段**仅 plan_type=team 子号有**;personal/free 子号该字段为 null/缺失 — 与 grace 概念无关。

### 12.2 grace_until 解析契约

```python
# src/autoteam/master_health.py 或 utils
import base64, json, datetime as dt
from typing import Optional

def parse_grace_until_from_auth_file(auth_file_path: str) -> Optional[float]:
    """从子号 auth_file 的 id_token JWT 解析 chatgpt_subscription_active_until。

    返回:
      - epoch seconds(float) 当字段存在且解析成功
      - None 当 auth_file 不存在 / id_token 缺失 / 字段缺失 / 解析失败
    永不抛异常。
    """
    try:
        data = json.loads(Path(auth_file_path).read_text())
        id_token = data.get("id_token") or data.get("tokens", {}).get("id_token")
        if not id_token:
            return None
        # JWT payload 是中段(base64url-encoded JSON,无填充)
        parts = id_token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        active_until_str = (
            payload.get("https://api.openai.com/auth", {})
                   .get("chatgpt_subscription_active_until")
        )
        if not active_until_str:
            return None
        return dt.datetime.fromisoformat(
            active_until_str.replace("Z", "+00:00")
        ).timestamp()
    except Exception:
        return None
```

### 12.3 grace 判定决策表

helper 拿到 `(workspace_id, master_health=cancelled)` 后,对每个候选子号:

| 输入条件 | 决策 |
|---|---|
| acc.workspace_account_id != workspace_id | 跳过(不属于此降级母号) |
| acc.status not in {ACTIVE, EXHAUSTED, DEGRADED_GRACE} | 跳过(状态机不允许进 GRACE) |
| acc.auth_file 缺失 | 跳过(无 JWT 可解,记 warning) |
| `parse_grace_until_from_auth_file()` 返回 None | 跳过(JWT 解析失败,保守不动) |
| acc.status ∈ {ACTIVE, EXHAUSTED} **且** now < grace_until | → DEGRADED_GRACE,落 grace_until / grace_marked_at / master_account_id_at_grace |
| acc.status == DEGRADED_GRACE **且** now >= acc.grace_until | → STANDBY,清空 grace_*(到期降级) |
| acc.status == DEGRADED_GRACE **且** master_health 转 healthy 且 acc.master_account_id_at_grace == 当前 account_id | → ACTIVE,清空 grace_*(撤回) |
| acc.status ∈ {ACTIVE, EXHAUSTED} **且** now >= grace_until | → STANDBY(grace 已过,直接降级,跳过 GRACE 中间态) |

### 12.4 grace_until 守恒规约

- 一旦写入 `acc.grace_until`,**禁止**被业务路径(invite / OAuth / reinvite / sync / quota_check)清空;
- 仅在退出 GRACE 状态(转 STANDBY / ACTIVE / PERSONAL / deleted)时由 helper 显式清空;
- 字段值与 `acc.master_account_id_at_grace` 是**对儿**关系,撤回路径若 `master_account_id_at_grace` 与当前 account_id 不一致,优先按 STANDBY 处理(可能母号已被切换)。

---

## 13. M-I1 endpoint 守恒规约(v1.1 Round 9 新增)

### 13.1 现状(Round 9 实测 bug)

研究 §2.2 实测 `/api/admin/master-health` 直接调返回 **HTTP 500**。源头:`api.py:1024-1054 get_admin_master_health._do` 仅 wrap 了 `is_master_subscription_healthy(api, force_refresh=...)` 但**未 wrap `api.start()` / `api.stop()` 阶段**的异常。该路径下函数自身 M-I1 不变量(永不抛)虽保,但在 endpoint 外层被破坏。

### 13.2 endpoint 守恒契约

`/api/admin/master-health` 任何场景都**永不返回 5xx**,失败统一映射到 `(False, "auth_invalid" | "network_error", evidence)` 业务返回值。

实施期改造点(`api.py:get_admin_master_health`):

```python
@app.get("/api/admin/master-health")
def get_admin_master_health(force_refresh: bool = False):
    """M-I1 endpoint 守恒 — 任何场景永不抛 5xx。"""
    def _do():
        from autoteam.chatgpt_api import ChatGPTTeamAPI
        from autoteam.master_health import is_master_subscription_healthy
        api = None
        try:
            api = ChatGPTTeamAPI()
            api.start()
        except Exception as exc:
            # ★ Round 9 必修:start() 失败映射 auth_invalid + 200 OK
            return {
                "healthy": False,
                "reason": "auth_invalid",
                "evidence": {
                    "http_status": None,
                    "detail": f"chatgpt_api_start_failed: {exc!s}",
                    "cache_hit": False,
                    "probed_at": time.time(),
                },
            }
        try:
            healthy, reason, evidence = is_master_subscription_healthy(
                api, force_refresh=bool(force_refresh)
            )
            return {"healthy": healthy, "reason": reason, "evidence": evidence}
        except Exception as exc:
            # ★ Round 9 必修:probe 失败映射 network_error + 200 OK(双保险)
            return {
                "healthy": False,
                "reason": "network_error",
                "evidence": {
                    "http_status": None,
                    "detail": f"probe_unexpected_exception: {exc!s}",
                    "cache_hit": False,
                    "probed_at": time.time(),
                },
            }
        finally:
            try:
                if api is not None:
                    api.stop()
            except Exception:
                pass

    return _pw_executor.run(_do)  # _pw_executor 保留,避免 event loop 阻塞
```

### 13.3 守恒边界

| 边界 | 是否可返回 5xx |
|---|---|
| `_pw_executor.run` 调度异常 | 不可控 — 可保留;但需要 logger.error 留痕便于诊断 |
| 函数内任何 `Exception` | **不可** — 必须映射 200 OK + 业务字段 |
| FastAPI 自身校验失败(query param 类型错) | 422 是 FastAPI 原生行为,不算违反守恒 |

### 13.4 single source of truth

- `is_master_subscription_healthy()` 函数**自身**永不抛(M-I1 不变量);
- `/api/admin/master-health` endpoint 自身**永不返回 5xx**(本节 §13 守恒);
- `/api/admin/diagnose` 已有的 try/except wrap(Round 8 实施)继续保留;
- M-T1~T5 触发位点的 record_failure 路径不变。

### 13.5 单测覆盖期望

| 测试 | 说明 |
|---|---|
| `test_master_health_endpoint_chatgpt_api_start_failure` | mock `ChatGPTTeamAPI.start()` 抛 RuntimeError,断言 endpoint 200 OK + body.reason == "auth_invalid" |
| `test_master_health_endpoint_probe_exception` | mock `is_master_subscription_healthy` 抛 ValueError,断言 endpoint 200 OK + body.reason == "network_error" |
| `test_master_health_endpoint_force_refresh_query_param` | 用 `?force_refresh=1`,断言函数收到 `force_refresh=True` |
| `test_master_health_endpoint_pw_executor_failure_observable` | mock `_pw_executor.run` 抛异常,断言 logger.error 至少 1 次记录(可保留 5xx,但必须留痕) |

---

## 14. subscription_grace healthy=True 状态(v1.2 Round 11 新增)

### 14.1 背景与根因

v1.0 / v1.1 把 `eligible_for_auto_reactivation == True` 直接等价于 `subscription_cancelled`(healthy=False),fail-fast 入口对 cancelled 一律 503。**Round 11 user Q1 实证发现**:该字段在 `cancel_at_period_end=True` 后**立即**置为 true(grace 期内),而**不是** "period 已过" 才置位 — ChatGPT 网页端 team 权限仍可用,新 invite 仍能拿 plan_type=team。

**结果**(Round 11 实证):
- master_health 误把 healthy 母号判 cancelled
- fail-fast 入口(api.py:fill 任务、manager.py M-T1/M-T2)503 拒绝合法 fill 请求
- UI banner 红色 critical 误导用户去续费

**修复策略**(Round 11 Approach A,见 task PRD `Decision (ADR-lite)`):在 `_classify_l1` 中对 `eligible_for_auto_reactivation == True` 的 case 分两支处理 — `grace_until > now` → `subscription_grace`(healthy=True),否则 → `subscription_cancelled`(healthy=False)。

### 14.2 决策矩阵(v1.3 双信号源升级)

| `eligible_for_auto_reactivation` | grace_until JWT(id_token 主路径) | plan_type JWT(access_token fallback) | (healthy, reason) | 备注 |
|---|---|---|---|---|
| `False / null / missing` | (任意) | (任意) | `(True, "active")` | 主号订阅活跃 |
| `True` | `grace_until > now` | (任意) | `(True, "subscription_grace")` | **OAuth 路径** — 有倒计时,Codex 重登场景 |
| `True` | `grace_until <= now` | `team / business / enterprise / edu` | `(True, "subscription_grace")` | **混合路径** — grace_until 已过期但当前权益仍是付费层(罕见,JWT 滞后)|
| `True` | None / 解析失败 | `team / business / enterprise / edu` | `(True, "subscription_grace")` | **v1.3 新增 web session 路径** — 无倒计时,常见(用户走 state.json 登录) |
| `True` | `grace_until <= now` | `free / None / 其他` | `(False, "subscription_cancelled")` | grace 已过且权益已降级,真 cancelled |
| `True` | None / 解析失败 | `free / None / 其他` | `(False, "subscription_cancelled")` | **保守失败** — 双信号都失败,按 cancelled 处理 |

**关键**:`subscription_grace` 是**双信号 OR**(任一信号有效即 grace);`subscription_cancelled` 是**双信号 AND**(两信号都失败才 cancelled)。

**不变量蕴含**:
- `M-I15` v1.3 调整:`reason == "subscription_grace"` 时 `evidence.grace_until > now()` **OR** `evidence.plan_type_jwt ∈ {team, business, enterprise, edu}` 至少其一成立(原 v1.2 严格要求 grace_until > now,v1.3 放宽)
- `M-I17` (新增 v1.3):`reason == "subscription_grace" + evidence.grace_until is None` 时,`evidence.plan_type_jwt` 必须 ∈ 付费层集合(M-I15 的对偶约束 — 无 grace_until 时必须有 plan_type 兜底证据)

### 14.3 实施位点(v1.3 双路径 fallback)

**主修改点**(预计 ~50 行):

```python
# src/autoteam/master_health.py:_classify_l1

def _classify_l1(items, account_id, *, id_token=None):
    target = next((it for it in items if it.get("id") == account_id), None)
    if not target:
        return False, "workspace_missing", {"items_count": len(items)}
    role = (target.get("current_user_role") or "").lower()
    if role and role not in _OWNER_ROLES:
        return False, "role_not_owner", {"current_user_role": role, "raw_item": target}

    if target.get("eligible_for_auto_reactivation") is True:
        # 路径 1:OAuth id_token JWT 含 chatgpt_subscription_active_until → 有倒计时
        grace_until = extract_grace_until_from_jwt(id_token) if id_token else None
        now = time.time()
        if grace_until and grace_until > now:
            return True, "subscription_grace", {
                "current_user_role": role,
                "raw_item": target,
                "grace_until": grace_until,
                "grace_remain_seconds": grace_until - now,
            }
        # 路径 2(v1.3 新增):web access_token JWT 仅含 chatgpt_plan_type → 无倒计时
        plan_type = extract_plan_type_from_jwt(id_token) if id_token else None
        _PAID_PLAN_TYPES = ("team", "business", "enterprise", "edu")
        if plan_type in _PAID_PLAN_TYPES:
            return True, "subscription_grace", {
                "current_user_role": role,
                "raw_item": target,
                "grace_until": grace_until,    # 可能 None,前端不显示倒计时
                "plan_type_jwt": plan_type,    # 区分 grace 来源(诊断用)
            }
        return False, "subscription_cancelled", {
            "current_user_role": role,
            "raw_item": target,
            "grace_until": grace_until,
            "plan_type_jwt": plan_type,
        }

    return True, "active", {"current_user_role": role, "raw_item": target}
```

**helper 复用**(Round 9 / Round 11 一轮已实现):
- `extract_grace_until_from_jwt(token)` — 解 `https://api.openai.com/auth.chatgpt_subscription_active_until` claim;Round 9 v1.1 已实现,token 既可以是 access_token 也可以是 id_token(只要 payload 含此 claim)
- `_read_access_token_from_auth_file()` — codex-main-*.json 解析,Round 9 v1.1 已实现

**v1.3 新增 helper**:

```python
def extract_plan_type_from_jwt(token):
    """从 JWT payload 解析 chatgpt_plan_type → lowercase 字符串。

    v1.3:ChatGPT web access_token 不含 chatgpt_subscription_active_until claim,
    但含 chatgpt_plan_type 表示当前权益层级。grace 期内此字段仍为 "team" 等付费层。

    返回:
        小写字符串 ("team", "free", "business" 等) — 字段存在
        None — token 缺失 / 字段缺失 / 格式错 / 解析失败 (永不抛)
    """
```

**`_load_admin_id_token` 签名变更(v1.3)**:

```python
def _load_admin_id_token(chatgpt_api=None) -> str | None:
    """加载用于解 grace 信号的 JWT token。

    优先级(v1.3):
      1. chatgpt_api.access_token(ChatGPT web JWT,/api/auth/session 拿到,
         含 chatgpt_plan_type claim) — **走 web session 路径的用户主路径**
      2. accounts/codex-main-*.json 最近修改文件的 id_token
         (含 chatgpt_subscription_active_until + chatgpt_plan_type 双 claim) — OAuth 重登路径兜底
      3. None
    """
```

**`is_master_subscription_healthy` 调用处**:`_load_admin_id_token(chatgpt_api)` — 把外层传入的 chatgpt_api 透传给 helper。

**实测事实(2026-04-28 dump)** — ChatGPT web access_token JWT payload 完整 keys:
```
['aud', 'client_id', 'exp', 'https://api.openai.com/auth', 'https://api.openai.com/profile',
 'iat', 'iss', 'jti', 'nbf', 'pwd_auth_time', 'scp', 'session_id', 'sl', 'sub']
```
`https://api.openai.com/auth` claims:
```
['chatgpt_account_id', 'chatgpt_account_user_id', 'chatgpt_compute_residency',
 'chatgpt_plan_type', 'chatgpt_user_id', 'is_signup', 'user_id', 'verified_org_ids']
```
**无** `chatgpt_subscription_active_until`,**有** `chatgpt_plan_type: "team"`(grace 期内的关键信号)。

### 14.4 fail-fast 入口零改动 — 守恒自动正确

**关键**:fail-fast 入口现行代码语义为 `if not healthy and reason == "subscription_cancelled": raise 503`。在 Round 11 修改后,grace 期内 `healthy=True, reason="subscription_grace"` → `not healthy` 为 False → **自动跳过 fail-fast**,无需改动:

- `api.py:fill_team_task` / `fill_personal_task`(M-T3 入口)
- `manager.py:_run_post_register_oauth(use_personal=False)` 入口(M-T2)
- `manager.py:_run_post_register_oauth(use_personal=True)` 入口(M-T1)

**仅需保证 `_pw_executor.run` 包装层与单测保护**(见 §14.7 单测期望)。

### 14.5 retroactive helper 撤回路径扩展(关联 §11)

Round 9 v1.1 §11 retroactive helper `_apply_master_degraded_classification` 在母号续费回 healthy 时撤回 GRACE → ACTIVE。Round 11 后,**healthy 路径扩展为两枚 reason**:

```python
# manager.py:_apply_master_degraded_classification (Round 11 调整)

def _apply_master_degraded_classification(workspace_id=None, grace_until=None, *, chatgpt_api=None, dry_run=False):
    healthy, reason, evidence = is_master_subscription_healthy(chatgpt_api)
    # Round 11:撤回路径触发条件扩展为 "active" OR "subscription_grace"
    if reason in ("active", "subscription_grace"):
        # 把所有 status=GRACE 且 master_account_id_at_grace == 当前 account_id 的子号
        # 转回 ACTIVE,清空 grace_*
        ...
        return {"reverted_active": [...]}
    if reason == "subscription_cancelled":
        # 既有 GRACE 进入路径不变(子号转 GRACE / GRACE 到期转 STANDBY)
        ...
```

**理由**:
- master 在 `subscription_grace` 期内,**新 invite 仍能拿 plan_type=team**(Round 11 user Q1 实证)
- 既然新子号都能正常 ACTIVE,那既有的 GRACE 子号(它们当时被标 GRACE 是因为 master 旧路径误判 cancelled)也应该撤回 ACTIVE
- 直到 grace_until 真正过期(转 cancelled),retroactive 才会再次把它们打成 GRACE

### 14.6 缓存策略对齐(v1.3 升级)

**v1.2 的处理(回顾)**:`reason` 字段 enum 扩展为 6 个字面量(增 `subscription_grace`),schema_version 保持 1。

**v1.3 强制升级**:**schema_version 1 → 2**。理由:v1.2 的 cache 在用户走 web session 路径时持久化了大量 `subscription_cancelled` 误判 entry(因 v1.2 `_load_admin_id_token` 仅读 codex-main-*.json,web 用户永远拿不到 token)。如不升级 schema,deploy v1.3 后旧 cache 仍会让 banner 显示红色 cancelled 直到 cache TTL(5min)自然过期 — 用户体验不连续。

升级行为(由 `_load_cache` 现有逻辑承担,无需额外代码):
1. 启动后 `_load_cache` 读到 `data["schema_version"] == 1` ≠ `CACHE_SCHEMA_VERSION (2)` → 整体丢弃,返回空 cache
2. 首次 `is_master_subscription_healthy` 调用走 cache miss 路径 → 实测 + 写新 schema 2 cache
3. 旧 cache 文件在第一次写盘时被新结构整体覆盖

**evidence 持久化字段扩展**(v1.3):
- 新增 `plan_type_jwt`(可选,可能 "team" / "free" / None,None 时不写盘以节省磁盘)
- `grace_until` 持久化路径不变(Round 11 v1.2 已支持)
- cache 命中时 `_build_evidence` 必须从 raw_ev 还原 `plan_type_jwt` 到 ev,与 grace_until 还原同等处理

### 14.7 单测期望(Round 11 一轮 ≥6 + 二轮 +8 = 总 ≥14)

**一轮(v1.2,grace_until OAuth 路径)**:

| 测试 | 说明 |
|---|---|
| `test_classify_l1_grace_period_returns_healthy` | mock items + id_token JWT 含 chatgpt_subscription_active_until = now+7d,断言 (True, "subscription_grace", evidence) + evidence.grace_remain_seconds > 0 |
| `test_classify_l1_grace_expired_returns_cancelled` | mock JWT grace_until = now-1d 且无 plan_type 兜底,断言 (False, "subscription_cancelled") + evidence.grace_until 仍提供 |
| `test_classify_l1_id_token_missing_returns_cancelled_conservatively` | id_token=None,eligible_for_auto_reactivation=True,断言保守落 cancelled |
| `test_classify_l1_jwt_parse_failure_returns_cancelled` | id_token 不是合法 JWT(随机字符串),断言保守落 cancelled |
| `test_fail_fast_entry_grace_not_503` | mock master_health 返回 (True, "subscription_grace", ...),POST `/api/tasks/fill` 不被 503 |
| `test_retroactive_helper_grace_reason_reverts_active` | mock master_health 返回 subscription_grace,acc.status=GRACE,断言 helper 把它撤回 ACTIVE |

**v1.3 二轮新增(`_load_admin_id_token` chatgpt_api fallback + plan_type 路径)**:

| 测试 | 说明 |
|---|---|
| `test_load_admin_id_token_uses_chatgpt_api_access_token_first` | chatgpt_api.access_token 存在时优先返回 web JWT,不去读 codex-main-*.json — 直接保护用户报告的 bug 路径 |
| `test_load_admin_id_token_falls_back_to_codex_main_json` | chatgpt_api.access_token=None / api 缺 attr / api=None 三种 fallback 触发,正确读 codex-main-*.json |
| `test_load_admin_id_token_returns_none_when_both_missing` | 两源都缺失返回 None,不抛异常(M-I1 守恒)|
| `test_classify_l1_grace_via_chatgpt_api_access_token` | 端到端回归:无 codex-main + chatgpt_api.access_token JWT 含 grace_until → (True, "subscription_grace", ...)|
| `test_extract_plan_type_from_jwt_returns_team` | helper 大小写归一化:"Team"/"team"/"BUSINESS" → "team"/"team"/"business",验证 .lower() 处理 |
| `test_extract_plan_type_from_jwt_returns_none_when_missing` | helper 异常路径(≥9 种):None / 空串 / 非字符串 / 单段 token / base64 失败 / 非 JSON / claims 缺失 / 字段缺失 / 空字符串 / 非字符串值 — 永不抛 |
| `test_classify_l1_grace_via_plan_type_fallback_when_grace_until_missing` | **核心修复回归**:JWT 只有 chatgpt_plan_type=team 而无 grace_until,仍返回 (True, "subscription_grace") + evidence.plan_type_jwt="team" |
| `test_classify_l1_cancelled_when_plan_type_free_fallback` | 边界守恒:plan_type=free 不被误判 grace,正确 cancelled,evidence.plan_type_jwt="free" 如实暴露 |

### 14.8 Round 11 新增不变量(v1.3 调整)

- **M-I14**:`healthy == True ⇔ reason ∈ {"active", "subscription_grace"}`(M-I3 v1.2 形式,严格双向蕴含,v1.3 不变)
- **M-I15** **(v1.3 调整)**:`reason == "subscription_grace"` 时 `evidence.grace_until > time.time()` **OR** `evidence.plan_type_jwt ∈ {team, business, enterprise, edu}` 至少其一成立。原 v1.2 严格要求 `grace_until > now`,v1.3 因 web session 路径无 grace_until,放宽为双信号 OR。
- **M-I16** **(v1.3 调整)**:`reason == "subscription_grace"` 时 `evidence.grace_remain_seconds` **可选**(原 v1.2 必然存在);若存在必须 > 0(向下取整,负值视作实施 bug);若 grace_until 为 None,grace_remain_seconds 也应缺失,前端不渲染倒计时。
- **M-I17** **(v1.3 新增)**:`reason == "subscription_grace" + evidence.grace_until is None` 时,`evidence.plan_type_jwt` 必须 ∈ 付费层集合 `{team, business, enterprise, edu}`。即"无 grace_until 时必须有 plan_type 兜底证据"— 这是 M-I15 的对偶约束,防止 plan_type fallback 路径返回 grace 时 evidence 缺少证据字段。

### 14.9 与 §13 endpoint 守恒的关系(v1.3 双路径示例)

**OAuth 路径(有倒计时)**:

```json
{
  "healthy": true,
  "reason": "subscription_grace",
  "evidence": {
    "current_user_role": "account-owner",
    "raw_account_item": {...},
    "grace_until": 1777699200.0,
    "grace_remain_seconds": 604800.0,
    "plan_type_jwt": "team",
    "probed_at": 1777094400.0,
    "cache_hit": false
  }
}
```

**Web session 路径(v1.3 新增,无倒计时)**:

```json
{
  "healthy": true,
  "reason": "subscription_grace",
  "evidence": {
    "current_user_role": "account-owner",
    "raw_account_item": {
      "id": "bac969ea-468b-4ff4-8d7a-6f4f183394d9",
      "structure": "workspace",
      "current_user_role": "account-owner",
      "eligible_for_auto_reactivation": true,
      "name": "Icoulsysad"
    },
    "plan_type_jwt": "team",
    "http_status": 200,
    "probed_at": 1777357190.0,
    "cache_hit": false
  }
}
```
注意:web session 响应中 evidence **不含** `grace_until` / `grace_remain_seconds`(只有 `plan_type_jwt: "team"` 作为 grace 证据)。

**UI 渲染契约**(`MasterHealthBanner.vue` + `useStatus.js`):
- `healthy=true + reason="subscription_grace"` → severity="warning",黄色 banner
- `evidence.grace_until` 存在且 > now → 显示倒计时(`grace · 7d 12h`)
- `evidence.grace_until` 缺失 → 不显示倒计时(`v-if="graceCountdown"` 守卫,`formatGraceRemain(undefined) === ""`),banner 仍黄色
- `evidence.plan_type_jwt`(诊断用)— 不直接渲染,可在 `evidenceLine` 调试展开时显示

---

## 15. OAuth 连续失败 backoff(v1.4 Round 11 二轮新增,M-OA-backoff)

### 15.1 背景与根因

Round 11 二轮实证:某次 master 母号 grace 期内,workspace 选择页 consent loop 因页面变化点不到 button → callback 30s timeout → 18 次连续 OAuth 失败 → `accounts.json` 累积 18 条 status=auth_invalid 子号 + 18 条 cloudmail 邮箱浪费(每条 invite/register/oauth 流程都消耗一个邮箱配额)。

根因 A:fill 巡检每 30 分钟无脑触发 `cmd_rotate`(仅看 active < HARD_CAP),不感知 OAuth 是否已稳定失败。
根因 B:即使 master_health 母号探针正常(grace 期内 healthy=True),OAuth 子流程仍可能因 consent 页面 / cookie / network 等独立路径失败,master_health 无法预测此类故障。

**结论**:需要一条独立于母号 health 的失败堆积保护机制 — 监测 OAuth 失败实际累积速率,达阈值后强制延长 fill 冷却,逼迫人工介入。

### 15.2 决策矩阵

| 触发条件 | 行为 | log 级别 |
|---|---|---|
| `len(active) >= TEAM_SUB_ACCOUNT_HARD_CAP` | 不进 backoff 检查(active 已满,fill 自然不触发)| - |
| `len(active) < HARD_CAP` 且 `cooldown_remaining > 0` | 走原 cooldown 分支,不进 backoff 检查 | info |
| `len(active) < HARD_CAP` 且 `cooldown_remaining <= 0` 且最近 2h 内 `master_aid` 上 status=auth_invalid 账号数 < 3 | 走原 fill 触发分支(`cmd_rotate`)| warning(原 fill log)|
| `len(active) < HARD_CAP` 且 `cooldown_remaining <= 0` 且最近 2h 内 `master_aid` 上 status=auth_invalid 账号数 ≥ 3 | **触发 backoff** — `_auto_fill_last_trigger_ts = now - cooldown + 4h`,本轮 `continue` 不触发 fill,下一轮 cooldown_remaining ≈ 4h 内仍 > 0 | **warning**(显式标记需人工介入)|
| backoff 检查内部抛异常(罕见,如 `get_chatgpt_account_id` 失败)| `logger.warning("[巡检] OAuth backoff 检查异常: %s,按原逻辑继续", exc)` 不进 backoff,走原 fill | warning |

### 15.3 实施位点

**位置**:`src/autoteam/api.py:2858-2893`(`_auto_check_loop` cooldown 通过分支后、playwright lock 获取前)。

**完整决策段**(实施期 1:1 对齐):

```python
# api.py:2858+
else:
    # Round 11 — OAuth 连续失败 backoff:
    # 最近 2 小时内 master workspace 累积 ≥3 个 auth_invalid 账号 → fill 已稳定失败,
    # 延长有效冷却到 4 小时,避免每 30 分钟无脑循环浪费 cloudmail 邮箱 + 累积僵尸账号。
    backoff_triggered = False
    try:
        from autoteam.accounts import STATUS_AUTH_INVALID
        from autoteam.admin_state import get_chatgpt_account_id

        master_aid = get_chatgpt_account_id() or ""
        recent_window = 2 * 3600  # 2 小时
        recent_failures = [
            a for a in accounts
            if a.get("status") == STATUS_AUTH_INVALID
            and (a.get("workspace_account_id") or "") == master_aid
            and (a.get("created_at") or 0) >= now_ts - recent_window
        ]
        if len(recent_failures) >= 3:
            # 强制延长冷却,记 last_trigger_ts 让下次巡检也走 cooldown 分支
            _auto_fill_last_trigger_ts = now_ts - _AUTO_FILL_COOLDOWN_SECONDS + 4 * 3600
            logger.warning(
                "[巡检] active=%d < %d 但近 2h 累积 %d 个 OAuth 失败账号 → "
                "backoff 生效,延长冷却到 4h(避免无谓循环)。"
                "请检查 codex_auth consent 页面或 master 订阅",
                len(active), TEAM_SUB_ACCOUNT_HARD_CAP, len(recent_failures),
            )
            backoff_triggered = True
    except Exception as exc:
        logger.warning("[巡检] OAuth backoff 检查异常: %s,按原逻辑继续", exc)

    if backoff_triggered:
        continue
    # ... 原 fill 触发逻辑 ...
```

### 15.4 契约字段(7-section 模板)

#### Scope / Trigger

`api.py:_auto_check_loop` 后台线程每巡检循环、cooldown 通过(`cooldown_remaining <= 0`)的分支起始;只在 `len(active) < TEAM_SUB_ACCOUNT_HARD_CAP` 时才进入(active 已满时 fill 自然不触发,无需 backoff)。

#### Signatures

无新公开函数(纯内联决策块),依赖既有 helper:
- `autoteam.admin_state.get_chatgpt_account_id() -> str | None` — 取当前 master account_id
- `autoteam.accounts.STATUS_AUTH_INVALID: Literal["auth_invalid"]` — 状态枚举
- 全局 `_auto_fill_last_trigger_ts: float`(api.py 模块级 mutable)— 最近一次 fill 触发时戳
- 全局 `_AUTO_FILL_COOLDOWN_SECONDS: int`(api.py 模块级常量)— 基础冷却期(实施期为 1800 / 30min)

#### Contracts

| 字段 | 值 | 注 |
|---|---|---|
| `recent_window` | `2 * 3600`(秒)| 滑动窗口大小,最近 2h |
| `failure_threshold` | `3`(条)| ≥3 触发 |
| `cooldown_extension_target` | `now - _AUTO_FILL_COOLDOWN_SECONDS + 4 * 3600`(秒)| `_auto_fill_last_trigger_ts` 推到此值,下一轮 `cooldown_remaining ≈ 4h - (round_interval)` 仍 > 0 |
| 实际效果冷却 | 触发后 4h 内不再 fill(基础 cooldown 30min,扩展后实际 ≈ 4h)| **若任务文档说 8h**,实测 cooldown 4h(代码 4 \* 3600);8h 是"基础 cooldown 4h + 扩展 4h"误解,实际代码为 30min 基础 + 4h 强制扩展 = 4h |
| 过滤条件 | `status=AUTH_INVALID` AND `workspace_account_id == master_aid` AND `created_at >= now - 2h` | 多 master 切换场景下,只算当前 master 失败 — workspace 隔离保证 |

#### Validation & Error Matrix

| 异常 | 处理 | 后果 |
|---|---|---|
| `get_chatgpt_account_id()` 抛(admin_state 文件读取失败)| `try/except` 内层捕获,`logger.warning` | backoff 不触发,走原 fill 路径 |
| `accounts` list 为 None(罕见,run-time 状态)| `for a in accounts` 抛 → `try/except` 兜底 | 同上 |
| `master_aid` 为空字符串(admin_state 未配置)| 比较 `(workspace_account_id or "") == ""` 仍正确 — 只匹配同样空 workspace 的失败子号(数据上几乎不可能 ≥3)| backoff 大概率不触发 |

#### Good / Base / Bad Cases

**Good case**(backoff 正确触发):
- 02:00 fill 触发 → 20 个 OAuth 失败(全 auth_invalid + master_aid="abc123")
- 02:30 巡检 cooldown 通过 → 检测最近 2h 内 20 条 auth_invalid + master_aid match → 触发 backoff,延长到 ~06:30
- 02:30 ~ 06:30 之间所有巡检都走 cooldown 分支,不触发 fill → 不再浪费邮箱

**Base case**(失败但不到阈值,正常 fill):
- 02:00 fill 触发 → 1 个 OAuth 失败(偶发的 cloudmail 5xx)
- 02:30 巡检 cooldown 通过 → recent_failures=1 < 3 → backoff 不触发,fill 正常运行 → 多数情况下下批 OAuth 恢复正常

**Bad case**(backoff 未触发但应触发 — 防御失败场景):
- 多 master 切换:旧 master "old-aid" 上有 5 条 auth_invalid;切到新 master "new-aid" 后,backoff 检查 master_aid == "new-aid",旧失败不计入 → backoff 不触发,fill 正常跑(切换 master 后清零是预期行为)

#### Tests Required

测试文件:`tests/unit/test_round11_oauth_failure_backoff.py`(7 cases,与 spec 决策矩阵 1:1 对应)

| Case | 文件:测试名 | 关键断言 |
|---|---|---|
| OA-T1 status=AUTH_INVALID 一致性 | `TestRunPostRegisterOauthTeamNoBundle.test_run_post_register_oauth_team_no_bundle_marks_auth_invalid` | bundle 缺失分支 status 必须 AUTH_INVALID 不是 ACTIVE(防御 fill 计数器误算)|
| OA-T2 锚 grep 防回归 | `TestRunPostRegisterOauthTeamNoBundle.test_manager_source_no_bundle_branch_uses_auth_invalid` | `Round 11 — OAuth bundle 缺失分支` 锚后 1200 字符内必含 `status=STATUS_AUTH_INVALID`,不含 `status=STATUS_ACTIVE` |
| OA-T3 backoff 触发 | `TestAutoCheckLoopOauthBackoff.test_auto_check_loop_oauth_backoff_triggers_when_3_recent_failures` | 3 条最近 2h auth_invalid + master 一致 → triggered=True;new_ts 让下一轮 cooldown_remaining > 0 |
| OA-T4 时间窗 | `TestAutoCheckLoopOauthBackoff.test_auto_check_loop_oauth_backoff_skipped_when_failures_old` | 3 条 auth_invalid 全 3h+ 前 → triggered=False,failures=0 |
| OA-T5 阈值严格 | `TestAutoCheckLoopOauthBackoff.test_auto_check_loop_oauth_backoff_skipped_when_only_2_failures` | 2 条最近失败 < 阈值 3 → triggered=False |
| OA-T6 workspace 隔离 | `TestAutoCheckLoopOauthBackoff.test_auto_check_loop_oauth_backoff_skipped_when_failures_other_workspace` | 3 条最近 auth_invalid 但 workspace_account_id != master_aid → triggered=False(隔离守恒)|
| OA-T7 锚 grep 防回归 | `TestAutoCheckLoopOauthBackoff.test_api_source_contains_backoff_logic` | `api.py` 必含 `Round 11 — OAuth 连续失败 backoff` + `len(recent_failures) >= 3` + `recent_window = 2 * 3600` + `4 * 3600` |

#### Wrong vs Correct

**Wrong example**(无 backoff,fill 无脑循环):

```python
# 旧 _auto_check_loop:
if cooldown_remaining > 0:
    continue  # 冷却分支
else:
    # ❌ 直接 _start_task("auto-fill", cmd_rotate, ...)
    # 失败连续累积 18 条都不感知,每 30min 仍触发 → 浪费 cloudmail × 36 / 18h
    _start_task("auto-fill", cmd_rotate, ...)
```

**Correct example**(backoff 守护):

```python
else:
    backoff_triggered = False
    try:
        master_aid = get_chatgpt_account_id() or ""
        recent_failures = [
            a for a in accounts
            if a.get("status") == STATUS_AUTH_INVALID
            and (a.get("workspace_account_id") or "") == master_aid
            and (a.get("created_at") or 0) >= now_ts - 2 * 3600
        ]
        if len(recent_failures) >= 3:
            _auto_fill_last_trigger_ts = now_ts - _AUTO_FILL_COOLDOWN_SECONDS + 4 * 3600
            logger.warning("...backoff 生效...")
            backoff_triggered = True
    except Exception as exc:
        logger.warning("[巡检] OAuth backoff 检查异常: %s", exc)
    if backoff_triggered:
        continue  # ✅ 跳过本轮 fill
    # 原 fill 逻辑 ...
```

### 15.5 不变量(M-OA-backoff)

> **M-OA-backoff(强制)**:`_auto_check_loop` 在 cooldown 通过(`cooldown_remaining <= 0`)的分支起始,**必须先做 OAuth 失败计数检查**,再决定是否进 fill:
>
>   1. 取 `master_aid = get_chatgpt_account_id() or ""`
>   2. 计 `recent_failures` = `[a for a in accounts if status==AUTH_INVALID and workspace_account_id==master_aid and created_at >= now-2h]`
>   3. `if len(recent_failures) >= 3`:
>      - `_auto_fill_last_trigger_ts = now - cooldown + 4h`
>      - `logger.warning(...)` 显式标记
>      - `continue`(跳过本轮 fill)
>   4. 检查异常 → `logger.warning` 吞掉,走原 fill(不阻塞主流程)
>
> 等价**禁止**:
>   - 不做 backoff 检查,cooldown 通过即触发 fill
>   - 把 `recent_window` / `threshold` / `cooldown_extension` 变成 runtime 可调参数(写死,不暴露 API 给调用方调,避免误配)
>   - workspace 隔离丢失(忘了 `workspace_account_id == master_aid` 过滤)→ 多 master 切换误触发
>
> 等价**允许**:
>   - backoff 检查内部异常时**不**触发 backoff(优先保证 fill 不被无效检查误阻断)
>   - 同一时间点累积 >> 3 条失败仍只触发一次(`continue` 跳过本轮即可)
>
> 与 §14 subscription_grace 关系:**完全独立**,subscription_grace 是母号 health 概念(grace 期内 healthy=True 自动放行 OAuth);M-OA-backoff 是 fill 频率保护(无论母号 health 如何,只要子号 OAuth 实际失败堆积就 backoff)。两者任一触发都能阻止无谓循环。

### 15.6 与既有机制的关系

| 既有机制 | 关系 |
|---|---|
| §14 subscription_grace 状态(母号 health)| 独立,grace 期内 healthy=True 不阻 fill,但 backoff 仍守 fill 频率 |
| §6 master health cache(5min TTL)| 独立,cache 仅保护 health 探针自身,不影响 backoff 计数 |
| `_AUTO_FILL_COOLDOWN_SECONDS` 基础冷却(30min)| 配合,基础冷却负责 30min 内不重复触发,backoff 在此之上加 4h 强约束 |
| §11 retroactive helper RT-1~RT-5 5 触发点 | 独立,retroactive 修正 active stale-grace 状态;backoff 关注 fill 失败频率 |
| reconcile 5min 兜底(spec-2 §3.5)| 独立,reconcile 清理 workspace 残留;backoff 阻止生产新失败 |
| **`OAUTH_SUBPROCESS_TIMEOUT_S` 单次 OAuth 上界(`oauth-subprocess-timeout.md` v1.0.0)** | **维度不同,正交** — 本 §15 M-OA-backoff 是**多次失败间隔下界**(2h 窗口 ≥3 失败 → 4h cooldown);`OAUTH_SUBPROCESS_TIMEOUT_S` 是**单次 OAuth 子进程上界**(默认 200s,P95 < 180s + 余量)。两者并存:本常量保单次 OAuth 不被错误地早 abort,M-OA-backoff 保多次失败累积时不浪费 cloudmail 配额。详见 `oauth-subprocess-timeout.md` §4.3。 |
| **§4.7 OAuth issuer ledger TTL(`account-state-machine.md` v2.1.2)** | **正交但相关** — 本 §15 backoff 是 fill 频率守护(每个失败计入 4h cooldown);issuer ledger TTL 是后端最终一致性窗口(几小时 +,kick 后 issuer 端 `oai-oauth-session.workspaces[]` 不立即清空)。两者相关性:OAuth retry 5 次累计 ~215s 远短于 ledger TTL,需依赖 `use_personal=True` 强切 personal workspace(`oauth-workspace-selection.md` v1.5.0 §4.4)绕开 ledger 滞后,此路径外加 M-OA-backoff 双重保护。 |

---

**文档结束。** 工程师据此可直接编写 `is_master_subscription_healthy` 函数 + 5 处接入 + retroactive helper(5 触发点)+ M-I1 endpoint 守恒 + **subscription_grace healthy=True 状态(Round 11)** + **OAuth 连续失败 backoff(Round 11 二轮)** + 单测,无需额外决策。

---

## 附录 A:修订记录

| 版本 | 时间 | 变更 |
|---|---|---|
| v1.0 | 2026-04-27 Round 8 | 初版 — 三层探针(L1 主 / L2 反推 / L3 副)+ 5 触发位点(M-T1~T5)+ 5min cache + 5 误判缓解(FN-A~E + FP-A~E)+ 10 不变量(M-I1~I10)。源自 `.trellis/tasks/04-27-master-team-degrade-oauth-rejoin/research/master-subscription-probe.md` §1-§7。配套 PRD-7 Approach A R1 母号订阅探针落地。 |
| **v1.1** | **2026-04-28 Round 9** — 加 retroactive 5 触发点 + grace 期 + endpoint 守恒。(1) §0 元数据 bump,引用方加 Round 9 task / spec-2 v1.6 / state-machine v2.0 / AC-B1~AC-B8;(2) **新增 §11 Retroactive 触发位点矩阵** — 抽 helper `_apply_master_degraded_classification(workspace_id, grace_until)` 5 触发点 RT-1~RT-5(lifespan / `_auto_check_loop` / `cmd_check` / `sync_account_states` / `cmd_rotate`)+ RT-6 既有(cmd_reconcile),全部走 5min cache,失败 logger.warning 不阻塞调用方;(3) **新增 §12 Grace 期处理** — `parse_grace_until_from_auth_file()` 从子号 JWT id_token `chatgpt_subscription_active_until` 解析 grace_until + 决策表 7 行(进入 GRACE / 转 STANDBY / 撤回 ACTIVE / 跳过路径)+ grace_until 守恒规约(仅 helper 退出态时清空);(4) **新增 §13 M-I1 endpoint 守恒** — `/api/admin/master-health` 永不返回 5xx,`ChatGPTTeamAPI.start()` 失败映射 auth_invalid 200 OK,probe 异常映射 network_error 200 OK,双保险 try/except,4 个单测覆盖。Round 8 既有 §1~§10 内容(M-T1~T5 / M-I1~I10 / 三层探针 / 5min cache)不变。配套 Round 9 task `04-28-account-usability-state-correction` Approach B 决策(ADR-lite)落地。 |
| **v1.2** | **2026-04-28 Round 11** — 修复 master_health 守恒 disconnect bug。(1) §0 元数据 bump,引用方加 Round 11 task / spec-2 v1.7 / state-machine v2.1 / realtime-probe v1.0 / AC1~AC4;(2) **§1 修正 `eligible_for_auto_reactivation` 语义** — 从 v1.0/v1.1 的"period 已过"改为"grace 期内立即为 true"(Round 11 user Q1 实证:ChatGPT 网页 team 权限仍可用),共因加 Round 11 user Q1;(3) **§2.1 MasterHealthReason Literal 扩** 6 个字面量(增 `subscription_grace`);(4) **§7 M-I3 不变量 BREAKING 扩展** — `healthy=True ⇔ reason ∈ {"active", "subscription_grace"}`;(5) **新增 §14 subscription_grace healthy=True 状态** — 决策矩阵(eligible_for_auto_reactivation=true × {grace_until > now / grace_until ≤ now / id_token 缺失} → reason)+ 主修改点(`_classify_l1` ~30 行 grace 判定)+ fail-fast 入口零改动(healthy=True 自动放行 — 不动 api.py:fill / manager.py M-T1/M-T2)+ retroactive helper 撤回路径扩展(reason ∈ ("active", "subscription_grace") 都触发 GRACE → ACTIVE)+ 缓存 schema 兼容(reason enum 扩 1 项,schema_version 仍 1)+ 6 个单测期望(grace 期内 / grace 已过期 / id_token 缺失 / JWT 解析失败 / fail-fast 不 503 / retroactive 撤回)+ 3 新不变量(M-I14/M-I15/M-I16)。Round 8/9 既有 §1~§13 内容不变,仅 §14 增量 + §1/§2.1/§7 局部修订。配套 Round 11 task `04-28-round11-master-resub-models-validate` Approach A 决策(ADR-lite)落地。 |
| **v1.4** | **2026-04-28 Round 11 二轮收尾** — OAuth 连续失败 backoff 独立机制。**根因**:Round 11 二轮实测 18 条 OAuth 连续失败堆积,每 30min fill 巡检无脑触发 `cmd_rotate`,即使 master_health 母号探针 grace 期内 healthy=True 也无法预测 consent 页面 / cookie / network 等独立路径失败。**修复**:(1) §0 元数据 bump v1.3 → v1.4,version 注 + 引用方加二轮收尾;(2) **新增 §15 OAuth 连续失败 backoff** — 在 `_auto_check_loop` cooldown 通过分支起始加失败计数检查,`recent_window=2h` + `master_aid` workspace 隔离 + `threshold≥3` 触发 → `_auto_fill_last_trigger_ts = now - cooldown + 4h` 实际效果 4h 内不再 fill;7 section 完整覆盖(Scope / Signatures / Contracts / Error Matrix / Good-Base-Bad / Tests Required / Wrong-vs-Correct);7 个测试 case(`tests/unit/test_round11_oauth_failure_backoff.py`)。(3) **新不变量 M-OA-backoff** — `_auto_check_loop` 必须先做失败计数检查再决定是否触发 fill;workspace 隔离不能丢;backoff 检查内部异常吞掉走原 fill 路径。(4) **§15.6 与既有机制的关系** — 与 §14 subscription_grace、§6 master health cache、§11 retroactive helper、reconcile 5min 兜底全部正交独立。(5) **未改动**:Round 8/9/11 一轮既有 §1~§14 内容全部保持,仅 §15 增量。 |
| **v1.4.1** | **2026-04-29 Round 11 五轮 spec-update** — §15.6 关系表新增 `OAUTH_SUBPROCESS_TIMEOUT_S` 与 `account-state-machine.md` §4.7 issuer ledger TTL 两行交叉引用,纯 spec 增量,无代码改动。**为什么加**:Round 11 五轮新建 `oauth-subprocess-timeout.md` v1.0.0(模块级常量 `OAUTH_SUBPROCESS_TIMEOUT_S = 200`,`manager.py:82`),与本 §15 M-OA-backoff 维度不同(单次上界 vs 失败间隔下界)需显式标注以避免读者混淆;同时 `account-state-machine.md` v2.1.2 §4.7 新增 OAuth issuer ledger TTL 现象与本 §15 fill 频率守护正交但相关(retry 5 次 215s << ledger TTL),需在关系表中点明。**改动**:(1) §0 元数据 bump v1.4 → v1.4.1,version 注 + 引用方加 oauth-subprocess-timeout v1.0.0 + account-state-machine v2.1.2 §4.7 + oauth-workspace-selection v1.5.0 §4.4 + Round 11 五轮 task;(2) §15.6 关系表追加 2 行(`OAUTH_SUBPROCESS_TIMEOUT_S` 单次 OAuth 上界 / §4.7 OAuth issuer ledger TTL),正交关系标注清晰。**未改动**:Round 8/9/11 一轮/二轮 §1~§15.5 + §15.6 既有 5 行内容全部保持,仅 §15.6 表追加 2 行 + §0 元数据局部修订。配套 Round 11 五轮 task `04-28-round11-master-resub-models-validate` trellis-update-spec 阶段。 |
| **v1.3** | **2026-04-28 Round 11 二轮** — JWT 双信号源 + cache schema 升级。**根因**:v1.2 `_load_admin_id_token` 仅读 `accounts/codex-main-*.json`,用户走 web session(state.json)路径时该文件不存在 → grace_until 永远解不出 → fallback 误判 cancelled。**实测 dump**(2026-04-28):ChatGPT web access_token JWT 不含 `chatgpt_subscription_active_until` claim,但含 `chatgpt_plan_type` claim 反映当前权益层级。**修复**:(1) §0 元数据 bump v1.2 → v1.3,共因加 Round 11 二轮根因;(2) **§1 `subscription_grace` 概念扩展为双信号源** — grace_until JWT(OAuth 路径,有倒计时)OR plan_type ∈ 付费层(web session 路径,无倒计时);(3) **§2.1 MasterHealthReason 注释扩展** — `subscription_grace` 注双信号 OR、`subscription_cancelled` 注双信号 AND 失败;(4) **§2.3 cache schema 1 → 2 强制升级** — v1.2 cache 持久化的 cancelled 误判必须作废,`_load_cache` 检测到不一致整体丢弃;(5) **§14.2 决策矩阵扩展** — 加 plan_type fallback 行,标"OAuth 路径""web session 路径""混合路径"分类;(6) **§14.3 实施位点更新** — `_load_admin_id_token(chatgpt_api=None)` 签名变化加 access_token 优先,新增 `extract_plan_type_from_jwt(token)` helper,主流程 _classify_l1 加 plan_type fallback 分支(~20 行新增);(7) **§14.6 缓存策略升级** — schema 升级行为说明 + evidence 持久化加 `plan_type_jwt` 字段;(8) **§14.7 单测期望** — 一轮 6 个保留 + 二轮新增 8 个(`_load_admin_id_token` chatgpt_api fallback 3 + `extract_plan_type_from_jwt` helper 2 + classify_l1 plan_type 路径 2 + `test_classify_l1_grace_via_chatgpt_api_access_token` 集成 1);(9) **§14.8 不变量调整** — M-I15 v1.3 放宽为双信号 OR、M-I16 grace_remain_seconds 改为可选、新增 M-I17(grace_until is None 时 plan_type_jwt 必须 ∈ 付费层);(10) **§14.9 endpoint 响应示例** — 加 web session 路径示例 + UI 渲染契约(无 grace_until 时不显示倒计时,banner 仍黄色)。Round 8/9/11 一轮既有 §1~§13 + §14.1/§14.4/§14.5 内容不变。配套 Round 11 task `04-28-round11-master-resub-models-validate` 二轮修复落地。 |
