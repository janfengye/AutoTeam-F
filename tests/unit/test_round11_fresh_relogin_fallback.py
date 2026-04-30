"""Round 11 五轮 — Fresh chatgpt.com re-login fallback (Option A).

阻塞场景:
- Round 11 四轮(session_token 注入 + silent step-0 NextAuth refresh)落地后,
  实测 fill-personal 仍然 5 次重试都拿到 plan_type=team(被 codex_auth plan_drift 守卫
  拒收成 None)。
- 根因:注册阶段抽出来的 chatgpt.com session_token 内嵌的 user identity
  锁死在原 Team workspace,即使 NextAuth `?update` 刷新也切不到 Personal。
  retry 2-5 次还会被检测到 hasUser=False(kick 已传播,session 服务端失效),
  OAuth 落到 /log-in 灰按钮死循环。

修复路径(本测试文件覆盖):
- `_typewrite_credential`:用 `page.keyboard.type(value, delay=50)` 触发 React
  onChange/onInput 事件链,避开 Playwright `fill()` 在 OpenAI auth /log-in
  React 表单引发的"Continue 按钮变灰"现象。
- `_perform_fresh_relogin_in_context`:阶段 2 fallback —
  `context.clear_cookies()` 清掉 stale 的 chatgpt.com / auth.openai.com 双域
  session,重新走 chatgpt.com/auth/login(用 keyboard.type 填 email + password,
  自动收 OTP),拿到 Personal-bound 全新 session,再走 OAuth → plan=free。
- `login_codex_via_browser`:阶段 1 仍走 silent step-0 + cookie 注入快路径
  (Round 11 四轮已落地);阶段 1 拿到 plan != free 或 bundle=None 时,触发
  阶段 2 fallback,仅 personal 路径生效。
"""

from __future__ import annotations

import inspect
import re

from autoteam import codex_auth

# ---------------------------------------------------------------------------
# Helper signature contract checks
# ---------------------------------------------------------------------------


def test_typewrite_credential_helper_exists():
    """_typewrite_credential helper 必须存在,签名稳定."""
    assert hasattr(codex_auth, "_typewrite_credential"), "_typewrite_credential 必须存在"
    sig = inspect.signature(codex_auth._typewrite_credential)
    params = sig.parameters
    # 强制 keyword-only 参数:delay_ms / post_sleep
    assert "delay_ms" in params
    assert params["delay_ms"].kind is inspect.Parameter.KEYWORD_ONLY
    assert "post_sleep" in params
    assert params["post_sleep"].kind is inspect.Parameter.KEYWORD_ONLY


def test_perform_fresh_relogin_helper_exists():
    """_perform_fresh_relogin_in_context helper 必须存在,签名稳定."""
    assert hasattr(codex_auth, "_perform_fresh_relogin_in_context")
    sig = inspect.signature(codex_auth._perform_fresh_relogin_in_context)
    params = sig.parameters
    # 必须接受 used_email_ids (keyword-only,与 login_codex_via_browser 的 set 共享)
    assert "used_email_ids" in params
    assert params["used_email_ids"].kind is inspect.Parameter.KEYWORD_ONLY


# ---------------------------------------------------------------------------
# Source-level invariant — 阶段 2 用 keyboard.type 不用 fill()
# ---------------------------------------------------------------------------


def test_typewrite_credential_uses_keyboard_type_not_fill():
    """_typewrite_credential 必须用 page.keyboard.type 而不是 locator.fill().

    回归保护 — 实战 bug:用 fill() 触发 OpenAI auth /log-in 灰按钮检测。
    """
    src = inspect.getsource(codex_auth._typewrite_credential)
    # 必须包含 page.keyboard.type
    assert "page.keyboard.type" in src
    # 不能用 .fill( 在 helper 里(避免回归到 fill 触发灰按钮)
    # 注:locator.fill 在其他地方仍然 OK,但 _typewrite_credential 内不应有
    fill_calls = re.findall(r"\.fill\s*\(", src)
    assert not fill_calls, f"_typewrite_credential 仍有 .fill() 调用: {fill_calls}"


