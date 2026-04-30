# Shared SPEC: 账号状态机

## 0. 元数据 + 引用方

| 字段 | 内容 |
|---|---|
| 名称 | 账号 8 状态完整状态机与转移规则 |
| 版本 | **v2.1.2 (2026-04-29 Round 11 五轮 spec-update — 新增 §4.7 OAuth issuer ledger TTL 现象。实证 user kick 后 issuer 端 `oai-oauth-session.workspaces[]` 不会立即清,有最终一致性 TTL(几小时 +);影响:retry 之间需充分 backoff(当前 5.4s/9.5s/17.5s/33.7s 累计 215s 远短于 ledger TTL,可能不足以让 ledger 清空,backlog round 12+ 重新评估);AutoTeam 本地状态与后端真相会脱节(部分 rejoin / 部分真踢),需用户决定是否重 sync;纯 spec 增量,无代码改动)** |
| 主题归属 | `accounts.py` STATUS_* 常量 + 各转移点的触发函数 + 不变量 + uninitialized_seat 中间态(Round 6 引入)+ grace 期 retroactive 重分类(Round 9 引入)+ **母号 subscription_grace × 子号 GRACE 联动(Round 11)** + **OAuth issuer ledger TTL 现象(Round 11 五轮)** |
| 引用方 | PRD-2 / PRD-5 / PRD-6 / Round 9 task `04-28-account-usability-state-correction` / **Round 11 task `04-28-round11-master-resub-models-validate`(含五轮 spec-update)** / spec-2-account-lifecycle.md **v1.7** / master-subscription-health.md **v1.4** §14~§15 / oauth-workspace-selection.md **v1.5.0** §4.4 / `./realtime-probe.md` v1.0 / FR-D1~D4、FR-E1~E4、FR-H1~H3、FR-P0、FR-P1.2 / FR-P1.4 / FR-D6 / FR-D8 / AC-B1~AC-B8 / **Round 11 AC1~AC4** |
| 共因 | synthesis §1 共因 D、E + Issue#6 + Round 9 根因(retroactive helper 触发位点 gap)+ **Round 11 根因**(master_health 把 grace 期误判 cancelled,导致 fail-fast 入口拒绝合法 fill — 见 master-subscription-health v1.2 §14)+ **Round 11 五轮观察**(OpenAI auth issuer 端 `claimed_domain_org_id` ledger 清空有 TTL,kick 后 AutoTeam 本地立即标 kicked_no_session 但 issuer 仍可能短期内 auto-rejoin 该号到 master Team) |
| 不在范围 | seat_type 字段(见 PRD-2 FR-F1~F6)、cpa_sync 同步细节(参考 `cpa_sync.py`)、母号订阅探针实现细节(见 `./master-subscription-health.md` v1.2 §14) |

---

## 1. 概念定义

| 术语 | 定义 |
|---|---|
| `account state` | 落盘到 `accounts.json` 的 `status` 字段,7 个枚举之一 |
| `transition` | 由代码事件(同步/注册/踢出/探测)触发的状态变化 |
| `terminal state` | 没有自动转出 transition 的状态,需要人工介入或 reconcile 接管 |
| 被踢(kicked) | 在 ChatGPT Team 后端不可见但本地仍 active 的账号;wham/usage 401/403 时确认 |
| 自然待机(natural standby) | 账号 quota 耗尽后被本系统主动 kick,等待 5h reset 自然恢复 |

---

## 2. 完整数据契约

### 2.1 状态枚举(`accounts.py:13-20` + Round 9 v2.0 新增)

```python
# src/autoteam/accounts.py 已有(v1.x 7 状态):
STATUS_ACTIVE = "active"            # 在 team 中,额度可用
STATUS_EXHAUSTED = "exhausted"      # 在 team 中,额度用完
STATUS_STANDBY = "standby"          # 已移出 team,等待额度恢复
STATUS_PENDING = "pending"          # 已邀请,等待注册完成
STATUS_PERSONAL = "personal"        # 已主动退出 team,走个人号 Codex OAuth
STATUS_AUTH_INVALID = "auth_invalid" # auth_file token 已不可用,待 reconcile 清理或重登
STATUS_ORPHAN = "orphan"            # 在 workspace 占席位但本地无 auth_file

# v2.0 Round 9 新增(BREAKING):
STATUS_DEGRADED_GRACE = "degraded_grace"  # 母号已 cancel_at_period_end,但 grace 期内
                                          # 子号 wham 仍 200 plan=team — 仍可消耗配额,
                                          # 不参与 fill 轮转,grace 到期自动转 STANDBY
```

**STATUS_DEGRADED_GRACE 语义**:

- **触发条件**:子号 `workspace_account_id` 命中已降级(`subscription_cancelled`)的母号 workspace,**且**子号 JWT id_token 的 `chatgpt_subscription_active_until` 仍未过期(`now < grace_until`)
- **可消耗性**:仍持有有效 auth_file 与 access_token,wham/usage 200 plan=team allowed=true,**用户/外部消费方可继续用 codex 跑活**
- **轮转排除**:不参与 `cmd_rotate / cmd_check / fill-team / fill-personal` 工作池筛选(避免被当成 active 名额误算)
- **不被 KICK**:reconcile 看到 GRACE 不执行 KICK / state flip(M-I10 在母号降级期间禁止改写)
- **自动转 STANDBY**:retroactive helper 在 grace 到期后自动转 STANDBY(而非 AUTH_INVALID — 因为 token 仍可能在新 invite 后做 plan_drift 验证)
- **手动转 PERSONAL**:用户在 UI 显式 `leave_workspace` 后由 manual_account 走 personal OAuth → STATUS_PERSONAL

### 2.2 Pydantic AccountRecord(完整契约,本 spec 引入)

```python
from typing import Literal, Optional
from pydantic import BaseModel, Field

AccountStatus = Literal[
    "active", "exhausted", "standby", "pending", "personal", "auth_invalid", "orphan",
    "degraded_grace",   # v2.0 Round 9 BREAKING — 母号 cancel grace 期内子号
]
SeatType = Literal["chatgpt", "codex", "unknown"]


class QuotaSnapshot(BaseModel):
    """详见 ./quota-classification.md §2.1"""
    primary_pct: int = 0
    primary_resets_at: int = 0
    primary_total: Optional[int] = None
    primary_remaining: Optional[int] = None
    weekly_pct: int = 0
    weekly_resets_at: int = 0


class AccountRecord(BaseModel):
    email: str
    password: str
    cloudmail_account_id: Optional[str] = None
    status: AccountStatus
    seat_type: SeatType = "unknown"
    workspace_account_id: Optional[str] = None
    auth_file: Optional[str] = None
    quota_exhausted_at: Optional[float] = None
    quota_resets_at: Optional[float] = None
    last_quota_check_at: Optional[float] = None  # FR-E3 探测去重
    last_quota: Optional[QuotaSnapshot] = None
    last_active_at: Optional[float] = None
    created_at: float
    plan_supported: Optional[bool] = None        # 新增,见 ./plan-type-whitelist.md
    plan_type_raw: Optional[str] = None          # 新增,记录原始 OAuth 字面量
    last_kicked_at: Optional[float] = None       # 新增,被踢识别时间戳;reconcile 用
    # v2.0 Round 9 新增 — grace 期相关字段
    grace_until: Optional[float] = None          # 子号 JWT id_token chatgpt_subscription_active_until,
                                                 # epoch seconds;degraded_grace → standby 的转换判定阈值
    grace_marked_at: Optional[float] = None      # 进入 GRACE 状态的时间戳(retroactive helper 写入)
    master_account_id_at_grace: Optional[str] = None  # 进入 GRACE 时的母号 workspace_account_id 快照
                                                      # (用于母号续费回 healthy 时反向恢复 ACTIVE)
```

### 2.3 状态-字段不变量

| 状态 | 必备字段 | 禁用字段 |
|---|---|---|
| `pending` | `email`, `password`, `created_at` | `auth_file` 必须为 None(注册未完成) |
| `active` | `email`, `auth_file`(非 None) | — |
| `exhausted` | `email`, `auth_file`, `quota_exhausted_at`, `quota_resets_at` | — |
| `standby` | `email` | — |
| `personal` | `email`, `auth_file`(非 None,personal 专属 plan_type=free) | — |
| `auth_invalid` | `email` | — |
| `orphan` | `email`(在 workspace 占席位) | `auth_file` 必须为 None |
| **`degraded_grace`(v2.0)** | `email`, `auth_file`(非 None,token 仍有效), `workspace_account_id`(必须等于一个降级母号 id), `grace_until`(>0), `grace_marked_at`(>0), `master_account_id_at_grace` | `quota_exhausted_at` 必须为 None(grace 是配额仍可用前提下的轮转排除态,不是耗尽) |

