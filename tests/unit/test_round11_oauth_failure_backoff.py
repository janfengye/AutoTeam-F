"""Round 11 — OAuth 失败 backoff + status 一致性修复测试。

覆盖两个修复:

  修复 1 (manager.py:1987):
    `_run_post_register_oauth` Team 分支 OAuth bundle 缺失分支,
    status 写入由 STATUS_ACTIVE 改为 STATUS_AUTH_INVALID。
    与同函数其他失败路径保持一致(都是 AUTH_INVALID),让 reconcile 能接管,
    避免半残账号(无 auth_file)被 active 计数器误算导致每 30 分钟重复触发 fill。

  修复 2 (api.py:_auto_check_loop):
    在 cooldown 通过(cooldown_remaining <= 0)后、playwright lock 获取前,
    新增 OAuth 失败 backoff 检查 — 最近 2h 内 master workspace 累积 ≥3 个
    auth_invalid 账号 → 强制延长冷却到 4h,避免每 30 分钟无脑触发 fill 浪费
    cloudmail 邮箱 + 累积更多僵尸。

测试用例:
  1. test_run_post_register_oauth_team_no_bundle_marks_auth_invalid — 修复 1
  2. test_auto_check_loop_oauth_backoff_triggers_when_3_recent_failures — 修复 2 触发
  3. test_auto_check_loop_oauth_backoff_skipped_when_failures_old — 修复 2 时间窗口
  4. test_auto_check_loop_oauth_backoff_skipped_when_only_2_failures — 修复 2 阈值
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

# =====================================================================
# 修复 1 — manager.py:1987 status=AUTH_INVALID 一致性
# =====================================================================


class TestRunPostRegisterOauthTeamNoBundle:
    """SPEC-2 §3.1 + 修复 1 — OAuth bundle 缺失时 status=AUTH_INVALID(原误用 ACTIVE)"""

    def test_run_post_register_oauth_team_no_bundle_marks_auth_invalid(self, tmp_path, monkeypatch):
        """Team 分支 login_codex_via_browser 返回 None → update_account(status=AUTH_INVALID),
        且仍然 return email 让 cmd_fill produced+=1 计数生效。"""
        from autoteam import accounts as accounts_mod
        from autoteam import manager

        accounts_file = tmp_path / "accounts.json"
        monkeypatch.setattr(accounts_mod, "ACCOUNTS_FILE", accounts_file)

        # 先放一条 PENDING 记录,让 update_account 能找到
        accounts_mod.save_accounts([{
            "email": "team_no_bundle@x.com",
            "password": "pwd",
            "status": accounts_mod.STATUS_PENDING,
            "auth_file": None,
            "cloudmail_account_id": None,
            "workspace_account_id": "test-master",
            "created_at": time.time(),
        }])

        # mock chatgpt_api / master_health / login 返回 None / get_chatgpt_account_id
        # 让流程走到 1987 行的"bundle is None"兜底分支
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
                        out = {}
                        result = manager._run_post_register_oauth(
                            email="team_no_bundle@x.com",
                            password="pwd",
                            mail_client=MagicMock(),
                            leave_workspace=False,
                            out_outcome=out,
                        )

        # 仍然 return email 让 cmd_fill 按席位计数 +1(行为不变)
        assert result == "team_no_bundle@x.com"
        # outcome 仍打 team_auth_missing(行为不变)
        assert out.get("status") == "team_auth_missing"

        # ★ 修复 1 关键断言:status 必须是 AUTH_INVALID 不是 ACTIVE
        reloaded = accounts_mod.load_accounts()
        rec = next(a for a in reloaded if a["email"] == "team_no_bundle@x.com")
        assert rec["status"] == accounts_mod.STATUS_AUTH_INVALID, (
            "修复 1:OAuth bundle 缺失分支 status 必须是 AUTH_INVALID(原误用 ACTIVE 让"
            "auth_file 缺失的半残账号被 active 计数器忽略,导致 cmd_rotate 每 30 分钟"
            "重复触发 fill 累积僵尸账号)"
        )
        # workspace_account_id 仍写入(让 reconcile 知道哪个 master)
        assert rec.get("workspace_account_id") == "test-master"

    def test_manager_source_no_bundle_branch_uses_auth_invalid(self):
        """落地证据:manager.py 的 `bundle 缺失` 兜底分支必须用 STATUS_AUTH_INVALID(防回归)。

        通过 grep 模式匹配 1987 行附近的注释 + status= 关键字面量,确保不会被误改回。
        """
        from pathlib import Path
        src = Path(__file__).parent.parent.parent / "src" / "autoteam" / "manager.py"
        content = src.read_text(encoding="utf-8")
        # 锚:Round 11 修复注释段(独一无二)
        anchor = "Round 11 — OAuth bundle 缺失分支"
        assert anchor in content, "Round 11 修复注释丢失,可能被回滚"

        # 找到 anchor 之后第一个 update_account(...) 行,断言 status=STATUS_AUTH_INVALID
        idx = content.find(anchor)
        assert idx > 0
        snippet = content[idx:idx + 1200]  # 抓 anchor 之后 1.2k 字符
        assert "status=STATUS_AUTH_INVALID" in snippet, (
            "修复 1 锚位置 1200 字符内未找到 status=STATUS_AUTH_INVALID — "
            "OAuth bundle 缺失分支必须用 AUTH_INVALID"
        )
        # 同时确认这个分支不再用 STATUS_ACTIVE(防止有人加了一行 update_account(STATUS_ACTIVE,..))
        # 注意 anchor 之后可能有其他 update_account 调用,这里只检查 anchor 紧邻的代码段(700 字符)
        narrow_snippet = content[idx:idx + 700]
        assert "status=STATUS_ACTIVE" not in narrow_snippet, (
            "修复 1 锚紧邻 700 字符内出现 status=STATUS_ACTIVE — 可能被回滚"
        )


# =====================================================================
# 修复 2 — api.py:_auto_check_loop OAuth 失败 backoff
# =====================================================================
#
# 直接测 _auto_check_loop 后台线程比较复杂(需要 mock 整个事件循环 + threading)。
# 改为直接复刻 backoff 决策段,用纯 Python 等价代码验证决策路径,
# 避开 FastAPI 启动 / threading 复杂度。这与 round6 patches 的测试策略一致(见
# TestDeleteBatchAllPersonal._run_batch_directly 注释)。
#
# 关键决策段(从 api.py:2858+ 复刻):
#   else:                                               # cooldown 通过
#       backoff_triggered = False
#       try:
#           master_aid = get_chatgpt_account_id() or ""
#           recent_window = 2 * 3600
#           recent_failures = [a for a in accounts ...]
#           if len(recent_failures) >= 3:
#               _auto_fill_last_trigger_ts = now_ts - _AUTO_FILL_COOLDOWN_SECONDS + 4 * 3600
#               backoff_triggered = True
#       except Exception: pass
#       if backoff_triggered:
#           continue
#       # ... fill 触发逻辑 ...


class TestAutoCheckLoopOauthBackoff:
    """修复 2 — OAuth 连续失败 4 小时 backoff 决策路径"""

    def _evaluate_backoff(
        self,
        accounts,
        master_aid,
        now_ts,
        last_trigger_ts,
        cooldown_seconds,
        threshold=3,
        recent_window_seconds=2 * 3600,
    ):
        """复刻 api.py:_auto_check_loop 的 backoff 决策逻辑。

        返回:(backoff_triggered, new_last_trigger_ts, recent_failures_count)
        """
        from autoteam.accounts import STATUS_AUTH_INVALID

        # 第一步:cooldown 检查(对应 api.py:2848-2857)
        cooldown_remaining = (last_trigger_ts + cooldown_seconds) - now_ts
        if cooldown_remaining > 0:
            # 走 cooldown 分支 — backoff 检查不触发
            return False, last_trigger_ts, 0

        # 第二步:OAuth 失败 backoff 检查(对应 api.py:2858+)
        try:
            recent_failures = [
                a for a in accounts
                if a.get("status") == STATUS_AUTH_INVALID
                and (a.get("workspace_account_id") or "") == master_aid
                and (a.get("created_at") or 0) >= now_ts - recent_window_seconds
            ]
            if len(recent_failures) >= threshold:
                new_ts = now_ts - cooldown_seconds + 4 * 3600
                return True, new_ts, len(recent_failures)
        except Exception:
            pass

        return False, last_trigger_ts, 0

    def test_auto_check_loop_oauth_backoff_triggers_when_3_recent_failures(self):
        """3 个最近 2h 内 auth_invalid + ws=master → backoff 触发,
        last_trigger_ts 推进到未来 4h(下次巡检 cooldown_remaining 仍 > 0)。"""
        from autoteam.accounts import STATUS_ACTIVE, STATUS_AUTH_INVALID

        now_ts = time.time()
        master_aid = "master-uuid-aaaa"
        accounts = [
            # 2 active 数量 — 触发 active < HARD_CAP=4 条件
            {"email": "a1@x.com", "status": STATUS_ACTIVE, "auth_file": "auths/a1.json",
             "workspace_account_id": master_aid, "created_at": now_ts - 86400},
            {"email": "a2@x.com", "status": STATUS_ACTIVE, "auth_file": "auths/a2.json",
             "workspace_account_id": master_aid, "created_at": now_ts - 86400},
            # 3 个最近 2h 内 OAuth 失败(auth_invalid + ws=master)— 触发 backoff
            {"email": "fail1@x.com", "status": STATUS_AUTH_INVALID,
             "workspace_account_id": master_aid, "created_at": now_ts - 1800},
            {"email": "fail2@x.com", "status": STATUS_AUTH_INVALID,
             "workspace_account_id": master_aid, "created_at": now_ts - 3000},
            {"email": "fail3@x.com", "status": STATUS_AUTH_INVALID,
             "workspace_account_id": master_aid, "created_at": now_ts - 5400},
        ]

        # cooldown 已过去(last_trigger_ts 是 1 小时前)
        last_trigger_ts = now_ts - 3600
        cooldown_seconds = 1800  # 30 min

        triggered, new_ts, failures = self._evaluate_backoff(
            accounts, master_aid, now_ts, last_trigger_ts, cooldown_seconds,
        )

        assert triggered is True, "3 个最近 OAuth 失败应触发 backoff"
        assert failures == 3
        # 关键:new_ts 必须让下一次 cooldown_remaining > 0(确保 4h 内不再触发 fill)
        # 即 (new_ts + cooldown_seconds) - now_ts > 0 要够大(实际差额 ≈ 4h)
        cooldown_extension = (new_ts + cooldown_seconds) - now_ts
        assert cooldown_extension >= 3.5 * 3600, (
            f"backoff 后 cooldown 扩展应 ≥ 3.5h,实际 {cooldown_extension/3600:.2f}h"
        )
        # backoff 触发后的下一次巡检,cooldown_remaining > 0,fill 不会启动 — 复刻验证
        next_cooldown_remaining = (new_ts + cooldown_seconds) - now_ts
        assert next_cooldown_remaining > 0, "下一次巡检 cooldown 必须仍生效"

    def test_auto_check_loop_oauth_backoff_skipped_when_failures_old(self):
        """3 个 auth_invalid 但都是 3 小时前(超出 2h 窗口)→ backoff 不触发。"""
        from autoteam.accounts import STATUS_AUTH_INVALID

        now_ts = time.time()
        master_aid = "master-uuid-bbbb"
        # 全部超出 2h 窗口(3 小时前)
        accounts = [
            {"email": "old1@x.com", "status": STATUS_AUTH_INVALID,
             "workspace_account_id": master_aid, "created_at": now_ts - 3 * 3600},
            {"email": "old2@x.com", "status": STATUS_AUTH_INVALID,
             "workspace_account_id": master_aid, "created_at": now_ts - 4 * 3600},
            {"email": "old3@x.com", "status": STATUS_AUTH_INVALID,
             "workspace_account_id": master_aid, "created_at": now_ts - 5 * 3600},
        ]
        last_trigger_ts = now_ts - 3600
        cooldown_seconds = 1800

        triggered, new_ts, failures = self._evaluate_backoff(
            accounts, master_aid, now_ts, last_trigger_ts, cooldown_seconds,
        )

        assert triggered is False, "超出 2h 窗口的旧失败不应触发 backoff"
        assert failures == 0, "时间窗口外的 auth_invalid 不应被计入"
        # last_trigger_ts 不变,fill 应正常启动(走原路径)
        assert new_ts == last_trigger_ts

    def test_auto_check_loop_oauth_backoff_skipped_when_only_2_failures(self):
        """只有 2 个最近 2h auth_invalid(< 阈值 3)→ backoff 不触发。

        阈值严格:必须 ≥3 才触发,2 个不够(避免误伤偶发的 1-2 次失败)。
        """
        from autoteam.accounts import STATUS_AUTH_INVALID

        now_ts = time.time()
        master_aid = "master-uuid-cccc"
        # 只有 2 个最近失败(刚好够 1 小时内)
        accounts = [
            {"email": "f1@x.com", "status": STATUS_AUTH_INVALID,
             "workspace_account_id": master_aid, "created_at": now_ts - 1200},
            {"email": "f2@x.com", "status": STATUS_AUTH_INVALID,
             "workspace_account_id": master_aid, "created_at": now_ts - 1500},
        ]
        last_trigger_ts = now_ts - 3600
        cooldown_seconds = 1800

        triggered, new_ts, failures = self._evaluate_backoff(
            accounts, master_aid, now_ts, last_trigger_ts, cooldown_seconds,
        )

        assert triggered is False, "2 个失败 < 阈值 3,backoff 不应触发"
        assert failures == 0, "未达阈值时 recent_failures 计数也不应返回"
        assert new_ts == last_trigger_ts, "未触发 backoff 时 last_trigger_ts 不变"

    def test_auto_check_loop_oauth_backoff_skipped_when_failures_other_workspace(self):
        """3 个最近 auth_invalid 但属于 *别的* workspace → backoff 不触发(workspace 隔离)。

        防回归:确保 backoff 不会被其他 master 的失败干扰。
        """
        from autoteam.accounts import STATUS_AUTH_INVALID

        now_ts = time.time()
        master_aid = "current-master"
        other_master = "other-master"
        accounts = [
            # 3 个最近失败但都属于其他 master
            {"email": "om1@x.com", "status": STATUS_AUTH_INVALID,
             "workspace_account_id": other_master, "created_at": now_ts - 1000},
            {"email": "om2@x.com", "status": STATUS_AUTH_INVALID,
             "workspace_account_id": other_master, "created_at": now_ts - 2000},
            {"email": "om3@x.com", "status": STATUS_AUTH_INVALID,
             "workspace_account_id": other_master, "created_at": now_ts - 3000},
        ]
        last_trigger_ts = now_ts - 3600
        cooldown_seconds = 1800

        triggered, new_ts, failures = self._evaluate_backoff(
            accounts, master_aid, now_ts, last_trigger_ts, cooldown_seconds,
        )

        assert triggered is False, "其他 workspace 的失败不应触发当前 master 的 backoff"
        assert failures == 0, "workspace 不匹配时计数为 0"

    def test_api_source_contains_backoff_logic(self):
        """落地证据:api.py 必须包含 backoff 检查实现(防回归)。"""
        from pathlib import Path
        src = Path(__file__).parent.parent.parent / "src" / "autoteam" / "api.py"
        content = src.read_text(encoding="utf-8")

        # Round 11 backoff 注释锚(独一无二)
        assert "Round 11 — OAuth 连续失败 backoff" in content, (
            "api.py 缺失 Round 11 OAuth 失败 backoff 检查代码段"
        )
        # 阈值 3
        assert "len(recent_failures) >= 3" in content, "backoff 阈值 3 缺失"
        # 2 小时窗口
        assert "recent_window = 2 * 3600" in content, "2 小时时间窗口缺失"
        # 4 小时延长
        assert "4 * 3600" in content, "4 小时冷却扩展缺失"
