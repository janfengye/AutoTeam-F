"""mail/probe.py — `/api/mail-provider/probe` 与 `register-domain` 共享 helper。

SPEC-1 §3.1。每个 step 都有同名 helper:
    - probe_fingerprint(base_url, expected_provider)
    - probe_credentials(base_url, provider, username/password/admin_password)
    - probe_domain_ownership(base_url, provider, *, bearer_token/admin_password, domain)

api.py 端点直接调用并把返回值封装为 ProbeResponse;register-domain PUT 复用
`probe_domain_ownership`(SPEC-1 §FR-005)。

设计要点:
  - 后端无状态 — step=domain_ownership 内部重调一次 login,前端不持有 token(SPEC §4.2 决策)
  - 单次调用 timeout=5s (PROBE_TIMEOUT),失败按错误码归类
  - ProbeError 内部抛出,api 层转 ProbeResult(ok=False)
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

import requests

from autoteam.mail.base import decode_jwt_payload

logger = logging.getLogger(__name__)

PROBE_TIMEOUT = 5  # 秒;每个 HTTP 调用上限


# ----------------------------------------------------------------- error codes


class ProbeError(Exception):
    """probe.* helper 内部抛错;api 层捕获后转成 ProbeResult(ok=False)。"""

    def __init__(self, error_code: str, message: str, hint: str | None = None):
        self.error_code = error_code
        self.message = message
        self.hint = hint
        super().__init__(message)


# ----------------------------------------------------------------- result


@dataclass
class ProbeResult:
    """通用结果。step=fingerprint/credentials/domain_ownership 共用,字段按 step 填。"""

    ok: bool
    step: Literal["fingerprint", "credentials", "domain_ownership"]
    error_code: str | None = None
    message: str | None = None
    hint: str | None = None
    warnings: list[str] = field(default_factory=list)
    # fingerprint
    detected_provider: Literal["cf_temp_email", "maillab", "unknown"] | None = None
    domain_list: list[str] | None = None
    add_verify_open: bool | None = None
    register_verify_open: bool | None = None
    # credentials
    is_admin: bool | None = None
    user_email: str | None = None
    token_preview: str | None = None
    bearer_token: str | None = None  # 内部传给下游;api 层不返回前端
    # domain_ownership
    probe_email: str | None = None
    probe_account_id: int | None = None
    cleaned: bool | None = None
    leaked_probe: dict | None = None


# ----------------------------------------------------------------- helpers


def _safe_get(url: str, **kw) -> requests.Response | None:
    try:
        return requests.get(url, timeout=PROBE_TIMEOUT, **kw)
    except requests.Timeout as exc:
        raise ProbeError("TIMEOUT", f"GET {url} 超时(>{PROBE_TIMEOUT}s)", "检查 base_url 可达性") from exc
    except requests.ConnectionError as exc:
        raise ProbeError("NETWORK", f"GET {url} 连接失败: {exc}", "检查 base_url、防火墙、代理") from exc
    except requests.RequestException as exc:
        raise ProbeError("UNKNOWN", f"GET {url} 异常: {exc}") from exc


def _safe_post(url: str, **kw) -> requests.Response | None:
    try:
        return requests.post(url, timeout=PROBE_TIMEOUT, **kw)
    except requests.Timeout as exc:
        raise ProbeError("TIMEOUT", f"POST {url} 超时(>{PROBE_TIMEOUT}s)", "检查 base_url 可达性") from exc
    except requests.ConnectionError as exc:
        raise ProbeError("NETWORK", f"POST {url} 连接失败: {exc}", "检查 base_url、防火墙、代理") from exc
    except requests.RequestException as exc:
        raise ProbeError("UNKNOWN", f"POST {url} 异常: {exc}") from exc


def _safe_delete(url: str, **kw) -> requests.Response | None:
    try:
        return requests.delete(url, timeout=PROBE_TIMEOUT, **kw)
    except requests.RequestException:
        return None


# ----------------------------------------------------------------- fingerprint


def probe_fingerprint(base_url: str, expected_provider: str) -> ProbeResult:
    """SPEC §3.1 Step 1:无凭据指纹探测。

    依次尝试:
      - GET /setting/websiteConfig (maillab 独有,通常含 domainList)
      - GET /admin/address         (cf_temp_email 独有,401/403/200)

    返回 detected_provider:
      - "maillab"          → /setting/websiteConfig 200 + dict(可能含 domainList)
      - "cf_temp_email"    → /admin/address 401/403/(200 含 results)
      - "unknown"          → 两个都不像

    若 expected_provider 与 detected 不一致,返 PROVIDER_MISMATCH。
    """
    base = base_url.rstrip("/")

    # 探测 1: /setting/websiteConfig (maillab)
    detected = "unknown"
    domain_list: list[str] | None = None
    add_verify_open = None
    register_verify_open = None
    warnings: list[str] = []

    r_wc = _safe_get(f"{base}/setting/websiteConfig")
    if r_wc is not None and r_wc.status_code == 200:
        try:
            wc = r_wc.json()
        except Exception:
            wc = None
        if isinstance(wc, dict):
            detected = "maillab"
            raw_domains = wc.get("domainList")
            if isinstance(raw_domains, list):
                domain_list = [str(d) for d in raw_domains]
            add_verify_open = bool(wc.get("addVerifyOpen") or wc.get("addEmailVerify") or False)
            register_verify_open = bool(wc.get("registerVerifyOpen") or wc.get("registerVerify") or False)
            if add_verify_open or register_verify_open:
                warnings.append(
                    "服务端启用了 Turnstile / addVerify;AutoTeam 自动化路径暂不支持 captcha,"
                    "请到 maillab 后台 → 设置 → 安全 关闭 captcha。"
                )

    # 若 detected 还不是 maillab,尝试 cf_temp_email 探测
    if detected != "maillab":
        r_admin = _safe_get(f"{base}/admin/address")
        if r_admin is not None and r_admin.status_code in (200, 401, 403):
            if r_admin.status_code == 200:
                try:
                    adm = r_admin.json()
                except Exception:
                    adm = None
                if isinstance(adm, dict) and "results" in adm:
                    detected = "cf_temp_email"
                else:
                    # 200 但不像 cf 也不像 maillab — 可能是 /api 前缀错或反向代理
                    detected = "unknown"
            else:
                detected = "cf_temp_email"

    if detected == "unknown":
        return ProbeResult(
            ok=False,
            step="fingerprint",
            error_code="ROUTE_NOT_FOUND",
            message=f"base_url {base} 既无 /setting/websiteConfig 也无 /admin/address 路由",
            hint="检查地址拼写。maillab 通常无 /api 前缀;cf_temp_email 通常带 /api",
            detected_provider=detected,
        )

    if expected_provider != detected:
        return ProbeResult(
            ok=False,
            step="fingerprint",
            error_code="PROVIDER_MISMATCH",
            message=f"选择的 provider={expected_provider},但服务器看起来是 {detected}",
            hint=f"把 MAIL_PROVIDER 改为 {detected}",
            detected_provider=detected,
            domain_list=domain_list,
            add_verify_open=add_verify_open,
            register_verify_open=register_verify_open,
        )

    return ProbeResult(
        ok=True,
        step="fingerprint",
        detected_provider=detected,
        domain_list=domain_list,
        add_verify_open=add_verify_open,
        register_verify_open=register_verify_open,
        warnings=warnings,
    )


# ----------------------------------------------------------------- credentials


def probe_credentials(
    base_url: str,
    provider: Literal["cf_temp_email", "maillab"],
    *,
    username: str = "",
    password: str = "",
    admin_password: str = "",
) -> ProbeResult:
    """SPEC §3.1 Step 2:凭据校验。

    cf_temp_email:GET /admin/address with `x-admin-auth: <admin_password>` header
    maillab:POST /login body `{email, password}` → JWT
    """
    base = base_url.rstrip("/")

    if provider in ("cf_temp_email", "cloudflare_temp_email"):
        if not admin_password:
            raise ProbeError("UNAUTHORIZED", "cf_temp_email 凭据校验需要 admin_password", "填写 CLOUDMAIL_PASSWORD")
        r = _safe_get(f"{base}/admin/address", headers={"x-admin-auth": admin_password})
        if r is None:
            raise ProbeError("NETWORK", "凭据校验请求未返回")
        if r.status_code in (401, 403):
            raise ProbeError("UNAUTHORIZED", "cf_temp_email admin 密码错误", "检查 CLOUDMAIL_PASSWORD")
        if r.status_code != 200:
            raise ProbeError("UNKNOWN", f"HTTP {r.status_code}: {(r.text or '')[:200]}")
        try:
            data = r.json()
        except Exception as exc:
            raise ProbeError("PROVIDER_MISMATCH", f"响应非 JSON: {exc}",
                             "可能不是 cf_temp_email 服务器") from exc
        if not isinstance(data, dict) or "results" not in data:
            raise ProbeError(
                "PROVIDER_MISMATCH",
                f"响应缺 `results` 字段: {data!r}",
                "确认 base_url 与 provider 匹配",
            )
        return ProbeResult(
            ok=True,
            step="credentials",
            is_admin=True,  # cf_temp_email 单 admin 模型
            user_email=None,
            token_preview=admin_password[:6] + "...",
            bearer_token=admin_password,  # 后续 step 复用
        )

    # provider == "maillab"
    if not username or not password:
        raise ProbeError("UNAUTHORIZED", "maillab 凭据校验需要 username + password",
                         "填写 MAILLAB_USERNAME / MAILLAB_PASSWORD")

    r = _safe_post(
        f"{base}/login",
        json={"email": username, "password": password},
        headers={"Content-Type": "application/json"},
    )
    if r is None:
        raise ProbeError("NETWORK", "POST /login 未返回")
    if r.status_code != 200:
        raise ProbeError("UNKNOWN", f"HTTP {r.status_code}: {(r.text or '')[:200]}")

    try:
        body = r.json() or {}
    except Exception as exc:
        raise ProbeError("PROVIDER_MISMATCH", f"login 响应非 JSON: {exc}") from exc

    code = body.get("code")
    if code == 401:
        raise ProbeError("UNAUTHORIZED", body.get("message") or "凭据错误",
                         "检查 MAILLAB_USERNAME / MAILLAB_PASSWORD")
    if code in (403,) or "captcha" in (body.get("message") or "").lower() or "turnstile" in (body.get("message") or "").lower():
        raise ProbeError(
            "CAPTCHA_REQUIRED",
            body.get("message") or "服务端要求 captcha",
            "到 maillab 后台 → 设置 → 安全 关闭 captcha",
        )
    if code != 200:
        raise ProbeError("UNKNOWN", f"login code={code} message={body.get('message')!r}")

    data = body.get("data") or {}
    token = data.get("token")
    if not token:
        raise ProbeError("UNKNOWN", f"login 响应缺 token: {data!r}")

    payload = decode_jwt_payload(token)
    user_email = payload.get("email") or username
    user_type = payload.get("userType")
    is_admin = user_type == 1

    warnings: list[str] = []
    if not is_admin:
        warnings.append("当前 maillab 账号 userType != 1,非管理员;可创建临时邮箱但无法管理用户/角色")

    return ProbeResult(
        ok=True,
        step="credentials",
        is_admin=is_admin,
        user_email=user_email,
        token_preview=str(token)[:10] + "...",
        bearer_token=token,
        warnings=warnings,
    )


# ----------------------------------------------------------------- domain ownership


def probe_domain_ownership(
    base_url: str,
    provider: Literal["cf_temp_email", "maillab"],
    *,
    bearer_token: str = "",
    admin_password: str = "",
    domain: str,
    username: str = "",
    password: str = "",
) -> ProbeResult:
    """SPEC §3.1 Step 3:域名归属验证。

    通用流程:
      1. 在该 domain 下创建一个 probe-{ts} 邮箱
      2. 立即删除回收(failed 时填 leaked_probe)

    cf_temp_email:用 admin_password 直接调 /admin/new_address + /admin/delete_address
    maillab:若未提供 bearer_token,用 username/password 重 login 一次拿 token(后端无状态决策)
    """
    base = base_url.rstrip("/")
    domain_clean = (domain or "").strip().lstrip("@")
    if not domain_clean:
        raise ProbeError("EMPTY_DOMAIN_LIST", "domain 不能为空", "填写一个有效域名")

    probe_prefix = f"probe{int(time.time())}{uuid.uuid4().hex[:4]}"
    probe_email_addr = f"{probe_prefix}@{domain_clean}"

    if provider in ("cf_temp_email", "cloudflare_temp_email"):
        if not admin_password:
            raise ProbeError("UNAUTHORIZED", "cf_temp_email 域归属验证需要 admin_password")
        # 创建
        r = _safe_post(
            f"{base}/admin/new_address",
            headers={"x-admin-auth": admin_password, "Content-Type": "application/json"},
            json={"name": probe_prefix, "domain": domain_clean, "enablePrefix": False},
        )
        if r is None or r.status_code != 200:
            status = r.status_code if r is not None else "no-response"
            raise ProbeError("FORBIDDEN_DOMAIN", f"创建探测邮箱失败 HTTP {status}",
                             f"确认 cf_temp_email 已配置 domain {domain_clean}")
        try:
            data = r.json() or {}
        except Exception:
            data = {}
        if not isinstance(data, dict) or "address" not in data:
            raise ProbeError("PROVIDER_MISMATCH", f"创建邮箱响应缺 address: {data!r}")

        address_id = data.get("address_id")
        # 立即回收
        cleaned = True
        leaked_probe = None
        if address_id is not None:
            del_r = _safe_delete(
                f"{base}/admin/delete_address",
                headers={"x-admin-auth": admin_password},
                params={"id": address_id},
            )
            if del_r is None or del_r.status_code != 200:
                cleaned = False
                leaked_probe = {
                    "email": probe_email_addr,
                    "acct_id": address_id,
                    "error": f"DELETE failed HTTP {del_r.status_code if del_r else 'no-response'}",
                }
        return ProbeResult(
            ok=True,
            step="domain_ownership",
            probe_email=probe_email_addr,
            probe_account_id=address_id,
            cleaned=cleaned,
            leaked_probe=leaked_probe,
        )

    # provider == "maillab"
    token = bearer_token
    if not token:
        # 后端无状态:重 login 一次
        cred_result = probe_credentials(base, provider, username=username, password=password)
        token = cred_result.bearer_token
    if not token:
        raise ProbeError("UNAUTHORIZED", "maillab 域归属验证需要 bearer_token 或 username/password")

    headers = {"Authorization": token, "Content-Type": "application/json"}
    r = _safe_post(f"{base}/account/add", headers=headers, json={"email": probe_email_addr})
    if r is None or r.status_code != 200:
        status = r.status_code if r is not None else "no-response"
        raise ProbeError("FORBIDDEN_DOMAIN", f"创建探测邮箱 HTTP {status}",
                         f"确认 maillab 已配置 domain {domain_clean}")
    try:
        body = r.json() or {}
    except Exception as exc:
        raise ProbeError("PROVIDER_MISMATCH", f"/account/add 响应非 JSON: {exc}") from exc

    code = body.get("code")
    if code == 401:
        raise ProbeError("UNAUTHORIZED", body.get("message") or "登录已过期")
    if code == 403 or "domain" in (body.get("message") or "").lower():
        raise ProbeError(
            "FORBIDDEN_DOMAIN",
            body.get("message") or f"domain {domain_clean} 不在白名单",
            "联系 maillab 管理员把该 domain 加入,或选其他 domain",
        )
    if code != 200:
        raise ProbeError("UNKNOWN", f"/account/add code={code} message={body.get('message')!r}")

    data = body.get("data") or {}
    account_id = data.get("accountId") or data.get("id")
    actual_email = data.get("email") or probe_email_addr

    # 立即回收
    cleaned = True
    leaked_probe = None
    if account_id is not None:
        del_r = _safe_delete(f"{base}/account/delete", headers=headers, params={"accountId": account_id})
        if del_r is None or del_r.status_code != 200:
            cleaned = False
            leaked_probe = {
                "email": actual_email,
                "acct_id": account_id,
                "error": f"DELETE failed HTTP {del_r.status_code if del_r else 'no-response'}",
            }
        else:
            try:
                del_body = del_r.json() or {}
                if del_body.get("code") not in (200, None):
                    cleaned = False
                    leaked_probe = {
                        "email": actual_email,
                        "acct_id": account_id,
                        "error": f"DELETE code={del_body.get('code')} message={del_body.get('message')!r}",
                    }
            except Exception:
                pass

    return ProbeResult(
        ok=True,
        step="domain_ownership",
        probe_email=actual_email,
        probe_account_id=int(account_id) if account_id is not None else None,
        cleaned=cleaned,
        leaked_probe=leaked_probe,
    )