---

## 3. 状态机图(ASCII)

### 3.1 完整状态机

```
                            ┌──────────────┐
                            │   PENDING    │   注册中,等收邮件
                            └──────┬───────┘
                                   │
                  注册成功+OAuth+quota ok
                                   │
                                   ▼
       ┌──────────────────────────┐
       │         ACTIVE           │ ◄────┐  reinvite 验证 ok
       │   在 Team,可调 Codex      │      │
       └──┬───────────────────────┘      │
          │                              │
   quota  │ 100%      ┌──────────────────┤
          ▼           │                  │
   ┌──────────┐  本系统kick    ┌────────────┐
   │EXHAUSTED │ ────────────► │  STANDBY   │ ◄── reinvite plan!=team(旧路径)
   │ (lock 5h)│               │ 等待 reset  │     (新路径推 AUTH_INVALID)
   └────┬─────┘               └────┬───────┘
        │                          │
   reset│ 5h 后 reinvite           │ reinvite 验证
        └──────────────────────────┴────────►  返 ACTIVE
                                              或 fall through
                                              (见下方 fail_reason 分支)
                                              
                            ┌─────────────┐
   人工 leave_workspace ──► │  PERSONAL   │ (终态,不参与 Team 轮转)
                            └─────────────┘
                            
   sync 探测 wham 401/403  ┌────────────────┐    reconcile.KICK
   ──────────────────────► │ AUTH_INVALID   │ ──────────────────► 删本地或保留人工介入
   注册收尾 wham no_quota  │ token 已失效    │
   reinvite plan_drift    │                │
   add-phone 命中(Team)   └────────────────┘
   
   _reconcile 发现 ghost  ┌──────────┐    人工 KICK
   ──────────────────────► │  ORPHAN  │ ──────────────────► 删
                          └──────────┘

   ── v2.0 GRACE 子图 ──(Round 9 新增,5 触发点 retroactive helper 驱动)
   
   ACTIVE / EXHAUSTED            ┌─────────────────────┐
   workspace 命中已降级母号  ──► │ DEGRADED_GRACE      │
   且 now < grace_until          │ 仍可消耗 / 不入轮转  │
                                  │ wham 200 plan=team  │
                                  └──┬──┬───────────────┘
                                     │  │
       grace 到期(now >= grace_until) │  │ 母号续费回 healthy
       (lifespan/auto_check/sync     │  │ retroactive 撤回
        /cmd_check/cmd_rotate 任一)   │  │
                  ▼                  │  │
              STANDBY  ◄─────────────┘  └──────► ACTIVE
              (5h 后 reinvite 自动恢复)
                                     
       用户主动 leave_workspace ─────────────────► PERSONAL
                                                  (终态)
```

### 3.2 状态分类(v2.0 重排)

| 类别 | 状态 | 备注 |
|---|---|---|
| 工作池(轮转参与) | `active`、`exhausted`、`standby`、`pending` | 参与 fill / cmd_rotate / cmd_check 名额计数 |
| **过渡态(可消耗 / 不入轮转,v2.0)** | `degraded_grace` | 母号 cancel 但子号 grace 期内仍 200;**用户可手动用,系统 fill 不计** |
| 终态(不参与轮转) | `personal`、`auth_invalid`、`orphan` | 不在 fill 名额池;personal 是用户决定的最终态 |

**为何 GRACE 不归入"工作池"**:fill 系列动作需要"母号能继续生产新席位"作为前提,母号已 cancel 时新 invite 必拿 free,把 GRACE 计入工作池会让 fill 误判名额还充足并继续浪费 OAuth 周期。GRACE 子号已存的 quota 仍可被用户手动消费,但**系统轮转不再依赖它**。

### 3.3 uninitialized_seat 中间态(Round 6 引入,Round 7 文档同步)

**语义**:`uninitialized_seat` **不是** 7 个 status 枚举之一,而是 STATUS_ACTIVE / STATUS_PENDING 的"待验证"子态。当 wham/usage 返回半空载形态(`primary_total=null + reset_at>0`)时,`check_codex_quota` 标记 `quota_info.window=="uninitialized_seat"` + `needs_codex_smoke=True`,在内部完成 cheap_codex_smoke 二次验证之前,**该账号在状态机层面仍记为原 status**(PENDING 或 ACTIVE)。

**生命周期**:

```
PENDING (or ACTIVE)
  │
  │ wham/usage 200 + primary_total=null + reset>0 命中 I5
  │ → check_codex_quota 内部判定 uninitialized_seat,准备 smoke
  │
  ├─ 24h cache 命中 (Round 7 FR-D6)
  │   ├─ cached="alive"        → ("ok", quota_info[smoke_cache_hit=True]) → STATUS_ACTIVE
  │   ├─ cached="auth_invalid" → ("auth_error", None) → STATUS_AUTH_INVALID
  │   └─ cached="uncertain"    → ("network_error", None) → 保留原 status
  │
  └─ cache miss → cheap_codex_smoke 网络调用
      ├─ "alive"        → ("ok", quota_info[smoke_verified=True]) → STATUS_ACTIVE,落 last_codex_smoke_at + last_smoke_result
      ├─ "auth_invalid" → ("auth_error", None) → STATUS_AUTH_INVALID,同上落盘
      └─ "uncertain"    → ("network_error", None) → 保留原 status,同上落盘
```

**不变量保证**:
- 账号永远不会停留在 "uninitialized_seat" 中间态超过一次 check_codex_quota 调用周期;调用结束时必有 5 分类 status 之一返回
- `last_codex_smoke_at` / `last_smoke_result` 字段反映最近一次实际 smoke 网络调用,用于 24h 去重
- `quota_info.smoke_verified` / `quota_info.last_smoke_result` 反映本次 quota check 的结果(可能是 cache 命中复用)
- 详见 `./quota-classification.md §4.4 / I8 / I9`

---

## 4. 状态/分类规则

### 4.1 触发函数 → 转移矩阵

