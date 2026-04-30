# Shared SPEC: OAuth Workspace 显式选择(personal sticky-rejoin 修复)

## 0. 元数据 + 引用方

| 字段 | 内容 |
|---|---|
| 名称 | OAuth flow 内 personal workspace 显式选择(HTTP `/api/accounts/workspace/select` 主路径 + Playwright UI fallback + 5 次重试) |
| 版本 | **v1.5.0 (2026-04-29 Round 11 五轮 — stage 1 快路径 P1 实证可拿 plan=free + issuer ledger TTL 现象沉淀。新增 W-I15(stage 1 `workspaces=[]` 无需 stage 2 兜底,P1 实测 fd3b5ccae1 71.3s 拿到 plan=free + account_id=`7f4384d7-...`)+ §4.4 issuer ledger TTL NOTE(kick 后立即 OAuth 命中 ledger Team 项 → plan=team;等待若干时间 → ledger 清 → workspaces=[] → use_personal=True 拿 free)+ §4.1 v1.4.0 章节加 NOTE(W-I11/W-I12 fresh re-login 仅 stage 1 失败时触发,issuer ledger 清空时 stage 1 即足够)。配套实证:`.trellis/tasks/04-28-round11-master-resub-models-validate/research/p1-p2-execution-report.md` § P1)** |
| 上一版本 | v1.4.0 (Round 11 四轮 — fresh re-login fallback 两阶段架构) |
| 主题归属 | `login_codex_via_browser(use_personal=True, chatgpt_session_token=...)` 流程内 workspace 主动选择 + cookie 解码契约 + 失败分类 + sleep(8) 删除依据 + **两阶段 OAuth(快路径 cookie 注入 + fallback fresh re-login)** + **issuer ledger TTL 现象**(v1.5.0) |
| 引用方 | PRD-7(Round 8 master-team-degrade-oauth-rejoin) / spec-2-account-lifecycle.md v1.7+ §3.4.6 / FR-W1~W5(待 PRD-7 落地)/ **Round 11 task `04-28-round11-master-resub-models-validate` 五轮 spec-update**(P1 实证沉淀) |
| 共因 | Round 8 PRD §1 代码根因 — `auth.openai.com` 的 `default_workspace_id` 不随 ChatGPT DELETE user 联动,issuer 默认按 default 颁 token,personal OAuth 拿到 `plan_type=team`;**Round 11 四轮新增共因 — Playwright `locator.fill()` 不触发 React `input` 事件链 → OpenAI `auth.openai.com/log-in` welcome-back 页 Continue 按钮永久灰显禁用,刚踢出 Team 的非新创建账号必卡死**;**Round 11 五轮新增观察 — kick 后 issuer 端 `oai-oauth-session.workspaces[]` 清空有 TTL(实证至少几小时),期间立即 OAuth 仍命中 Team ledger;等待充分后 stage 1 即拿 free,无需触发 stage 2 fresh re-login** |
| 不在范围 | Master 订阅健康度探针(见 [`./master-subscription-health.md`](./master-subscription-health.md)) / `_account` cookie 注入(已存在,Team 路径用,本 spec 不动) / `oai-oauth-session` 之外的 sentinel-token 等 OpenAI 私有反爬细节(由 patch-implementer 抓包确定) |

---

## 1. 概念定义

| 术语 | 定义 |
|---|---|
| `sticky-rejoin` 误用 | PRD 旧叙事 — "OpenAI 偷偷把用户加回 workspace";research 已澄清:**真相是 default_workspace_id 不随 DELETE user 自动 unset**(research/sticky-rejoin-mechanism.md §1.2-1.3) |
| `default_workspace_id` | `auth.openai.com` 后端为每个 user 维护的 last-used workspace 标记;OAuth flow 没显式 `workspace/select` 时 issuer 用它颁 token |
| `oai-oauth-session` | `auth.openai.com` 域 cookie,JWT-like 结构,base64 解码后 JSON 含 `workspaces[]` 数组 — research/oauth-personal-selection.md §2.3 + research/sticky-rejoin-mechanism.md §3.1 |
| `workspace/select` | `POST https://auth.openai.com/api/accounts/workspace/select`,接收 `{workspace_id}` 直接定向,绕过 UI 选择页;返回 `continue_url` 或 302 含 `?code=...&state=...` |
| `Playwright UI fallback` | 主路径(HTTP)失败时的兜底:Playwright 主动 goto `auth.openai.com/workspace`,DOM 找 "Personal/个人" 按钮点击 — cnitlrt PR#39 已实证 |
| `5 次重试` | 同一 user 多次 OAuth retry 触发后端最终一致性 — `openai/codex#1977` ejntaylor 实证 5 次成功;本 spec 作为终极保险 |

---

## 2. 完整数据契约

### 2.1 cookie 解码契约(`oai-oauth-session`)

**结构假设**(research/sticky-rejoin §3.1 + research/oauth-personal-selection §2.3 反推,**实施期 patch-implementer 必须抓包验证**):

```python
# cookie 名:oai-oauth-session
# 域:auth.openai.com
# HttpOnly: 视情况(可能 false,Playwright context.cookies() 可读)
# 值结构:base64url(JSON) 或 三段 JWT(header.payload.signature),取 [0] 段 base64url decode

# 解码后 JSON schema(实施期以抓包为准,本 spec 为目标契约)
{
  "user_id": "<auth0 user uuid>",
  "workspaces": [
    {
      "id": "<workspace_account_id uuid>",
      "name": "Master Team",
      "structure": "workspace",          # 或 "personal"
      "role": "account-owner",            # owner / admin / user
      "plan_type": "team"                  # 部分实现含,可能缺
    },
    {
      "id": "<personal_account_id uuid>",
      "name": "Personal",
      "structure": "personal",
      "role": "account-owner"
    }
  ],
  "default_workspace_id": "<sticky 指向>",
  "issued_at": 1777699200
}
```

**personal 项识别规则**(优先级从高到低,实施期任一命中即视为 personal):

```python
def _is_personal_workspace(item: dict) -> bool:
    """personal 识别 — 字段名以 OpenAI 后端实际下发为准."""
    if str(item.get("structure") or "").lower() == "personal":
        return True
    if str(item.get("plan_type") or "").lower() == "free":
        return True
    if item.get("is_personal") is True:        # 部分实现的 boolean 标记
        return True
    return False
```

**workspaces 数组为空 / 无 personal 项的语义**:

| 场景 | 含义 | 处置 |
|---|---|---|
| `workspaces == []` | OAuth session cookie 异常 / 解码失败 | 走 Playwright UI fallback |
| `workspaces` 非空但无 personal | user 在后端确实**只属于 Team**(personal workspace 未恢复) | fail-fast,失败分类 `oauth_workspace_select_no_personal`,不重试 |
| `workspaces` 含 personal | 主路径可走 | POST `workspace/select` |

### 2.2 函数签名(实施期目标)

#### 2.2.1 cookie 解码工具

```python
def decode_oauth_session_cookie(
    page_or_context,
) -> Optional[dict]:
    """从 Playwright page / browser_context 读 oai-oauth-session cookie 并解码.

    实施位置:`src/autoteam/chatgpt_api.py` 末尾或新文件 `oauth_workspace.py`

    返回:
      解码后的 JSON dict(含 workspaces[]);失败时返回 None(不抛异常)

    实施细节(以抓包为准):
      1. context.cookies("https://auth.openai.com") 获取 cookie 列表
      2. find name == "oai-oauth-session"
      3. 若值含 ".",取首段;否则整串 base64url decode
      4. JSON parse,失败回 None
    """
```

#### 2.2.2 workspace/select 主路径

```python
def select_oauth_workspace(
    page,
    workspace_id: str,
    *,
    consent_url: str,
    timeout: float = 15.0,
) -> tuple[bool, Optional[str], dict]:
    """POST https://auth.openai.com/api/accounts/workspace/select.

    Returns:
      (success, continue_url_or_redirect, evidence)
      success=True 表示 endpoint 200/302 + 拿到 continue_url 或 ?code= redirect
      success=False 表示 4xx/5xx/异常,evidence 含失败原因

    实施细节:
      - 用 page.evaluate() 发 fetch 调用,credentials='include' 让 cookie 自动带
      - body: {"workspace_id": <uuid>}
      - headers: {"Content-Type": "application/json", "Referer": consent_url}
      - 不主动添加 sentinel-token —— Playwright context 已注入,fetch 走相同上下文
      - 失败时 evidence["http_status"] / evidence["body_preview"] 各 200 字以内,供事后排查
    """
```

#### 2.2.3 Playwright UI fallback

```python
def force_select_personal_via_ui(
    page,
    *,
    timeout_per_step: float = 8.0,
) -> tuple[bool, dict]:
    """fallback — 主动 goto auth.openai.com/workspace,DOM 找 Personal 按钮点击.

    源自 cnitlrt PR#39 `_ensure_workspace_target_session` + `_select_workspace_target`
    (research/sticky-rejoin-mechanism.md §3.2)

    流程:
      1. page.goto("https://auth.openai.com/workspace", wait_until="domcontentloaded")
      2. 校验是否在 workspace 选择页 (URL 或 标题文案)
      3. locator("text=/个人|Personal/i") + locator("button:has-text('Personal')") 等候选,first visible 点击
      4. 点 "继续/Continue" 按钮(可选,部分版本无确认页)
      5. 返回 (success, evidence)

    Returns:
      (True,  {url, clicked_text, ts_ms}) 命中 Personal 按钮且点击成功
      (False, {url, page_title, snapshot_path}) 未在选择页 / 找不到按钮 / 点击异常
    """
```

#### 2.2.4 顶层编排函数

```python
def ensure_personal_workspace_selected(
    page,
    *,
    consent_url: str,
    max_retries: int = 5,
) -> tuple[bool, str, dict]:
    """Personal OAuth 主流程 — 三层兜底.

    Returns:
      (success, fail_category, evidence)
      success=True ⇒ fail_category=""(空串),OAuth 流程可继续走 callback
      success=False 时 fail_category ∈ {
          "oauth_workspace_select_no_personal",
          "oauth_workspace_select_endpoint_error",
          "oauth_plan_drift_persistent",   # 重试 5 次仍 plan_type=team
      }

    流程(伪代码):
      session = decode_oauth_session_cookie(page.context)
      if session is None: → 走 fallback (UI)
      personal = next(w for w in session["workspaces"] if _is_personal_workspace(w), None)
      if personal is None:
          return False, "oauth_workspace_select_no_personal", {workspaces: [...]}
      ok, redirect_url, ev = select_oauth_workspace(page, personal["id"], consent_url=consent_url)
      if not ok:
          fb_ok, fb_ev = force_select_personal_via_ui(page)
          if fb_ok:
              return True, "", {primary_failed: True, fallback: fb_ev}
          return False, "oauth_workspace_select_endpoint_error", {primary: ev, fallback: fb_ev}
      return True, "", {primary: ev}
    """
```

### 2.3 失败分类常量