def test_fresh_relogin_uses_keyboard_type_for_credentials():
    """_perform_fresh_relogin_in_context 必须用 _typewrite_credential 填 email + password.

    Source-level invariant:阶段 2 代码块里 _typewrite_credential 调用次数 ≥ 2
    (email + password 各 1 次),保证不会回归到 fill 触发灰按钮。
    """
    src = inspect.getsource(codex_auth._perform_fresh_relogin_in_context)
    typewrite_calls = re.findall(r"_typewrite_credential\s*\(", src)
    assert len(typewrite_calls) >= 2, (
        f"_perform_fresh_relogin_in_context 应至少 2 次调 _typewrite_credential "
        f"(email + password),实际 {len(typewrite_calls)} 次"
    )


def test_fresh_relogin_clears_cookies_first():
    """_perform_fresh_relogin_in_context 必须先 context.clear_cookies() 再登录."""
    src = inspect.getsource(codex_auth._perform_fresh_relogin_in_context)
    assert "clear_cookies" in src, "_perform_fresh_relogin_in_context 必须 clear_cookies"

    # clear_cookies 必须在 chatgpt.com/auth/login goto 之前出现
    clear_idx = src.find("clear_cookies")
    login_idx = src.find("chatgpt.com/auth/login")
    assert clear_idx != -1
    assert login_idx != -1
    assert clear_idx < login_idx, "clear_cookies 必须在 goto chatgpt.com/auth/login 之前"


# ---------------------------------------------------------------------------
# login_codex_via_browser 集成 — 阶段 2 触发条件
# ---------------------------------------------------------------------------


def test_login_codex_has_stage2_fallback_block():
    """login_codex_via_browser 必须包含阶段 2 fresh re-login fallback 代码块."""
    src = inspect.getsource(codex_auth.login_codex_via_browser)
    # 必须调 _perform_fresh_relogin_in_context
    assert "_perform_fresh_relogin_in_context" in src, "login_codex_via_browser 必须调 _perform_fresh_relogin_in_context"


def test_login_codex_stage2_only_for_personal():
    """阶段 2 仅在 use_personal=True 时触发.

    Source invariant:_perform_fresh_relogin_in_context 调用必须被
    `use_personal` 守卫包裹,Team 路径绝对不能走阶段 2(Team 拿 plan=team 是合法的)。
    """
    src = inspect.getsource(codex_auth.login_codex_via_browser)
    # 找到 _perform_fresh_relogin_in_context 调用位置
    relogin_idx = src.find("_perform_fresh_relogin_in_context(")
    assert relogin_idx > 0, "_perform_fresh_relogin_in_context 必须被调用"

    # 倒查最近的 if 语句,必须含 use_personal
    # 取调用前 500 字符
    ctx_before = src[max(0, relogin_idx - 500) : relogin_idx]
    assert "use_personal" in ctx_before, (
        "_perform_fresh_relogin_in_context 调用前 500 字符必须含 use_personal 守卫"
    )


def test_login_codex_stage2_triggered_by_plan_team_or_none():
    """阶段 2 触发条件必须是 'stage1 拿到 plan != free 或 bundle=None'.

    Source invariant:login_codex_via_browser 必须区分阶段 1 / 阶段 2,
    不能无条件总是走阶段 2(性能 + 正确性 — 阶段 1 plan=free 时不该再清 cookies)。
    """
    src = inspect.getsource(codex_auth.login_codex_via_browser)
    # 必须出现 stage1_ok 之类的 flag
    has_stage_logic = (
        "stage1_ok" in src
        or "stage1_bundle" in src
    )
    assert has_stage_logic, (
        "login_codex_via_browser 必须有 stage1 / stage2 区分逻辑,"
        "不能无条件总是走阶段 2"
    )