| 触发函数 | 文件:行号 | 触发条件 | from → to |
|---|---|---|---|
| `add_account` | `accounts.py:58` | 新增账号(invite 成功) | (none) → PENDING |
| `_run_post_register_oauth` Team 分支 | `manager.py:1463` | bundle ok + quota ok(经 cheap_codex_smoke 24h cache 验证或 alive,Round 6/7) | PENDING → ACTIVE |
| `_run_post_register_oauth` Team 分支 | `manager.py:1463` | bundle ok + quota exhausted(新) | PENDING → EXHAUSTED |
| `_run_post_register_oauth` Team 分支 | `manager.py:1463` | bundle ok + quota no_quota(新) | PENDING → AUTH_INVALID |
| `_run_post_register_oauth` Team 分支 | `manager.py:1463` | bundle ok + quota auth_error(新)| PENDING → AUTH_INVALID |
| `_run_post_register_oauth` Team 分支 | `manager.py:1463` | bundle ok + uninitialized_seat 中间态 + cheap_codex_smoke=auth_invalid(Round 6 FR-P0)| PENDING → AUTH_INVALID(经 check_codex_quota 内部消化为 auth_error 路径)|
| `_run_post_register_oauth` Team 分支 | `manager.py:1463` | bundle ok + uninitialized_seat 中间态 + cheap_codex_smoke=uncertain(Round 6 FR-P0)| PENDING → ACTIVE(保留原状态,由下轮 sync 校准;经 check_codex_quota 内部转 network_error)|
| `_run_post_register_oauth` Team 分支 | `manager.py:1463` | bundle 但 plan_supported=False(新) | PENDING → AUTH_INVALID |
| `_run_post_register_oauth` Team 分支 | `manager.py:1463` | bundle 失败但已 invite(v1.7 之前)| PENDING → ACTIVE(team_auth_missing,旧行为)|
| `_run_post_register_oauth` Team 分支 | `manager.py:1463` | RegisterBlocked(is_phone=True)(新) | PENDING → AUTH_INVALID |
| `_run_post_register_oauth` Team 分支 + helper | `manager.py:1873`(Round 11 二轮)| master_degraded(订阅 cancel,fail-fast)| PENDING → AUTH_INVALID(ws **kicked 同步** by `_kick_team_seat_after_oauth_failure`)|
| `_run_post_register_oauth` Team 分支 + helper | `manager.py:1898`(Round 11 二轮)| RegisterBlocked is_phone(OAuth 阶段触发 add-phone)| PENDING → AUTH_INVALID(ws **kicked 同步**)|
| `_run_post_register_oauth` Team 分支 + helper | `manager.py:1905`(Round 11 二轮)| unexpected RegisterBlocked(非 phone)| PENDING → AUTH_INVALID(ws **kicked 同步**)|
| `_run_post_register_oauth` Team 分支 + helper | `manager.py:1938`(Round 11 二轮)| bundle plan_supported=False | PENDING → AUTH_INVALID(ws **kicked 同步**)|
| `_run_post_register_oauth` Team 分支 + helper | `manager.py:2014`(Round 11 二轮)| bundle 缺失(`login_codex_via_browser` 返回 None)| PENDING → AUTH_INVALID(ws **kicked 同步**;v1.7 之前误用 ACTIVE,二轮修正)|
| `_run_post_register_oauth` personal 分支 | `manager.py:1431` | bundle ok + plan=free | PENDING → PERSONAL |
| `_run_post_register_oauth` personal 分支 | `manager.py:1431` | bundle 失败 / plan != free | PENDING → deleted(record_failure) |
| `_run_post_register_oauth` personal 分支 | `manager.py:1431` | RegisterBlocked(is_phone=True)(新) | PENDING → deleted + record_failure |
| `_run_post_register_oauth` personal 分支 | `manager.py:1431`(Round 11 四轮)| **personal OAuth 走两阶段**:阶段 1 快路径(注册阶段透传 chatgpt.com `__Secure-next-auth.session-token` + 双域 cookie 注入 + silent step-0)→ 阶段 2 fallback(`context.clear_cookies()` + chatgpt.com fresh login + `keyboard.type(delay=50)` 绕开 OpenAI auth 表单灰按钮 bug);两阶段都目标 plan_type=free,bundle 由本行上面三条转移矩阵消费 | (无新增 state,转移规则同上)— 详见 [`./oauth-workspace-selection.md`](./oauth-workspace-selection.md) **v1.4.0** §4.1 v1.4.0 章节 + W-I11~W-I14 |
| `sync_account_states` | `manager.py:520` | active 在 Team 中(同步成功) | ACTIVE → ACTIVE(无变化) |
| `sync_account_states` | `manager.py:520` | standby/pending 在 Team 中 | STANDBY/PENDING → ACTIVE |
| `sync_account_states` | `manager.py:540` | active 不在 Team + workspace_account_id 不一致 | ACTIVE → ACTIVE(母号切换守卫,旧行为保留) |
| `sync_account_states` | `manager.py:540`(新) | active 不在 Team + wham 401/403 | ACTIVE → AUTH_INVALID |
| `sync_account_states` | `manager.py:540`(Round 6 FR-P0)| active 在 Team + wham uninitialized_seat + cheap_codex_smoke=auth_invalid | ACTIVE → AUTH_INVALID(短路 check_codex_quota 内部消化路径) |
| `sync_account_states` | `manager.py:540`(新) | active 不在 Team + wham ok / network_error | ACTIVE → STANDBY(自然待机,保留旧行为) |
| `cmd_check` quota 探测 | `manager.py:715/748/760` | wham 状态变化 | active → exhausted/auth_invalid/standby |
| `reinvite_account` | `manager.py:2466` | OAuth 成功 + plan=team + quota verified | STANDBY → ACTIVE |
| `reinvite_account` | `manager.py:2466`(新) | OAuth 成功 + plan != team / plan_supported=False | STANDBY → AUTH_INVALID(plan_drift) |
| `reinvite_account` | `manager.py:2466`(新) | OAuth 失败 RegisterBlocked(is_phone=True) | STANDBY → AUTH_INVALID(oauth_phone_blocked) |
| `reinvite_account` | `manager.py:2466`(旧) | OAuth 成功但 quota_low / exhausted | STANDBY → STANDBY(锁 5h) |
| `reinvite_account` | `manager.py:2466`(旧) | OAuth 成功但 quota auth_error / network_error / exception | STANDBY → STANDBY(不锁 5h) |
| `reinvite_account` | `manager.py:2466`(旧) | OAuth 失败 bundle=None | STANDBY → STANDBY(_cleanup_team_leftover) |
| `_reconcile_team_members` | `manager.py:312/339` | 发现 ghost / orphan 错位 | ACTIVE → AUTH_INVALID |
| `_reconcile_team_members` | `manager.py:312/339` | workspace 占席位但本地无 auth_file | (任意) → ORPHAN |
| `_replace_single` kick | `manager.py:2626` | 主动定点替换 | active → STANDBY |
| `delete_managed_account` | `account_ops.py:40` | 用户单点 / 批量删除 | (任意) → deleted |
| `manual_account._finalize_account` | `manual_account.py:227` | 用户粘贴 OAuth callback | (none) → STANDBY(team) / ACTIVE(team+plus 之外) / AUTH_INVALID(plan_unsupported,新) |

### 4.2 "被踢" vs "自然待机" 识别规则(FR-E1)

```
sync_account_states 看到 acc.status == ACTIVE 且 email 不在当前 workspace_team_emails:
  ├─ acc.workspace_account_id ≠ 当前 account_id → 母号切换遗留,保留 ACTIVE(旧行为)
  ├─ acc.auth_file 存在 → 用 access_token 调一次 wham/usage(并发限制 5,超时 5s)
  │   ├─ ("auth_error", _) → STATUS_AUTH_INVALID + last_kicked_at=now  ★被踢
  │   ├─ ("ok", info) → STATUS_STANDBY + last_quota=info  ★自然待机(罕见,可能 OpenAI 缓存延迟)
  │   ├─ ("exhausted", info) → STATUS_STANDBY + quota_exhausted_at  ★自然待机
  │   ├─ ("no_quota", info) → STATUS_AUTH_INVALID(无配额且不在 Team,不会自动恢复)
  │   └─ ("network_error", _) → 保持 ACTIVE 等下轮(避免抖动误标)
  └─ acc.auth_file 缺失 → STATUS_STANDBY(降级,无法验证)
```

### 4.3a 删除链短路(Round 6 FR-P1.2 / FR-P1.4 落地,Round 7 文档同步)

**语义**:`STATUS_AUTH_INVALID` 与 `STATUS_PERSONAL` 在删除链中**等价处置** — 都跳过 ChatGPTTeamAPI 远端同步,直接走本地清理。

**触发条件**:

| 触发函数 | 文件:行号 | 条件 | 行为 |
|---|---|---|---|
| `account_ops.delete_managed_account` | `account_ops.py:79` | `acc.status in (STATUS_PERSONAL, STATUS_AUTH_INVALID)` | short_circuit=True,跳过 fetch_team_state / 不实例化 ChatGPTTeamAPI |
| `api.delete_accounts_batch` | `api.py:1582` | `bool(targets) and all(a.status in (PERSONAL, AUTH_INVALID) for a in targets)` | all_local_only=True,整批不启动 ChatGPTTeamAPI |
| `api.delete_account` 单点 | 复用 `delete_managed_account` | 同 §4.3a 第 1 行 | 同上 |

**理由**:
- AUTH_INVALID 账号的 token 已 401,继续走 fetch_team_state 也很可能 401 拖累整个删除流程
- 主号 session 失效场景下,启动 ChatGPTTeamAPI 会卡死 30s
- 删除 AUTH_INVALID 不需要远端 KICK(reconcile 已经 KICK 过或正在排队),只需清本地 records / auth_file
- PERSONAL 账号已 leave_workspace,远端席位早已不存在

**单测覆盖**:`tests/unit/test_round6_patches.py`:
- `test_auth_invalid_short_circuit_skips_fetch_team_state`(FR-P1.2)
- `test_auth_invalid_short_circuit_does_not_start_chatgpt_api`(FR-P1.2)
- `test_all_personal_short_circuit_skips_chatgpt_api_start`(FR-P1.4)

### 4.3b GRACE 删除链短路(v2.0 Round 9)

**语义**:`STATUS_DEGRADED_GRACE` 在删除链中**等价**于 `STATUS_PERSONAL` / `STATUS_AUTH_INVALID` — 都跳过 ChatGPTTeamAPI 远端同步,直接走本地清理。

**理由**:
- GRACE 子号的母号已 cancel,即使 fetch_team_state 拿到席位,KICK 操作也大概率因母号 admin session 401 / 503 卡住;
- 删除 GRACE 不需要远端 KICK(grace 到期后 standby retroactive helper 会自然清理 workspace,无 ghost 风险);
- 与 §4.3a 处置矩阵 / spec-2 §3.5.1 short_circuit 实施一致 — `account_ops.delete_managed_account` short_circuit 条件需追加 `STATUS_DEGRADED_GRACE`,`api.delete_accounts_batch` `all_local_only` 条件同步追加。

