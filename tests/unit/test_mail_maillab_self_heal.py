"""SPEC-1 §5.1 — maillab 401 自愈测试。

覆盖 3 个场景:
  1. 业务方法收 401 → 自动 re-login → 重试成功
  2. 重 login 后仍 401 → 抛 MaillabAuthFailed,不无限循环
  3. login() 内部 _post 收 401 → 不递归触发自愈,抛"maillab 登录失败"
"""

from __future__ import annotations

import pytest

from autoteam.mail import maillab as mod


def _make_client(monkeypatch):
    monkeypatch.setenv("MAILLAB_API_URL", "http://m.example.com")
    monkeypatch.setenv("MAILLAB_USERNAME", "admin@x.com")
    monkeypatch.setenv("MAILLAB_PASSWORD", "p")
    return mod.MaillabClient()


class _FakeSession:
    """链式响应 mock。每次 get/post/delete/put 返回 responses 列表的下一项。"""

    def __init__(self, responses):
        self._iter = iter(responses)
        self.calls = []

    def _next(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return next(self._iter)

    def get(self, url, **kw):
        return self._next("GET", url, **kw)

    def post(self, url, **kw):
        return self._next("POST", url, **kw)

    def delete(self, url, **kw):
        return self._next("DELETE", url, **kw)

    def put(self, url, **kw):
        return self._next("PUT", url, **kw)


class _R:
    def __init__(self, code_field, status_code=200, data=None):
        self.status_code = status_code
        self.text = ""
        self._payload = {"code": code_field}
        if data is not None:
            self._payload["data"] = data

    def json(self):
        return self._payload


def test_401_self_heal_then_success(monkeypatch):
    """业务调用收 401 → re-login → 重试 → 成功。
    用 _delete (单次往返) 替代 list_accounts (会翻页) 来验证装饰器路径。
    """
    client = _make_client(monkeypatch)
    client.token = "stale-jwt"
    # 1) account/delete 401  →  2) /login 200 (返回新 token)  →  3) account/delete 200
    client.session = _FakeSession([
        _R(401),                                          # 第一次 _delete
        _R(200, data={"token": "new-jwt"}),               # /login
        _R(200),                                          # 重试成功
    ])

    # 直接调装饰过的 _delete,绕开 list_accounts/翻页/_resolve_account_id 复杂度
    resp = client._delete("/account/delete", params={"accountId": 42})
    assert resp.get("code") == 200
    methods = [c[0] for c in client.session.calls]
    assert methods == ["DELETE", "POST", "DELETE"]
    assert client.token == "new-jwt"


def test_401_repeated_raises_auth_failed(monkeypatch):
    """重 login 后仍 401 → 抛 MaillabAuthFailed。"""
    client = _make_client(monkeypatch)
    client.token = "stale-jwt"
    client.session = _FakeSession([
        _R(401),                                          # 第一次 401
        _R(200, data={"token": "new-jwt"}),               # /login 成功
        _R(401),                                          # 重试仍 401
    ])
    with pytest.raises(mod.MaillabAuthFailed, match="重 login 后仍 401"):
        client._delete("/account/delete", params={"accountId": 42})


def test_login_internal_no_recursion(monkeypatch):
    """login() 内部 _post 即便回 401,_with_login_retry 不应递归触发外层自愈。"""
    client = _make_client(monkeypatch)
    client.token = None
    # /login 直接回 {code:401} — 应抛"maillab 登录失败",而非循环
    client.session = _FakeSession([
        _R(401, data={"message": "凭据错误"}),
    ])
    with pytest.raises(Exception, match="maillab 登录失败"):
        client.login()
    # 验证仅 1 次 POST 调用,没有递归
    assert len(client.session.calls) == 1
    assert client.session.calls[0][0] == "POST"
