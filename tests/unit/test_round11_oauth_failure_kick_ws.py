"""Round 11 — OAuth 失败时同步 KICK ws 测试。

修复目标:`_run_post_register_oauth` 在 4 个失败位点(RegisterBlocked phone /
RegisterBlocked unexpected / plan_unsupported / bundle_missing)只标本地
status=AUTH_INVALID,workspace 中的 Team 席位等 reconcile 5min 后才异步清理。
本轮加 helper `_kick_team_seat_after_oauth_failure(email, reason)` 同步 KICK,
消除"workspace 有 + 本地 auth 缺失"残废延迟。

测试覆盖:
  1. helper 调用 remove_from_team(email, return_status=True) — 参数正确
  2. helper 异常吞掉只 warning(不传播 → reconcile 兜底)
  3. helper warning 文案含 reason 参数(便于事后排查失败位点)
  4. bundle_missing 分支 → helper 被调用且 reason="bundle_missing"
  5. plan_unsupported 分支 → helper 被调用且 reason="plan_unsupported"
"""
from __future__ import annotations

import logging
import time
from unittest.mock import MagicMock, patch

# =====================================================================
# helper 自身的契约测试 — 调用 remove_from_team / 吞异常 / 记 reason
# =====================================================================


class TestKickHelperContract:
    """_kick_team_seat_after_oauth_failure 自身的 3 个契约保证。"""

    def test_kick_helper_calls_remove_from_team_with_email(self):
        """helper 必须调 remove_from_team(api_inst, email, return_status=True)。"""
        from autoteam import manager

        with patch.object(manager, "ChatGPTTeamAPI") as mock_api_cls:
            mock_api_inst = MagicMock()
            mock_api_inst.start = MagicMock()
            mock_api_inst.stop = MagicMock()
            mock_api_cls.return_value = mock_api_inst

            with patch.object(manager, "remove_from_team") as mock_remove:
                mock_remove.return_value = "removed"
                manager._kick_team_seat_after_oauth_failure(
                    "fail@x.com", reason="test_reason"
                )

            # 必须 start() / 调 remove_from_team / stop()
            assert mock_api_inst.start.called, "cleanup_api.start() 必须被调用"
            assert mock_api_inst.stop.called, "cleanup_api.stop() 必须在 finally 被调用"

            # remove_from_team 调用参数必须 (api_inst, email, return_status=True)
            mock_remove.assert_called_once()
            call_args = mock_remove.call_args
            assert call_args[0][0] is mock_api_inst, "第 1 个 positional 参数应为 api 实例"
            assert call_args[0][1] == "fail@x.com", "第 2 个 positional 参数应为 email"
            assert call_args.kwargs.get("return_status") is True, "必须 return_status=True"

    def test_kick_helper_swallows_exceptions(self):
        """helper 必须吞异常(不传播),只 logger.warning 让 reconcile 兜底。"""
        from autoteam import manager

        with patch.object(manager, "ChatGPTTeamAPI") as mock_api_cls:
            mock_api_inst = MagicMock()
            mock_api_inst.start = MagicMock()
            mock_api_inst.stop = MagicMock()
            mock_api_cls.return_value = mock_api_inst

            with patch.object(manager, "remove_from_team", side_effect=RuntimeError("net err")):
                # helper 不应传播 RuntimeError
                # 如果传播这一行会抛 RuntimeError 测试 fail
                manager._kick_team_seat_after_oauth_failure(
                    "fail@x.com", reason="test_swallow"
                )

            # 即便 remove 抛异常,stop() 仍因 finally 被调
            assert mock_api_inst.stop.called, "remove 抛异常时 stop() 仍必须被调用"

    def test_kick_helper_logs_reason_in_warning(self, caplog):
        """helper 在 KICK 阶段失败时,warning log 必须含 reason 参数让排查能定位失败位点。"""
        from autoteam import manager

        with patch.object(manager, "ChatGPTTeamAPI") as mock_api_cls:
            # api.start() 抛异常 → 走 outer except,记 warning
            mock_api_cls.side_effect = ConnectionError("master session 失效")

            caplog.clear()
            with caplog.at_level(logging.WARNING, logger="autoteam.manager"):
                manager._kick_team_seat_after_oauth_failure(
                    "fail@x.com", reason="bundle_missing"
                )

            # warning log 必须含 reason
            warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
            assert warning_records, "helper 应该至少记一条 warning"
            joined = " ".join(r.getMessage() for r in warning_records)
            assert "bundle_missing" in joined, (
                f"warning log 必须含 reason 参数 'bundle_missing',实际: {joined}"
            )
            assert "fail@x.com" in joined, "warning log 必须含 email,便于事后排查"


# =====================================================================
# 集成:_run_post_register_oauth 的 4 个失败位点必调 helper
# =====================================================================


