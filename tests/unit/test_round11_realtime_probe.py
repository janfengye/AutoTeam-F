"""Round 11 — 子号实时探活 API endpoint(直接调函数,避免 httpx 依赖)。

覆盖:
  - POST /api/accounts/{email}/probe — force 探活
  - GET /api/accounts/{email}/models — 拿可用模型
  - 错误路径 (404 / 422 / 401 / 503)
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    fake_acc = tmp_path / "accounts.json"
    monkeypatch.setattr("autoteam.accounts.ACCOUNTS_FILE", fake_acc)
    monkeypatch.setattr(
        "autoteam.admin_state.get_admin_email",
        lambda: "admin@example.com",
    )
    yield


def _seed_account(tmp_path, email="sub1@x.com", auth_filename="auth.json"):
    """seed 一个子号 + auth_file。"""
    from autoteam.accounts import save_accounts
    auth_path = tmp_path / auth_filename
    auth_path.write_text(json.dumps({
        "type": "codex",
        "access_token": "ACCESS-TOKEN-X",
        "id_token": "id-token",
        "account_id": "ws-test",
    }))
    save_accounts([
        {
            "email": email,
            "password": "p",
            "status": "active",
            "auth_file": str(auth_path),
            "workspace_account_id": "ws-test",
        },
    ])
    return auth_path


def test_probe_endpoint_account_not_found_404():
    from autoteam.api import ProbeAccountParams, post_account_probe

    with pytest.raises(HTTPException) as exc:
        post_account_probe("nonexistent@x.com", ProbeAccountParams())
    assert exc.value.status_code == 404


def test_probe_endpoint_no_auth_file_422(tmp_path):
    from autoteam.accounts import save_accounts
    from autoteam.api import ProbeAccountParams, post_account_probe

    save_accounts([
        {"email": "a@x.com", "status": "pending", "auth_file": None},
    ])
    with pytest.raises(HTTPException) as exc:
        post_account_probe("a@x.com", ProbeAccountParams())
    assert exc.value.status_code == 422
    assert exc.value.detail["error"] == "auth_file_missing"


def test_probe_endpoint_runs_smoke_and_persists_check_at(tmp_path, monkeypatch):
    """成功路径:落 last_quota_check_at + 返回 smoke_result。"""
    _seed_account(tmp_path, "probe1@x.com")

    def fake_check(token, account_id=None):
        return "ok", {"primary_pct": 30, "primary_resets_at": 1700000000, "weekly_pct": 5}

    def fake_smoke(token, account_id=None, *, model="gpt-5", max_output_tokens=64, timeout=15.0, force=False):
        return "alive", {"model": model, "response_text": "ok ack", "raw_event": "response.completed"}

    monkeypatch.setattr("autoteam.codex_auth.check_codex_quota", fake_check)
    monkeypatch.setattr("autoteam.codex_auth.cheap_codex_smoke", fake_smoke)

    from autoteam.api import ProbeAccountParams, post_account_probe

    body = post_account_probe("probe1@x.com", ProbeAccountParams(force_codex_smoke=True))
    assert body["email"] == "probe1@x.com"
    assert body["status_before"] == "active"
    assert body["quota_status"] == "ok"
    assert body["smoke_result"] == "alive"
    assert isinstance(body["smoke_detail"], dict)
    assert body["smoke_detail"]["response_text"] == "ok ack"
    assert body["last_quota_check_at"] is not None

    # accounts.json 中 last_quota_check_at 已落盘
    from autoteam.accounts import find_account, load_accounts
    acc = find_account(load_accounts(), "probe1@x.com")
    assert acc.get("last_quota_check_at") is not None


def test_probe_endpoint_swallows_smoke_exception(tmp_path, monkeypatch):
    """smoke 抛异常不传播 — 返回 200 但 smoke_result=uncertain。"""
    _seed_account(tmp_path, "probe2@x.com")

    def fake_check(token, account_id=None):
        return "ok", {"primary_pct": 0}

    def fake_smoke_raises(*args, **kwargs):
        raise RuntimeError("network blip")

    monkeypatch.setattr("autoteam.codex_auth.check_codex_quota", fake_check)
    monkeypatch.setattr("autoteam.codex_auth.cheap_codex_smoke", fake_smoke_raises)

    from autoteam.api import ProbeAccountParams, post_account_probe

    body = post_account_probe("probe2@x.com", ProbeAccountParams())
    assert body["smoke_result"] == "uncertain"


def test_models_endpoint_returns_list(tmp_path, monkeypatch):
    _seed_account(tmp_path, "models1@x.com")

    fake_resp = type("R", (), {})()
    fake_resp.status_code = 200
    fake_resp.text = ""
    fake_resp.json = lambda: {
        "models": [
            {"slug": "gpt-5", "name": "GPT-5", "description": "default"},
            {"slug": "gpt-5-team", "name": "GPT-5 Team", "description": "team only"},
        ],
        "category": "team",
    }

    monkeypatch.setattr("requests.get", lambda *a, **kw: fake_resp)
    from autoteam.api import get_account_models
    body = get_account_models("models1@x.com")
    assert body["email"] == "models1@x.com"
    assert len(body["models"]) == 2
    assert body["models"][0]["slug"] == "gpt-5"
    assert body["plan_type"] == "team"


def test_models_endpoint_401_returns_auth_invalid(tmp_path, monkeypatch):
    _seed_account(tmp_path, "models2@x.com")

    fake_resp = type("R", (), {})()
    fake_resp.status_code = 401
    fake_resp.text = "Unauthorized"

    monkeypatch.setattr("requests.get", lambda *a, **kw: fake_resp)
    from autoteam.api import get_account_models
    with pytest.raises(HTTPException) as exc:
        get_account_models("models2@x.com")
    assert exc.value.status_code == 401
    assert exc.value.detail["error"] == "auth_invalid"


def test_models_endpoint_account_not_found_404():
    from autoteam.api import get_account_models
    with pytest.raises(HTTPException) as exc:
        get_account_models("nonexistent@x.com")
    assert exc.value.status_code == 404


def test_models_endpoint_timeout_503(tmp_path, monkeypatch):
    _seed_account(tmp_path, "models3@x.com")
    import requests

    def raise_timeout(*a, **kw):
        raise requests.exceptions.Timeout("slow")

    monkeypatch.setattr("requests.get", raise_timeout)

    from autoteam.api import get_account_models
    with pytest.raises(HTTPException) as exc:
        get_account_models("models3@x.com")
    assert exc.value.status_code == 503
