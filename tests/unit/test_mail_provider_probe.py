"""SPEC-1 §5.1 — `/api/mail-provider/probe` 三步 helper 测试。"""

from __future__ import annotations

import pytest

from autoteam.mail import probe as mod


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


# ----------------------------------------------------------------- fingerprint


def test_fingerprint_detect_maillab(monkeypatch):
    """maillab base_url:/setting/websiteConfig 200 + domainList → detected=maillab。"""
    payload = {
        "domainList": ["@a.com", "@b.com"],
        "addVerifyOpen": False,
        "registerVerifyOpen": False,
    }

    def fake_get(url, **kw):
        if "websiteConfig" in url:
            return _Resp(payload)
        return _Resp(None, status_code=404)

    monkeypatch.setattr(mod.requests, "get", fake_get)
    result = mod.probe_fingerprint("http://m.example.com", "maillab")
    assert result.ok
    assert result.detected_provider == "maillab"
    assert result.domain_list == ["@a.com", "@b.com"]


def test_fingerprint_detect_cf(monkeypatch):
    """cf_temp_email base_url:/setting/websiteConfig 404,/admin/address 200 含 results。"""

    def fake_get(url, **kw):
        if "websiteConfig" in url:
            return _Resp(None, status_code=404)
        if "admin/address" in url:
            return _Resp({"results": []})
        return _Resp(None, status_code=404)

    monkeypatch.setattr(mod.requests, "get", fake_get)
    result = mod.probe_fingerprint("https://cf.example.com/api", "cf_temp_email")
    assert result.ok
    assert result.detected_provider == "cf_temp_email"


def test_fingerprint_provider_mismatch(monkeypatch):
    """provider=cf_temp_email 但服务器是 maillab → PROVIDER_MISMATCH。"""

    def fake_get(url, **kw):
        if "websiteConfig" in url:
            return _Resp({"domainList": ["@a.com"]})
        return _Resp(None, status_code=404)

    monkeypatch.setattr(mod.requests, "get", fake_get)
    result = mod.probe_fingerprint("http://m.example.com", "cf_temp_email")
    assert not result.ok
    assert result.error_code == "PROVIDER_MISMATCH"
    assert result.detected_provider == "maillab"


def test_fingerprint_route_not_found(monkeypatch):
    """两个路由都不可达 → ROUTE_NOT_FOUND。"""

    def fake_get(url, **kw):
        return _Resp(None, status_code=404)

    monkeypatch.setattr(mod.requests, "get", fake_get)
    result = mod.probe_fingerprint("http://nope.example.com", "maillab")
    assert not result.ok
    assert result.error_code == "ROUTE_NOT_FOUND"


def test_fingerprint_captcha_warning(monkeypatch):
    """maillab 启用 captcha → warnings 非空,但 ok=True(不阻断)。"""
    payload = {
        "domainList": ["@a.com"],
        "addVerifyOpen": True,
        "registerVerifyOpen": False,
    }

    def fake_get(url, **kw):
        if "websiteConfig" in url:
            return _Resp(payload)
        return _Resp(None, status_code=404)

    monkeypatch.setattr(mod.requests, "get", fake_get)
    result = mod.probe_fingerprint("http://m.example.com", "maillab")
    assert result.ok
    assert result.warnings
    assert "captcha" in result.warnings[0].lower() or "Turnstile" in result.warnings[0]


# ----------------------------------------------------------------- credentials


def test_credentials_maillab_admin(monkeypatch):
    """maillab login 200 + JWT 解码 userType=1 → is_admin=True。"""
    # JWT payload {"userId":1,"userType":1,"email":"admin@x.com"} base64url
    # 简单构造 — base.decode_jwt_payload 仅看 part[1]
    import base64
    import json
    payload = {"userId": 1, "userType": 1, "email": "admin@x.com"}
    p_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    fake_jwt = f"hdr.{p_b64}.sig"

    def fake_post(url, **kw):
        assert "login" in url
        return _Resp({"code": 200, "data": {"token": fake_jwt}})

    monkeypatch.setattr(mod.requests, "post", fake_post)
    result = mod.probe_credentials(
        "http://m.example.com", "maillab",
        username="admin@x.com", password="p",
    )
    assert result.ok
    assert result.is_admin is True
    assert result.user_email == "admin@x.com"
    assert result.bearer_token == fake_jwt


