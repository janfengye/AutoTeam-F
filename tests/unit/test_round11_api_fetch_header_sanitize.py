"""Round 11 patch — _api_fetch headers ISO-8859-1 sanitize.

修复 import_admin_session 路径下的 fetch RequestInit 抛
"String contains non ISO-8859-1 code point" 错误。

覆盖:
  - 非 Latin1 字符强制 replace 为 '?' + logger.warning
  - None header value 不写入 headers dict(skip)
  - 正常 ASCII 字段不被改写,no warning
  - bytes value decode + sanitize
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock


def _make_api(*, account_id="bac969ea", device_id="dev-uuid-ascii", access_token="JWT-ASCII"):
    """构造一个最小可用的 ChatGPTTeamAPI 实例,只填 _api_fetch 必需字段。"""
    from autoteam.chatgpt_api import ChatGPTTeamAPI

    api = ChatGPTTeamAPI.__new__(ChatGPTTeamAPI)
    api.account_id = account_id
    api.oai_device_id = device_id
    api.access_token = access_token

    # mock page.evaluate — 捕获参数后返回固定 dict,避免真起 Playwright
    api.page = MagicMock()
    api.page.evaluate = MagicMock(return_value={"status": 200, "body": "{}"})
    return api


def _captured_headers(api):
    """从 mock page.evaluate 调用拿到 headers_js dict(第二个 positional 的 [2] 元素)。"""
    call_args = api.page.evaluate.call_args
    payload = call_args.args[1] if call_args.args else call_args.kwargs.get("arg")
    # payload = [method, url, headers_js, body]
    return payload[2]


def test_api_fetch_strips_unicode_from_account_id(caplog):
    """中文 / 非 Latin1 unicode 字段触发 sanitize + warning。"""
    api = _make_api(account_id="bac\u6d4b\u8bd5969ea")  # 中文 "测试"

    with caplog.at_level(logging.WARNING, logger="autoteam.chatgpt_api"):
        api._api_fetch("GET", "/backend-api/accounts")

    headers = _captured_headers(api)
    # account_id 被 replace 后应该是纯 Latin1
    sanitized_account = headers["chatgpt-account-id"]
    sanitized_account.encode("latin-1")  # 不抛即 OK

    # 至少 1 条 warning 提到 chatgpt-account-id
    sanitize_warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "chatgpt-account-id" in r.getMessage()
    ]
    assert sanitize_warnings, f"expected warning for chatgpt-account-id, got: {[r.getMessage() for r in caplog.records]}"


def test_api_fetch_handles_none_header_value(caplog):
    """access_token=None 时 authorization key 应被 skip,不出现在 headers_js。"""
    api = _make_api(access_token=None)

    with caplog.at_level(logging.WARNING, logger="autoteam.chatgpt_api"):
        api._api_fetch("GET", "/backend-api/accounts")

    headers = _captured_headers(api)
    assert "authorization" not in headers, (
        f"authorization should be skipped when access_token=None, got: {headers}"
    )

    # 其他必备字段仍在
    assert "Content-Type" in headers
    assert "chatgpt-account-id" in headers
    assert "oai-device-id" in headers
    assert "oai-language" in headers


def test_api_fetch_normal_ascii_unchanged(caplog):
    """正常 ASCII 字段:headers_js 内容 unchanged,no sanitize warning。"""
    api = _make_api(
        account_id="bac969ea-aaaa-bbbb-cccc-1234567890ab",
        device_id="dev-uuid-ascii-only",
        access_token="eyJhbGciOiJIUzI1NiJ9.ascii_only.signature",
    )

    with caplog.at_level(logging.WARNING, logger="autoteam.chatgpt_api"):
        api._api_fetch("GET", "/backend-api/accounts")

    headers = _captured_headers(api)
    assert headers["chatgpt-account-id"] == "bac969ea-aaaa-bbbb-cccc-1234567890ab"
    assert headers["oai-device-id"] == "dev-uuid-ascii-only"
    assert headers["authorization"] == "Bearer eyJhbGciOiJIUzI1NiJ9.ascii_only.signature"
    assert headers["oai-language"] == "en-US"
    assert headers["Content-Type"] == "application/json"

    # 不该有 sanitize warning
    sanitize_warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "_api_fetch header" in r.getMessage()
    ]
    assert not sanitize_warnings, (
        f"unexpected sanitize warnings: {[r.getMessage() for r in sanitize_warnings]}"
    )


def test_api_fetch_handles_bytes_value(caplog):
    """bytes value(罕见但可能):decode + sanitize 后入 headers。"""
    api = _make_api(account_id=b"bytes-account-id-ascii")

    with caplog.at_level(logging.WARNING, logger="autoteam.chatgpt_api"):
        api._api_fetch("GET", "/backend-api/accounts")

    headers = _captured_headers(api)
    assert headers["chatgpt-account-id"] == "bytes-account-id-ascii"
