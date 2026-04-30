"""Round 11 — master_health subscription_grace healthy=True 状态。

覆盖 Approach A 决策:`eligible_for_auto_reactivation=true` 时,先解 admin id_token
JWT chatgpt_subscription_active_until,grace 期内 → (True, "subscription_grace", evidence)
healthy=True;过期 / JWT 缺失 → (False, "subscription_cancelled", ...) 保留旧行为。

不变量:
  M-I3:healthy ⇔ reason ∈ {"active", "subscription_grace"}(双向蕴含扩展)
  M-I7:`eligible_for_auto_reactivation` 严格 `is True` 比对(不变)
"""
from __future__ import annotations

import base64
import json
import time

import pytest


def _make_id_token_jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(b"sig").rstrip(b"=").decode()
    return f"{header}.{body}.{sig}"


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    fake_cache = tmp_path / "master_health_cache.json"
    monkeypatch.setattr("autoteam.master_health.CACHE_FILE", fake_cache)
    fake_acc = tmp_path / "accounts.json"
    monkeypatch.setattr("autoteam.accounts.ACCOUNTS_FILE", fake_acc)
    fake_acc_dir = tmp_path / "accounts"
    fake_acc_dir.mkdir(exist_ok=True)
    monkeypatch.setattr("autoteam.master_health.ACCOUNTS_DIR", fake_acc_dir)
    monkeypatch.setattr(
        "autoteam.admin_state.get_chatgpt_account_id", lambda: "test-master",
    )
    yield


class _StubAPI:
    def __init__(self, items, status=200):
        self.browser = object()
        self._items = items
        self._status = status

    def _api_fetch(self, method, path):
        if path == "/backend-api/accounts":
            return {"status": self._status, "body": json.dumps({"items": self._items})}
        return {"status": 404, "body": ""}

    def stop(self):
        self.browser = None


def _seed_admin_codex_main(tmp_dir, id_token):
    """生成 accounts/codex-main-*.json 让 _load_admin_id_token 能拿到。"""
    auth_path = tmp_dir / "codex-main-test-master.json"
    auth_path.write_text(json.dumps({
        "type": "codex",
        "access_token": "ACC",
        "id_token": id_token,
        "account_id": "test-master",
        "email": "admin@example.com",
    }))
    return auth_path


def test_grace_period_returns_healthy_subscription_grace(tmp_path):
    """grace_until 在 30 天后 → healthy=True, reason=subscription_grace,evidence 含 grace 字段。"""
    from autoteam.master_health import is_master_subscription_healthy

    future = time.time() + 30 * 86400
    id_token = _make_id_token_jwt({
        "https://api.openai.com/auth": {
            "chatgpt_subscription_active_until": future,
        },
    })
    accounts_dir = tmp_path / "accounts"
    _seed_admin_codex_main(accounts_dir, id_token)

    api = _StubAPI([
        {
            "id": "test-master",
            "structure": "workspace",
            "current_user_role": "account-owner",
            "eligible_for_auto_reactivation": True,
        },
    ])

    healthy, reason, evidence = is_master_subscription_healthy(api, cache_ttl=0)
    assert healthy is True
    assert reason == "subscription_grace"
    assert evidence.get("grace_until") is not None
    assert float(evidence["grace_until"]) == pytest.approx(future, abs=2)
    assert evidence.get("grace_remain_seconds") is not None
    assert evidence["grace_remain_seconds"] > 0


def test_grace_expired_falls_back_subscription_cancelled(tmp_path):
    """grace_until 已过期 → healthy=False, reason=subscription_cancelled。"""
    from autoteam.master_health import is_master_subscription_healthy

    past = time.time() - 1000
    id_token = _make_id_token_jwt({
        "https://api.openai.com/auth": {
            "chatgpt_subscription_active_until": past,
        },
    })
    accounts_dir = tmp_path / "accounts"
    _seed_admin_codex_main(accounts_dir, id_token)

    api = _StubAPI([
        {
            "id": "test-master",
            "structure": "workspace",
            "current_user_role": "account-owner",
            "eligible_for_auto_reactivation": True,
        },
    ])

    healthy, reason, evidence = is_master_subscription_healthy(api, cache_ttl=0)
    assert healthy is False
    assert reason == "subscription_cancelled"
    # cancelled 路径 evidence 也带 grace_until(已过期)
    assert evidence.get("grace_until") is not None
    assert float(evidence["grace_until"]) == pytest.approx(past, abs=2)