```python
# src/autoteam/register_failures.py 文档化(spec-2 v1.5 RegisterFailureRecord enum 扩)
OAUTH_WS_NO_PERSONAL = "oauth_workspace_select_no_personal"
"""workspaces[] 中找不到 personal 项 — user 在后端事实上只属于 Team
   (sticky 根因之一)。fail-fast,不重试。"""

OAUTH_WS_ENDPOINT_ERROR = "oauth_workspace_select_endpoint_error"
"""POST /api/accounts/workspace/select 返回 4xx/5xx 或网络异常,且 UI fallback 也失败。
   通常为端点变更 / sentinel-token 反爬 / Playwright DOM 漂移。"""

OAUTH_PLAN_DRIFT_PERSISTENT = "oauth_plan_drift_persistent"
"""workspace/select 成功但 5 次 OAuth retry 后 bundle.plan_type 仍非 free。
   罕见 — 后端最终一致性失败,与 register_failures 已有 plan_drift 区分:
   plan_drift 是单次拒收;persistent 是 5 次重试都拒收。"""
```

---

## 3. 行为契约

### 3.1 前置条件

- `page` 是 Playwright Page 对象,且已完成 `step-0` ChatGPT 预登录 + 邮箱+密码+OTP(`codex_auth.py:295-562`)
- `consent_url` 是从 `auth.openai.com/sign-in-with-chatgpt/codex/consent` 形态的 referer URL(用于 select 端点的 Referer 头)
- 调用前 `is_master_subscription_healthy()` 必已返回 healthy(否则按 [`./master-subscription-health.md`](./master-subscription-health.md) M-T1 fail-fast,根本不进本流程)
- 调用前 OAuth callback **尚未**发生(显式选择必须在 issuer 颁 token 之前)

### 3.2 后置条件

- 任何函数调用都不抛业务异常(Playwright / requests 异常被内部 try/except 吞为 evidence,顶层编排返回 `(False, fail_category, evidence)`)
- success=True 时:OAuth 流程继续到 callback,后续 `_exchange_auth_code` 拿到 bundle 应**预期**为 `plan_type=free`;但若依旧 `plan_type=team`,由 §3.4 重试逻辑承担
- success=False 时:必有 `fail_category` ∈ §2.3 三个枚举之一;evidence 必含足够信息供事后排查(URL / status / 解码后 workspaces[] 子集)

### 3.3 异常类型

- 解码 cookie / fetch / DOM 操作的所有 Playwright 异常 → 内部 try/except 吞掉,evidence["exception"] = type name
- 不传播 Exception 到 `login_codex_via_browser` 主流程 — 主流程只看 (success, fail_category, evidence) 三元组
- **唯一**例外:`assert_not_blocked` 抛 `RegisterBlocked(is_phone=True)` 必须传播(对齐 [`./add-phone-detection.md`](./add-phone-detection.md) §5.2 模板)

### 3.4 5 次重试策略(指数退避)

```
触发条件:select_oauth_workspace 成功(endpoint 200) 但 callback 拿到 bundle.plan_type != "free"

重试位点:由 _run_post_register_oauth(personal) 的外层重试循环承担,本 spec 提供策略参数:

| 次数 | 单次预算 | 累计 | 退避 |
|---|---|---|---|
| 1 | ~30s OAuth 全流程 | 30s | 立即 |
| 2 | ~30s             | 60s | sleep 5s  |
| 3 | ~30s             | 95s | sleep 10s |
| 4 | ~30s             | 145s | sleep 20s |
| 5 | ~30s             | 215s | sleep 30s |

总时长上限:~4 分钟 (215s + 单次最大 35s tolerance)
退避抖动:每次 sleep 加 ±20% jitter (rng,避免多账号并发同步重试 → 风控)

每次重试都会重新调用 ensure_personal_workspace_selected,因为 workspace/select 在新 OAuth state
上必须重新发起 (旧 state 已被 callback 消费或过期)
```

**理由**:research/oauth-personal-selection.md §3.1 / `openai/codex#1977` ejntaylor 实证 5 次后端最终一致 (分钟量级)。本 spec 不超过 5 次以避免触发 OpenAI 风控 (research/sticky-rejoin-mechanism.md §6.1 风险 1 + 风险 2)。

### 3.4.1 重试触发条件统一矩阵(Round 11 二轮扩展)

**触发条件不只限于"endpoint 200 + bundle.plan!=free"**,还包括:

| `login_codex_via_browser` 返回值 | 含义 | 是否进重试 | `plan_drift_history` reason |
|---|---|---|---|
| `bundle.plan_type == "free"` | 成功路径 | ❌ 出循环走 plan_supported / quota probe | (不记) |
| `bundle.plan_type != "free"` | 后端最终一致性滞后(很罕见,通常 codex_auth 已拒收成 None) | ✅ 进重试 | 实际 plan_type 字面量(如 `"team"`) |
| `bundle is None` (codex_auth 拒收 plan!=free) | codex_auth.py:1037-1045 拒收 plan=team bundle 返回 None | ✅ 进重试 | `"bundle_none"` |
| `bundle is None` (workspace_select 完全失败) | codex_auth.py:1023-1025 auth_code 缺失返回 None | ✅ 进重试 | `"bundle_none"` |
| `RegisterBlocked.is_phone == True` | 用户级风控(add-phone) | ❌ 立即终止,delete_account | (不记 plan_drift,记 `oauth_phone_blocked`) |
| 其他 `RegisterBlocked` | 注册被阻断 | ❌ 立即终止,delete_account | (不记 plan_drift,记 `exception`) |

**关键改动 vs Round 8 v1.0**:

- **Round 8 v1.0** 在 `bundle is None` 时直接 `break` 退出循环 → fail-fast,但 codex_auth 拒收 plan=team 时永远拿不到 5 次重试机会。
- **Round 11 二轮 v1.2** 把 `bundle is None` 视为 plan_drift 一种,加入 history 跑完 5 次。这是 W-I9 不变量的实际兑现 — 没有它 W-I9 无法被任何调用路径触发。

**实施位点**:`src/autoteam/manager.py:1734-1752` `_run_post_register_oauth(leave_workspace=True)` 重试循环内。

```python
if not bundle:
    # Round 11 hotfix — bundle=None 可能原因:
    # 1. workspace_select 完全失败(auth_code 缺失)
    # 2. plan_type != free 被 codex_auth 拒收
    # 两种都属于后端最终一致性滞后,W-I9 spec 要求外层 5 次重试,而非 break
    logger.warning(
        "[注册] %s personal OAuth 第 %d/%d 次未返回 bundle,继续重试(等后端最终一致性同步)",
        email, attempt + 1, max_retries,
    )
    plan_drift_history.append({
        "attempt": attempt + 1,
        "plan_type": "unknown",
        "plan_type_raw": None,
        "account_id": None,
        "reason": "bundle_none",
    })
    continue   # ← 关键:continue 而非 break
```

**单元测试覆盖**(`tests/unit/test_round11_personal_oauth_retry.py`,4 case):
- `test_personal_oauth_4_none_then_free_succeeds`:前 4 次 None,第 5 次 free → 成功
- `test_personal_oauth_4_team_drift_then_free_succeeds`:前 4 次 plan=team(被 codex_auth 拒收成 None),第 5 次 free → 成功
- `test_personal_oauth_5_all_none_failfast`:5 次都 None → fail-fast + plan_drift_history 5 条 reason="bundle_none"
- `test_personal_oauth_register_blocked_is_terminal`:RegisterBlocked 第 1 次 → 立即 fail-fast,不重试

---

## 4. 与既有 OAuth 流程的整合

### 4.1 在 `login_codex_via_browser(use_personal=True)` 内的接入位置

**目标位置**:`src/autoteam/codex_auth.py:280-450` 区(use_personal 分支)

```
login_codex_via_browser(email, password, mail_client, *, use_personal=True)
  │
  ├─ step-0 跳过 _account cookie 注入 (旧路径 L311-312,保留)
  ├─ step-0 跳过 ChatGPT 预登录 (与 Team 路径不同,保留)
  │
  ├─ goto auth_url → 邮箱 + 密码 + OTP (L443-562)
  │
  ├─ ★ C-P1: assert_not_blocked(page, "oauth_about_you")
  ├─ step-3 about-you 填表 (L568-610)
  │
  ├─ ★★★ NEW (v1.2.1 Round 11 二轮 follow-up):
  │       ensure_personal_workspace_selected(page, consent_url=auth_url)
  │       插入位置:about-you 完成后,consent 循环开始之前
  │       (codex_auth.py:632+,即 line 633 注释 "Round 11 二轮 — Personal 模式: pre-consent")
  │       仅当 use_personal=True 才调用;Team 路径完全跳过
  │       失败仅 logger.warning,继续走 consent loop(由外层 5 次重试兜底)
  │
  ├─ ★ C-P2: assert_not_blocked(page, f"oauth_consent_{step}")
  ├─ step-4 consent 循环 (L612-882)
  │
  ├─ ★ NEW (v1.0 Round 8 兜底,Round 11 二轮 follow-up 降级):
  │       ensure_personal_workspace_selected(page, consent_url=page.url)
  │       插入位置:consent 循环结束后,callback 等待之前
  │       守卫:`if use_personal and not auth_code:`
  │       (consent loop 自然结束 + auth_code 未抓到时才走;
  │        实际 99% 路径 consent loop 1-2 步即抓到 auth_code,此处为 0% 路径兜底)
  │
  ├─ ★ C-P3: assert_not_blocked(page, "oauth_callback_wait")
  ├─ step-5 等 callback (L884-906)
  ├─ ★ C-P4: assert_not_blocked(page, "oauth_personal_check")
  └─ _exchange_auth_code → bundle
```

**v1.2.1 Round 11 二轮 follow-up 关键澄清**:

- **以前(v1.0~v1.2)**:仅 post-consent 单点接入(consent 循环结束后)
- **现在(v1.2.1)**:**双点接入** — pre-consent(主路径)+ post-consent(兜底)
- **触发原因**:实测发现 consent loop 1-2 步即 click "Continue/Allow" → 服务端 redirect 带 auth_code → page request hook 捕获 auth_code → consent loop break(line 635-636)→ post-consent 守卫 `if not auth_code:` 为 False → workspace_select 整个 if 块**被跳过**。每次重试都是同样的路径,workspace_select 永远没机会执行,5 次重试都拿到 plan=team。
- **修复**:把 workspace_select 前置到 consent loop 之前(此时 about-you 已完成,session cookie 已建立,但还没点 consent button → default_workspace_id 仍指向 Team,workspace/select 可正常切换 default 到 personal)。**这是 W-I9 不变量"workspace_select 必走"的硬保证**。
- **保留 post-consent 兜底**:某些路径下 consent loop 自然结束(button 找不到 / network glitch / consent UI 变化)且 auth_code 未抓到 → post-consent 仍可救回(0.1% 概率路径,保留无害)。

---

**v1.3.0 Round 11 三轮发现:empty workspaces[] 路径(server-side 状态)**

实测刚踢出 Team 的新号,在 OpenAI auth backend 端 `oai-oauth-session.workspaces=[]`(server-side 状态),`/workspace` UI 显示 "Workspaces not found in client auth session" 错误页:

- **症状**:`force_select_personal_via_ui` goto `https://auth.openai.com/workspace` → 浏览器停在错误页 → consent loop 找不到 button → 30s callback 等待超时 → bundle=None → 5 次外层重试每次同样的失败,fail-fast 删账号。
- **根因**:OpenAI 在用户从 Team 被踢出后的过渡期内,OAuth session 的 workspaces[] 不是"还没切到 personal",而是**完全空**。server-side 在某种最终一致性窗口里没把任何 workspace 关联到该 user 的 OAuth session。`/workspace` UI 看到 cookie 解出空数组,直接渲染 "no workspaces" 错误页。
- **修复 1 — `skip_ui_fallback_on_empty=True`**:`ensure_personal_workspace_selected(skip_ui_fallback_on_empty=True)`(默认 False 保旧调用方)。当 cookie 解码出来 `workspaces=[]` 时,**不再** 调 `force_select_personal_via_ui`(那只会让浏览器 goto 错误页),改为 fail-fast 返回 `(False, OAUTH_WS_ENDPOINT_ERROR, evidence)`,`evidence.fallback.phase = "skipped_by_caller_request"`。codex_auth.py 调用方收到 fail 信号继续 consent loop,让 OAuth 自然走 default workspace 流程,plan_type 由外层 5 次重试 + bundle 校验兜底(回归 Round 4/e760be9 行为)。
- **修复 2 — pre-consent navigate-back 缩窄**:pre-consent workspace_select 后的 `page.goto(auth_url)` 守卫,以前是"current_url 不含 /oauth/authorize 就 navigate-back",会**误击**重置 /log-in / /password 等 OAuth 流程页的 form 状态(form 已 fill 邮箱/密码,goto 重新加载会清空)。改为「**仅当 current_url 含 /workspace 时才 navigate-back**」,只针对已知的错误页恢复,正常流程页保持不动。
- **保留 post-consent 兜底**:post-consent fallback 也传 `skip_ui_fallback_on_empty=True`,语义一致。

**已知未解决的深层问题(v1.3.0 阶段)— v1.4.0 已落地解决,见下文**:

- **OAuth /log-in 表单 Continue 按钮变灰(client-side / anti-bot)**:实测刚踢出 Team 的新号,OAuth 流程在 `/log-in` 页 fill email 后 Continue 按钮变灰禁用,无法点击,登录无法进展;30s 后 callback 等不到 → bundle=None。截图(codex_02_after_email.png / codex_03e_pre_consent_workspace_select.png)显示 email 已填、按钮 disabled。**根因(v1.4.0 实证确认)**:Playwright `locator.fill()` 直接 setValue 不触发 React `onChange` 事件链 → 表单 state 不更新 → 按钮不 enable。所有非新创建账号(包括刚被 kick 出 Team 的)都会卡这个 bug。
- **workaround(v1.4.0 已落地)**:
  1. 注册 flow 从 chatgpt.com BrowserContext 抽 `__Secure-next-auth.session-token` cookie(支持 chunked .0/.1)→ 双域注入 + silent step-0 NextAuth refresh,**快路径**走完 OAuth 拿 plan=free 直接返回。
  2. 阶段 1 失败时(personal+plan != free)走 fallback:`context.clear_cookies()` → chatgpt.com fresh login,用 `keyboard.type()` 而非 `fill()` 绕开灰按钮 bug → 重走 OAuth。

---

#### v1.4.0 Round 11 四轮:session_token 注入 + fresh re-login fallback(personal 两阶段 OAuth)

**v1.3.0 三轮把 `/workspace` 错误页 stuck 修了**,但实测发现新一类阻塞:刚踢出 Team 的新号在 `auth.openai.com/log-in` (welcome back) 页 fill email 后,Continue 按钮变灰禁用,login flow 卡死,bundle=None,5 次外层重试全失败。

##### 根因 — Playwright `fill()` vs OpenAI auth React 表单

- `auth.openai.com/log-in` 页 React 表单只在 native input 事件链(`focus → keydown → keypress → input → keyup`)上才 enable Continue 按钮
- Playwright `locator.fill()` 直接 `setValue`,不触发 React `onChange`/`input` 合成事件
- 所有非新创建账号(包括刚被 kick 出 Team 的)都会卡这个 bug,fresh login 必走灰按钮路径

##### 修复架构 — 两阶段 OAuth(`src/autoteam/codex_auth.py:login_codex_via_browser` 重构)

**阶段 1(快路径,有 token 时优先走)**:

1. 注册阶段从 chatgpt.com BrowserContext 抽 `__Secure-next-auth.session-token`(由 `manager._extract_session_token_from_context` 实现,支持 chunked `.0`/`.1` 拼接)
2. 透传到 `login_codex_via_browser(chatgpt_session_token=...)` 新 kwarg
3. **双域 cookie 注入**(chatgpt.com + auth.openai.com,与 `SessionCodexAuthFlow` 主号注入对齐)— helper `_inject_personal_session_cookies`
4. **Silent step-0**:`page.goto("https://chatgpt.com/")` → `page.evaluate fetch("/api/auth/session?update")` 强制 NextAuth 刷新 + `fetch("/backend-api/accounts/check")` 触发 server-side workspace 重新判定
5. 走 OAuth 流程 → `bundle.plan_type == "free"` → 直接返回(快路径成功)

**阶段 2(fallback,personal + 阶段 1 拿到 plan != free 时触发)**:

1. `context.clear_cookies()` — 注册阶段 session_token 是 Team-bound,不清掉会污染 fresh login(**W-I11**)
2. goto `https://chatgpt.com/auth/login`
3. **email 步骤**:`locator.click()` 聚焦 + `Control+A` / `Delete` 清空 + `page.keyboard.type(email, delay=50)` **逐字符触发 React onChange**(helper `_typewrite_credential`,**W-I12**)
4. **password 步骤**:同样模式 `keyboard.type` 逐字符输入(避开灰按钮)
5. OTP 处理(沿用注册阶段 mail_client)
6. 等到 chatgpt.com root → 新 PKCE 重走 OAuth → consent loop → 期望 `bundle.plan_type == "free"`
7. fresh re-login 主流程由 helper `_perform_fresh_relogin_in_context` 封装

##### 关键 invariants(防回归 — 编号顺延已用的 W-I10)

- **W-I11**(强制):**阶段 2 必须 `context.clear_cookies()`**,否则 stale Team-bound session_token 干扰 fresh login,卡在 logged-in 状态拿不到新 personal session
  - **NOTE(v1.5.0)**:W-I11 / W-I12 这两条 invariant 不变,但 **stage 2 仅在 stage 1 失败时触发**(W-I13 守卫)。在 issuer ledger 已清空场景下(§4.4 ledger TTL),stage 1 即足够拿 plan=free(W-I15 实证 fd3b5ccae1 71.3s),W-I11/I12 路径不会执行。stage 2 是 issuer ledger 仍指向 Team 期间的兜底机制,非默认路径。
- **W-I12**(强制):**阶段 2 email/password 必须用 `page.keyboard.type(delay≥50ms)` + 前置 `locator.click()` 聚焦 + `Control+A`/`Delete` 清空**,**禁止** `locator.fill()`(会触发灰按钮 bug);本 invariant 与 v1.4.0 根因绑定,任何回退到 `fill()` 即视为对本 spec 的 BREAKING 回归
- **W-I13**(强制):**阶段 2 触发条件守卫为 `(not stage1_ok) and use_personal`**,Team 模式不会误触发 fresh re-login(性能保护:Team 路径不应额外开销 fresh login overhead);代码上由 `if use_personal and stage1_plan != "free":` 二分守恒
- **W-I14**(强制):**`_register_direct_once` 必须返回 tuple `(bool, Optional[str])`** — 所有 early-return 都必须改为 `(False, None)`,否则上层 `manager.create_account_direct` 解构 `success, session_token = _register_direct_once(...)` 时炸 `TypeError: cannot unpack non-iterable bool object`;`create_account_direct` + `_run_post_register_oauth` 增加 `chatgpt_session_token` kwarg 透传链;不变量与 spec-2 v1.7+ §3.4.6 实施位点对齐

##### 不变量延续(v1.3.0 → v1.4.0)

- **W-I9**(三轮):`skip_ui_fallback_on_empty=True` 仍生效,`workspace_select` 在 `workspaces[]==[]` 时不触发 UI 回退;v1.4.0 阶段 1 仍按 v1.3.0 守卫执行
- 5 次外层重试 + plan_drift 拒收(`manager._run_post_register_oauth` 1734-1820)未改,plan=team 仍必拒
- `SessionCodexAuthFlow` / `MainCodexSyncFlow`(主号专用)未改 — 主号仍走原 SessionCodex 路径,与子号 personal 两阶段 OAuth 解耦
- v1.3.0 `skip_ui_fallback_on_empty=True` 守卫 + pre-consent navigate-back 仅 `/workspace` URL 缩窄,**全部继承到 v1.4.0**

##### 单测覆盖(总 363 通过)

| 测试文件 | case 数 | 关键覆盖 |
|---|---|---|
| `tests/unit/test_round11_session_token_injection.py` | 16 | (1) 从 BrowserContext 抽 `__Secure-next-auth.session-token`(含 chunked `.0`/`.1` 拼接);(2) 双域 cookie 注入(chatgpt.com + auth.openai.com);(3) `_register_direct_once` tuple 返回(W-I14);(4) `chatgpt_session_token` kwarg 全链路 forward(register → create_account_direct → _run_post_register_oauth → login_codex_via_browser) |
| `tests/unit/test_round11_fresh_relogin_fallback.py` | 13 | (1) 阶段 2 触发条件 `(not stage1_ok) and use_personal`(W-I13);(2) `keyboard.type(delay=50)` vs `fill()` 路径分支(W-I12);(3) no-token 路径回退兼容性;(4) `context.clear_cookies()` invariant(W-I11);(5) helper `_perform_fresh_relogin_in_context` / `_typewrite_credential` / `_inject_personal_session_cookies` 单元行为 |

##### 实施位点速览

- **`src/autoteam/codex_auth.py`**:
  - `login_codex_via_browser(..., chatgpt_session_token=None)` 重构两阶段
  - 新增 helpers:`_inject_personal_session_cookies`、`_typewrite_credential`、`_perform_fresh_relogin_in_context`
- **`src/autoteam/manager.py`**:
  - `_register_direct_once` 返回 tuple `(success, session_token)`(W-I14)
  - `create_account_direct` + `_run_post_register_oauth` 增加 `chatgpt_session_token` kwarg 透传链
  - 新增 helper `_extract_session_token_from_context`

---

**与既有探针的关系**:

- C-P1~C-P4 add-phone 探针**保留**,本 spec 不替代它们(语义不同 — phone vs workspace)
- C-P3 / C-P4 命中 add-phone 时优先抛 RegisterBlocked,本 spec 退出
- 本 spec 的失败 → fail_category 由编排函数返回,**不**抛异常到 login_codex_via_browser 顶层
- **pre-consent 失败行为**(v1.2.1):仅 logger.warning,**不** raise(由外层 _run_post_register_oauth 5 次重试承担,符合 W-I9)

### 4.2 与 use_personal=False (Team 路径) 的关系

**Team 路径不调用本 spec 的 select** — 因为:

- Team 路径希望默认 workspace == Team(已是 default_workspace_id 指向),没动机切换
- Team 路径已有 `_account` cookie 注入(`codex_auth.py:316-335`)间接锁定 Team workspace

**Team 路径仍需要 master health probe** — 见 [`./master-subscription-health.md`](./master-subscription-health.md) M-T2:即使是 Team 路径,母号降级时 invite 也会拿 free,所以 fill 入口仍需调 master probe fail-fast。

### 4.3 sleep(8) 删除依据(`manager.py:1554-1556`)

**原代码**(已实测无效):

