"""Round 11 — cheap_codex_smoke 加 model 参数 + 读完整 SSE 拿真实对话内容。

覆盖:
  - model 默认值 gpt-5(向后兼容)
  - model 入参可定制(gpt-5.5 团队号 / gpt-5.4 通用号)
  - SSE 完整读出 response.completed,detail 升级为 dict 含 response_text
  - 8 行内未见 response.created → uncertain
  - 30 帧仍无 completed → alive 兜底(标 raw_event=no_completed_within_30_frames)
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def _make_sse_resp(status_code: int, lines):
    fake_resp = MagicMock()
    fake_resp.status_code = status_code
    fake_resp.iter_lines.return_value = iter(lines)
    return fake_resp


def test_smoke_with_custom_model_param_passes_through():
    """传 model='gpt-5.5' → payload['model'] == 'gpt-5.5'。"""
    from autoteam import codex_auth

    captured = {}

    def fake_post(*args, **kwargs):
        captured["payload"] = kwargs.get("json")
        return _make_sse_resp(200, [
            "",
            'event: response.created',
            'data: {"type": "response.created"}',
            'data: {"type": "response.completed", "response": {"usage": {"output_tokens": 5}}}',
        ])

    with patch("requests.post", side_effect=fake_post):
        result, detail = codex_auth.cheap_codex_smoke(
            "test-token",
            account_id="acc-1",
            model="gpt-5.5",
            force=True,
        )

    assert result == "alive"
    assert captured["payload"]["model"] == "gpt-5.5"
    assert isinstance(detail, dict)
    assert detail.get("model") == "gpt-5.5"


def test_smoke_default_model_gpt5_backward_compat():
    """不传 model → 默认 gpt-5(向后兼容)。"""
    from autoteam import codex_auth

    captured = {}

    def fake_post(*args, **kwargs):
        captured["payload"] = kwargs.get("json")
        return _make_sse_resp(200, [
            'data: {"type": "response.created"}',
            'data: {"type": "response.completed"}',
        ])

    with patch("requests.post", side_effect=fake_post):
        result, _detail = codex_auth.cheap_codex_smoke("tok", account_id="acc-1", force=True)

    assert result == "alive"
    assert captured["payload"]["model"] == "gpt-5"


def test_smoke_returns_response_text_in_dict():
    """SSE 多帧 delta + completed → detail 是 dict,response_text 是拼接后的真实文本。"""
    from autoteam import codex_auth

    sse_lines = [
        "",
        'event: response.created',
        'data: {"type": "response.created"}',
        'data: {"type": "response.output_text.delta", "delta": "Hello"}',
        'data: {"type": "response.output_text.delta", "delta": ", "}',
        'data: {"type": "response.output_text.delta", "delta": "world!"}',
        'data: {"type": "response.completed", "response": {"usage": {"output_tokens": 3}}}',
    ]
    with patch("requests.post", return_value=_make_sse_resp(200, sse_lines)):
        result, detail = codex_auth.cheap_codex_smoke(
            "tok",
            account_id="acc-1",
            model="gpt-5.4",
            force=True,
        )

    assert result == "alive"
    assert isinstance(detail, dict)
    assert detail["response_text"] == "Hello, world!"
    assert detail["raw_event"] == "response.completed"
    assert detail["tokens"] == 3
    assert detail["model"] == "gpt-5.4"


def test_smoke_no_response_created_returns_uncertain():
    """8 行内仍无 response.created → uncertain 兜底。"""
    from autoteam import codex_auth

    sse_lines = ["", "noise", "more noise", ""]
    with patch("requests.post", return_value=_make_sse_resp(200, sse_lines)):
        result, detail = codex_auth.cheap_codex_smoke("tok", account_id="acc-1", force=True)

    assert result == "uncertain"
    assert "no_response_created_frame" in (detail or "")


def test_smoke_max_output_tokens_passed():
    """传 max_output_tokens=128 → payload 内有此值。"""
    from autoteam import codex_auth

    captured = {}

    def fake_post(*args, **kwargs):
        captured["payload"] = kwargs.get("json")
        return _make_sse_resp(200, [
            'data: {"type": "response.created"}',
            'data: {"type": "response.completed"}',
        ])

    with patch("requests.post", side_effect=fake_post):
        codex_auth.cheap_codex_smoke(
            "tok", account_id="acc-1", model="gpt-5", max_output_tokens=128, force=True,
        )

    assert captured["payload"]["max_output_tokens"] == 128


def test_smoke_auth_invalid_path_unchanged():
    """401/403/429 → auth_invalid 路径不动(向后兼容)。"""
    from autoteam import codex_auth

    fake_resp = MagicMock()
    fake_resp.status_code = 401
    with patch("requests.post", return_value=fake_resp):
        result, detail = codex_auth.cheap_codex_smoke("tok", account_id="acc-1", force=True)
    assert result == "auth_invalid"
    assert detail == "http_401"