**单测覆盖**(实施期 backend-implementer 加):
- `test_grace_short_circuit_skips_fetch_team_state`
- `test_all_grace_short_circuit_does_not_start_chatgpt_api`
- `test_mixed_grace_active_starts_chatgpt_api`(GRACE + ACTIVE 混批不能短路)

### 4.3 reinvite_account fail_reason 分支(扩 FR-H1)

```
reinvite_account 拿到 bundle 后:
  ├─ bundle == None
  │   ├─ 由 RegisterBlocked(is_phone=True) 引发 → STATUS_AUTH_INVALID(新,FR-C3)
  │   └─ 其他 → _cleanup_team_leftover("no_bundle") + STATUS_STANDBY(旧)
  ├─ plan_supported == False → STATUS_AUTH_INVALID + record_failure("plan_unsupported")(新)
  ├─ plan_type != "team" → STATUS_AUTH_INVALID + record_failure("plan_drift")(新,替代旧 STATUS_STANDBY)
  └─ plan_type == "team":
      ├─ quota verified ok → STATUS_ACTIVE(旧)
      ├─ quota fail_reason in (exhausted, quota_low) → STATUS_STANDBY + 锁 5h(旧)
      └─ quota fail_reason in (auth_error, network_error, exception) → STATUS_STANDBY 不锁 5h(旧)
```

### 4.4 STATUS_DEGRADED_GRACE 转移规则(v2.0 Round 9 新增)

GRACE 状态由"母号订阅 retroactive 重分类 helper"集中驱动,不会被 invite / OAuth / reinvite 路径直接写入。

#### 4.4.1 进入 GRACE 的转移

| 触发函数 | 触发条件 | from → to | 必须落字段 |
|---|---|---|---|
| `_apply_master_degraded_classification(workspace_id, grace_until)` 内部循环 | acc.status ∈ {ACTIVE, EXHAUSTED} **且** acc.workspace_account_id == 已降级母号 workspace_id **且** now < grace_until | ACTIVE/EXHAUSTED → DEGRADED_GRACE | grace_until=<JWT id_token chatgpt_subscription_active_until>, grace_marked_at=time.time(), master_account_id_at_grace=<workspace_id> |
| 同上 | acc.status == AUTH_INVALID(token 已 401)且 workspace 命中已降级母号 | **不转 GRACE**,保持 AUTH_INVALID(grace 前提是 token 仍可用) | (跳过) |

#### 4.4.2 退出 GRACE 的转移

| 触发函数 | 触发条件 | from → to | 字段处理 |
|---|---|---|---|
| `_apply_master_degraded_classification` retroactive 扫描 | acc.status == GRACE **且** now >= acc.grace_until | DEGRADED_GRACE → STANDBY | 清空 grace_*,保留 last_quota / last_kicked_at;不动 auth_file(等用户决定 reinvite 或 delete) |
| `_apply_master_degraded_classification` retroactive 撤回(母号续费) | acc.status == GRACE **且** master_health 此时 reason == "active" **且** acc.workspace_account_id == 当前活跃母号 | DEGRADED_GRACE → ACTIVE | 清空 grace_*,保留 master_account_id_at_grace 字段最近值用于审计日志,可置 None |
| `manual_account.leave_workspace` 用户主动退出 | 用户 UI 点 "leave_workspace" | DEGRADED_GRACE → PERSONAL | grace_* 清空;经 personal OAuth 走 STATUS_PERSONAL 路径(plan_type=free 验证通过) |
| `delete_managed_account` | 用户删除 | DEGRADED_GRACE → deleted | 走 §4.3a short_circuit 等价路径(GRACE 与 PERSONAL/AUTH_INVALID 视为可短路 — 见 §4.3b) |

#### 4.4.3 5 触发点 retroactive 矩阵(与 master-subscription-health.md §11 联动)

| # | 触发位点 | 文件:函数 | 调 helper 时机 | 备注 |
|---|---|---|---|---|
| RT-1 | server 启动 lifespan | `api.py:app_lifespan` 内 ensure_auth_file_permissions 之后 | yield 前 / 后台线程,失败不阻塞启动 | 解 "重启后 stale active" 问题(Round 9 根因) |
| RT-2 | `_auto_check_loop` 后台巡检 | `api.py:_auto_check_loop` 内 cmd_rotate 末尾或巡检循环末尾 | 每个 interval 周期 1 次 | 持续保护:即便用户从不点 reconcile 也能自愈 |
| RT-3 | `cmd_check` / `_reconcile_team_members` 末尾 | `manager.py:_reconcile_team_members` return 之前 | 复用同一 chatgpt_api 实例,无额外 Playwright 启动 | spec-2 §3.7 + research §6 方案 A |
| RT-4 | `sync_account_states` 末尾 | `manager.py:sync_account_states` Team 成员同步完成后 | save_accounts 之前 | UI 用户点"同步"按钮也能命中 |
| RT-5 | `cmd_rotate` 末尾 | `manager.py:cmd_rotate` 5/5 步之后 | 主巡检链路收尾 | 与 RT-3 在 cmd_rotate 内嵌的 cmd_check 互补,防遗漏 |
| RT-6(已有) | `cmd_reconcile` 末尾 | `manager.py:cmd_reconcile` 末尾 `_reconcile_master_degraded_subaccounts` | 现 round-8 唯一接入 | v2.0 后改为复用 helper(避免重复 spawn ChatGPTTeamAPI) |

**helper 必须做的事**:
1. 调 `is_master_subscription_healthy(chatgpt_api)`(可走 5min cache);
2. 若 `reason != "subscription_cancelled"`,处理"撤回"路径 — 把所有 `status==GRACE` **且** `master_account_id_at_grace == account_id` 的子号转回 ACTIVE;
3. 若 `reason == "subscription_cancelled"`,从 evidence 取 master account_id,从 子号 auth_file JWT 解析 `chatgpt_subscription_active_until`(见 master-subscription-health.md §12 grace 期处理),按 §4.4.1 / §4.4.2 矩阵重分类;
4. 全程不抛异常 — 任何失败 logger.warning,不影响调用方主流程。

### 4.5 反向不变量

| 不变量 | 说明 |
|---|---|
| `auth_file 存在 ⇒ status ∈ {active, exhausted, standby_with_token, personal, auth_invalid, degraded_grace}` | orphan / pending 必无 auth_file;v2.0 增加 GRACE — token 可用但 workspace 已 cancel |
| `status == personal ⇒ plan_type_raw == "free"` | personal 路径强校验(`codex_auth.py:920-930`) |
| `status == active ⇒ workspace_account_id 与当前一致 OR workspace_account_id is None` | sync 守卫(`manager.py:531-538`) |
| `last_kicked_at != None ⇒ status in {auth_invalid, deleted}` | 被踢标记的语义边界 |

### 4.6 母号 subscription_grace × 子号 GRACE 联动表(v2.1 Round 11 新增)

**背景**:Round 11 master-subscription-health v1.2 §14 引入 `subscription_grace`(healthy=True)新母号状态后,母号侧与子号侧双侧 grace 期的语义对齐 — 两侧都是 healthy=True 的过渡态,正交但有联动。

**联动决策表**(retroactive helper `_apply_master_degraded_classification` 入口):

| # | 母号 master_health.reason | 子号 acc.status | retroactive helper 决策 | 备注 |
|---|---|---|---|---|
| L-1 | `active` | `active` | 不动 | 双侧 healthy 平稳态 |
| L-2 | `active` | `degraded_grace` | **撤回 → ACTIVE**(清空 grace_*) | 母号续费回 active,GRACE 子号一律恢复(M-master-health v1.2 §14.5) |
| L-3 | `active` | `exhausted / standby / pending` | 不动 | 配额相关状态由 quota check 路径管理,不归本 helper |
| L-4 | **`subscription_grace`** | `active` | 不动(plan_type=team 仍可,fill 池保留) | **Round 11 新分支**:grace 期内母号 healthy=True,子号继续轮转 |
| L-5 | **`subscription_grace`** | `degraded_grace` | **撤回 → ACTIVE**(清空 grace_*) | **Round 11 新分支**:grace 期内既然 master healthy=True,旧 GRACE 子号(被 v1.0/v1.1 误打)应撤回 |
| L-6 | **`subscription_grace`** | `exhausted / standby / pending` | 不动 | 同 L-3 |
| L-7 | `subscription_cancelled` | `active / exhausted` | **进入 GRACE → DEGRADED_GRACE** | Round 9 既有路径(grace_until > now 时) |
| L-8 | `subscription_cancelled` | `degraded_grace` (now < grace_until) | 不动 | grace 倒计时未到期,保持 GRACE |
| L-9 | `subscription_cancelled` | `degraded_grace` (now >= grace_until) | **GRACE → STANDBY**(清空 grace_*) | grace 已过,标 STANDBY 等用户决定 |
| L-10 | `workspace_missing / role_not_owner / auth_invalid / network_error` | (任意) | 不动(skipped) | 母号探针失败/缺位,保守不动 |