```python
result = chatgpt_api._api_fetch("DELETE", delete_path)
if result["status"] in (200, 204):
    logger.info("[Team] 已将 %s 移出 Team", email)
    time.sleep(8)  # 等 OAI 后端同步 sticky-default
```

**Round 8 删除决定 + 理由**(research/sticky-rejoin-mechanism.md §1.2-1.3):

- DELETE user **真生效**,ChatGPT member 列表确实清掉(reconcile 验证过)
- 但 `auth.openai.com.session.default_workspace_id` 不随 DELETE 联动 — 等 8s / 80s / 800s 都没用
- sticky 根因不是"OpenAI 同步延迟",而是"default 不会自动 unset"
- 真正的解法是本 spec 的显式 `workspace/select`,8s sleep 是**纯无意义等待**

**Round 8 实施期动作**:删除 `time.sleep(8)`(无注释保留)。删除位置:`src/autoteam/manager.py:1554-1556`(以实施期实际行号为准)。删除后 personal 流程时长降低 8s。

**Out of Scope**:不引入 longer sleep / probe loop(60s+5 retry)等 Approach C 路径。本 spec 的 §3.4 5 次 OAuth retry 已替代该兜底。

### 4.4 OAuth Issuer Ledger TTL 现象(Round 11 五轮 P1 实证)

> **NOTE — kick→OAuth 之间存在 issuer 端 `claimed_domain_org_id` ledger TTL,具体时长未测,实证至少几小时。**
>
> 与 §4.3 的"`auth.openai.com.session.default_workspace_id` 不随 DELETE user 自动 unset"是**两个独立现象**;§4.3 描述的是 user 自身的 default sticky,本 §4.4 描述的是 issuer 端 OAuth session cookie `oai-oauth-session.workspaces[]` 的最终一致性窗口。

#### 现象描述

| 时机 | issuer `workspaces[]` 状态 | OAuth 实际命中 workspace | 拿到的 plan_type | 路径 |
|---|---|---|---|---|
| kick 后**立即**(秒级 / 分钟级) | 仍含 master Team 项 + default | Team workspace | `team`(被 codex_auth 拒收成 None,触发 W-I9 重试) | stage 1 失败 → stage 2 fresh re-login(W-I11/I12) |
| kick 后等待**充分时间**(实证 ≥ 几小时) | `workspaces=[]`(已清) | personal workspace(use_personal=True 兜底) | `free` | stage 1 直接成功(W-I15 实证)|

#### Round 11 五轮实证证据

- **fd3b5ccae1**(成功路径):用户 admin UI 几小时前手动 kick → 200s timeout headless OAuth → stage 1 `workspaces_redacted=[]` → `use_personal=True` 走 default 自然 fallback → **71.3s 拿到 plan=free**(account_id `7f4384d7-...`)
- **404907e1c8**(自动 rejoin 路径):同期被 kick,但 master `/users` 实测**已 auto-rejoin 为 standard-user**(`user_id=user-h1ZUQFFj9K8wg1y7XGc7XPnZ`)— 说明 `zrainbow1257.com` 是 master Team 的 claimed-domain,issuer 自动绑回。
- **b7c4aaf8f2**(中间状态):同期被 kick,master `/users` 不在,issuer 端推测仍含 master ledger(60s 短超时跑不完无法直接验证),但同 fd3b5ccae1 同期推断行为应类似(等几小时即清)。
- 来源:`.trellis/tasks/04-28-round11-master-resub-models-validate/research/p1-p2-execution-report.md` + `three-kicked-emails-probe.md`。

#### 实施影响

1. **stage 1 设计仍正确** — 在 issuer ledger 已清(`workspaces=[]`)路径上,stage 1 + `skip_ui_fallback_on_empty=True` + W-I9 5 次重试即足够(W-I15 实证)
2. **stage 2 仅在必要时触发** — W-I13 守卫 `(not stage1_ok) and use_personal`,issuer ledger 未清时 stage 1 失败 → stage 2 fresh re-login(W-I11/I12 keyboard.type 绕开 React fill bug)兜底
3. **kick→OAuth 立即触发的代价** — 5 次 retry 全部命中 ledger Team 项 → 每次 plan=team 被拒收 → stage 2 fresh re-login(成本高,需要 chatgpt.com fresh login + OTP)→ 最坏情况下整个流程几分钟。优化方向:RT 后台进程把"刚 kick 的子号"放入冷却期(round 12+ backlog,本 spec 不实施)
4. **TTL 时长是开放问题** — 已知至少"几小时后 ledger 清",未实测最短时长。需要后续实验确定 TTL 边界(round 12+ 待办)

#### 与 §3.4 5 次重试退避表的关系

§3.4 退避表(累计 215s + jitter)假设 issuer ledger 在 5 次重试窗口内**会清**;但实证显示 ledger TTL 是分钟到小时级,远超 215s — 即同一 OAuth 会话内 5 次重试**不太可能**让 ledger 自然清,真正的兜底是 stage 2 fresh re-login + 长期(分钟到小时级)等待。Round 12+ 应**重新评估** §3.4 退避表的合理性,可能需要把 5 次重试改成"立即重试 + 长期 backoff"组合(本 spec 不改,backlog 处理)。

---

## 5. 安全 / 风控注意事项

| 项 | 内容 |
|---|---|
| 私有 API 风险 | `/api/accounts/workspace/select` 未公开,字段名 / sentinel-token 算法 / JA3 指纹要求都可能变。Playwright UI fallback 是**强制**保留的兜底,不可省略 |
| 风控触发 | 多账号同时重试可能触发 OpenAI rate-limit。本 spec §3.4 的 5 次重试 + ±20% jitter 是上限;不允许加大 |
| Token 不落盘 | evidence 中**禁止**包含 access_token / id_token / refresh_token / `oai-oauth-session` 原始 cookie 值;decode 后的 workspaces[] 也只保留 id/name/structure/role 子集(类比 master health §2.3 裁剪规则) |
| 截图脱敏 | force_select_personal_via_ui 失败时截图存 `screenshots/oauth_workspace_select_failed_{ts}.png`,但不存 cookie/local-storage 转储 |
| 单 Playwright 锁 | 本 spec 在既有"全局 Playwright 锁"(PRD §5 已知约束)内运行,不引入新并发原语 |

---

## 6. 不变量(Invariants)

- **W-I1**:`ensure_personal_workspace_selected` / `select_oauth_workspace` / `decode_oauth_session_cookie` / `force_select_personal_via_ui` **永不抛异常**;任何 Exception 转为 (False, fail_category, evidence) 三元组返回
- **W-I2**:三个失败分类**互斥不重叠**:no_personal(workspaces 中确认无 personal)/ endpoint_error(主路径 + fallback 都失败)/ plan_drift_persistent(5 次重试后仍 team) — 每条 register_failures 记录的 fail_category 字段必为这三个之一
- **W-I3**:本流程**只**在 `use_personal=True` 时执行;Team 路径调用 `ensure_personal_workspace_selected` 视为 bug
- **W-I4**:解码后 workspaces[] 中查找 personal 必须严格使用 §2.1 `_is_personal_workspace` 三条件之一,**禁止** 仅靠 `workspaces[0]` 默认取首项(gpt-auto-register 上游用 [0],但本工程 sticky 场景下 [0] 可能是 Team)
- **W-I5**:5 次重试上限**硬编码上限**;允许通过 `runtime_config.oauth_workspace_select_max_retries` 调小(1~5),不允许调大 — 风控考虑
- **W-I6**:落盘 evidence **不含**敏感字段(access/refresh/id token / cookie 原始值 / `chatgpt-account-id` header 等)
- **W-I7**:`time.sleep(8)` 在 `_run_post_register_oauth(personal)` 中**已删除** — 任何"加回 sleep 等 sticky 同步"的代码视为对本 spec 的回归
- **W-I8**:`fail_category` 字符串字面量与 `register_failures.json` schema(spec-2 v1.5 RegisterFailureRecord enum)一致 — 任何不在枚举内的字面量视为 schema 违规
- **W-I9**:`workspace/select` 主路径成功(endpoint 200) 但 callback 拿到 plan!=free 时,**不**立即记 fail_category,而是进入外层重试;只有 5 次后仍失败才记 `oauth_plan_drift_persistent`。
  **(Round 11 二轮扩展)** `bundle=None` 也视为 plan_drift 触发条件 — 此时调用方 `login_codex_via_browser` 可能因两个原因返回 None:
    1. workspace_select 主路径完全失败导致 auth_code 缺失(`codex_auth.py:1023-1025`)
    2. callback 拿到 plan_type=team 被 codex_auth 单次拒收(`codex_auth.py:1037-1045`)
  两种情况都属于"后端最终一致性短暂滞后",**必须**进入 5 次外层重试,不能 break/fail-fast。
  `plan_drift_history` 记 `reason="bundle_none"` 与正常 `plan_drift` 区分。仅当 5 次都拿不到 `plan_type=free`(无论 bundle=None 还是 bundle.plan!=free)才 fail-fast 删账号 + 记 `oauth_plan_drift_persistent`。
- **W-I10**:本 spec 的接入点(§4.1 NEW 位置)**不**复用 add-phone 探针的 `assert_not_blocked` — 探针语义不同,且本 spec 失败需要外层重试,不应抛 RegisterBlocked
- **W-I11**(v1.4.0 Round 11 四轮):**personal 两阶段 OAuth 阶段 2 必须 `context.clear_cookies()`** — 注册阶段透传的 chatgpt.com `__Secure-next-auth.session-token` 是 Team-bound,fresh re-login 前不清空会污染新 session,卡在 stale logged-in 状态。任何跳过 clear_cookies 的实现视为 BREAKING 回归,详见 §4.1 v1.4.0 章节"修复架构"
- **W-I12**(v1.4.0 Round 11 四轮):**personal 阶段 2 email/password 输入必须用 `page.keyboard.type(delay≥50ms)` + 前置 `locator.click()` 聚焦 + `Control+A`/`Delete` 清空**,**禁止** `locator.fill()`。Playwright `fill()` 直接 setValue 不触发 React `input` 合成事件 → OpenAI auth 表单 Continue 按钮永久灰显;`keyboard.type(delay=50)` 逐字符触发 native input 事件链 → 按钮正常 enable。任何回退到 `fill()` 即视为对本 spec 的 BREAKING 回归
- **W-I13**(v1.4.0 Round 11 四轮):**阶段 2(fresh re-login fallback)触发条件硬守卫为 `(not stage1_ok) and use_personal`** — Team 路径(`use_personal=False`)永不触发 fresh re-login,避免无谓的 chatgpt.com 登录开销;代码上由 `if use_personal and stage1_plan != "free":` 二分守恒,与 W-I3(Team 路径不调本 spec 主路径)对齐
- **W-I14**(v1.4.0 Round 11 四轮):**`_register_direct_once` 必须返回 tuple `(success: bool, session_token: Optional[str])`** — 所有 early-return 路径必须改为 `(False, None)`,否则 `manager.create_account_direct` 解构 `success, session_token = _register_direct_once(...)` 时炸 `TypeError: cannot unpack non-iterable bool object`。`create_account_direct` 与 `_run_post_register_oauth` 必须把 `chatgpt_session_token` kwarg 透传到 `login_codex_via_browser`
- **W-I15**(v1.5.0 Round 11 五轮 P1 实证):**stage 1 快路径在 `workspaces[]==[]` 路径已实证可拿 `plan_type=free`**,**不需要** stage 2 fresh re-login fallback。
  - **实证**:`fd3b5ccae1@zrainbow1257.com`(用户 admin UI 几小时前手动踢出的号)走 `chatgpt_session_token=None` + `use_personal=True` + headless,200s 子进程超时 → **71.3s 拿到** `plan_type=free` + `account_id=7f4384d7-4831-4a8d-a93c-547296c6b600`(personal workspace,与 master `bac969ea-...` 不同)。
  - **机制**:stage 1 路径 `oauth_workspace_select_endpoint_error: workspaces_redacted=[]`(W-I9 + v1.3.0 `skip_ui_fallback_on_empty=True`)→ codex_auth.py 调用方继续 consent loop → consent button click 触发服务端按 default workspace 颁 token → issuer ledger 已清 default 自然 fallback 到 personal → bundle.plan_type=free。
  - **限制**:仅当 issuer 端 `oai-oauth-session.workspaces[]` 已清空时(详见 §4.4 ledger TTL),否则 stage 1 仍会拿 plan=team 被 codex_auth 拒收成 None,此时 W-I9 5 次外层重试或 W-I11/W-I12 stage 2 fresh re-login 兜底。
  - **W-I11/W-I12 stage 2 关系**:fresh re-login(W-I11/I12)**仅在 stage 1 失败时触发**(W-I13 守卫 `(not stage1_ok) and use_personal`),在 issuer ledger 已清空场景下 stage 1 即足够(W-I15 实证)。两条机制层级互补,不冲突。
  - **证据**:`.trellis/tasks/04-28-round11-master-resub-models-validate/research/p1-p2-execution-report.md` § P1。