def test_no_admin_id_token_falls_back_subscription_cancelled(tmp_path):
    """accounts/codex-main-*.json 不存在 → _load_admin_id_token=None → cancelled(无 grace_until)。"""
    from autoteam.master_health import is_master_subscription_healthy

    # 不创建 codex-main 文件
    api = _StubAPI([
        {
            "id": "test-master",
            "structure": "workspace",
            "current_user_role": "account-owner",
            "eligible_for_auto_reactivation": True,
        },
    ])

    healthy, reason, evidence = is_master_subscription_healthy(api, cache_ttl=0)
    assert healthy is False
    assert reason == "subscription_cancelled"
    # 没 id_token 解 → grace_until=None,evidence 不带 grace_until 键(_build_evidence 删 None 值)
    assert evidence.get("grace_until") is None


def test_active_path_unchanged_no_grace_lookup(tmp_path):
    """eligible=False → reason=active,与 Round 8 行为不变,即使有 admin id_token。"""
    from autoteam.master_health import is_master_subscription_healthy

    future = time.time() + 30 * 86400
    id_token = _make_id_token_jwt({
        "https://api.openai.com/auth": {
            "chatgpt_subscription_active_until": future,
        },
    })
    accounts_dir = tmp_path / "accounts"
    _seed_admin_codex_main(accounts_dir, id_token)

    api = _StubAPI([
        {
            "id": "test-master",
            "structure": "workspace",
            "current_user_role": "account-owner",
            "eligible_for_auto_reactivation": False,
        },
    ])

    healthy, reason, _ev = is_master_subscription_healthy(api, cache_ttl=0)
    assert healthy is True
    assert reason == "active"


def test_m_i3_guard_allows_subscription_grace_in_cache(tmp_path):
    """cache 写盘后再读出 — subscription_grace 必须不被 M-I3 守卫降级为 network_error。"""
    from autoteam.master_health import is_master_subscription_healthy

    future = time.time() + 30 * 86400
    id_token = _make_id_token_jwt({
        "https://api.openai.com/auth": {
            "chatgpt_subscription_active_until": future,
        },
    })
    accounts_dir = tmp_path / "accounts"
    _seed_admin_codex_main(accounts_dir, id_token)

    api = _StubAPI([
        {
            "id": "test-master",
            "structure": "workspace",
            "current_user_role": "account-owner",
            "eligible_for_auto_reactivation": True,
        },
    ])
    # 第一次:实测,写 cache
    healthy1, reason1, _ev1 = is_master_subscription_healthy(api, cache_ttl=300)
    assert healthy1 is True and reason1 == "subscription_grace"

    # 第二次:命中 cache,健康守卫不应降级
    healthy2, reason2, ev2 = is_master_subscription_healthy(api, cache_ttl=300)
    assert healthy2 is True
    assert reason2 == "subscription_grace"
    assert ev2.get("cache_hit") is True
    # cache 命中也要还原 grace_until
    assert ev2.get("grace_until") is not None


def test_apply_master_degraded_classification_revert_on_grace(tmp_path):
    """子号 GRACE + master 探针返回 subscription_grace → 撤回 GRACE → ACTIVE(母号 grace 期 healthy)。"""
    from autoteam.accounts import (
        STATUS_ACTIVE,
        STATUS_DEGRADED_GRACE,
        load_accounts,
        save_accounts,
    )
    from autoteam.master_health import _apply_master_degraded_classification

    future = time.time() + 30 * 86400
    id_token = _make_id_token_jwt({
        "https://api.openai.com/auth": {
            "chatgpt_subscription_active_until": future,
        },
    })
    accounts_dir = tmp_path / "accounts"
    _seed_admin_codex_main(accounts_dir, id_token)

    api = _StubAPI([
        {
            "id": "test-master",
            "structure": "workspace",
            "current_user_role": "account-owner",
            "eligible_for_auto_reactivation": True,  # 母号 cancel,但 JWT grace 未过 → healthy
        },
    ])

    # seed 一个 GRACE 子号
    save_accounts([
        {
            "email": "g@x.com",
            "status": STATUS_DEGRADED_GRACE,
            "workspace_account_id": "test-master",
            "grace_until": time.time() + 1000,
            "master_account_id_at_grace": "test-master",
        },
    ])

    result = _apply_master_degraded_classification(chatgpt_api=api)
    # 在 grace 期 healthy 状态下,子号 GRACE 应该撤回到 ACTIVE
    assert "g@x.com" in result["reverted_active"]
    accs = load_accounts()
    assert accs[0]["status"] == STATUS_ACTIVE
    assert accs[0]["grace_until"] is None
    assert accs[0]["master_account_id_at_grace"] is None