**关键改动 vs v2.0**:
- L-2 / L-5 撤回路径触发条件由 `reason == "active"` 扩展为 `reason ∈ ("active", "subscription_grace")` — 即 master healthy=True 的两枚字面量都触发撤回。
- L-4 是新增的"双侧 healthy"平稳态,fill 池可继续添加 plan_type=team 子号(因为 master_health.healthy=True 时 fail-fast 入口放行)。

**helper 实施期改动**(`manager.py:_apply_master_degraded_classification` 或 `master_health.py`):

```python
def _apply_master_degraded_classification(workspace_id=None, grace_until=None, *, chatgpt_api=None, dry_run=False):
    healthy, reason, evidence = is_master_subscription_healthy(chatgpt_api)
    # ★ Round 11 v2.1:撤回路径触发条件扩为 healthy 双枚 reason
    if reason in ("active", "subscription_grace"):
        return _revert_grace_subaccounts(...)
    if reason == "subscription_cancelled":
        return _classify_grace_subaccounts(workspace_id, grace_until, ...)
    return {"skipped_reason": reason}
```

**与 master_health v1.2 §14.5 的联动**:本 §4.6 的 L-2 / L-5 条目对应 master-subscription-health v1.2 §14.5 撤回路径扩展。两份 spec 必须同步更新,否则 helper 在 grace 期内不会撤回。

**单测期望补充**(基于 Round 9 既有 6 个 case):

| 测试 | 说明 |
|---|---|
| `test_retroactive_helper_subscription_grace_reverts_active` | mock master_health → (True, "subscription_grace"),acc.status=GRACE → 撤回 ACTIVE(对应 L-5)|
| `test_retroactive_helper_subscription_grace_keeps_active` | mock master_health → (True, "subscription_grace"),acc.status=ACTIVE → 不动(对应 L-4)|

### 4.7 OAuth issuer ledger TTL 现象(Round 11 五轮实证)

**背景**:Round 11 五轮通过 master /users 与子号 OAuth 实证两个独立视角同时观测,发现 `KICK + Auth issuer 端 ledger 清理` 之间存在最终一致性 TTL,而非立即一致。本节描述该现象的语义与对状态机的影响。

#### 4.7.1 现象描述

调用 `DELETE /backend-api/accounts/{wid}/users/{uid}`(或 admin UI "Remove from team" 实测等价,见 `admin-ui-kick-endpoint.md`)成功返回 `200 {"success":true}` 后:

- **master /users 视图(后端真相)**: 立即生效。该 user_id 不再出现在 master Team 成员列表中。
- **OAuth issuer 端(`auth.openai.com /backend-api/oai-oauth-session`)**: **不立即清空** `workspaces[]` ledger,存在最终一致性 TTL 窗口(实证至少几小时,上界未量化)。
- **域级 auto-rejoin(`claimed_domain_org_id`)**: master Team 已认证 `zrainbow1257.com` 域名后,部分子号在 kick 后被 issuer 自动加回为 `standard-user`(非 invited),此过程不需要邮件 invite,纯后端联动。

**关键含义**:**踢人成功 ≠ 该子号能立即拿到 plan=free 的 OAuth bundle**。若 kick 后立即重新 OAuth,issuer 仍会基于其 ledger 把该号绑回 master Team,bundle 仍是 plan=team(或被 plan_drift 拒收)。需等候 ledger TTL 过期后再重试,或通过显式选 personal workspace 路径绕开。

#### 4.7.2 实证证据(Round 11 五轮 P1+B + admin UI 报告)

引用 `.trellis/tasks/04-28-round11-master-resub-models-validate/research/`:

| 证据点 | 数据 | 引用 |
|---|---|---|
| `404907e1c8@zrainbow1257.com` 被 issuer auto-rejoin | KICK 成功后几小时,master /users 显示该号已变为 `standard-user`(user_id `user-h1ZUQFFj9K8wg1y7XGc7XPnZ`),无 pending invite,确认是 `claimed_domain_org_id` 触发 | `three-kicked-emails-probe.md` §0、`p1-p2-execution-report.md` §"auto-domain rejoin 仍然是事实" |
| `b7c4aaf8f2` / `fd3b5ccae1` 同期未被 rejoin | 同 zrainbow1257.com 域,同期实测仍不在 master Team(真踢生效),OAuth 200s 长 timeout 拿到 plan=free | `three-kicked-emails-probe.md`、`p1-p2-execution-report.md` §P1 |
| issuer ledger 已清的实证 | `fd3b5ccae1` OAuth headless 链路实测 `oai-oauth-session.workspaces[]==[]`(stderr line 11:44:07),随后 stage 1 快路径直接拿到 plan=free | `p1-p2-execution-report.md` §P1 关键路径 |
| stage 1 快路径首次成功 | `fd3b5ccae1` 71.3s 内拿到 `plan_type=free` + `account_id=7f4384d7-4831-4a8d-a93c-547296c6b600`(personal workspace),不进 stage 2 fresh re-login fallback | 同上 |

**部分 rejoin = 概率性,非确定性**:同域三号同期 kick 后,1/3 被 issuer auto-rejoin(`404907e1c8`),2/3 真踢成功(`b7c4aaf8f2` / `fd3b5ccae1`),无明显规律。原因可能是 issuer 端 `claimed_domain_org_id` 触发条件涉及 user_id 创建时间 / 历史 owner 状态等内部因素,Round 11 未能反推决定性条件。

#### 4.7.3 对状态机的影响

##### 4.7.3.1 retry backoff 充分性疑问

`oauth-workspace-selection.md` v1.4.0 §3.4 现有 5 次外层重试 backoff 序列:`5.4s/9.5s/17.5s/33.7s` (jitter 后),累计 `5.4 + 9.5 + 17.5 + 33.7 = 66.1s` 子号 OAuth 间隔(每次 OAuth 自身 ~71s,完整重试链 215s+ 等级)。

- **问题**: 215s 远短于 issuer ledger TTL(实证至少几小时)。重试只能解决 transient 网络/UI 问题,**不能等到 ledger 清空**。
- **当前对策**: AutoTeam 通过 `use_personal=True` 强切 personal workspace 路径绕开,而非依赖 ledger 自然清空。此路径在 v1.5.0 §4.4 已记录,实证可达 plan=free。
- **遗留风险**: 极端情况下 issuer 即使 `oai-oauth-session.workspaces[]==[]` 仍走默认 Team workspace(尚未实证),需 backlog round 12+ 用更长间隔重试或额外信号(`x-oai-account-id` header 显式钉死 personal workspace)进一步验证。

##### 4.7.3.2 本地状态与后端真相脱节

KICK 成功后,本地 `accounts.json` 与后端真相在 issuer ledger TTL 窗口内会出现以下脱节模式:

| # | 本地 status | master /users 真相 | issuer ledger 真相 | 风险 |
|---|---|---|---|---|
| TTL-1 | `active`(未及时同步) | 真踢(不在 Team) | `workspaces=[]` | 低 — `sync_account_states` 周期会捞回 |
| TTL-2 | `auth_invalid + last_kicked_at` | 真踢(不在 Team) | `workspaces=[]` | 安全 — 本地与后端一致 |
| TTL-3 | `auth_invalid + last_kicked_at` | 已 auto-rejoin(回到 Team `standard-user`) | `workspaces=[{wid: master}]` | **本地标 invalid 但后端实质恢复** — 用户需决定是否手工 re-sync 或跑 OAuth 重拿 bundle |
| TTL-4 | `active`(假象 healthy) | 真踢但已 issuer auto-rejoin 回 standard-user | 同 TTL-3 | sync 周期会发现 fetch 不到 quota / role 不匹配,自动处置 |
| TTL-5 | `kicked_no_session`(P2 reconcile 临时字面量) | 真踢 | 任意 | **非 spec 合法 status**(参见 `p1-p2-execution-report.md` §"Spec 一致性说明") — 后续遍历需识别或切回 `auth_invalid` |

**调用方启示**:
- `_reconcile_team_members` 在见到"本地 invalid 但 master /users 仍存在 standard-user"时,需 **二次决策**(用户裁定 / 重跑 OAuth / 直接接受);不应假设"踢成功 ⇒ 本地标 invalid 永久成立"。
- 非 spec 字面量(如 `kicked_no_session`)只用于人工 reconcile note,任何代码路径不得依赖该字面量做分支判断。
- `last_kicked_at` 被设置时,允许 `status` 短暂不一致,但 sync 周期内必须在一个 polling 间隔内回到 I6 不变量(`auth_invalid` 或 `deleted`)。