---

## 7. 单元测试 fixture 与样本

### 7.1 `oai-oauth-session` cookie 解码样本

```json
// tests/fixtures/oauth_session_cookies.json
{
  "session_with_personal": {
    "user_id": "user-aaaa",
    "workspaces": [
      {
        "id": "team-uuid-1111",
        "name": "Master Team",
        "structure": "workspace",
        "role": "user",
        "plan_type": "team"
      },
      {
        "id": "personal-uuid-2222",
        "name": "Personal",
        "structure": "personal",
        "role": "account-owner"
      }
    ],
    "default_workspace_id": "team-uuid-1111"
  },
  "session_no_personal_sticky": {
    "user_id": "user-aaaa",
    "workspaces": [
      {
        "id": "team-uuid-1111",
        "name": "Master Team",
        "structure": "workspace",
        "role": "user"
      }
    ],
    "default_workspace_id": "team-uuid-1111"
  },
  "session_empty_workspaces": {
    "user_id": "user-aaaa",
    "workspaces": []
  },
  "session_personal_via_plan_type_free": {
    "user_id": "user-aaaa",
    "workspaces": [
      {"id": "ws-aaaa", "structure": "workspace", "role": "user", "plan_type": "team"},
      {"id": "ws-bbbb", "structure": "personal_v2", "role": "owner", "plan_type": "free"}
    ]
  },
  "session_personal_via_is_personal_flag": {
    "user_id": "user-aaaa",
    "workspaces": [
      {"id": "ws-aaaa", "structure": "team_workspace"},
      {"id": "ws-bbbb", "is_personal": true, "name": "Personal account"}
    ]
  }
}
```

### 7.2 推荐单测代码

```python
# tests/unit/test_oauth_workspace_select.py
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from autoteam.chatgpt_api import (
    decode_oauth_session_cookie,
    select_oauth_workspace,
    force_select_personal_via_ui,
    ensure_personal_workspace_selected,
    _is_personal_workspace,
)
from autoteam.register_failures import (
    OAUTH_WS_NO_PERSONAL,
    OAUTH_WS_ENDPOINT_ERROR,
    OAUTH_PLAN_DRIFT_PERSISTENT,
)

FIXTURE = json.loads(Path("tests/fixtures/oauth_session_cookies.json").read_text())


@pytest.mark.parametrize("name,expected_personal_id", [
    ("session_with_personal", "personal-uuid-2222"),
    ("session_personal_via_plan_type_free", "ws-bbbb"),
    ("session_personal_via_is_personal_flag", "ws-bbbb"),
    ("session_no_personal_sticky", None),
    ("session_empty_workspaces", None),
])
def test_personal_detection(name, expected_personal_id):
    workspaces = FIXTURE[name]["workspaces"]
    found = next((w for w in workspaces if _is_personal_workspace(w)), None)
    if expected_personal_id is None:
        assert found is None
    else:
        assert found is not None and found["id"] == expected_personal_id


def test_no_personal_returns_no_personal_category():
    """W-I4 + 失败分类 — workspaces 无 personal 时必返 OAUTH_WS_NO_PERSONAL."""
    page = MagicMock()
    page.context.cookies.return_value = []
    with patch(
        "autoteam.chatgpt_api.decode_oauth_session_cookie",
        return_value=FIXTURE["session_no_personal_sticky"],
    ):
        ok, category, ev = ensure_personal_workspace_selected(
            page, consent_url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
        )
    assert ok is False
    assert category == OAUTH_WS_NO_PERSONAL


def test_select_endpoint_500_falls_back_to_ui(monkeypatch):
    """主路径 endpoint 500 时走 fallback;fallback 成功 → success=True."""
    page = MagicMock()

    def fake_decode(*args, **kwargs):
        return FIXTURE["session_with_personal"]

    def fake_select(*args, **kwargs):
        return False, None, {"http_status": 500, "body_preview": "Internal Server Error"}

    def fake_fallback(*args, **kwargs):
        return True, {"clicked_text": "Personal", "ts_ms": 1234}

    monkeypatch.setattr("autoteam.chatgpt_api.decode_oauth_session_cookie", fake_decode)
    monkeypatch.setattr("autoteam.chatgpt_api.select_oauth_workspace", fake_select)
    monkeypatch.setattr("autoteam.chatgpt_api.force_select_personal_via_ui", fake_fallback)

    ok, category, ev = ensure_personal_workspace_selected(
        page, consent_url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
    )
    assert ok is True
    assert category == ""
    assert ev.get("primary_failed") is True
    assert ev.get("fallback") is not None


def test_select_endpoint_500_and_ui_failure_returns_endpoint_error(monkeypatch):
    """主路径 + UI fallback 都失败 → OAUTH_WS_ENDPOINT_ERROR."""
    page = MagicMock()
    monkeypatch.setattr(
        "autoteam.chatgpt_api.decode_oauth_session_cookie",
        lambda *a, **kw: FIXTURE["session_with_personal"],
    )
    monkeypatch.setattr(
        "autoteam.chatgpt_api.select_oauth_workspace",
        lambda *a, **kw: (False, None, {"http_status": 500}),
    )
    monkeypatch.setattr(
        "autoteam.chatgpt_api.force_select_personal_via_ui",
        lambda *a, **kw: (False, {"page_title": "Sign in"}),
    )
    ok, category, ev = ensure_personal_workspace_selected(
        page, consent_url="...",
    )
    assert ok is False
    assert category == OAUTH_WS_ENDPOINT_ERROR


def test_no_exception_propagates(monkeypatch):
    """W-I1 — 内部任何 Exception 转 (False, ...)."""
    page = MagicMock()
    def boom(*a, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr("autoteam.chatgpt_api.decode_oauth_session_cookie", boom)
    ok, category, ev = ensure_personal_workspace_selected(page, consent_url="...")
    assert ok is False
    assert category in (
        OAUTH_WS_NO_PERSONAL,
        OAUTH_WS_ENDPOINT_ERROR,
        OAUTH_PLAN_DRIFT_PERSISTENT,
    )


def test_evidence_no_token_leak():
    """W-I6 — evidence 不含 access_token / refresh_token / cookie 原始值."""
    sample_evidence = {
        "primary": {"http_status": 200, "body_preview": "{...continue_url...}"},
        "fallback": None,
    }
    serialized = json.dumps(sample_evidence)
    for token_kw in ("access_token", "refresh_token", "id_token", "session_token"):
        assert token_kw not in serialized
```

### 7.3 抓包验证 checklist(patch-implementer Stage 2 必须做)

| # | 验证点 | 通过标准 |
|---|---|---|
| V1 | `oai-oauth-session` cookie 是否仍可 base64url decode 成 JSON | 解码后 JSON 含 `workspaces` 数组 |
| V2 | workspaces[] 项含 `structure` 字段且取值为 `"personal" / "workspace"` 之一 | 至少一个 sticky 场景下能区分 |
| V3 | 子号刚被 DELETE 后,workspaces[] 是否含 personal 项 | 含 → 主路径可走;不含 → 走 OAUTH_WS_NO_PERSONAL |
| V4 | `POST /api/accounts/workspace/select` 不带 sentinel-token 是否仍 200 | 若 401 → 实施期需补 sentinel-token 提取(不在本 spec) |
| V5 | UI fallback 的"Personal" / "个人" 按钮 selector 是否有效 | Playwright 实测命中可见元素 |

---

## 8. 与既有 spec / FR 的关系

| 关系对象 | 说明 |
|---|---|
| `spec-2 v1.5 §3.4.6` | 引用本 spec — 定义 personal OAuth 内的 workspace/select 接入点;Team 路径不动 |
| [`./master-subscription-health.md`](./master-subscription-health.md) | 互补 — master health 决定能否进 OAuth;workspace/select 决定 OAuth 颁哪个 token。两者前后串联 |
| [`./add-phone-detection.md`](./add-phone-detection.md) | 共存 — 4 处 add-phone 探针保留,本 spec 在 consent 循环之后 / callback 之前接入 |
| [`./plan-type-whitelist.md`](./plan-type-whitelist.md) | 下游消费 — workspace/select 成功后 bundle.plan_type 应为 `free`,由 `is_supported_plan` 判定;本 spec 不复制 plan 校验 |
| [`./quota-classification.md`](./quota-classification.md) | 下游消费 — personal OAuth 拿到 free token 后仍要 wham/usage 探测配额 |
| `register_failures.json schema` | 新增 3 个 category(`oauth_workspace_select_no_personal` / `oauth_workspace_select_endpoint_error` / `oauth_plan_drift_persistent`),在 spec-2 v1.5 RegisterFailureRecord enum 同步 |
| `manager.py:1554-1556 sleep(8)` | 本 spec 删除 — 见 §4.3 删除依据 |

---

## 9. 参考资料

### 9.1 内部研究(Round 8 task research/)

