"""首次启动初始化向导 — 交互式填写 .env 中的必填配置"""

import logging
import os
import re
import secrets

from autoteam.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

ENV_FILE = PROJECT_ROOT / ".env"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"

# 需要交互式输入的配置项（key, 提示, 默认值, 是否可选）
REQUIRED_CONFIGS = [
    ("CLOUDMAIL_BASE_URL", "CloudMail API 地址", "", False),
    ("CLOUDMAIL_EMAIL", "CloudMail 登录邮箱", "", False),
    ("CLOUDMAIL_PASSWORD", "CloudMail 登录密码", "", False),
    ("CLOUDMAIL_DOMAIN", "CloudMail 邮箱域名（如 @example.com）", "", False),
    ("CPA_URL", "CPA (CLIProxyAPI) 地址", "http://127.0.0.1:8317", True),
    ("CPA_KEY", "CPA 管理密钥", "", False),
    ("API_KEY", "API 鉴权密钥（回车自动生成）", "", True),
]


def _read_env() -> dict[str, str]:
    """读取 .env 文件为 dict"""
    result = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                result[key.strip()] = value.strip()
    return result


def _write_env(key: str, value: str):
    """写入或更新 .env 中的某个 key"""
    if ENV_FILE.exists():
        content = ENV_FILE.read_text()
        pattern = rf"^{re.escape(key)}=.*$"
        if re.search(pattern, content, re.MULTILINE):
            content = re.sub(pattern, f"{key}={value}", content, flags=re.MULTILINE)
        else:
            content = content.rstrip() + f"\n{key}={value}\n"
        ENV_FILE.write_text(content)
    else:
        # 从 .env.example 复制再写入
        if ENV_EXAMPLE.exists():
            content = ENV_EXAMPLE.read_text()
            pattern = rf"^{re.escape(key)}=.*$"
            if re.search(pattern, content, re.MULTILINE):
                content = re.sub(pattern, f"{key}={value}", content, flags=re.MULTILINE)
            ENV_FILE.write_text(content)
        else:
            ENV_FILE.write_text(f"{key}={value}\n")


def check_and_setup(interactive: bool = True) -> bool:
    """
    检查必填配置是否齐全，缺失时交互式提示输入。
    返回 True 表示配置完整，False 表示用户中断或非交互模式下缺配置。
    """
    env = _read_env()
    missing = []

    for key, prompt, default, optional in REQUIRED_CONFIGS:
        val = env.get(key, "") or os.environ.get(key, "")
        if not val and not optional:
            missing.append((key, prompt, default, optional))
        elif not val and optional and key == "API_KEY":
            missing.append((key, prompt, default, optional))

    if not missing:
        return True

    if not interactive:
        for key, prompt, _, _ in missing:
            logger.warning("[配置] 缺少必填项: %s (%s)", key, prompt)
        return False

    print("\n=== AutoTeam 首次配置 ===\n")
    print("检测到以下配置项需要填写，直接回车使用默认值（如有）:\n")

    for key, prompt, default, optional in missing:
        hint = f" [{default}]" if default else ""
        if key == "API_KEY":
            hint = " [回车自动生成]"

        value = input(f"  {prompt}{hint}: ").strip()

        if not value:
            if key == "API_KEY":
                value = secrets.token_urlsafe(24)
                print(f"    -> 已自动生成: {value}")
            elif default:
                value = default
                print(f"    -> 使用默认值: {value}")
            elif not optional:
                print("    -> 跳过（必填项，后续可在 .env 中补充）")
                continue

        if value:
            _write_env(key, value)
            # 同步到当前进程的环境变量
            os.environ[key] = value

    print("\n配置已保存到 .env\n")

    # 重新加载 config 模块
    import importlib

    import autoteam.config

    importlib.reload(autoteam.config)

    return True