##### 4.7.3.3 §4.5 反向不变量补充约束(非 breaking)

I6 不变量(`last_kicked_at != None ⇒ status in {auth_invalid, deleted}`)在 issuer auto-rejoin 路径下短暂出现"被踢标 + 后端 standard-user"的脱节态,该状态非 I6 违反 — `last_kicked_at` 表达"本地观测到的最后一次踢操作时间戳",不直接等价于"当前后端 user 是否存在"。建议未来引入辅助字段(如 `last_known_team_state`,P2 reconcile 已使用)记录 reconcile 周期最后一次 master /users 视图,与 `last_kicked_at` 配合解决脱节判断。

#### 4.7.4 退避策略关系(对应 oauth-workspace-selection.md §3.4)

本 §4.7 与 oauth-workspace-selection.md v1.5.0 §4.4 同一现象的两份视角:

- §4.4(workspace-selection 视角): 描述 OAuth 子进程内部如何绕开 ledger TTL(stage 1 快路径 + skip_ui_fallback_on_empty + use_personal)
- §4.7(account-state-machine 视角): 描述本地账号状态如何在 ledger TTL 窗口内与后端真相脱节,以及调用方如何处置

两份 spec 同时引用,**任何对 retry backoff 的调整必须双侧对齐**,否则 fill / sync 路径会观察到不一致行为。

#### 4.7.5 backlog(round 12+ 候选)

- 用更长 retry 间隔(指数退避到分钟级)实测 issuer ledger TTL 上界
- 探针 `auth.openai.com /backend-api/oai-oauth-session` 在踢后定期 GET,记录 `workspaces[]` 何时清空,实证 TTL
- 评估是否在 fill 路径下显式钉 `x-oai-account-id` header 强制 personal workspace,降低对 issuer ledger 的依赖
- 评估 `claimed_domain_org_id` auto-join 关闭路径(需 master Team admin 后台手工操作 + 文档化)

---

## 5. 调用方处置规范(状态消费方)

### 5.1 工作池筛选

```python
# accounts.py 已存在
def get_active_accounts():
    """status == active 且非主号"""
    return [a for a in load_accounts() if a["status"] == STATUS_ACTIVE]


def get_personal_accounts():
    """status == personal 且非主号"""


def get_standby_accounts():
    """status == standby,按 quota_recovered 排序"""


# 新增推荐(便于 UI 与 reconcile)
def get_terminal_accounts():
    """auth_invalid + orphan,需要人工介入或 reconcile 清理"""
    return [a for a in load_accounts()
            if a["status"] in (STATUS_AUTH_INVALID, STATUS_ORPHAN)]
```

### 5.2 reconcile 处置(`manager.py:_reconcile_team_members` 旧 + 新)

| 输入状态 | reconcile 行为 |
|---|---|
| `auth_invalid` + 在 workspace 占席位 | KICK + 保留本地记录 + 等用户/批量删除 |
| `auth_invalid` + 不在 workspace | 保留本地记录(已自然清理),等用户决定是否删 |
| `orphan` + 在 workspace 占席位 | KICK |
| 其他状态 | 沿用现有处理 |

### 5.3 UI 显示规范

| 状态 | UI 文案 | 操作按钮 |
|---|---|---|
| `active` | 工作中 | 强制下线 / 删除 |
| `exhausted` | 已耗尽,X 小时后恢复 | 删除(灰显:auto check 已锁) |
| `standby` | 待机中(quota 已恢复 / 未恢复) | 立即重用 / 删除 |
| `pending` | 注册中... | (无) |
| `personal` | 个人 free 号 | 删除(短路 fetch_team_state,见 PRD-2 FR-G1) |
| `auth_invalid` | **token 失效,已退出 Team** | 删除(短路 fetch_team_state) |
| `orphan` | **席位异常,等待清理** | KICK + 删除 |
| **`degraded_grace`(v2.0)** | **过渡期(母号已取消,grace 到期 YYYY-MM-DD HH:MM)** + grace 倒计时 | 删除(短路 fetch_team_state)/ 切个人号(走 leave_workspace + manual_account)/ 立即重新对账(`/api/admin/reconcile?force=1`)|

---

## 6. 单元测试 fixture 与样本数据

### 6.1 状态机 transition 表(yaml)

```yaml
# tests/fixtures/state_transitions.yaml
- name: register_team_success
  from: pending
  trigger: _run_post_register_oauth(leave_workspace=False, bundle.plan="team", quota="ok")
  to: active
  expected_fields:
    auth_file: not_null
    seat_type: chatgpt | codex
    last_active_at: not_null

- name: register_team_quota_no_quota
  from: pending
  trigger: _run_post_register_oauth(leave_workspace=False, bundle.plan="team", quota="no_quota")
  to: auth_invalid
  expected_register_failure:
    category: no_quota_assigned

- name: register_team_plan_unsupported
  from: pending
  trigger: _run_post_register_oauth(bundle.plan_supported=False)
  to: auth_invalid
  expected_register_failure:
    category: plan_unsupported

- name: register_team_phone_blocked
  from: pending
  trigger: _run_post_register_oauth raises RegisterBlocked(is_phone=True)
  to: auth_invalid
  expected_register_failure:
    category: oauth_phone_blocked
    stage: run_post_register_oauth_team

- name: sync_active_kicked_by_admin
  from: active
  trigger: sync_account_states(not in_team) + wham 401
  to: auth_invalid
  expected_fields:
    last_kicked_at: not_null

- name: sync_active_natural_standby
  from: active
  trigger: sync_account_states(not in_team) + wham exhausted
  to: standby

- name: sync_active_workspace_drift
  from: active
  trigger: sync_account_states(not in_team) + workspace_account_id 不一致
  to: active  # 保留(母号切换遗留)

- name: reinvite_plan_drift
  from: standby
  trigger: reinvite_account(bundle.plan="free")
  to: auth_invalid
  expected_register_failure:
    category: plan_drift

- name: reinvite_plan_unsupported
  from: standby
  trigger: reinvite_account(bundle.plan_supported=False)
  to: auth_invalid
  expected_register_failure:
    category: plan_unsupported

- name: reinvite_phone_blocked
  from: standby
  trigger: reinvite_account raises RegisterBlocked(is_phone=True)
  to: auth_invalid
  expected_register_failure:
    category: oauth_phone_blocked
    stage: reinvite_account

- name: reinvite_team_quota_low
  from: standby
  trigger: reinvite_account(bundle.plan="team", quota="ok" but pct<threshold)
  to: standby  # 锁 5h(旧行为)
  expected_fields:
    quota_exhausted_at: not_null

- name: reinvite_team_success
  from: standby
  trigger: reinvite_account(bundle.plan="team", quota verified)
  to: active
```

### 6.2 单测代码

```python
# tests/unit/test_state_machine.py
import pytest
import yaml
from pathlib import Path
from autoteam.accounts import (
    STATUS_ACTIVE, STATUS_STANDBY, STATUS_PENDING, STATUS_AUTH_INVALID,
    STATUS_PERSONAL, STATUS_ORPHAN, STATUS_EXHAUSTED, AccountRecord,
)

TRANSITIONS = yaml.safe_load(Path("tests/fixtures/state_transitions.yaml").read_text())


@pytest.mark.parametrize("case", TRANSITIONS)
def test_state_transition(case, mock_factory):
    """每个 transition 跑一遍,验证 from / to / 失败记录字段"""
    acc = mock_factory.account(status=case["from"])
    mock_factory.fire_trigger(case["trigger"], acc)
    final = mock_factory.reload(acc.email)
    assert final.status == case["to"]
    if "expected_register_failure" in case:
        rec = mock_factory.last_failure(acc.email)
        for k, v in case["expected_register_failure"].items():
            assert rec[k] == v


def test_state_field_invariants():
    """每个 status 的 必备/禁用 字段不变量"""
    cases = [
        # (status, must_have_keys, must_be_none_keys)
        (STATUS_PENDING, ["email", "password", "created_at"], ["auth_file"]),
        (STATUS_ACTIVE, ["email", "auth_file"], []),
        (STATUS_EXHAUSTED, ["email", "auth_file", "quota_exhausted_at", "quota_resets_at"], []),
        (STATUS_PERSONAL, ["email", "auth_file"], []),
        (STATUS_ORPHAN, ["email"], ["auth_file"]),
    ]
    for status, must_have, must_none in cases:
        acc = AccountRecord(...)  # 用工厂构造
        for k in must_have:
            assert getattr(acc, k) is not None
        for k in must_none:
            assert getattr(acc, k) is None


def test_pydantic_account_record_round_trip():
    """JSON 序列化 / 反序列化后字段不丢失"""
    acc = AccountRecord(
        email="t@example.com", password="x", status="active",
        seat_type="codex", auth_file="/auths/codex-t.json",
        plan_supported=True, plan_type_raw="team",
        created_at=1714000000.0,
    )
    js = acc.model_dump_json()
    acc2 = AccountRecord.model_validate_json(js)
    assert acc2.status == "active"
    assert acc2.plan_supported is True
```

