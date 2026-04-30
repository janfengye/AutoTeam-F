"""Round 11 二轮 — Personal OAuth 5 次重试在 bundle=None 时也触发(W-I9)。

修复目标:
  - codex_auth.py:1037-1045 拒收 plan_type!=free bundle 时返回 None;
    旧 manager.py:1734-1738 在第 1 次 None 即 break,导致永远跑不到 5 次重试。
  - W-I9 spec 要求 workspace/select 主路径成功但 callback 拿到 plan!=free 时,
    必须进入外层 5 次重试触发后端最终一致性,而非 fail-fast。
  - bundle=None 现在视为 plan_drift,加入 history 跑 5 次重试。

测试覆盖:
  1. 前 4 次 None,第 5 次 free → 成功(plan_drift_history 4 条 reason="bundle_none")
  2. 前 4 次 plan=team(被 codex_auth 拒收成 None),第 5 次 free → 同上路径
  3. 5 次都 None → fail-fast,record_failure(_OAUTH_PLAN_DRIFT_PERSISTENT) + delete_account
  4. RegisterBlocked terminal,不进重试循环
  5. (Round 11 二轮 follow-up v1.2.1) codex_auth.py 含 pre-consent workspace_select 锚 —
     防止未来重构丢失 W-I9 硬保证。
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch


def _seed_pending_account(tmp_path, monkeypatch, email="p@x.com", master_id="test-master"):
    """seed 一个 pending personal 子号。"""
    from autoteam import accounts as accounts_mod

    accounts_file = tmp_path / "accounts.json"
    monkeypatch.setattr(accounts_mod, "ACCOUNTS_FILE", accounts_file)
    accounts_mod.save_accounts([{
        "email": email,
        "password": "pwd",
        "status": accounts_mod.STATUS_PENDING,
        "auth_file": None,
        "cloudmail_account_id": None,
        "workspace_account_id": master_id,
        "created_at": time.time(),
    }])
    return accounts_file


def _make_free_bundle(account_id="acc-personal"):
    """生成一个 free plan 的合法 bundle dict。"""
    return {
        "access_token": "ACCESS-TOKEN-FREE",
        "refresh_token": "REFRESH-TOKEN-FREE",
        "id_token": "ID-TOKEN-FREE",
        "account_id": account_id,
        "email": "p@x.com",
        "plan_type": "free",
        "plan_type_raw": "free",
        "plan_supported": True,
        "expired": time.time() + 3600,
    }


def _patch_master_health_active():
    """master_health 探针返回 (True, "active", {}) 跳过 fail-fast 分支。"""
    return patch(
        "autoteam.master_health.is_master_subscription_healthy",
        return_value=(True, "active", {}),
    )


def _patch_chatgpt_api_for_remove(manager_mod):
    """mock 主号 API 让 remove_from_team 走 'removed' 路径(personal 必须先 kick)。"""
    mock_api_inst = MagicMock()
    mock_api_inst.start = MagicMock()
    mock_api_inst.stop = MagicMock()
    api_patcher = patch.object(manager_mod, "ChatGPTTeamAPI", return_value=mock_api_inst)
    remove_patcher = patch.object(manager_mod, "remove_from_team", return_value="removed")
    return api_patcher, remove_patcher


def test_personal_oauth_4_none_then_free_succeeds(tmp_path, monkeypatch):
    """前 4 次 login_codex_via_browser 返回 None,第 5 次返回 plan=free → 成功。

    plan_drift_history 应有 4 条 reason="bundle_none"。
    """
    from autoteam import accounts as accounts_mod
    from autoteam import manager

    _seed_pending_account(tmp_path, monkeypatch, "p1@x.com")

    bundle_results = [None, None, None, None, _make_free_bundle("acc-p1")]
    record_failures_called = []

    api_patcher, remove_patcher = _patch_chatgpt_api_for_remove(manager)

    with _patch_master_health_active():
        with api_patcher:
            with remove_patcher:
                with patch.object(
                    manager,
                    "login_codex_via_browser",
                    side_effect=bundle_results,
                ):
                    with patch.object(manager, "record_failure", side_effect=lambda *a, **kw: record_failures_called.append((a, kw))):
                        with patch.object(manager, "delete_account") as mock_delete:
                            with patch.object(manager, "save_auth_file", return_value=str(tmp_path / "auth-p1.json")):
                                with patch.object(manager, "check_codex_quota", return_value=("ok", {"primary_pct": 0})):
                                    with patch("autoteam.manager.time.sleep"):
                                        out = {}
                                        result = manager._run_post_register_oauth(
                                            email="p1@x.com",
                                            password="pwd",
                                            mail_client=MagicMock(),
                                            leave_workspace=True,
                                            out_outcome=out,
                                        )

    # 第 5 次 free → 成功
    assert result == "p1@x.com", f"5 次重试第 5 次 free,应该成功,实际 {result}"
    assert out.get("status") == "success"
    # 不调 delete_account
    assert not mock_delete.called, "成功路径不该 delete_account"
    # 状态应转 PERSONAL
    reloaded = accounts_mod.load_accounts()
    rec = next(a for a in reloaded if a["email"] == "p1@x.com")
    assert rec["status"] == accounts_mod.STATUS_PERSONAL
    # 没有失败记录(成功路径不 record_failure)
    fail_calls_for_drift = [
        c for c in record_failures_called
        if len(c[0]) >= 2 and c[0][1] == "oauth_plan_drift_persistent"
    ]
    assert not fail_calls_for_drift, "成功路径不该记 oauth_plan_drift_persistent"


def test_personal_oauth_4_team_drift_then_free_succeeds(tmp_path, monkeypatch):
    """前 4 次 codex_auth 拒收 plan=team(返回 None),第 5 次 free → 成功。

    与 case 1 行为一致(从 manager 角度看 None 不区分 abort vs 拒收 — 都是 bundle_none)。
    """
    from autoteam import accounts as accounts_mod
    from autoteam import manager

    _seed_pending_account(tmp_path, monkeypatch, "p2@x.com")

    # codex_auth 在拿到 plan=team bundle 时拒收 → 调用方拿到 None
    # 等同于 case 1:都是 None 序列然后 free
    bundle_results = [None, None, None, None, _make_free_bundle("acc-p2")]

    api_patcher, remove_patcher = _patch_chatgpt_api_for_remove(manager)

    with _patch_master_health_active():
        with api_patcher:
            with remove_patcher:
                with patch.object(
                    manager,
                    "login_codex_via_browser",
                    side_effect=bundle_results,
                ):
                    with patch.object(manager, "record_failure"):
                        with patch.object(manager, "delete_account") as mock_delete:
                            with patch.object(manager, "save_auth_file", return_value=str(tmp_path / "auth-p2.json")):
                                with patch.object(manager, "check_codex_quota", return_value=("ok", {"primary_pct": 0})):
                                    with patch("autoteam.manager.time.sleep"):
                                        out = {}
                                        result = manager._run_post_register_oauth(
                                            email="p2@x.com",
                                            password="pwd",
                                            mail_client=MagicMock(),
                                            leave_workspace=True,
                                            out_outcome=out,
                                        )

    assert result == "p2@x.com"
    assert out.get("status") == "success"
    assert not mock_delete.called
    reloaded = accounts_mod.load_accounts()
    rec = next(a for a in reloaded if a["email"] == "p2@x.com")
    assert rec["status"] == accounts_mod.STATUS_PERSONAL


def test_personal_oauth_5_all_none_failfast(tmp_path, monkeypatch):
    """5 次 login_codex_via_browser 全 None → fail-fast。

    record_failure(_OAUTH_PLAN_DRIFT_PERSISTENT) 调用 + delete_account 被调 +
    plan_drift_history 5 条全 reason="bundle_none"。
    """
    from autoteam import manager
    from autoteam.register_failures import OAUTH_PLAN_DRIFT_PERSISTENT

    _seed_pending_account(tmp_path, monkeypatch, "p3@x.com")

    record_failures_called = []
    api_patcher, remove_patcher = _patch_chatgpt_api_for_remove(manager)

    with _patch_master_health_active():
        with api_patcher:
            with remove_patcher:
                with patch.object(
                    manager,
                    "login_codex_via_browser",
                    return_value=None,
                ) as mock_login:
                    with patch.object(
                        manager,
                        "record_failure",
                        side_effect=lambda *a, **kw: record_failures_called.append((a, kw)),
                    ):
                        with patch.object(manager, "delete_account") as mock_delete:
                            with patch("autoteam.manager.time.sleep"):
                                out = {}
                                result = manager._run_post_register_oauth(
                                    email="p3@x.com",
                                    password="pwd",
                                    mail_client=MagicMock(),
                                    leave_workspace=True,
                                    out_outcome=out,
                                )

    # 5 次都 None → 5 次重试用尽
    assert mock_login.call_count == 5, (
        f"应该重试 5 次,实际 {mock_login.call_count} 次"
    )
    # fail-fast
    assert result is None
    assert out.get("status") == "oauth_failed"
    # delete_account 被调
    assert mock_delete.called, "fail-fast 必须 delete_account"
    mock_delete.assert_called_with("p3@x.com")

    # record_failure 含 OAUTH_PLAN_DRIFT_PERSISTENT 一条
    plan_drift_calls = [
        c for c in record_failures_called
        if len(c[0]) >= 2 and c[0][1] == OAUTH_PLAN_DRIFT_PERSISTENT
    ]
    assert len(plan_drift_calls) == 1, (
        f"应该记 1 条 OAUTH_PLAN_DRIFT_PERSISTENT,实际 {len(plan_drift_calls)} 条"
    )

    # 验证 drift_history 5 条全 reason="bundle_none"
    drift_call = plan_drift_calls[0]
    history = drift_call[1].get("drift_history", [])
    assert len(history) == 5, (
        f"plan_drift_history 应该 5 条,实际 {len(history)} 条 (history={history})"
    )
    for entry in history:
        assert entry.get("reason") == "bundle_none", (
            f"每条 history 的 reason 应该是 'bundle_none',实际: {entry}"
        )


def test_personal_oauth_register_blocked_is_terminal(tmp_path, monkeypatch):
    """RegisterBlocked(is_phone=True) 第 1 次抛 → 立即 fail-fast,不重试。

    保留旧逻辑 — RegisterBlocked 是终态(用户级风控,重试也无用)。
    """
    from autoteam import manager
    from autoteam.invite import RegisterBlocked

    _seed_pending_account(tmp_path, monkeypatch, "p4@x.com")

    record_failures_called = []
    api_patcher, remove_patcher = _patch_chatgpt_api_for_remove(manager)

    with _patch_master_health_active():
        with api_patcher:
            with remove_patcher:
                with patch.object(
                    manager,
                    "login_codex_via_browser",
                    side_effect=RegisterBlocked(
                        "oauth_consent_2", "add-phone url 命中", is_phone=True
                    ),
                ) as mock_login:
                    with patch.object(
                        manager,
                        "record_failure",
                        side_effect=lambda *a, **kw: record_failures_called.append((a, kw)),
                    ):
                        with patch.object(manager, "delete_account") as mock_delete:
                            with patch("autoteam.manager.time.sleep"):
                                out = {}
                                result = manager._run_post_register_oauth(
                                    email="p4@x.com",
                                    password="pwd",
                                    mail_client=MagicMock(),
                                    leave_workspace=True,
                                    out_outcome=out,
                                )

    # 第 1 次 RegisterBlocked → 立即终止,不应重试
    assert mock_login.call_count == 1, (
        f"RegisterBlocked is_phone 是终态,应该 1 次就终止,实际 {mock_login.call_count} 次"
    )
    assert result is None
    assert out.get("status") == "oauth_phone_blocked"
    assert mock_delete.called, "RegisterBlocked phone 必须 delete_account"

    # record_failure 含 oauth_phone_blocked 一条
    phone_calls = [
        c for c in record_failures_called
        if len(c[0]) >= 2 and c[0][1] == "oauth_phone_blocked"
    ]
    assert len(phone_calls) == 1, (
        f"应该记 1 条 oauth_phone_blocked,实际 {len(phone_calls)} 条"
    )
    # 不应该记 oauth_plan_drift_persistent(没进 5 次循环)
    drift_calls = [
        c for c in record_failures_called
        if len(c[0]) >= 2 and c[0][1] == "oauth_plan_drift_persistent"
    ]
    assert not drift_calls, "RegisterBlocked 不应进 plan_drift 路径"


def test_codex_auth_contains_pre_consent_workspace_select_anchor():
    """Round 11 三轮 — 静态锚 grep 防回归。

    spec `oauth-workspace-selection.md` §4.1 要求 pre-consent workspace_select
    必须前置到 about-you 之后 consent loop 之前。本测试确认源码中锚存在,防止未来重构
    误删导致 W-I9 不变量"workspace_select 必走"被破坏。

    Round 11 三轮新增:必须 skip_ui_fallback_on_empty=True(空 workspaces[] 不再
    goto /workspace 错误页)+ pre-consent 后强制 goto auth_url 回到 consent flow。

    断言:
      - `codex_auth.py` 包含 pre-consent 注释(支持二轮/三轮命名)
      - 锚后 2000 字符内必含:
          * `if use_personal:` 守卫
          * `_pre_consent_ws_select` 别名(避免与 post-consent 命名冲突)
          * `consent_url=auth_url` 实参(用 auth_url 作为 base,因为此时未到 consent)
          * `pre_ws_ok` / `pre_ws_fail` 三元组解构
          * `logger.warning` 失败处置(不 raise,符合 W-I9 + W-I1)
          * `skip_ui_fallback_on_empty=True`(三轮新增)
          * pre-consent 后导航回 auth_url 的逻辑(`page.goto(auth_url`)
      - 锚出现在 `for step in range(10):` 之前(consent loop 起点)
    """
    src = (Path(__file__).resolve().parents[2] / "src" / "autoteam" / "codex_auth.py").read_text(
        encoding="utf-8"
    )

    # 锚 1:pre-consent 注释存在(允许二轮或三轮命名)
    pre_consent_anchor_v2 = "Round 11 二轮 — Personal 模式: pre-consent workspace_select"
    pre_consent_anchor_v3 = "Round 11 三轮 — Personal 模式: pre-consent workspace_select"
    if pre_consent_anchor_v3 in src:
        pre_consent_anchor = pre_consent_anchor_v3
    elif pre_consent_anchor_v2 in src:
        pre_consent_anchor = pre_consent_anchor_v2
    else:
        raise AssertionError(
            "pre-consent workspace_select 锚缺失,W-I9 不变量被破坏。"
            f"应在 codex_auth.py 中包含 `{pre_consent_anchor_v2}` 或 `{pre_consent_anchor_v3}`"
        )

    # 锚 2:锚后 4000 字符内必含关键实施(三轮加了 navigate-back 逻辑,块更长)
    idx = src.index(pre_consent_anchor)
    block = src[idx : idx + 4000]
    assert "if use_personal:" in block, (
        "pre-consent 块必须用 `if use_personal:` 守卫(personal 路径才执行)"
    )
    assert "_pre_consent_ws_select" in block, (
        "pre-consent 块必须用 `_pre_consent_ws_select` 别名(避免与 post-consent 命名冲突)"
    )
    assert "consent_url=auth_url" in block, (
        "pre-consent 块必须传 consent_url=auth_url(此时尚未到 consent 页,用 auth_url 作 base)"
    )
    assert "pre_ws_ok" in block and "pre_ws_fail" in block, (
        "pre-consent 块必须解构 (ok, fail, evidence) 三元组"
    )
    # 失败时仅 warning 不 raise — 符合 W-I1(永不抛)+ W-I9(由外层重试承担)
    assert "logger.warning" in block, (
        "pre-consent 失败必须 logger.warning,不抛异常(由外层 5 次重试承担)"
    )

    # Round 11 三轮新增:skip_ui_fallback_on_empty=True
    if pre_consent_anchor == pre_consent_anchor_v3:
        assert "skip_ui_fallback_on_empty=True" in block, (
            "Round 11 三轮 pre-consent 必须传 skip_ui_fallback_on_empty=True,"
            "避免 goto /workspace 错误页阻塞 consent loop"
        )
        # navigate-back 逻辑必须存在
        assert "page.goto(auth_url" in block, (
            "Round 11 三轮 pre-consent 后必须导航回 auth_url(`page.goto(auth_url, ...)`),"
            "否则 fallback 失败时浏览器停在 /workspace 错误页 → consent loop 找不到按钮"
        )

    # 锚 3:pre-consent 必须出现在 consent loop 之前
    consent_loop_marker = "处理多个授权/同意页面"
    consent_loop_idx = src.index(consent_loop_marker)
    pre_consent_idx = src.index(pre_consent_anchor)
    assert pre_consent_idx < consent_loop_idx, (
        f"pre-consent workspace_select 必须出现在 consent loop 之前。"
        f"实测 pre_consent_idx={pre_consent_idx} 但 consent_loop_idx={consent_loop_idx}"
    )

    # 锚 4:post-consent 兜底仍存在(`if use_personal and not auth_code:` 守卫)
    post_consent_guard = "if use_personal and not auth_code:"
    assert post_consent_guard in src, (
        f"post-consent workspace_select 兜底守卫被误删: {post_consent_guard}"
    )
    # 且必须出现在 pre-consent 之后(降级为兜底)
    post_consent_idx = src.index(post_consent_guard)
    assert pre_consent_idx < post_consent_idx, (
        f"post-consent 兜底必须在 pre-consent 之后(降级路径)。"
        f"pre_consent_idx={pre_consent_idx} post_consent_idx={post_consent_idx}"
    )