def test_load_admin_id_token_picks_latest_codex_main(tmp_path):
    """_load_admin_id_token — 多个 codex-main 文件时按 mtime 取最新。"""
    from autoteam.master_health import _load_admin_id_token

    accounts_dir = tmp_path / "accounts"
    # 旧文件
    old_token = _make_id_token_jwt({"sub": "old"})
    p1 = accounts_dir / "codex-main-old.json"
    p1.write_text(json.dumps({"id_token": old_token}))
    # 改 mtime 让它在过去
    import os
    past = time.time() - 100
    os.utime(p1, (past, past))

    # 新文件
    new_token = _make_id_token_jwt({"sub": "new"})
    p2 = accounts_dir / "codex-main-new.json"
    p2.write_text(json.dumps({"id_token": new_token}))

    loaded = _load_admin_id_token()
    assert loaded == new_token


def test_load_admin_id_token_returns_none_when_no_files(tmp_path):
    """accounts/ 目录无 codex-main-*.json → _load_admin_id_token=None。"""
    from autoteam.master_health import _load_admin_id_token

    # 不创建任何 codex-main 文件
    assert _load_admin_id_token() is None


# ---------------------------------------------------------------------------
# Round 11 后续修复 — _load_admin_id_token 加 chatgpt_api 参数,优先 web access_token
# 解 grace_until,fallback 到 codex-main-*.json id_token。
# ---------------------------------------------------------------------------


class _StubChatgptApi:
    """带 access_token 的 stub,模拟 ChatGPTTeamAPI 实例。"""

    def __init__(self, access_token=None):
        self.access_token = access_token


def test_load_admin_id_token_uses_chatgpt_api_access_token_first(tmp_path):
    """chatgpt_api.access_token 存在时优先返回该 token,不去读 codex-main-*.json。"""
    from autoteam.master_health import _load_admin_id_token

    # seed codex-main 文件,但不应被读到
    accounts_dir = tmp_path / "accounts"
    fallback_token = _make_id_token_jwt({"sub": "codex-main-fallback"})
    _seed_admin_codex_main(accounts_dir, fallback_token)

    web_token = _make_id_token_jwt({
        "https://api.openai.com/auth": {
            "chatgpt_subscription_active_until": time.time() + 86400,
        },
    })
    api = _StubChatgptApi(access_token=web_token)

    loaded = _load_admin_id_token(api)
    assert loaded == web_token
    assert loaded != fallback_token


def test_load_admin_id_token_falls_back_to_codex_main_json(tmp_path):
    """chatgpt_api 没有 access_token 时,fallback 到 codex-main-*.json。"""
    from autoteam.master_health import _load_admin_id_token

    accounts_dir = tmp_path / "accounts"
    fallback_token = _make_id_token_jwt({"sub": "from-codex-main"})
    _seed_admin_codex_main(accounts_dir, fallback_token)

    # access_token=None / chatgpt_api=None 都走 fallback
    api_no_token = _StubChatgptApi(access_token=None)
    assert _load_admin_id_token(api_no_token) == fallback_token

    # chatgpt_api 缺失 attr 也得能 fallback(非 _StubChatgptApi 实例)
    class _Bare:
        pass

    assert _load_admin_id_token(_Bare()) == fallback_token

    # 默认参数也走 fallback
    assert _load_admin_id_token() == fallback_token