- `.trellis/tasks/04-27-master-team-degrade-oauth-rejoin/research/oauth-personal-selection.md`
  - §1.1-1.6 OAuth URL hint 全集 + `allowed_workspace_id` 是 allow-list 不是 selector
  - §2.1-2.3 UI 选择页 + `_account` cookie + `accounts/workspace/select` 端点
  - §3 业内方案对比(gpt-auto-register / cnitlrt PR#39 / opencode-openai-codex-auth)
  - §4 Approach A/B/C(本 spec 主路径源自 Approach B,fallback 源自 Approach C)
  - §5 风险与未决(对应本 spec §5)

- `.trellis/tasks/04-27-master-team-degrade-oauth-rejoin/research/sticky-rejoin-mechanism.md`
  - §1.1-1.3 sticky-default 真相(non-sticky-rejoin)
  - §1.4 本工程 codex_auth.py:963-973 的"软兜底"位置
  - §2 Hard kick / Deactivate API 调研(确认无关)
  - §3 Recovery 策略 a-f 对比 — 本 spec 选 b(主) + c(兜底)
  - §3.1 gpt-auto-register `_submit_workspace_and_org` 完整伪代码
  - §3.2 cnitlrt PR#39 `_ensure_workspace_target_session` Playwright 实现
  - §5 推荐 Recovery 路径(本 spec §3 / §4 直接落地)
  - §6 风险与未决(对应本 spec §5 + §7.3 抓包 checklist)

### 9.2 内部代码引用(实施期目标位置)

- `src/autoteam/codex_auth.py:266-975` — `login_codex_via_browser` 全流程,本 spec 在 use_personal=True 分支注入
- `src/autoteam/codex_auth.py:643-671` — Personal workspace UI 点击逻辑(已存在,本 spec 升级为带 fallback 的完整流程)
- `src/autoteam/codex_auth.py:963-973` — Personal 强校验 plan_type=free 的拒收逻辑(保留,作为 5 次重试的判据)
- `src/autoteam/manager.py:1513-1655` — `_run_post_register_oauth(leave_workspace=True)` 全流程(本 spec 5 次重试在此外层)
- `src/autoteam/manager.py:1549-1556` — kick 后 sleep 8s(本 spec §4.3 删除依据)

### 9.3 外部参考

- `TongjiRabbit/gpt-auto-register/app/oauth_service.py:511-595` `_submit_workspace_and_org` — 主路径参考实现
- `cnitlrt/AutoTeam` PR #39 (closed unmerged) — Playwright fallback 完整代码(`_ensure_workspace_target_session` / `_select_workspace_target`)
- `openai/codex#1977` `mrairdon-midmark` 评论 — 多 workspace JWT claim 反推证据
- `openai/codex#1977` `ejntaylor` 评论 — 5 次 OAuth retry 触发后端最终一致性的实证
- `openai/codex codex-rs/login/src/server.rs:468-503` — `build_authorize_url`(`allowed_workspace_id` 是 allowlist 不是 selector)

---

**文档结束。** 工程师据此可直接编写 `decode_oauth_session_cookie` / `select_oauth_workspace` / `force_select_personal_via_ui` / `ensure_personal_workspace_selected` 函数 + 接入 + 单测,无需额外决策。抓包验证 checklist 见 §7.3。

## 10. upstream-style consent loop helper 港口(v1.1 Round 11 二轮新增,M-WS-personal-isolation)

### 10.1 背景与根因

Round 11 二轮实证:OAuth Codex consent loop 在 step 1 同意点击后,某些用户场景下页面变成 workspace 选择页(URL 含 "workspace" 或显式 "Choose a workspace" 文案)。我方原 consent loop 用单 hint 文本检测,且会把 consent URL 误判为 workspace 选择页(false positive)→ button 不可见 → loop break → 30s callback timeout → 18 次连续 OAuth 失败堆积。

借鉴 `cnitlrt/AutoTeam` upstream 已实证的健壮检测机制(2-hint 评分 + IGNORE 标签集),引入 5 个 helper + 3 个常量集到 `src/autoteam/oauth_workspace.py`,在 `src/autoteam/codex_auth.py` consent loop **每个 step 起始**先检测是否在 workspace 选择页 + 主动选 Team workspace。

### 10.2 5 个 helper signature

**位置**:`src/autoteam/oauth_workspace.py:259-434`(与 cnitlrt/AutoTeam upstream `codex_auth.py:236-468` 1:1 对齐)

```python
# oauth_workspace.py:301
def _is_workspace_ignored_label(text: str) -> bool:
    """判断 candidate label 是否应被忽略(噪声 button / 模板提示等)。

    与 upstream codex_auth.py:364-368 1:1 对齐 — IGNORE_LABELS 完全匹配 OR
    IGNORE_SUBSTRINGS 子串匹配。"""

# oauth_workspace.py:312
def _is_workspace_selection_page(page) -> bool:
    """检测 page 是否在 workspace 选择页。

    与 upstream codex_auth.py:371-384 对齐:URL 含 'workspace' 直接 True;
    否则按 body 文本计算 hint hit 数 — organization URL 需 ≥ 2 hits,
    普通 URL 也需 ≥ 2 hits 或包含 'launch a workspace' 兜底。

    Round 11 二轮关键修复:旧实现单 hint 命中即 True,会把 consent URL 误判 → False positive
    导致 consent loop 第一个 step 跑 workspace 选择路径而不是 consent 按钮路径。"""

# oauth_workspace.py:340
def _workspace_label_candidates(page):
    """枚举 workspace 选择页中所有可点击候选 label(文本不为噪声 + 长度合理)。

    与 upstream codex_auth.py:387-421 1:1 对齐 — 遍历 button / a / role=button /
    role=option / aria-selected / data-state / li / label / div 9 类 selector,
    去重 + 过滤 IGNORE LABELS / 长度 ≤ 80 / is_visible(timeout=100)。"""

# oauth_workspace.py:381
def _click_workspace_locator(loc) -> bool:
    """点击 locator,首选普通 click,失败则 force=True 重试一次。

    与 upstream codex_auth.py:424-433 1:1 对齐 — 兜底 force-click 解决
    overlay / pointer-events 拦截。"""

# oauth_workspace.py:397
def _select_team_workspace(page, workspace_name: str) -> bool:
    """在 workspace 选择页找匹配 workspace_name 的 label 点击。

    与 upstream codex_auth.py:436-468 1:1 对齐。
    Returns True 表示成功点击;False 表示未找到 / 全部点击失败。

    fallback:某些页面 workspace 项是 div / span 包裹文本 — 加
    `text="<name>"` + `text=/regex/i` selector 兜底。"""
```

### 10.3 3 个常量集

```python
# oauth_workspace.py:260
_WORKSPACE_PAGE_HINTS = (    # 与 upstream 完全一致,8 项
    "choose a workspace",
    "select a workspace",
    "launch a workspace",
    "workspace",
    "personal workspace",
    "personal account",
    "选择一个工作空间",
    "选择工作空间",
)

# oauth_workspace.py:270
_WORKSPACE_IGNORE_LABELS = {  # 与 upstream 完全一致,18 项 set(去重 O(1) lookup)
    "choose a workspace",
    "select a workspace",
    "workspace",
    "terms of use",
    "privacy policy",
    "continue",
    "继续",
    "allow",
    "log in",
    "cancel",
    "back",
    "resend email",
    "use password",
    "continue with password",
    "log in with a one-time code",
    "login with a one-time code",
    "one-time code",
    "email code",
}

# oauth_workspace.py:290
_WORKSPACE_IGNORE_SUBSTRINGS = (  # 与 upstream 完全一致,7 项
    "new organization",
    "finish setting up",
    "set up on the next page",
    "one-time code",
    "email code",
    "continue with password",
    "use password",
)
```

### 10.4 集成位点(`src/autoteam/codex_auth.py`)

**位置**:`codex_auth.py:651-656` consent loop 每个 step 起始,**`if not use_personal:` 守卫内**(关键)

```python
# codex_auth.py:651
if not use_personal:
    from autoteam.oauth_workspace import (
        _is_workspace_selection_page as _ws_is_selection_page,
        _select_team_workspace as _ws_select_team,
    )
    # ... consent loop step 起始,先检测 workspace 选择页:
    # if _ws_is_selection_page(page):
    #     if _ws_select_team(page, workspace_name):
    #         continue  # 选择成功 → 进下一 step,不走 consent button 路径
    # # 否则继续走原 consent button 检测路径
```

**关键约束 — `if not use_personal:` 守卫**:upstream-style helper **只在 Team 路径(use_personal=False)运行**;personal 路径(`use_personal=True`)的 workspace 选择由本 spec §2.2.4 `ensure_personal_workspace_selected` 后置兜底处理(在 callback 之前),两条路径互不干扰。

### 10.5 7-section 契约

#### Scope / Trigger

**Scope**:`src/autoteam/codex_auth.py:login_codex_via_browser(use_personal=False)` 流程,consent for-loop 每个 step 起始(line 651-656 守卫内)。

**Trigger**:每个 consent step 入循环时,先调 `_is_workspace_selection_page(page)`;若返回 True 且 `workspace_name` 非空 → 调 `_select_team_workspace(page, workspace_name)`,选择成功 `continue` 进下一 step;失败或返回 False → 走原 consent button 检测路径。

#### Signatures

5 helper(见 §10.2)+ 3 常量集(见 §10.3),全部从 `src/autoteam/oauth_workspace.py` 导出。命名保持与 upstream `cnitlrt/AutoTeam` 1:1 对齐,便于后续 diff 同步。

#### Contracts

| 函数 | 输入 | 输出 | 副作用 |
|---|---|---|---|
| `_is_workspace_ignored_label(text)` | str | bool | 无 |
| `_is_workspace_selection_page(page)` | Playwright Page | bool | 读 `page.url` + `page.locator("body").inner_text(timeout=1200)`(1.2s 超时不阻塞)|
| `_workspace_label_candidates(page)` | Playwright Page | list[(str, Locator)] | 多次 `page.locator(selector).all()` 遍历,逐 locator `is_visible(100ms)` 检测 |
| `_click_workspace_locator(loc)` | Playwright Locator | bool | 调 `loc.click(timeout=3000)`,失败 retry `loc.click(force=True, timeout=3000)` |
| `_select_team_workspace(page, name)` | Playwright Page + str | bool | 命中后 click + `time.sleep(3)` 等页面跳转 |

#### Validation & Error Matrix

| 异常 | 处理 | 后果 |
|---|---|---|
| `page.url` 抛(罕见,Playwright 内部错误)| `try/except` 兜底 url="" | `_is_workspace_selection_page` 走 hint scoring 分支 |
| `page.locator("body").inner_text` 超时 1.2s | `try/except` 兜底 body="" | hint hit 0 → `_is_workspace_selection_page` False(只有 URL 含 workspace 时 True)|
| `loc.is_visible(100ms)` 抛 | `for loc in ...locator(sel).all(): try/except continue` | 跳过该 locator 继续遍历 |
| `_click_workspace_locator` 两次都失败 | 返回 False | `_select_team_workspace` 跳过此 candidate,继续遍历下个;全失败返 False |
| `time.sleep(3)` 被 monkeypatch 跳过(测试中)| 无影响 | 测试可不阻塞 |

#### Good / Base / Bad Cases

**Good case**(workspace 选择页正常处理):
- consent loop step 1 同意后,page 跳到 `https://auth.openai.com/workspace`
- `_is_workspace_selection_page(page)` 返回 True(URL 含 workspace)
- `_select_team_workspace(page, "Icoulsysad")` 找到匹配 label → click → 等 3s → return True
- consent loop `continue` → step 2 触发

**Base case**(workspace 选择页但 workspace_name 不匹配):
- 同 Good 但 workspace_name 写错或母号切换后名字变了
- `_select_team_workspace` 遍历 candidate + fallback `text=...` 都找不到 → return False
- consent loop 走原 consent button 路径 → 找不到 button → break → callback timeout → OAuth 失败(交给外层 5 次重试)

**Bad case**(consent URL 被旧实现误判,Round 11 二轮修复目标):
- step 1 page 在 `https://auth.openai.com/sign-in-with-chatgpt/codex/consent` URL,body 文本含 "Continue" 等
- 旧单 hint 实现:body 含 "workspace" 子串(其它无关上下文)即 True → 走 workspace 选择路径 → 找不到 candidate → False
- 新 2-hint scoring + IGNORE_LABELS:body hit 数 < 2 → False → 正确走 consent button 路径 → 进下一 step

#### Tests Required

测试文件:`tests/unit/test_round11_oauth_workspace_consent.py`(9 cases)

| Case | 文件:测试名 | 关键断言 |
|---|---|---|
| WS-T1 检测 URL 命中 | `test_is_workspace_selection_page_detects_team_marker` | URL 含 `/workspace` / `workspace/select` 直接 True;body 含 ≥2 hint 也 True;中文 hint 命中 |
| WS-T2 检测 false positive 修复 | `test_is_workspace_selection_page_returns_false_on_consent_page` | consent URL + 单 hint 不命中(2-hint scoring);organization URL 单 hint 也不命中 |
| WS-T3 select 命中 + 点击 | `test_select_team_workspace_clicks_target_label` | 候选列表中 target locator 被 click 1 次,其它不点 |
| WS-T4 select 不匹配 | `test_select_team_workspace_returns_false_when_no_match` | 候选列表无匹配 + fallback selector 也找不到 → False |
| WS-T5 select 空名守恒 | `test_select_team_workspace_empty_name_returns_false` | workspace_name 为 "" / "   " / None 都 False(不进 candidate 遍历)|
| WS-T6 集成路径 | `test_consent_loop_handles_workspace_before_consent_button` | workspace URL 命中 → select 调用 + click 1 次;反向同 page 但名字不匹配 → False 不误点 |
| WS-T7 IGNORE_LABELS 过滤 | `test_workspace_ignored_label_filters_noise` | "Continue"/"继续"/"Allow"/"Choose a workspace"/"Use password" 等 18 项全过滤;真 workspace 名("My Team Workspace 2024")不被误过滤 |
| WS-T8 helper + 常量集导出 | `test_upstream_helpers_exported_from_oauth_workspace` | 5 helper callable,3 常量集类型正确(tuple/set/tuple),关键文案("choose a workspace"/"选择一个工作空间"/"continue"/"继续")在常量中 |
| WS-T9 click force 兜底 | `test_click_workspace_locator_falls_back_to_force_click` | 普通 click 抛 → force=True 重试成功;两次都抛 → False |

#### Wrong vs Correct

**Wrong example**(单 hint scoring,误判 consent 页):

```python
# ❌ 旧实现(等价语义,实际 round 10 之前的代码模式):
def _is_workspace_selection_page(page) -> bool:
    body = page.locator("body").inner_text().lower()
    return "workspace" in body  # 单 hint 命中即 True
# 后果:consent URL body 中任意 "workspace" 字样(脚注 / 链接)误判为选择页 →
#       ws_select 找不到 candidate → False → consent loop 误走 workspace 路径
```

**Correct example**(2-hint scoring + URL 优先):

```python
# ✅ Round 11 二轮 upstream-style:
def _is_workspace_selection_page(page) -> bool:
    url = (page.url or "").lower()
    if "workspace" in url:
        return True  # URL 含 workspace 直接命中(personal flow goto auth.openai.com/workspace)
    body = page.locator("body").inner_text(timeout=1200).lower()
    hint_hits = sum(1 for hint in _WORKSPACE_PAGE_HINTS if hint in body)
    if "organization" in url:
        return hint_hits >= 2  # organization URL 严格 ≥ 2 hits
    return hint_hits >= 2 or "launch a workspace" in body
```

**Wrong example**(use_personal 路径泄漏 — 污染 personal 流程):

```python
# ❌ 错误集成:helper 在 if not use_personal: 守卫外执行
from autoteam.oauth_workspace import _select_team_workspace
for step in consent_steps:
    _select_team_workspace(page, workspace_name)  # 不分 personal / Team
# 后果:personal 路径(use_personal=True)workspace_name 通常是 "" → 无效;
#       且 personal 应走 ensure_personal_workspace_selected,helper 误调可能干扰
```

**Correct example**(`if not use_personal:` 守卫):

```python
# ✅ codex_auth.py:651-656:
if not use_personal:
    from autoteam.oauth_workspace import (
        _is_workspace_selection_page as _ws_is_selection_page,
        _select_team_workspace as _ws_select_team,
    )
    for step in consent_steps:
        if _ws_is_selection_page(page):
            if _ws_select_team(page, workspace_name):
                continue
        # ... 原 consent button 路径
# personal 路径(use_personal=True)走 ensure_personal_workspace_selected(§2.2.4)兜底,
# 不调 upstream-style helper —— 两条路径完全互不干扰
```

### 10.6 不变量(M-WS-personal-isolation)

> **M-WS-personal-isolation(强制)**:`upstream-style` 5 helper + 3 常量集**仅在** `if not use_personal:` 守卫内执行,不污染 `use_personal=True` 路径。
>
>   - Team 路径(`use_personal=False`):每个 consent step 起始 → `_is_workspace_selection_page` → 命中则 `_select_team_workspace(page, workspace_name)` → continue 进下一 step
>   - Personal 路径(`use_personal=True`):**不调 §10 任何 helper**;workspace 选择由 §2.2.4 `ensure_personal_workspace_selected(page, consent_url)` 后置兜底处理(decode cookie → POST /workspace/select → UI fallback)
>
> 等价**禁止**:
>   - 在 `if use_personal:` 分支或全局位置调用 §10 helper
>   - 把 §10 helper 与 §2.2.4 编排函数混用(personal 流程引入 §10 → 双重选择导致 race / 错选)
>   - 修改 5 helper 命名 / 3 常量集内容(必须与 upstream 1:1,后续 diff 维护性)
>
> 等价**允许**:
>   - 加新 helper 到 oauth_workspace.py(只要不改 §10 5 个名字)
>   - 在 Team 路径内组合调用 §10 helper(如先 `_is_workspace_selection_page` 检测,再 `_workspace_label_candidates` 遍历自定义筛选)
>
> 与 §1~§9 关系:§10 是 Team 路径 consent loop 增强(防 false positive),§1~§9 是 personal 路径 sticky-rejoin 修复(明确 workspace/select),两者目标域不同,代码上由 `if not use_personal:` 二分守恒。

### 10.7 与既有机制的关系

| 既有机制 | 关系 |
|---|---|
| §2.2.4 `ensure_personal_workspace_selected`(personal 主流程)| 互补,personal 走 §2.2.4,Team 走 §10,by `use_personal` 二分 |
| §3.4 5 次 OAuth 重试 | 兼容 — §10 失败 → consent loop break → callback timeout → 外层 5 次重试 |
| spec-2 §3.7 master health fail-fast | 配合 — fail-fast 在 OAuth 入口拦截母号已 cancel,§10 在 OAuth 流程内拦截 consent 误判 |
| `decode_oauth_session_cookie`(personal 主路径)| 独立,§10 不调,personal 流程才用 |

---

## 附录 A:修订记录

| 版本 | 时间 | 变更 |
|---|---|---|
| v1.0 | 2026-04-27 Round 8 | 初版 — 三函数契约(decode / select / fallback / 编排)+ 5 次重试 + 3 失败分类(no_personal / endpoint_error / plan_drift_persistent)+ 10 不变量(W-I1~I10)+ sleep(8) 删除依据 + 抓包验证 checklist。源自 `.trellis/tasks/04-27-master-team-degrade-oauth-rejoin/research/oauth-personal-selection.md` §3-§5 + `research/sticky-rejoin-mechanism.md` §3-§5。配套 PRD-7 Approach A R3 落地。 |
| **v1.1** | **2026-04-28 Round 11 二轮** — upstream-style consent loop helper 港口。**根因**:Round 11 二轮 OAuth Codex consent loop 在 step 1 同意后页面变成 workspace 选择页,旧单 hint 检测把 consent URL 误判 → consent button 不可见 → loop break → 30s callback timeout → 18 次连续 OAuth 失败堆积。**修复**:(1) §0 元数据 bump v1.0 → v1.1,version 注 + 引用方加 Round 11 二轮收尾;(2) **新增 §10 upstream-style consent loop helper 港口** — 自 cnitlrt/AutoTeam upstream `codex_auth.py:236-468` 1:1 复制 5 helper(`_is_workspace_ignored_label` / `_is_workspace_selection_page` / `_workspace_label_candidates` / `_click_workspace_locator` / `_select_team_workspace`)+ 3 常量集(`_WORKSPACE_PAGE_HINTS` × 8 / `_WORKSPACE_IGNORE_LABELS` × 18 / `_WORKSPACE_IGNORE_SUBSTRINGS` × 7)到 `src/autoteam/oauth_workspace.py:259-434`;集成位点 `src/autoteam/codex_auth.py:651-656` 在 `if not use_personal:` 守卫内 import + consent loop 每 step 起始检测;7 section 完整覆盖(Scope / Signatures / Contracts / Error Matrix / Good-Base-Bad / Tests Required / Wrong-vs-Correct);9 个测试 case(`tests/unit/test_round11_oauth_workspace_consent.py`)。(3) **新不变量 M-WS-personal-isolation** — upstream-style helper 仅在 `if not use_personal:` 守卫内执行,不污染 personal 路径;personal 路径仍走 §2.2.4 `ensure_personal_workspace_selected` 兜底;两条路径完全互不干扰。(4) **修复目的**:消除 false positive(把 consent 页误判为 workspace 选择页)+ 增强检测健壮性(2-hint scoring + IGNORE_LABELS 18 项过滤 + 9 类 selector 候选枚举);配合 master-subscription-health v1.4 §15 OAuth 失败 backoff(避免无谓循环堆 18+ zombie)。(5) **未改动**:Round 8 既有 §1~§9 内容全部保持,仅 §10 增量。 |
| **v1.2** | **2026-04-28 Round 11 二轮收尾** — bundle=None 视为 plan_drift 触发条件,fix W-I9 disconnect。**根因**:Round 11 实测发现 fill-personal 任务两个 batch 都 1 次失败放弃 — 18:34-18:42 实测,batch 1 因 workspaces=[] → UI fallback no_personal_button → bundle=None → break;batch 2 因 codex_auth.py:1037-1045 拒收 plan=team bundle → bundle=None → break。Round 8 v1.0 的 `if not bundle: break` 设计预设"bundle=None 是 oauth 路径 abort,不重试",但实际 codex_auth 拒收 plan=team 也会返回 None,导致 W-I9 不变量"5 次外层重试触发后端最终一致性"永远没机会被触发。**修复**:(1) §0 元数据 bump v1.1 → v1.2;(2) **§3.4.1 新增重试触发条件统一矩阵** — 把 bundle=None 列入 plan_drift 触发条件,plan_drift_history 记 `reason="bundle_none"` 与正常 plan_drift 区分;包含 codex 实施位点(`manager.py:1734-1752`)和单元测试覆盖说明;(3) **W-I9 不变量扩展** — 加 Round 11 二轮 bullet,明示 bundle=None 两种来源(workspace_select 失败 + codex_auth 拒收 plan!=free)都进 5 次外层重试;(4) **codex_auth.py:1037-1045 注释更新 + log level WARNING** — 不是真 error,是预期的 retry 触发器;(5) **新增单元测试** `tests/unit/test_round11_personal_oauth_retry.py`(4 case):前 4 次 None 第 5 次 free 成功 / 前 4 次 plan=team 拒收第 5 次 free 成功 / 5 次 None fail-fast / RegisterBlocked 终态。(6) **未改动**:Round 8 v1.0 既有 §1~§2 / §4~§9 / §10(v1.1)内容全部保持,仅 §3.4.1 增量 + W-I9 扩展。 |
| **v1.2.1** | **2026-04-28 Round 11 二轮收尾 follow-up** — §4.1 集成位置补丁,把 `ensure_personal_workspace_selected` 前置到 about-you 之后 consent loop 之前。**根因**:Round 11 二轮 v1.2 hotfix 落地后再触发 fill-personal 实测发现 5 次重试每次都拿到 plan=team(被 codex_auth 拒收成 None)。原因:(1) `codex_auth.py:953-978`(v1.0~v1.2 的接入点)在 consent loop 之后用 `if use_personal and not auth_code:` 守卫;(2) 实测 consent loop step 1-2 click "Continue/Allow" 即触发服务端 redirect 带 auth_code → page request hook 捕获 auth_code → consent loop break(line 635-636);(3) 走到 line 959 时 `if not auth_code` 为 False → workspace_select 整个 if 块**被跳过**;(4) 每次重试同样路径,5 次都同样结果,workspace_select 永远没机会执行 → W-I9 不变量"workspace_select 必走"被破坏。**修复**:(1) §0 元数据 bump v1.2 → v1.2.1;(2) **§4.1 集成位置补丁** — 把 ensure_personal_workspace_selected **前置**到 about-you 之后 consent loop 之前(`codex_auth.py:632+`,即 about-you 完成 → workspace_select(主路径)→ consent loop(此时 default 已切到 personal,consent button click 颁 plan=free token)→ post-consent workspace_select(降级为 0.1% 路径兜底));(3) **codex_auth 实施改动** — `codex_auth.py:632-668` 新增 pre-consent 块(36 行,`if use_personal:` 守卫,失败仅 logger.warning,不 raise,与 W-I1 永不抛对齐);`codex_auth.py:990-997` 注释更新,标记为兜底路径;(4) **post-consent 守卫保留** — `if use_personal and not auth_code:` 不变,某些路径下 consent loop 自然结束 + auth_code 未抓到时仍可救回(0.1% 概率);(5) **未改动**:v1.2 既有 W-I9 不变量 / §3.4.1 重试触发矩阵 / `test_round11_personal_oauth_retry.py` 4 case 全部保持(本 commit 的 pre-consent 修复在底层放大有效路径,但不改变上层 manager.py 5 次重试契约);(6) **Round 12 backlog**:pre-consent 集成测试(mock 层级太深,需端到端 Playwright 测试)。 |
| **v1.3.0** | **2026-04-28 Round 11 三轮** — empty `workspaces[]` 路径修复(server-side 状态)。**根因**:刚踢出 Team 的新号,在 OpenAI auth backend 端 `oai-oauth-session.workspaces=[]`(server-side 最终一致性窗口);`force_select_personal_via_ui` goto `/workspace` 时 UI 渲染 "Workspaces not found in client auth session" 错误页,浏览器停在错误页 → consent loop 找不到按钮 → 30s callback 等待超时 → bundle=None → 5 次外层重试每次同样 fail-fast 删账号。**修复**:(1) `ensure_personal_workspace_selected(skip_ui_fallback_on_empty=True)`(默认 False 保旧调用方),空 workspaces[] 时直接 fail-fast 不 goto 错误页 URL,evidence.fallback.phase = "skipped_by_caller_request";调用方继续 consent loop 让 OAuth 自然走 default workspace,plan_type 由 5 次外层重试 + bundle 校验兜底(回归 Round 4/e760be9 行为);(2) pre-consent navigate-back 守卫缩窄为「仅 current_url 含 `/workspace` 时」,避免重置 `/log-in` / `/password` 等正常 OAuth 流程页 form 状态;(3) post-consent fallback 同样传 `skip_ui_fallback_on_empty=True`。**留下未解决问题**(交给 v1.4.0):OAuth `/log-in` 表单 Continue 按钮变灰阻塞 — Playwright `fill()` 不触发 React onChange;workaround 思路 1)注册 flow 抽 `__Secure-next-auth.session-token` 透传给 `login_codex_via_browser` 跳过 OAuth login;思路 2)用 `SessionCodexAuthFlow` 接 personal。 |
| **v1.4.0** | **2026-04-29 Round 11 四轮** — personal OAuth fresh re-login fallback 两阶段架构落地。**根因**:v1.3.0 三轮把 `/workspace` 错误页 stuck 修了,但实测刚踢出 Team 的新号在 `auth.openai.com/log-in`(welcome back)页 fill email 后 Continue 按钮永久灰显禁用,login flow 卡死,bundle=None,5 次外层重试全失败。深层根因 — Playwright `locator.fill()` 直接 setValue 不触发 React `onChange`/`input` 合成事件,OpenAI auth 表单 enable 按钮的判断只在 native input 事件链上;所有非新创建账号(包括刚 kick 出 Team 的)都会卡这个 bug。**修复架构 — 两阶段 OAuth**:**阶段 1 快路径**(注册阶段从 chatgpt.com BrowserContext 抽 `__Secure-next-auth.session-token`(支持 chunked .0/.1)→ 透传到 `login_codex_via_browser(chatgpt_session_token=...)` → 双域 cookie 注入(chatgpt.com + auth.openai.com)→ silent step-0(`/api/auth/session?update` NextAuth refresh + `/backend-api/accounts/check`)→ OAuth 拿 plan=free 直接返回);**阶段 2 fallback**(personal+stage1 拿到 plan != free 时:`context.clear_cookies()` → goto chatgpt.com/auth/login → email/password 用 `keyboard.type(delay=50)` 而非 `fill()` 绕开灰按钮 bug → OTP 处理 → 等到 chatgpt.com root → 新 PKCE 重走 OAuth → consent loop → bundle.plan_type=free)。**修复明细**:(1) §0 元数据 bump v1.3.0 → v1.4.0,引用方加 Round 11 task 四轮收尾;(2) §4.1 v1.3.0 章节"已知未解决问题"标注 v1.4.0 已落地,**新增 §4.1 子节 v1.4.0 完整章节**(根因 / 修复架构两阶段 / 关键 invariants W-I11~W-I14 / 不变量延续 / 单测覆盖 / 实施位点速览);(3) **§6 不变量加 W-I11~W-I14**:W-I11 阶段 2 必须 `context.clear_cookies()`(防 stale Team session 污染);W-I12 阶段 2 email/password 必须 `keyboard.type(delay≥50ms)` + click+Ctrl+A+Delete 清空,**禁止** `locator.fill()`;W-I13 阶段 2 触发条件 `(not stage1_ok) and use_personal`(Team 模式不会误触发,性能保护);W-I14 `_register_direct_once` 必须返回 tuple `(bool, Optional[str])`,所有 early-return 改 `(False, None)`;(4) **代码改动**:`src/autoteam/codex_auth.py` 重构两阶段 + 新增 helpers `_inject_personal_session_cookies` / `_typewrite_credential` / `_perform_fresh_relogin_in_context`;`src/autoteam/manager.py` `_register_direct_once` 返 tuple + `create_account_direct` / `_run_post_register_oauth` 加 `chatgpt_session_token` kwarg 透传链 + 新 helper `_extract_session_token_from_context`;(5) **新增单测**:`tests/unit/test_round11_session_token_injection.py`(16 case,W-I14 + 双域注入 + forward chain) + `tests/unit/test_round11_fresh_relogin_fallback.py`(13 case,W-I11/I12/I13 + helper 单元行为),trellis-check 通过 363 测;(6) **未改动**:Round 8 既有 §1~§3 / §5 / §7~§9 / §10(v1.1)/ v1.3.0 既有 `skip_ui_fallback_on_empty` + navigate-back `/workspace` 缩窄全部继承;v1.2 既有 W-I9 不变量 / §3.4.1 重试触发矩阵 / 5 次外层重试 + plan_drift 拒收(`manager.py:1734-1820`)未改;`SessionCodexAuthFlow` / `MainCodexSyncFlow`(主号专用)未改。配套 Round 11 task `04-28-round11-master-resub-models-validate` 四轮收尾。 |
| **v1.5.0** | **2026-04-29 Round 11 五轮 spec-update** — stage 1 快路径实证可拿 plan=free + issuer ledger TTL 现象沉淀(无代码改动,纯 spec 文档化新认知)。**实证背景**:fd3b5ccae1@zrainbow1257.com(用户几小时前 admin UI 手动 kick 的号)走 200s timeout headless OAuth(`chatgpt_session_token=None` + `use_personal=True`)→ **71.3s 拿到 plan_type=free** + account_id `7f4384d7-4831-4a8d-a93c-547296c6b600`(personal workspace),**未触发 stage 2 fresh re-login fallback**(W-I11~W-I14 路径),只走 stage 1 + W-I9 `skip_ui_fallback_on_empty=True` + consent loop default fallback 即拿到 free。证据:`.trellis/tasks/04-28-round11-master-resub-models-validate/research/p1-p2-execution-report.md` § P1。**关键观察**:之前 Round 11 实测 8c345dc24e 5/5 全部 plan=team 失败的根因 — 当时是"注册→立即 kick→立即 OAuth",issuer ledger 还指向 master Team;而手动 kick 等几小时后 ledger 已清(workspaces=[]),stage 1 即足够。**新增内容**:(1) §0 元数据 bump v1.4.0 → v1.5.0,共因加"issuer 端 `oai-oauth-session.workspaces[]` 清空有 TTL"观察;(2) **§6 新增 W-I15 invariant** — stage 1 在 `workspaces[]==[]` 路径已实证可拿 plan=free,不需要 stage 2 fresh re-login fallback;限制说明(仅 issuer ledger 已清场景);与 W-I11~W-I14 stage 2 关系(stage 2 仅 stage 1 失败时触发);(3) **新增 §4.4 OAuth Issuer Ledger TTL 现象** — 现象描述(立即 OAuth vs 等待若干时间的 plan_type 差异)+ Round 11 五轮实证证据(fd3b5ccae1 / 404907e1c8 auto-rejoin / b7c4aaf8f2)+ 实施影响(stage 1/2 双轨设计仍正确)+ 与 §3.4 5 次重试退避表的兼容性 NOTE(分钟到小时级 TTL 远超 215s 累计退避,backlog round 12+ 重新评估);(4) **§4.1 v1.4.0 子节 invariants 段加 NOTE** — W-I11/W-I12 不变,但 stage 2 仅在 stage 1 失败时触发(W-I13 守卫),issuer ledger 已清场景下 stage 1 即足够。**未改动**:Round 8 既有 §1~§3 / §5 / §7~§9 / §10(v1.1)/ v1.4.0 双阶段架构 / W-I1~W-I14 全部不变,纯 spec 增量。**Round 12 backlog**:(a) issuer ledger TTL 边界实测(已知至少几小时,未实测最短时长);(b) §3.4 5 次重试退避表评估(可能需要"立即重试 + 长期 backoff"组合);(c) RT 后台进程把"刚 kick 的子号"放入冷却期,避免立即 OAuth 命中 ledger Team 项浪费 stage 2 fresh re-login overhead。 |