def test_login_codex_stage2_uses_new_pkce():
    """阶段 2 的 OAuth 必须用新 PKCE 对(原 auth_code 已 used)."""
    src = inspect.getsource(codex_auth.login_codex_via_browser)
    # _generate_pkce 必须出现 ≥ 2 次(初始 + 阶段 2)
    pkce_calls = re.findall(r"_generate_pkce\s*\(\)", src)
    assert len(pkce_calls) >= 2, (
        f"login_codex_via_browser 应至少调用 _generate_pkce 2 次"
        f"(初始 + 阶段 2 重新生成),实际 {len(pkce_calls)} 次"
    )


# ---------------------------------------------------------------------------
# Stage 1 path is preserved — silent step-0 + cookie injection 仍然走
# ---------------------------------------------------------------------------


def test_login_codex_preserves_stage1_silent_step0():
    """阶段 1 silent step-0 + cookie 双域注入路径不能被删."""
    src = inspect.getsource(codex_auth.login_codex_via_browser)
    # silent step-0 关键调用
    assert "_inject_personal_session_cookies" in src, "阶段 1 cookie 注入不能被删"
    assert "/api/auth/session?update" in src, "阶段 1 silent NextAuth refresh 不能被删"


def test_login_codex_no_token_skips_dual_domain_injection():
    """chatgpt_session_token=None 时不走双域注入(参数 contract 守恒)."""
    src = inspect.getsource(codex_auth.login_codex_via_browser)
    # 注入必须被 personal+token 双重守卫
    inject_idx = src.find("_inject_personal_session_cookies(context, chatgpt_session_token)")
    guard_idx = src.find("if use_personal and chatgpt_session_token:")
    assert inject_idx > 0
    assert guard_idx > 0
    assert inject_idx > guard_idx, (
        "_inject_personal_session_cookies 调用必须被 'use_personal and chatgpt_session_token' 守卫包裹"
    )


# ---------------------------------------------------------------------------
# Manager.py plan_drift guard 不被破坏 — Round 11 一轮 W-I9 仍然生效
# ---------------------------------------------------------------------------


def test_manager_plan_drift_guard_unchanged():
    """manager._run_post_register_oauth 的 plan != free 拒收逻辑必须保留.

    Round 11 一轮 W-I9:plan=team 视为 plan_drift 触发外层 5 次重试,不能改成放行。
    Round 11 五轮的 fresh re-login fallback 在 codex_auth 层做,manager 层守卫不动。
    """
    from autoteam import manager

    src = inspect.getsource(manager._run_post_register_oauth)
    # 必须仍存在 plan_drift / plan_type 校验
    assert "plan_type" in src
    # 必须仍有 W-I9 风格的 5 次重试逻辑
    has_retry = "max_retries" in src or "for attempt" in src or "retry" in src.lower()
    assert has_retry, "manager._run_post_register_oauth 仍需 W-I9 5 次重试"


# ---------------------------------------------------------------------------
# Defensive — fresh relogin returns False 时调用方应放弃返回 None
# ---------------------------------------------------------------------------


def test_login_codex_handles_relogin_failure():
    """_perform_fresh_relogin_in_context 返回 False 时,login_codex_via_browser 必须放弃返回 None.

    Source invariant:relogin_ok 检查后必须有 return None 路径。
    """
    src = inspect.getsource(codex_auth.login_codex_via_browser)
    # 找到 relogin_ok 变量
    assert "relogin_ok" in src, "login_codex_via_browser 必须用 relogin_ok 接 fresh relogin 返回值"

    # relogin_ok 后必须有 if not / 等价的判断 + return None
    relogin_idx = src.find("relogin_ok")
    ctx_after = src[relogin_idx : relogin_idx + 500]
    has_failure_handling = (
        "not relogin_ok" in ctx_after
        or "if relogin_ok is False" in ctx_after
        or "if not " in ctx_after
    )
    assert has_failure_handling, (
        "relogin_ok 后必须有失败分支处理(if not relogin_ok: return None)"
    )