### 6.3 完整 accounts.json 样本

```json
[
  {
    "email": "alice-team@example.com",
    "password": "abc",
    "cloudmail_account_id": "cm-1",
    "status": "active",
    "seat_type": "chatgpt",
    "workspace_account_id": "ws-100",
    "auth_file": "/abs/auths/codex-alice-team-team-deadbeef.json",
    "quota_exhausted_at": null,
    "quota_resets_at": null,
    "last_quota_check_at": 1714050000.0,
    "last_quota": {
      "primary_pct": 35,
      "primary_resets_at": 1714060000,
      "primary_total": 100,
      "primary_remaining": 65,
      "weekly_pct": 10,
      "weekly_resets_at": 1714600000
    },
    "last_active_at": 1714050000.0,
    "created_at": 1714000000.0,
    "plan_supported": true,
    "plan_type_raw": "team",
    "last_kicked_at": null
  },
  {
    "email": "bob-self-serve@example.com",
    "password": "xyz",
    "cloudmail_account_id": "cm-2",
    "status": "auth_invalid",
    "seat_type": "codex",
    "workspace_account_id": "ws-100",
    "auth_file": null,
    "quota_exhausted_at": null,
    "quota_resets_at": null,
    "last_quota_check_at": null,
    "last_quota": null,
    "last_active_at": null,
    "created_at": 1714000000.0,
    "plan_supported": false,
    "plan_type_raw": "self_serve_business_usage_based",
    "last_kicked_at": null
  },
  {
    "email": "charlie-kicked@example.com",
    "password": "def",
    "cloudmail_account_id": "cm-3",
    "status": "auth_invalid",
    "seat_type": "chatgpt",
    "workspace_account_id": "ws-100",
    "auth_file": "/abs/auths/codex-charlie-team-team-cafe1234.json",
    "quota_exhausted_at": null,
    "quota_resets_at": null,
    "last_quota_check_at": 1714050000.0,
    "last_quota": null,
    "last_active_at": 1714045000.0,
    "created_at": 1714000000.0,
    "plan_supported": true,
    "plan_type_raw": "team",
    "last_kicked_at": 1714050000.0
  }
]
```

---

## 7. 不变量(Invariants)

- **I1**:任何状态变更必须经过 `update_account` 入口,**禁止**直接 dict 改写后 `save_accounts`(避免漏触发持久化)
- **I2**:`STATUS_ACTIVE` 必须有 `auth_file`(注册收尾或 reinvite 验证后写入);任何把 active 设回但不写 auth_file 的代码都是 bug
- **I3**:`STATUS_AUTH_INVALID` 不能有 `quota_exhausted_at`(因为不会自然恢复);写时必须清空
- **I4**:`STATUS_PERSONAL` 是终态,不能回到 active(转换路径只有 manual_account 重新注册或新 OAuth bundle)
- **I5**:`STATUS_ORPHAN` 必须 `auth_file == None`(定义:占席位但本地无凭证);`auth_file` 存在的应判 active / standby / auth_invalid
- **I6**:`last_kicked_at` 字段一旦写入,后续状态转移不能清掉(用于 reconcile 历史回放;只在 delete_account 时随记录一起删)
- **I7**:reconcile_anomalies(`manager.py:161-471`)对 `auth_invalid` 的 KICK 行为必须保持幂等(重复 KICK 不抛异常,`kick_status="already_absent"` 视为成功)
- **I8**:状态白名单变更(新增枚举)需要全局检查 4 处:`Dashboard.vue` statusClass / `cpa_sync.py` 同步规则 / `sync_account_states` 处置 / 本 spec §4.2
- **I10**(v2.0 Round 9):**STATUS_DEGRADED_GRACE 仅由 `_apply_master_degraded_classification` helper 写入与撤回**;invite / OAuth / reinvite / `_run_post_register_oauth` / `manual_account._finalize_account` 等业务路径**禁止**直接把 status 写为 `degraded_grace`。原因:GRACE 是"母号订阅状态 × 子号工作状态"的组合派生,只有同时持有 master_health 探测结果与子号 JWT grace_until 的 helper 才有完整决策上下文。
- **I11**(v2.0 Round 9):**helper 调用必须永不抛**(与 master-subscription-health.md M-I1 永不抛对齐)— 任何 master_health probe 异常 / JWT 解析异常 / save_accounts 异常都必须 logger.warning 兜住,不影响调用方主流程(lifespan 启动 / sync 完成 / cmd_check / cmd_rotate 都不能因 retroactive 异常而失败)。
- **I12**(v2.0 Round 9):**GRACE 子号绝不被 KICK** — `_reconcile_team_members` 处理 ghost / orphan 时若发现 acc.status == GRACE,跳过 KICK 与 state flip;只允许 read-only 扫描和日志输出。理由与 master-subscription-health.md M-I10 同源:母号降级期间任何 KICK 都可能踢掉用户仍在用的实活号。
- **I9**(Round 6 落地,Round 7 文档同步):**add-phone 探针必须接入 7 处**(invite 4 + OAuth 3,Round 6 P1.1 后 OAuth 扩为 4 处,合计 8 处)。具体清单:
  - **invite 阶段 4 处**(`invite.py:247/282/364/446`):`invite_filling`、`invite_confirm`、`invite_pre_submit`、`invite_post_submit`
  - **OAuth 阶段 4 处**(`codex_auth.py:586/638/910/939`,Round 6 加 C-P4):`oauth_about_you`(C-P1)、`oauth_consent_{step}`(C-P2)、`oauth_callback_wait`(C-P3)、`oauth_personal_check`(C-P4,Round 6 PRD-5 FR-P1.1 引入)
  - 任一探针缺失即视为 bug;新增 personal/team 邀请路径必须复用 `assert_not_blocked` + `RegisterBlocked` 复用语义
  - 详见 `./add-phone-detection.md §4.1` 探针接入清单

---

## 附录 A:状态机变更历史