def test_load_admin_id_token_returns_none_when_both_missing(tmp_path):
    """chatgpt_api.access_token=None + codex-main-*.json 缺失 → None。"""
    from autoteam.master_health import _load_admin_id_token

    # 不创建 codex-main 文件,api 也没 access_token
    api = _StubChatgptApi(access_token=None)
    assert _load_admin_id_token(api) is None
    assert _load_admin_id_token(None) is None


def test_classify_l1_grace_via_chatgpt_api_access_token(tmp_path):
    """eligible=True + chatgpt_api.access_token JWT 含 grace_until > now → subscription_grace。

    根因修复回归保护:用户报 banner 误报"母号订阅 cancel"是因为 _load_admin_id_token
    只读 codex-main-*.json,而用户走 web session 路径无该文件。修复后 chatgpt_api.access_token
    被优先读取,正确返回 healthy=True。
    """
    from autoteam.master_health import is_master_subscription_healthy

    future = time.time() + 30 * 86400
    web_jwt = _make_id_token_jwt({
        "https://api.openai.com/auth": {
            "chatgpt_subscription_active_until": future,
        },
    })

    # 不创建 codex-main 文件 — 模拟用户的 web session 场景
    class _StubAPIWithToken(_StubAPI):
        def __init__(self, items, access_token):
            super().__init__(items)
            self.access_token = access_token

    api = _StubAPIWithToken(
        [
            {
                "id": "test-master",
                "structure": "workspace",
                "current_user_role": "account-owner",
                "eligible_for_auto_reactivation": True,
            },
        ],
        access_token=web_jwt,
    )

    healthy, reason, evidence = is_master_subscription_healthy(api, cache_ttl=0)
    assert healthy is True
    assert reason == "subscription_grace"
    assert evidence.get("grace_until") is not None
    assert float(evidence["grace_until"]) == pytest.approx(future, abs=2)
    assert evidence.get("grace_remain_seconds") is not None
    assert evidence["grace_remain_seconds"] > 0


# ---------------------------------------------------------------------------
# Round 11 二轮修复 — chatgpt_plan_type fallback 当 grace_until 解不出时
#
# 根因:ChatGPT web access_token 不含 chatgpt_subscription_active_until claim
# 但含 chatgpt_plan_type → grace 期内值为付费层(team / business / enterprise / edu)。
# extract_plan_type_from_jwt + _classify_l1 fallback 分支保证 web session 路径正确判定。
# ---------------------------------------------------------------------------


def test_extract_plan_type_from_jwt_returns_team():
    """payload 含 chatgpt_plan_type 字段(任意大小写) → 返回 lowercase 字符串。"""
    from autoteam.master_health import extract_plan_type_from_jwt

    # 大写 Team(测大小写归一化)
    jwt_team_caps = _make_id_token_jwt({
        "https://api.openai.com/auth": {"chatgpt_plan_type": "Team"},
    })
    assert extract_plan_type_from_jwt(jwt_team_caps) == "team"

    # 小写 team
    jwt_team_low = _make_id_token_jwt({
        "https://api.openai.com/auth": {"chatgpt_plan_type": "team"},
    })
    assert extract_plan_type_from_jwt(jwt_team_low) == "team"

    # 其他付费层
    jwt_business = _make_id_token_jwt({
        "https://api.openai.com/auth": {"chatgpt_plan_type": "BUSINESS"},
    })
    assert extract_plan_type_from_jwt(jwt_business) == "business"

    # free
    jwt_free = _make_id_token_jwt({
        "https://api.openai.com/auth": {"chatgpt_plan_type": "free"},
    })
    assert extract_plan_type_from_jwt(jwt_free) == "free"


