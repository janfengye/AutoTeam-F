"""SPEC-1 §5.1 — cf_temp_email 嗅探收紧:正向白名单(必须含 results / address)。

round-3 漏判的"空 dict {}"也必须命中。
"""

from __future__ import annotations

import pytest

from autoteam.mail import cf_temp_email as mod


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        if self._payload is _MISSING:
            raise ValueError("non-json")
        return self._payload


_MISSING = object()


def _make_client(monkeypatch):
    monkeypatch.setenv("CLOUDMAIL_BASE_URL", "https://example.com/api")
    monkeypatch.setenv("CLOUDMAIL_PASSWORD", "secret")
    monkeypatch.setenv("CLOUDMAIL_DOMAIN", "@example.com")
    return mod.CfTempEmailClient()


@pytest.mark.parametrize(
    "body, should_raise",
    [
        ({"results": []}, False),                            # cf 正常空列表
        ({"results": [{"id": 1}]}, False),                   # cf 正常有数据
        ({}, True),                                          # 空 dict — round-3 漏判,本次必须捕获
        ({"code": 401, "message": "auth"}, True),            # maillab 风格
        ({"code": 200, "data": {}}, True),                   # maillab 风格 200
        (None, True),                                        # 非 dict
    ],
)
def test_login_sniff(monkeypatch, body, should_raise):
    """login() 必须在响应缺 `results` 时抛错(覆盖空 {} 漏判)。"""
    client = _make_client(monkeypatch)
    monkeypatch.setattr(client, "_admin_get", lambda path, params=None: _Resp(body))
    if should_raise:
        with pytest.raises(Exception, match="不像.*cloudflare_temp_email"):
            client.login()
    else:
        client.login()


@pytest.mark.parametrize(
    "body, should_raise",
    [
        ({"address": "x@example.com", "address_id": 1, "jwt": ""}, False),
        ({}, True),                                          # 空 dict
        ({"code": 401, "message": "auth"}, True),
        ({"code": 200}, True),                               # 缺 address
        (None, True),
    ],
)
def test_create_temp_email_sniff(monkeypatch, body, should_raise):
    """create_temp_email() 同样收紧:必须含 address。"""
    client = _make_client(monkeypatch)
    monkeypatch.setattr(client, "_admin_post", lambda path, payload: _Resp(body))
    monkeypatch.setattr(client, "_admin_get", lambda path, params=None: _Resp({"results": []}))
    from autoteam import runtime_config
    monkeypatch.setattr(runtime_config, "get_register_domain", lambda: "example.com")
    if should_raise:
        with pytest.raises(Exception, match="不像 cf_temp_email|缺少 address|响应"):
            client.create_temp_email(prefix="probe")
    else:
        # 期望成功(虽然 address_id fallback 路径会被触发)
        client.create_temp_email(prefix="probe")
