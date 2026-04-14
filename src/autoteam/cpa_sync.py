"""CPA (CLIProxyAPI) 认证文件同步 - 保持本地 codex 认证文件与 CPA 一致"""

import json
import logging
import time
from pathlib import Path

import requests

from autoteam.config import CPA_KEY, CPA_URL
from autoteam.textio import write_text

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent
AUTH_DIR = PROJECT_ROOT / "auths"


def _headers():
    return {"Authorization": f"Bearer {CPA_KEY}"}


def list_cpa_files():
    """获取 CPA 中所有认证文件"""
    resp = requests.get(f"{CPA_URL}/v0/management/auth-files", headers=_headers(), timeout=10)
    if resp.status_code != 200:
        logger.error("[CPA] 获取文件列表失败: %d", resp.status_code)
        return []
    data = resp.json()
    return data.get("files", [])


def upload_to_cpa(filepath):
    """上传认证文件到 CPA"""
    filepath = Path(filepath)
    if not filepath.exists():
        logger.warning("[CPA] 文件不存在: %s", filepath)
        return False

    with open(filepath, "rb") as f:
        resp = requests.post(
            f"{CPA_URL}/v0/management/auth-files",
            headers=_headers(),
            files={"file": (filepath.name, f, "application/json")},
            timeout=10,
        )

    if resp.status_code == 200:
        logger.info("[CPA] 已上传: %s", filepath.name)
        return True
    else:
        logger.error("[CPA] 上传失败: %d %s", resp.status_code, resp.text[:200])
        return False


def delete_from_cpa(name):
    """从 CPA 删除认证文件"""
    resp = requests.delete(
        f"{CPA_URL}/v0/management/auth-files",
        headers=_headers(),
        params={"name": name},
        timeout=10,
    )
    if resp.status_code == 200:
        logger.info("[CPA] 已删除: %s", name)
        return True
    else:
        logger.error("[CPA] 删除失败: %d %s", resp.status_code, resp.text[:200])
        return False


def download_from_cpa(name):
    """从 CPA 下载认证文件内容。"""
    resp = requests.get(
        f"{CPA_URL}/v0/management/auth-files/download",
        headers=_headers(),
        params={"name": name},
        timeout=10,
    )
    if resp.status_code == 200:
        return resp.text
    logger.error("[CPA] 下载失败: %s -> %d %s", name, resp.status_code, resp.text[:200])
    return None


def _write_auth_text(path, content):
    write_text(path, content)
    try:
        path.chmod(0o600)
    except Exception:
        pass


def sync_from_cpa():
    """
    从 CPA 反向同步认证文件到本地。

    规则：
    - 下载 CPA 中所有 codex 认证文件到本地 auths/
    - 非主号文件会导入/修复到 accounts.json，默认状态为 standby（保守导入）
    - 不删除本地账号记录，仅补充/更新 auth_file
    """
    from autoteam.accounts import STATUS_STANDBY, find_account, load_accounts, save_accounts

    AUTH_DIR.mkdir(exist_ok=True)

    accounts = load_accounts()
    changed_accounts = False
    imported_files = 0
    updated_files = 0
    added_accounts = 0
    updated_accounts = 0
    skipped = 0

    cpa_files = list_cpa_files()
    if not cpa_files:
        logger.info("[CPA] 未发现可反向同步的认证文件")
        return {
            "downloaded": 0,
            "updated": 0,
            "accounts_added": 0,
            "accounts_updated": 0,
            "skipped": 0,
            "total": 0,
        }

    for item in cpa_files:
        name = (item.get("name") or "").strip()
        if not name or not name.endswith(".json") or not name.startswith("codex-"):
            skipped += 1
            continue

        content = download_from_cpa(name)
        if not content:
            skipped += 1
            continue

        try:
            auth_data = json.loads(content)
        except Exception:
            logger.warning("[CPA] 跳过无效 JSON: %s", name)
            skipped += 1
            continue

        if auth_data.get("type") != "codex":
            logger.info("[CPA] 跳过非 codex 文件: %s", name)
            skipped += 1
            continue

        local_path = AUTH_DIR / name
        existed = local_path.exists()
        previous = None
        if existed:
            try:
                previous = local_path.read_text(encoding="utf-8")
            except Exception:
                previous = None

        if not existed:
            imported_files += 1
        elif previous != content:
            updated_files += 1

        _write_auth_text(local_path, content)

        email = (auth_data.get("email") or item.get("email") or "").lower().strip()
        if name.startswith("codex-main-") or not email:
            continue

        # 清理同邮箱的旧本地文件，保留 CPA 当前下载的版本
        for old in AUTH_DIR.glob(f"codex-{email}-*.json"):
            if old.name != name and old.exists():
                old.unlink()

        acc = find_account(accounts, email)
        resolved_path = str(local_path.resolve())
        if acc:
            if acc.get("auth_file") != resolved_path:
                acc["auth_file"] = resolved_path
                changed_accounts = True
                updated_accounts += 1
        else:
            accounts.append(
                {
                    "email": email,
                    "password": "",
                    "cloudmail_account_id": None,
                    "status": STATUS_STANDBY,
                    "auth_file": resolved_path,
                    "quota_exhausted_at": None,
                    "quota_resets_at": None,
                    "created_at": time.time(),
                    "last_active_at": None,
                }
            )
            changed_accounts = True
            added_accounts += 1

    if changed_accounts:
        save_accounts(accounts)

    logger.info(
        "[CPA] 反向同步完成: 新增文件 %d, 更新文件 %d, 新增账号 %d, 更新账号 %d, 跳过 %d",
        imported_files,
        updated_files,
        added_accounts,
        updated_accounts,
        skipped,
    )
    return {
        "downloaded": imported_files,
        "updated": updated_files,
        "accounts_added": added_accounts,
        "accounts_updated": updated_accounts,
        "skipped": skipped,
        "total": len(cpa_files),
    }