class TestRunPostRegisterOauthKicksWs:
    """4 个失败位点(bundle_missing / plan_unsupported / register_blocked_phone /
    register_blocked_unexpected)必须调 _kick_team_seat_after_oauth_failure。"""

    def test_run_post_register_oauth_bundle_missing_kicks_ws(self, tmp_path, monkeypatch):
        """bundle_missing 分支(login_codex_via_browser 返回 None)必调 helper,reason=bundle_missing。"""
        from autoteam import accounts as accounts_mod
        from autoteam import manager

        accounts_file = tmp_path / "accounts.json"
        monkeypatch.setattr(accounts_mod, "ACCOUNTS_FILE", accounts_file)

        accounts_mod.save_accounts([{
            "email": "bm@x.com",
            "password": "pwd",
            "status": accounts_mod.STATUS_PENDING,
            "auth_file": None,
            "cloudmail_account_id": None,
            "workspace_account_id": "test-master",
            "created_at": time.time(),
        }])

        with patch.object(manager, "ChatGPTTeamAPI") as mock_api_cls:
            mock_api_inst = MagicMock()
            mock_api_inst.start = MagicMock()
            mock_api_inst.stop = MagicMock()
            mock_api_cls.return_value = mock_api_inst

            with patch(
                "autoteam.master_health.is_master_subscription_healthy",
                return_value=(True, "active", {}),
            ):
                with patch.object(manager, "login_codex_via_browser", return_value=None):
                    with patch.object(manager, "get_chatgpt_account_id", return_value="test-master"):
                        with patch.object(manager, "_kick_team_seat_after_oauth_failure") as mock_kick:
                            out = {}
                            result = manager._run_post_register_oauth(
                                email="bm@x.com",
                                password="pwd",
                                mail_client=MagicMock(),
                                leave_workspace=False,
                                out_outcome=out,
                            )

        # ★ 关键断言:helper 必须被调,reason=bundle_missing
        mock_kick.assert_called_once()
        call = mock_kick.call_args
        assert call.args[0] == "bm@x.com" or call.kwargs.get("email") == "bm@x.com"
        assert call.kwargs.get("reason") == "bundle_missing", (
            f"bundle_missing 分支 reason 必须为 'bundle_missing',实际: {call.kwargs.get('reason')}"
        )

        # 行为不变:仍然 return email + outcome=team_auth_missing
        assert result == "bm@x.com"
        assert out.get("status") == "team_auth_missing"

        # 状态仍然是 AUTH_INVALID(防回归)
        reloaded = accounts_mod.load_accounts()
        rec = next(a for a in reloaded if a["email"] == "bm@x.com")
        assert rec["status"] == accounts_mod.STATUS_AUTH_INVALID

    def test_run_post_register_oauth_plan_unsupported_kicks_ws(self, tmp_path, monkeypatch):
        """plan_unsupported 分支(bundle.plan_type 不在白名单)必调 helper,reason=plan_unsupported。"""
        from autoteam import accounts as accounts_mod
        from autoteam import manager

        accounts_file = tmp_path / "accounts.json"
        monkeypatch.setattr(accounts_mod, "ACCOUNTS_FILE", accounts_file)

        accounts_mod.save_accounts([{
            "email": "pu@x.com",
            "password": "pwd",
            "status": accounts_mod.STATUS_PENDING,
            "auth_file": None,
            "cloudmail_account_id": None,
            "workspace_account_id": "test-master",
            "created_at": time.time(),
        }])

        # 模拟 free plan(不在 Team 白名单里 → plan_supported=False)
        # is_supported_plan("free") 返回 False
        fake_bundle = {
            "plan_type": "free",
            "plan_type_raw": "self_serve_free",
            "plan_supported": False,
            "access_token": "tok",
            "account_id": "acc",
        }

        with patch.object(manager, "ChatGPTTeamAPI") as mock_api_cls:
            mock_api_inst = MagicMock()
            mock_api_inst.start = MagicMock()
            mock_api_inst.stop = MagicMock()
            mock_api_cls.return_value = mock_api_inst

            with patch(
                "autoteam.master_health.is_master_subscription_healthy",
                return_value=(True, "active", {}),
            ):
                with patch.object(manager, "login_codex_via_browser", return_value=fake_bundle):
                    with patch.object(manager, "get_chatgpt_account_id", return_value="test-master"):
                        with patch.object(
                            manager, "save_auth_file", return_value=str(tmp_path / "fake_auth.json")
                        ):
                            with patch.object(manager, "_kick_team_seat_after_oauth_failure") as mock_kick:
                                out = {}
                                result = manager._run_post_register_oauth(
                                    email="pu@x.com",
                                    password="pwd",
                                    mail_client=MagicMock(),
                                    leave_workspace=False,
                                    out_outcome=out,
                                )

        # ★ 关键断言:helper 必须被调,reason=plan_unsupported
        mock_kick.assert_called_once()
        call = mock_kick.call_args
        assert call.kwargs.get("reason") == "plan_unsupported", (
            f"plan_unsupported 分支 reason 必须为 'plan_unsupported',实际: {call.kwargs.get('reason')}"
        )

        # 行为不变:return None + outcome=plan_unsupported
        assert result is None
        assert out.get("status") == "plan_unsupported"

        # 状态 AUTH_INVALID + 保留 auth_file 供调试
        reloaded = accounts_mod.load_accounts()
        rec = next(a for a in reloaded if a["email"] == "pu@x.com")
        assert rec["status"] == accounts_mod.STATUS_AUTH_INVALID
