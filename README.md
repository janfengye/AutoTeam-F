<div align="center">

# AutoTeam

**ChatGPT Team 账号自动轮转管理工具**

自动创建账号、注册、获取 Codex 认证、检查额度、智能轮换，并同步认证文件到 [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![Playwright](https://img.shields.io/badge/Playwright-Chromium-2EAD33?style=for-the-badge&logo=playwright&logoColor=white)](https://playwright.dev)
[![uv](https://img.shields.io/badge/uv-Package_Manager-DE5FE9?style=for-the-badge)](https://docs.astral.sh/uv/)

</div>

---

## 特性

| | 功能 | 描述 |
|---|---|---|
| 📧 | **自动注册** | 创建临时邮箱 → 注册 ChatGPT → 自动填写验证码/个人信息 |
| 🔐 | **Codex OAuth** | 自动完成 Codex 登录，保存 CPA 兼容的认证文件 |
| 📊 | **额度检查** | 检测 Codex 5h 额度，token 过期自动刷新/重新登录 |
| 🔄 | **智能轮转** | 额度用完自动移出，优先复用旧号，万不得已才创建新号 |
| ☁️ | **CPA 同步** | 认证文件自动上传/删除，只同步 active 账号 |
| 👥 | **Team 管理** | 自动补满/清理成员，同步 Team 实际状态 |

## 快速开始

### 安装

```bash
# 一键安装
bash setup.sh

# 或手动
uv sync
uv run playwright install chromium
```

### 配置

```bash
cp .env.example .env   # 复制配置模板，填入实际值
```

| 配置项 | 说明 |
|--------|------|
| **CloudMail** | 临时邮箱服务地址和凭据 |
| **ChatGPT** | Team Account ID（从 ChatGPT admin 页面获取），Workspace 名称可自动检测 |
| **CPA** | CLIProxyAPI 地址和管理密钥 |
| **session** | ChatGPT 管理员的 `__Secure-next-auth.session-token`（拼接 `.0` 和 `.1`）写入 `session` 文件 |

### 使用

```bash
uv run autoteam <command> [args]
```

| 命令 | 说明 |
|------|------|
| `status` | 查看所有账号状态（自动同步 Team 实际成员） |
| `check` | 检查 active 账号额度，token 失效自动重新登录 |
| `rotate [N]` | 智能轮转：检查额度 → 移出用完的 → 复用旧号 → 补满到 N 个（默认 5） |
| `add` | 手动添加一个新账号 |
| `fill [N]` | 补满 Team 成员到 N 个（默认 5） |
| `cleanup [N]` | 清理多余成员到 N 个（只移除本地管理的） |
| `sync` | 手动同步认证文件到 CPA |

**日常只需一条命令：**

```bash
uv run autoteam rotate
```

## 工作原理

### 轮转流程

```
                    ┌─────────────┐
                    │  同步 Team   │
                    │  实际状态    │
                    └──────┬──────┘
                           ▼
                    ┌─────────────┐
                    │  检查所有    │
                    │ active 额度  │
                    └──────┬──────┘
                           ▼
              ┌────────────┴────────────┐
              ▼                         ▼
        额度可用 ✅               额度用完 ❌
        保持不动                  移出 Team
                                       │
                           ┌───────────┴───────────┐
                           ▼                       ▼
                    有旧号额度已恢复？         全部不可用？
                     复用旧号 ♻️             创建新号 🆕
                           │                       │
                           └───────────┬───────────┘
                                       ▼
                                ┌─────────────┐
                                │  同步到 CPA  │
                                └─────────────┘
```

### 账号状态机

```
  ┌──────────┐   额度用完   ┌───────────┐   移出Team   ┌──────────┐
  │  active  │ ──────────→ │ exhausted  │ ──────────→ │ standby  │
  └──────────┘             └───────────┘              └────┬─────┘
       ▲                                                   │
       └───────────── 额度恢复，重新加入 ──────────────────┘
```

| 状态 | 含义 |
|------|------|
| `active` | 在 Team 中，额度可用 |
| `exhausted` | 在 Team 中，额度用完，等待移出 |
| `standby` | 已移出 Team，等待额度恢复后复用 |
| `pending` | 已创建，等待注册完成 |

## 项目结构

```
autoteam/
├── pyproject.toml              # 项目配置 + 依赖
├── setup.sh                    # 一键安装脚本
├── .env.example                # 配置模板
└── src/autoteam/
    ├── manager.py              # CLI 入口，所有命令
    ├── config.py               # 配置加载（从 .env）
    ├── display.py              # 虚拟显示器自动设置
    ├── accounts.py             # 账号池持久化管理
    ├── chatgpt_api.py          # ChatGPT 内部 API
    ├── cloudmail.py            # CloudMail API 客户端
    ├── codex_auth.py           # Codex OAuth + token 管理
    ├── cpa_sync.py             # CPA 认证文件同步
    └── invite.py               # 注册流程自动化
```

## 认证文件格式

兼容 CLIProxyAPI，文件名格式：`codex-{email}-{plan_type}-{hash}.json`

```json
{
  "type": "codex",
  "id_token": "eyJ...",
  "access_token": "eyJ...",
  "refresh_token": "rt_...",
  "account_id": "...",
  "email": "...",
  "expired": "2026-04-20T10:00:00Z",
  "last_refresh": "2026-04-10T10:00:00Z"
}
```

## 依赖

| 依赖 | 用途 |
|------|------|
| [Python 3.10+](https://python.org) | 运行环境 |
| [uv](https://docs.astral.sh/uv/) | 包管理 |
| [Playwright](https://playwright.dev) | 浏览器自动化 (Chromium) |
| xvfb | Linux 无头服务器虚拟显示 |
| [CloudMail](https://github.com/maillab/cloud-mail) | Cloudflare Workers 临时邮箱服务 |
| [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) | Codex 代理，认证文件同步目标 |

## 友情链接

感谢 **LinuxDo** 社区的支持！

[![LinuxDo](https://img.shields.io/badge/社区-LinuxDo-blue?style=for-the-badge)](https://linux.do/)