def test_credentials_unauthorized(monkeypatch):
    """maillab login code=401 → ProbeError UNAUTHORIZED。"""

    def fake_post(url, **kw):
        return _Resp({"code": 401, "message": "凭据错误"})

    monkeypatch.setattr(mod.requests, "post", fake_post)
    with pytest.raises(mod.ProbeError) as ei:
        mod.probe_credentials(
            "http://m.example.com", "maillab",
            username="admin@x.com", password="wrong",
        )
    assert ei.value.error_code == "UNAUTHORIZED"


def test_credentials_captcha_required(monkeypatch):
    """maillab message 含 turnstile/captcha → CAPTCHA_REQUIRED。"""

    def fake_post(url, **kw):
        return _Resp({"code": 403, "message": "Turnstile verification required"})

    monkeypatch.setattr(mod.requests, "post", fake_post)
    with pytest.raises(mod.ProbeError) as ei:
        mod.probe_credentials(
            "http://m.example.com", "maillab",
            username="admin@x.com", password="p",
        )
    assert ei.value.error_code == "CAPTCHA_REQUIRED"


# ----------------------------------------------------------------- domain ownership


def test_domain_ownership_success(monkeypatch):
    """maillab account/add code=200 + delete code=200 → cleaned=True。"""
    posts = []
    deletes = []

    def fake_post(url, **kw):
        posts.append(url)
        return _Resp({"code": 200, "data": {"accountId": 42, "email": "probe@x.com"}})

    def fake_delete(url, **kw):
        deletes.append(url)
        return _Resp({"code": 200})

    monkeypatch.setattr(mod.requests, "post", fake_post)
    monkeypatch.setattr(mod.requests, "delete", fake_delete)
    result = mod.probe_domain_ownership(
        "http://m.example.com", "maillab",
        bearer_token="fake.jwt", domain="x.com",
    )
    assert result.ok
    assert result.cleaned is True
    assert result.probe_account_id == 42
    assert len(posts) == 1 and len(deletes) == 1


def test_domain_ownership_forbidden(monkeypatch):
    """maillab account/add code=403 message 含 domain → FORBIDDEN_DOMAIN。"""

    def fake_post(url, **kw):
        return _Resp({"code": 403, "message": "domain not in white list"})

    monkeypatch.setattr(mod.requests, "post", fake_post)
    with pytest.raises(mod.ProbeError) as ei:
        mod.probe_domain_ownership(
            "http://m.example.com", "maillab",
            bearer_token="fake.jwt", domain="forbidden.com",
        )
    assert ei.value.error_code == "FORBIDDEN_DOMAIN"


def test_domain_ownership_leaked_probe(monkeypatch):
    """maillab account/add 成功但 delete 失败 → cleaned=False, leaked_probe 透传。"""

    def fake_post(url, **kw):
        return _Resp({"code": 200, "data": {"accountId": 99, "email": "probe@x.com"}})

    def fake_delete(url, **kw):
        return _Resp({"code": 500, "message": "internal"})

    monkeypatch.setattr(mod.requests, "post", fake_post)
    monkeypatch.setattr(mod.requests, "delete", fake_delete)
    result = mod.probe_domain_ownership(
        "http://m.example.com", "maillab",
        bearer_token="fake.jwt", domain="x.com",
    )
    assert result.ok
    assert result.cleaned is False
    assert result.leaked_probe is not None
    assert result.leaked_probe["acct_id"] == 99