def sync_to_cpa():
    """
    同步本地认证文件到 CPA，只同步 active 状态的账号。
    - active 且 CPA 没有 → 上传
    - CPA 有但不是 active（或本地已删除）→ 从 CPA 删除
    """
    from autoteam.accounts import STATUS_ACTIVE, load_accounts, save_accounts

    accounts = load_accounts()
    local_emails = {a["email"].lower() for a in accounts}

    # 修复断裂的 auth_file 路径
    changed = False
    for acc in accounts:
        auth_path = acc.get("auth_file")
        if auth_path and not Path(auth_path).exists():
            matches = list(AUTH_DIR.glob(f"codex-{acc['email']}-*.json"))
            if matches:
                acc["auth_file"] = str(matches[0].resolve())
                changed = True
    if changed:
        save_accounts(accounts)

    # active 账号的认证文件
    active_files = {}
    for acc in accounts:
        if acc["status"] == STATUS_ACTIVE and acc.get("auth_file"):
            path = Path(acc["auth_file"])
            if path.exists():
                active_files[path.name] = path

    # CPA 认证文件
    cpa_files = list_cpa_files()
    cpa_names = {f["name"]: f for f in cpa_files}

    logger.info("[CPA] active 认证文件: %d, CPA 认证文件: %d", len(active_files), len(cpa_files))

    # 上传：所有 active 认证文件（覆盖同名文件，确保 token 最新）
    uploaded = 0
    for name, path in active_files.items():
        logger.info("[CPA] 上传: %s", name)
        if upload_to_cpa(path):
            uploaded += 1

    # 删除：CPA 中有但不在 active 列表的（仅限本地管理的账号）
    deleted = 0
    for name, cpa_file in cpa_names.items():
        email = cpa_file.get("email", "").lower()
        if email in local_emails and name not in active_files:
            logger.info("[CPA] 删除非 active 文件: %s (%s)", name, email)
            if delete_from_cpa(name):
                deleted += 1

    logger.info("[CPA] 同步完成: 上传 %d, 删除 %d", uploaded, deleted)

    # 最终状态
    final_cpa = list_cpa_files()
    final_local_managed = [f for f in final_cpa if f.get("email", "").lower() in local_emails]
    logger.info("[CPA] CPA 中本地管理: %d, 本地 active: %d", len(final_local_managed), len(active_files))


def sync_main_codex_to_cpa(filepath):
    """同步主号 Codex 认证文件到 CPA。"""
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"主号认证文件不存在: {filepath}")

    name = filepath.name
    existing = {item.get("name"): item for item in list_cpa_files()}

    for old_name in existing:
        if old_name and old_name.startswith("codex-main-"):
            logger.info("[CPA] 删除旧主号文件: %s", old_name)
            delete_from_cpa(old_name)

    if not upload_to_cpa(filepath):
        raise RuntimeError(f"上传主号认证文件失败: {name}")

    logger.info("[CPA] 主号 Codex 已同步: %s", name)
    return {"uploaded": name}