| 版本 | 时间 | 变更 |
|---|---|---|
| v0.1 | round-1 | 初始 4 状态(active/exhausted/standby/pending) |
| v0.2 | round-2 | 加 personal |
| v0.3 | round-3 | 加 auth_invalid + orphan(commit cf2f7d3) |
| v1.0 | 2026-04-26 PRD-2 | 加 last_kicked_at / plan_supported / plan_type_raw 字段;补全转移规则;不新增 STATUS_PHONE_REQUIRED(复用 auth_invalid + register_failures) |
| v1.1 | 2026-04-26 Round 7 P2 follow-up | (1) §3.3 加 uninitialized_seat 中间态(Round 6 引入,STATUS_ACTIVE/PENDING 的"待验证"子态,经 cheap_codex_smoke 二次验证后转 5 分类);(2) §4.1 转移矩阵加 cheap_codex_smoke 触发条件(uninitialized_seat + smoke=auth_invalid → AUTH_INVALID;smoke=uncertain → 保留原状态);(3) §4.3a 加删除链短路语义(STATUS_AUTH_INVALID 与 STATUS_PERSONAL 等价处置;Round 6 FR-P1.2 / FR-P1.4 落地);(4) §6 加 I9 不变量 — add-phone 探针 7 处接入(invite 4 + OAuth C-P1~C-P4 共 4 处,Round 6 加 C-P4);(5) 引用方加 PRD-5/PRD-6 + FR-P0/P1.2/P1.4/D6/D8;关联 `prompts/0426/prd/prd-6-p2-followup.md` §5.8 |
| **v2.0** | **2026-04-28 Round 9** — **BREAKING: 引入 STATUS_DEGRADED_GRACE 8 状态机**。(1) §2.1 加枚举 `STATUS_DEGRADED_GRACE = "degraded_grace"`;(2) §2.2 AccountStatus Literal 增 `degraded_grace`;AccountRecord 加 3 字段(`grace_until` / `grace_marked_at` / `master_account_id_at_grace`);(3) §2.3 不变量表追加 grace 行(必备 grace_until>0 + master_account_id_at_grace,禁用 quota_exhausted_at);(4) §3.1 ASCII 加 GRACE 子图(进入 / grace 到期 → standby / 母号续费 → active / 用户 leave_workspace → personal);(5) §3.2 分类表加"过渡态"分类;(6) §4.3b 删除链短路扩 GRACE 等价 PERSONAL/AUTH_INVALID;(7) §4.4 加 GRACE 转移规则三段(进入 / 退出 / 5 触发点矩阵 RT-1~RT-6),其中 retroactive helper 集中由 `_apply_master_degraded_classification(workspace_id, grace_until)` 承担;(8) §4.5 反向不变量加 grace;(9) §5.3 UI 显示规范加 grace 文案 + grace 倒计时 + 三档操作;(10) §7 不变量加 I10(GRACE 仅由 helper 写)/ I11(helper 永不抛)/ I12(GRACE 不被 KICK)。<br><br>**round-1~8 测试 mock 更新点**(本版本 BREAKING 必须扫):<br>① `tests/fixtures/state_transitions.yaml` 不影响(yaml 里没有 grace 行,新增即可);<br>② 任何 `mock` AccountRecord / accounts.json fixture 显式枚举状态字符串的位置(ripgrep `STATUS_(ACTIVE\|EXHAUSTED\|STANDBY\|PERSONAL\|AUTH_INVALID\|ORPHAN\|PENDING)` 单测里命中);<br>③ Round 5/6 `_reconcile_team_members` mock 现假设的"7 状态分类"枚举遍历(若有 `for status in [...]:` 写死 7 个,需补 grace);<br>④ Round 7 `test_state_field_invariants` 5 个 case 增 `(STATUS_DEGRADED_GRACE, [...], [...])`;<br>⑤ Round 8 `_reconcile_master_degraded_subaccounts` 测试 mock 期望"ACTIVE → STANDBY",v2.0 要改 "ACTIVE → DEGRADED_GRACE"(grace 期内)+ "DEGRADED_GRACE → STANDBY"(grace 到期);<br>⑥ Round 8 master-subscription probe 测试 fixture `master_accounts_responses.json` 不变,但调用方在 retroactive helper 内的处置 mock 需要改;<br>⑦ Round 7 / 8 删除链 short_circuit 测试加 `test_grace_short_circuit_*`(参见 §4.3b);<br>⑧ 前端 Vue / TS 任何写死 7 状态字符串集合的位置(`web/src/components/Dashboard.vue statusClass` / `web/src/utils/statusLabel.ts` 等)需要补 GRACE。<br><br>**引用方变更**:加 Round 9 task `04-28-account-usability-state-correction` / spec-2 v1.6 / master-subscription-health v1.1 §11~§13 / AC-B1~AC-B8 |

| **v2.1** | **2026-04-28 Round 11** — 加 §4.6 母号 subscription_grace × 子号 GRACE 联动表。(1) §0 元数据 bump,引用方加 Round 11 task / spec-2 v1.7 / master-subscription-health v1.2 §14 / realtime-probe v1.0 / AC1~AC4;(2) **新增 §4.6 联动决策表 10 行 L-1~L-10** — 双侧 healthy 平稳态(L-1 / L-4)/ 撤回路径(L-2 / L-5,触发条件 reason ∈ ("active", "subscription_grace"))/ 既有 cancelled 路径(L-7 / L-8 / L-9,Round 9 不变);(3) **关键改动 vs v2.0**:`_apply_master_degraded_classification` 撤回路径触发条件由 `reason == "active"` 扩为 `reason ∈ ("active", "subscription_grace")` — 与 master_health v1.2 §14.5 双向对齐;(4) 加 2 单测期望(`test_retroactive_helper_subscription_grace_reverts_active` / `test_retroactive_helper_subscription_grace_keeps_active`)。Round 9 v2.0 既有 8 状态机 / GRACE 子图 / 7 不变量 / 5 触发点矩阵全部不变,仅 §4.6 增量 + 元数据局部修订。配套 Round 11 task `04-28-round11-master-resub-models-validate` Approach A 决策落地。 |
| **v2.1.1** | **2026-04-28 Round 11 二轮** — OAuth 失败转移矩阵补全(`_kick_team_seat_after_oauth_failure` 同步 KICK 落地 5 触发点)。(1) §0 元数据 bump v2.1 → v2.1.1;(2) **§4.1 触发函数 → 转移矩阵新增 5 行** — `manager.py:1873`(master_degraded fail-fast)/ `manager.py:1898`(RegisterBlocked is_phone)/ `manager.py:1905`(unexpected RegisterBlocked)/ `manager.py:1938`(plan_supported=False)/ `manager.py:2014`(bundle 缺失);全部状态转移 PENDING → AUTH_INVALID 且**带 ws kicked 同步标识**(by `_kick_team_seat_after_oauth_failure(email, reason=...)`);(3) **修正 `manager.py:2014` bundle 缺失分支** — v1.7 之前是 PENDING → ACTIVE(team_auth_missing 旧行为),Round 11 二轮统一为 PENDING → AUTH_INVALID(防止 fill 计数器误算累积僵尸);旧行为 v1.7 之前矩阵保留作为对比参考;(4) **关联 spec-2 v1.7.1 §3.8** — `_kick_team_seat_after_oauth_failure(email, *, reason)` helper 完整契约 + 5 触发位点 + 5 测试 case(`tests/unit/test_round11_oauth_failure_kick_ws.py`);(5) **关联不变量 M-MA-helper**(spec-2 §3.8) — 任何走 STATUS_AUTH_INVALID 路径的 OAuth 失败位点必须配对调用 helper;helper 异常吞掉只 warning 不传播 → reconcile 兜底;状态机层面强制可观测的转移配对(state + ws kick)。Round 9/11 一轮既有 §4.1 旧矩阵 / §4.2 / §4.3 / §4.4 / §4.5 / §4.6 全部保持,仅 §4.1 新增 5 行 + 修订一行(line 237 旧 ACTIVE 行加 v1.7 之前注)。配套 Round 11 二轮任务 `04-28-round11-master-resub-models-validate` 收尾落地。 |
| **v2.1.2** | **2026-04-29 Round 11 五轮 spec-update** — 加 §4.7 OAuth issuer ledger TTL 现象(实证驱动,纯 spec 增量 — 无代码改动)。(1) §0 元数据 bump v2.1.1 → v2.1.2,引用方加 oauth-workspace-selection v1.5.0 §4.4(姊妹章节)与 Round 11 五轮三份研究报告(`p1-p2-execution-report.md` / `three-kicked-emails-probe.md` / `admin-ui-kick-endpoint.md`);(2) **新增 §4.7 OAuth issuer ledger TTL 现象** 五个子节 — §4.7.1 现象描述(KICK 后 master /users 立即生效,但 issuer 端 `oai-oauth-session.workspaces[]` 不立即清,有最终一致性 TTL 几小时 +,`claimed_domain_org_id` 域级 auto-rejoin 概率性触发)/ §4.7.2 实证证据(`404907e1c8` auto-rejoin / `b7c4aaf8f2` + `fd3b5ccae1` 真踢 / `fd3b5ccae1` stage 1 快路径 71.3s 拿到 plan=free + personal account_id `7f4384d7-...`)/ §4.7.3 对状态机的影响(retry backoff 215s 远短于 ledger TTL — 当前对策走 `use_personal=True` 强切 personal workspace,见 §4.4 / 本地状态与后端真相 5 种脱节模式 TTL-1~TTL-5 / §4.5 反向不变量 I6 在 auto-rejoin 路径短暂脱节非违反)/ §4.7.4 退避策略关系(本节与 oauth-workspace-selection v1.5.0 §4.4 同一现象的两份视角,任何 retry backoff 调整必须双侧对齐)/ §4.7.5 backlog round 12+(实证 ledger TTL 上界 / 探针 oai-oauth-session 周期 GET / `x-oai-account-id` header 钉死 personal / `claimed_domain_org_id` 关闭路径);(3) **关键启示**:KICK 成功 ≠ 该子号能立即拿到 plan=free 的 OAuth bundle;调用方在见到"本地 invalid 但 master /users 仍存在 standard-user"时需二次决策;非 spec 字面量(如 `kicked_no_session`)只用于人工 reconcile note,任何代码路径不得依赖该字面量做分支判断;(4) Round 9 v2.0 既有 8 状态机 / GRACE 子图 / 7 不变量 / 5 触发点矩阵 / Round 11 一轮 §4.6 / Round 11 二轮 §4.1 5 行新增全部不变,仅 §4.7 增量 + 元数据局部修订。配套 Round 11 五轮 spec-only 任务 `04-28-round11-master-resub-models-validate` trellis-update-spec 阶段。 |

---

**文档结束。** 工程师据此可直接编写 7 状态 + 转移点的代码改造、Pydantic 模型、单测,不需额外决策。