def test_extract_plan_type_from_jwt_returns_none_when_missing():
    """字段缺失 / token 缺失 / 格式损坏 → None,不抛异常。"""
    from autoteam.master_health import extract_plan_type_from_jwt

    # token=None
    assert extract_plan_type_from_jwt(None) is None
    # 空字符串
    assert extract_plan_type_from_jwt("") is None
    # 非字符串
    assert extract_plan_type_from_jwt(12345) is None
    # 单段 token(不合法)
    assert extract_plan_type_from_jwt("badtoken") is None
    # base64 decode 失败
    assert extract_plan_type_from_jwt("xxx.!!!.zzz") is None
    # payload 不是 JSON
    import base64
    bad_payload = base64.urlsafe_b64encode(b"not json").rstrip(b"=").decode()
    assert extract_plan_type_from_jwt(f"hdr.{bad_payload}.sig") is None
    # auth claims 缺失
    jwt_no_auth = _make_id_token_jwt({"sub": "user"})
    assert extract_plan_type_from_jwt(jwt_no_auth) is None
    # auth claims 存在但无 chatgpt_plan_type
    jwt_no_plan = _make_id_token_jwt({
        "https://api.openai.com/auth": {"chatgpt_user_id": "u-1"},
    })
    assert extract_plan_type_from_jwt(jwt_no_plan) is None
    # plan_type 是空字符串 → None
    jwt_empty = _make_id_token_jwt({
        "https://api.openai.com/auth": {"chatgpt_plan_type": "  "},
    })
    assert extract_plan_type_from_jwt(jwt_empty) is None
    # plan_type 不是字符串(整数等)→ None
    jwt_non_str = _make_id_token_jwt({
        "https://api.openai.com/auth": {"chatgpt_plan_type": 1},
    })
    assert extract_plan_type_from_jwt(jwt_non_str) is None


def test_classify_l1_grace_via_plan_type_fallback_when_grace_until_missing(tmp_path):
    """eligible=True + JWT 无 chatgpt_subscription_active_until 但有 chatgpt_plan_type=team
    → (True, "subscription_grace", evidence) + evidence.plan_type_jwt="team"
    + evidence.grace_until=None(因为 JWT 没该字段)。

    回归保护:web session JWT 路径(无 grace_until,只有 plan_type)的核心修复点。
    """
    from autoteam.master_health import is_master_subscription_healthy

    # 注意:web access_token 风格 — 只有 chatgpt_plan_type,没 chatgpt_subscription_active_until
    web_jwt = _make_id_token_jwt({
        "https://api.openai.com/auth": {
            "chatgpt_plan_type": "team",
            # 故意不含 chatgpt_subscription_active_until
        },
    })

    class _StubAPIWithToken(_StubAPI):
        def __init__(self, items, access_token):
            super().__init__(items)
            self.access_token = access_token

    api = _StubAPIWithToken(
        [
            {
                "id": "test-master",
                "structure": "workspace",
                "current_user_role": "account-owner",
                "eligible_for_auto_reactivation": True,
            },
        ],
        access_token=web_jwt,
    )

    healthy, reason, evidence = is_master_subscription_healthy(api, cache_ttl=0)
    assert healthy is True
    assert reason == "subscription_grace"
    # plan_type fallback 路径,grace_until 解不出来,evidence 不包含 grace_until 键
    # (因为 _build_evidence 在 grace_until 为 None 时不写入键)
    # 但 plan_type_jwt 字段必须暴露
    assert evidence.get("plan_type_jwt") == "team"
    # grace_until 为 None → 不被加到 evidence
    assert evidence.get("grace_until") is None


def test_classify_l1_cancelled_when_plan_type_free_fallback(tmp_path):
    """eligible=True + JWT 无 grace_until + chatgpt_plan_type=free → 真 cancelled。

    回归保护:确保只有付费层才视为 grace,降级到 free 后正确判 cancelled。
    """
    from autoteam.master_health import is_master_subscription_healthy

    # web access_token 但 plan_type 已降级到 free
    web_jwt = _make_id_token_jwt({
        "https://api.openai.com/auth": {
            "chatgpt_plan_type": "free",
        },
    })

    class _StubAPIWithToken(_StubAPI):
        def __init__(self, items, access_token):
            super().__init__(items)
            self.access_token = access_token

    api = _StubAPIWithToken(
        [
            {
                "id": "test-master",
                "structure": "workspace",
                "current_user_role": "account-owner",
                "eligible_for_auto_reactivation": True,
            },
        ],
        access_token=web_jwt,
    )

    healthy, reason, evidence = is_master_subscription_healthy(api, cache_ttl=0)
    assert healthy is False
    assert reason == "subscription_cancelled"
    # plan_type_jwt="free" 必须暴露(诊断用)
    assert evidence.get("plan_type_jwt") == "free"
