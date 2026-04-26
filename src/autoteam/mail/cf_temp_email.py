"""dreamhunter2333/cloudflare_temp_email 后端的 MailProvider 实现。

直接搬运历史 `autoteam.cloudmail.CloudMailClient` 的逻辑,所有公开方法行为保持原样
(包括 retry/timeout/jwt 解析/MIME 兜底等细节),不做语义重写。

- 鉴权:`x-admin-auth: {CLOUDMAIL_PASSWORD}` header
- 地址:/admin/new_address, /admin/delete_address/{id}, /admin/address
- 邮件:/admin/mails?address={email}; DELETE /admin/mails/{id} 或 /admin/clear_inbox/{email}
- `raw` 字段是完整 MIME,用 stdlib `email` 解析 subject/text/html
"""

from __future__ import annotations

import logging
import re
import uuid

import requests

from autoteam.config import (
    CLOUDMAIL_BASE_URL,
    CLOUDMAIL_EMAIL,  # noqa: F401 — 兼容旧配置,不实际使用
    CLOUDMAIL_PASSWORD,
)
from autoteam.mail.base import (
    MailProvider,
    decode_jwt_payload,
    normalize_email_addr,
    parse_mime,
)

logger = logging.getLogger(__name__)


class CfTempEmailClient(MailProvider):
    """dreamhunter2333/cloudflare_temp_email 后端客户端。"""

    provider_name = "CloudMail"

    def __init__(self):
        self.base_url = (CLOUDMAIL_BASE_URL or "").rstrip("/")
        self.admin_password = CLOUDMAIL_PASSWORD
        self.session = requests.Session()
        # 占位符,为兼容旧代码 `self.token`
        self.token = None
        # address (lower) -> JWT 缓存
        self._address_jwts: dict[str, str] = {}

    # ------------------------------------------------------------------ helpers

    def _admin_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-admin-auth": self.admin_password or "",
        }

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{path}"

    def _admin_get(self, path, params=None):
        return self.session.get(self._url(path), headers=self._admin_headers(), params=params, timeout=30)

    def _admin_post(self, path, data=None):
        return self.session.post(self._url(path), headers=self._admin_headers(), json=data, timeout=30)

    def _admin_delete(self, path):
        return self.session.delete(self._url(path), headers=self._admin_headers(), timeout=30)

    @staticmethod
    def _normalize_email(value):
        return normalize_email_addr(value)

    @staticmethod
    def _sanitize_prefix(prefix):
        """cloudflare_temp_email 只允许字母、数字、点、下划线,长度 <= 64;其余字符剔除。"""
        if not prefix:
            return uuid.uuid4().hex[:10]
        cleaned = re.sub(r"[^A-Za-z0-9._]", "", str(prefix))
        cleaned = cleaned.strip("._")
        return cleaned[:60] or uuid.uuid4().hex[:10]

    # ------------------------------------------------------------------ auth

    def login(self):
        """兼容旧接口:cloudflare_temp_email 无需登录,调用管理员列表验证密码可用。"""
        if not self.admin_password:
            raise Exception("CloudMail 登录失败: 未配置 CLOUDMAIL_PASSWORD(作为 admin password)")

        r = self._admin_get("/admin/address", params={"limit": 1, "offset": 0})
        if r.status_code == 401 or r.status_code == 403:
            raise Exception(f"CloudMail 登录失败: admin 密码无效 (HTTP {r.status_code})")
        if r.status_code != 200:
            body = (r.text or "")[:200]
            raise Exception(f"CloudMail 登录失败: HTTP {r.status_code} {body}")

        # SPEC-1 §3.2 — 正向白名单嗅探:cf_temp_email 的 /admin/address 必须返回含 `results`
        # 字段的 dict。任何缺 results 的响应(空 {}、maillab 风格 {code,...}、null 等)都视为
        # 协议错配,直接抛错。round-3 漏判的空 dict 也被本次收紧覆盖。
        try:
            payload = r.json()
        except Exception:
            payload = None
        if not isinstance(payload, dict) or "results" not in payload:
            payload_type = type(payload).__name__ if payload is not None else "None"
            raise Exception(
                "CloudMail 登录响应不像 dreamhunter2333/cloudflare_temp_email"
                f"(响应 {payload_type} 不含 `results` 字段)。"
                f"你的 CLOUDMAIL_BASE_URL={self.base_url} 可能指向 maillab/cloud-mail 服务器或路由错。"
                "请确认 .env 中 MAIL_PROVIDER=cf_temp_email 时填的是 dreamhunter2333 后端;"
                "如果你部署的是 maillab,请改 MAIL_PROVIDER=maillab 并补齐 MAILLAB_API_URL/USERNAME/PASSWORD/DOMAIN。"
            )

        self.token = "admin-" + self.admin_password[:6]
        logger.info("[CloudMail] 管理员鉴权通过")
        return self.token

    # ------------------------------------------------------------------ accounts

    def create_temp_email(self, prefix=None, domain=None):
        """创建临时邮箱,返回 (accountId, email)。

        domain 优先级:
        1. 显式传入的 domain 参数
        2. runtime_config.json 的 register_domain
        3. 环境变量 CLOUDMAIL_DOMAIN
        """
        if domain:
            domain = domain.lstrip("@").strip()
        else:
            from autoteam.runtime_config import get_register_domain

            domain = get_register_domain()
        if not domain:
            raise Exception("创建邮箱失败: 未配置注册域名")

        cleaned = self._sanitize_prefix(prefix)
        r = self._admin_post(
            "/admin/new_address",
            {"name": cleaned, "domain": domain, "enablePrefix": False},
        )
        if r.status_code != 200:
            raise Exception(f"创建邮箱失败: HTTP {r.status_code} {(r.text or '')[:200]}")

        data = {}
        try:
            data = r.json()
        except Exception:
            pass

        # SPEC-1 §3.2 — 正向白名单嗅探:任何缺 `address` 字段就报错。
        # 比 round-3 的"必须同时含 code+message"宽松条件更严,空 dict / null 也被捕获。
        if not isinstance(data, dict) or "address" not in data:
            raise Exception(
                f"创建邮箱响应不像 cf_temp_email(收到 {data!r})。"
                "请检查 MAIL_PROVIDER 与 base_url 是否对应;"
                "如果服务器是 maillab,请改 MAIL_PROVIDER=maillab。"
            )

        address = data.get("address")
        jwt = data.get("jwt") or ""
        payload = decode_jwt_payload(jwt) if jwt else {}
        address_id = data.get("address_id") or payload.get("address_id")

        if not address_id:
            # Fallback:按名称查询
            try:
                listed = self._admin_get(
                    "/admin/address",
                    params={"limit": 1, "offset": 0, "query": address or cleaned},
                )
                if listed.status_code == 200:
                    results = (listed.json() or {}).get("results") or []
                    if results:
                        address_id = results[0].get("id")
                        address = address or results[0].get("name")
            except Exception:
                pass

        if not address:
            raise Exception(f"创建邮箱失败: 响应缺少 address 字段: {data}")

        if jwt and address:
            self._address_jwts[self._normalize_email(address)] = jwt

        logger.info("[CloudMail] 临时邮箱已创建: %s (accountId=%s)", address, address_id)
        return address_id, address

    def list_accounts(self, size=200):
        """列出邮箱地址。返回与旧接口兼容的字典列表:{accountId, email, ...}"""
        r = self._admin_get("/admin/address", params={"limit": size, "offset": 0})
        if r.status_code != 200:
            return []
        try:
            data = r.json() or {}
        except Exception:
            return []

        out = []
        for row in data.get("results", []):
            out.append(
                {
                    "accountId": row.get("id"),
                    "email": row.get("name"),
                    "password": row.get("password"),
                    "createTime": row.get("created_at"),
                    "updateTime": row.get("updated_at"),
                    "mailCount": row.get("mail_count"),
                    "sendCount": row.get("send_count"),
                    "sourceMeta": row.get("source_meta"),
                    "raw": row,
                }
            )
        return out

    def delete_account(self, account_id):
        """删除临时邮箱账户。account_id 可以是数字 id,也可以是 email(自动查 id)。"""
        real_id = self._resolve_address_id(account_id)
        if not real_id:
            logger.warning("[CloudMail] delete_account: 找不到对应的 address id (%s)", account_id)
            return {"code": 404, "message": "address not found"}

        r = self._admin_delete(f"/admin/delete_address/{real_id}")
        if r.status_code == 200:
            try:
                body = r.json()
            except Exception:
                body = {}
            if body.get("success"):
                logger.info("[CloudMail] 临时邮箱已删除 (accountId=%s)", real_id)
                return {"code": 200}
        return {"code": r.status_code, "message": (r.text or "")[:200]}

    # ------------------------------------------------------------------ mails

    def _resolve_address_id(self, value):
        """把 account_id(可能是 id 或 email)统一解析为数字 id。"""
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            pass

        email_str = self._normalize_email(value)
        if "@" not in email_str:
            return None
        try:
            r = self._admin_get("/admin/address", params={"limit": 5, "offset": 0, "query": email_str})
            if r.status_code != 200:
                return None
            results = (r.json() or {}).get("results") or []
            for row in results:
                if self._normalize_email(row.get("name")) == email_str:
                    return row.get("id")
        except Exception:
            return None
        return None

    def _resolve_address_email(self, account_id):
        """数字 id → email 名称。"""
        if not account_id:
            return None
        try:
            int(account_id)
        except (TypeError, ValueError):
            return str(account_id) if "@" in str(account_id) else None

        try:
            r = self._admin_get("/admin/address", params={"limit": 20, "offset": 0})
            if r.status_code != 200:
                return None
            for row in (r.json() or {}).get("results") or []:
                if str(row.get("id")) == str(account_id):
                    return row.get("name")
        except Exception:
            return None
        return None

    def _normalize_mail_record(self, row):
        """把 /admin/mails 返回的一条 raw MIME 记录转成 AutoTeam 期望的字典。"""
        raw = row.get("raw") or ""
        subject, text, html_body, from_addr, to_addr, message_id = parse_mime(raw)

        return {
            "emailId": row.get("id"),
            "accountId": None,  # 上层会自己填入 account_id 或用 email 匹配
            "accountEmail": row.get("address"),
            "receiveEmail": row.get("address"),
            "toEmail": to_addr or row.get("address"),
            "sendEmail": row.get("source") or from_addr,
            "sender": from_addr,
            "subject": subject,
            "text": text,
            "content": html_body,
            "messageId": message_id or row.get("message_id"),
            "createTime": row.get("created_at"),
            "raw": raw,
        }

    def list_emails(self, account_id, size=10):
        """获取指定账户的收件列表。兼容旧接口:account_id 是数字 id 或 email。"""
        target_email = None
        if isinstance(account_id, str) and "@" in account_id:
            target_email = account_id
        else:
            target_email = self._resolve_address_email(account_id)

        if not target_email:
            return []

        return self.search_emails_by_recipient(target_email, size=size, account_id=account_id)

    def get_latest_emails(self, account_id, email_id=0, all_receive=0):
        """兼容旧接口:返回该账户最新邮件;用同一个 /admin/mails 查询替代。"""
        return self.list_emails(account_id, size=5)

    def search_emails_by_recipient(self, to_email, size=10, account_id=None):
        """按收件人查邮件(最新优先)。"""
        target = self._normalize_email(to_email)
        if not target:
            return []

        r = self._admin_get(
            "/admin/mails",
            params={"limit": size, "offset": 0, "address": target},
        )
        if r.status_code != 200:
            return []
        try:
            data = r.json() or {}
        except Exception:
            return []

        out = []
        for row in data.get("results", []):
            row_addr = self._normalize_email(row.get("address"))
            if row_addr and row_addr != target:
                continue
            normalized = self._normalize_mail_record(row)
            if account_id is not None:
                normalized["accountId"] = account_id
            out.append(normalized)
        return out

    def delete_emails_for(self, to_email):
        """删除指定收件人的全部邮件。优先 clear_inbox,其次逐封删除。"""
        target = self._normalize_email(to_email)
        if not target:
            return 0

        r = self._admin_delete(f"/admin/clear_inbox/{target}")
        if r.status_code == 200:
            try:
                body = r.json() or {}
            except Exception:
                body = {}
            if body.get("success"):
                logger.info("[CloudMail] 已清空 %s 的收件箱", target)
                return 1

        # 回退:逐封删除
        mails = self.search_emails_by_recipient(target, size=100)
        deleted = 0
        for mail in mails:
            mid = mail.get("emailId")
            if not mid:
                continue
            try:
                resp = self._admin_delete(f"/admin/mails/{mid}")
                if resp.status_code == 200:
                    deleted += 1
            except Exception:
                pass
        if deleted:
            logger.info("[CloudMail] 已逐封删除 %s 的 %d 封旧邮件", target, deleted)
        return deleted
