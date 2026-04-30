# SPEC-Shared: OAuth 子进程默认超时(`OAUTH_SUBPROCESS_TIMEOUT_S`)

## 0. 元数据 + 引用方

| 字段 | 内容 |
|---|---|
| 名称 | OAuth 子进程默认超时常量(`OAUTH_SUBPROCESS_TIMEOUT_S`)与契约 |
| 版本 | **v1.0.0(2026-04-29 Round 11 五轮 spec-update)** |
| Status | STABLE — 共享常量,任何 subprocess 包裹 OAuth 的代码必须引用 |
| Owner | AutoTeam manager 子系统(`src/autoteam/manager.py`) |
| 主题归属 | OAuth headless 子进程 timeout 上界 + 默认值依据 + 可覆盖渠道 + 引用方契约 + 不引用此常量的边界(生产同步路径) |
| 引用方 | Round 11 五轮 task `04-28-round11-master-resub-models-validate` / `oauth-workspace-selection.md` v1.5.0 §4.4(stage 1 快路径耗时实证)/ `account-state-machine.md` v2.1.2 §4.7(issuer ledger TTL — retry backoff 关系)/ `master-subscription-health.md` v1.4 §15(M-OA-backoff — 失败堆积保护机制,与本常量不同维度) |
| 共因 | Round 11 五轮三号探测发现 60s 子进程硬超时太短(实测 P95 < 180s),累积 D1/D2 timeout 假象;本常量统一 200s safety margin,任何 subprocess 包裹 `login_codex_via_browser` 的工具(probe 脚本 / 异步 worker / CLI 工具)必须引用,杜绝硬编码 60s 的回归 |
| 不在范围 | 生产同步路径(`_run_post_register_oauth*` 走 `_pw_executor.run(func) → run_with_timeout(300, ...)`,见 `api.py:467`)与本常量解耦 / 单步 OAuth 内部探针超时(MAIL_TIMEOUT 等独立常量)/ M-OA-backoff 失败堆积频率保护(见 `master-subscription-health.md` v1.4 §15) |

---

## 1. 概念定义

| 术语 | 定义 |
|---|---|
| `OAuth 子进程` | 任何 `subprocess.run` / `subprocess.Popen` 包裹 `python -c "...login_codex_via_browser(...)"` 的进程,典型场景:probe 脚本 / 一次性 CLI 工具 / 异步 worker 派生 |
| `OAUTH_SUBPROCESS_TIMEOUT_S` | 模块级常量,定义在 `src/autoteam/manager.py:82`,值 = `int(os.environ.get("OAUTH_SUBPROCESS_TIMEOUT_S", "200"))`,单位秒 |
| `生产同步路径` | `_run_post_register_oauth_team` / `_run_post_register_oauth_personal`(`manager.py`)在 fill / rotate 主流程内**同线程**同步执行 OAuth,经 `_pw_executor.run(func)`(`api.py:465-489`)派发至专用线程,**默认 300s timeout**,**不引用本常量** |
| `headless OAuth P95` | Round 11 五轮 P1 实测 `fd3b5ccae1@zrainbow1257.com` headless OAuth 全链路(域 cookie 注入 + step-0 + consent loop + auth code 交换)耗时 71.3s,P95 估算 < 180s(`p1-p2-execution-report.md` §P1) |
| `safety margin` | 200s = 71s × ~2.8 倍(覆盖 P95 + 网络抖动 + consent loop 多步密码 reentry 极端情况),实测 P1 实际只用 71s,200s 留 ~130s 余量 |

---

## 2. 常量定义(`manager.py:77-82`)

```python
# Round 11 二轮收尾 — OAuth 子进程默认超时(秒)。任何 subprocess 包裹 login_codex_via_browser
# (probe 脚本 / 异步 worker / CLI 工具) 必须用此常量,不再硬编码 60s。
# 实测 P95 < 180s(参考 P1 报告 fd3b5ccae1 实测 71.3s headless OAuth 完成),
# 200s 为 safety margin。生产路径(_run_post_register_oauth)在主线程内同步执行,
# 由 _pw_executor.run 默认 300s 包裹,不走本常量。
OAUTH_SUBPROCESS_TIMEOUT_S = int(os.environ.get("OAUTH_SUBPROCESS_TIMEOUT_S", "200"))
```

### 2.1 字段契约

| 字段 | 值 | 备注 |
|---|---|---|
| 名称 | `OAUTH_SUBPROCESS_TIMEOUT_S` | 全大写 + 单位后缀 `_S` 表示秒 |
| 默认值 | `200`(秒) | 覆盖 headless OAuth P95 < 180s + 余量 |
| 环境变量覆盖 | `OAUTH_SUBPROCESS_TIMEOUT_S` | 同名,部署时可调(运维可临时调高/调低) |
| 类型 | `int` | `int(os.environ.get(..., "200"))` 强制 int,非数字会抛 `ValueError`(进程启动期失败,符合 fail-fast)|
| 模块位置 | `src/autoteam/manager.py:82` | 与 `MAIL_TIMEOUT`(`manager.py:75`)邻接放置,集中管理 |
| 暴露范围 | `from autoteam.manager import OAUTH_SUBPROCESS_TIMEOUT_S` | 任何包裹方都通过 import 引用,不允许 hardcode |

---

## 3. 调用方契约(强制)

### 3.1 必须引用本常量的场景

任何**通过子进程**(`subprocess.*` / `multiprocessing` 派生进程)包裹 `login_codex_via_browser`、`SessionCodexAuthFlow.start`、或任何完整 OAuth 链路的代码**必须**:

1. `from autoteam.manager import OAUTH_SUBPROCESS_TIMEOUT_S`
2. 把该常量作为子进程 timeout 参数传给 `subprocess.run(..., timeout=OAUTH_SUBPROCESS_TIMEOUT_S)`
3. **禁止**硬编码任何具体秒数(60 / 90 / 120 / 180 / 200 / 300 等),即使该数字"碰巧"等于当前默认值

### 3.2 典型用法

```python
# ✅ 正确:通过常量,部署可覆盖
from autoteam.manager import OAUTH_SUBPROCESS_TIMEOUT_S

result = subprocess.run(
    [sys.executable, "-c", "import autoteam.codex_auth; autoteam.codex_auth.login_codex_via_browser(...)"],
    timeout=OAUTH_SUBPROCESS_TIMEOUT_S,
    capture_output=True, text=True,
)
```

```python
# ❌ 错误:硬编码,失去环境覆盖能力 + 与生产基线脱节
result = subprocess.run([...], timeout=60, ...)   # 60s — Round 11 五轮已废
result = subprocess.run([...], timeout=200, ...)  # 200s — 即使等于默认也禁止 hardcode
```

### 3.3 禁止的反例

| 反例 | 问题 |
|---|---|
| 硬编码 `timeout=60` | Round 11 五轮已实证 60s 不够(`p1-p2-execution-report.md` 提到之前的 D1/D2 timeout 是工具配置不当,本质是太短) |
| 硬编码 `timeout=200` | 即使数字等于默认值,部署侧无法通过 `OAUTH_SUBPROCESS_TIMEOUT_S=300` 覆盖 |
| 自定义模块级常量(如 `_MY_OAUTH_TIMEOUT = 200`)| 重复定义,无单一真相;调整时必须双侧 sync |
| `os.environ.get("OAUTH_SUBPROCESS_TIMEOUT_S", "60")` 直接读 | 默认值不一致导致脚本与主线程默认值漂移 |

---

## 4. 不引用本常量的场景(明确边界)

### 4.1 生产同步路径(`_run_post_register_oauth*`)

`manager.py:_run_post_register_oauth_team` 与 `_run_post_register_oauth_personal` 在 fill / rotate 巡检主流程内**同线程**调用 `login_codex_via_browser`,经 `_pw_executor.run(func)`(`api.py:465-489`)派发至 Playwright 专用线程,**默认 300s timeout**(`api.py:467`)。

```python
# api.py:465-467
def run(self, func, *args, **kwargs):
    """在专用线程中执行函数，阻塞等待结果(默认 5 分钟)"""
    return self.run_with_timeout(300, func, *args, **kwargs)
```

**为什么不引用本常量**:
- 生产路径不创建子进程,在同一进程的专用线程里执行 → 没有"进程间 timeout"概念
- `_pw_executor` 的 300s 是 **单步 Playwright 操作上界**,而非 OAuth 全链路上界(全链路由调用方代码自身控制)
- 历史上 `_pw_executor.run` 的 300s 与本常量 200s 各自独立演化,无强同步关系

### 4.2 单步 OAuth 内部探针超时

| 常量 | 含义 | 与本常量关系 |
|---|---|---|
| `MAIL_TIMEOUT`(180s,`manager.py:75`)| cloudmail 拉 OTP 邮件单次轮询上界 | 独立,不嵌套,典型 OAuth 流程会消耗一次 `MAIL_TIMEOUT` |
| Playwright `page.goto(timeout=...)` | 单次页面跳转超时 | 独立,内部值,与外层子进程超时无关 |
| Playwright `wait_for_*(timeout=...)` | 单元素等待超时 | 独立 |
| `cheap_codex_smoke` `httpx` timeout | 单次 wham 探针超时 | 独立 |

任何**外部 wrapper 之内**的细粒度超时**不**应替换为本常量,否则破坏内部超时的语义。

### 4.3 M-OA-backoff 失败堆积保护(`master-subscription-health.md` v1.4 §15)

| 维度 | `OAUTH_SUBPROCESS_TIMEOUT_S`(本 spec) | `M-OA-backoff` 4h cooldown(§15) |
|---|---|---|
| 触发条件 | **每次** OAuth 子进程启动,被动上界 | **多次失败累积** ≥ 3 条 / 2h,主动延长冷却 |
| 单位 | 秒(单次 OAuth 上界) | 小时(失败间隔下界) |
| 调整影响 | 调高 → 降低误报 timeout / 调低 → 加速失败暴露 | 调高 → 减少邮箱浪费 / 调低 → 失败响应更激进 |
| 互斥/共生 | **正交**,任意一者触发都能阻止异常累积 | 同左 |

**两者不可替代,必须并存**:本常量保单次 OAuth 不被错误地早 abort;M-OA-backoff 保多次 OAuth 失败时的失败间隔。

---

## 5. 实证依据(Round 11 五轮 P1 报告)

引用 `.trellis/tasks/04-28-round11-master-resub-models-validate/research/p1-p2-execution-report.md` §P1:

| 指标 | 实测值 | 来源 |
|---|---|---|
| `fd3b5ccae1@zrainbow1257.com` headless OAuth 全链路 | **71.3s** | `p1-p2-execution-report.md` 结果 JSON `elapsed_seconds: 71.3` |
| 阶段 1(快路径)实测耗时占比 | 100%(未进 stage 2) | 同上 §"判定"块 |
| consent loop 步数 | 4 步(含 password reentry × 2) | 同上 §"关键路径(stderr / stdout 提取)" |
| OAuth issuer 端 `oai-oauth-session.workspaces[]` | `[]`(已清,见 §4.7 issuer ledger TTL)| stderr line `11:44:07` |
| 子进程父端 timeout 设置 | `subprocess.run(timeout=200)` | `p1_long_oauth_fd3b5ccae1.py` 实测脚本(参见报告 §P1 配置表)|

**P95 估算**:71.3s 是一次成功路径,考虑 stage 2 fresh re-login fallback(workspaces=[] 但需 Cloudflare warm-up + cookie clear + keyboard.type 重输密码)估算多 60-90s,即上界估约 130-180s。**200s safety margin = ~2.8 × P50 = ~1.1 × P95 上限**。

---

## 6. 测试与验证

### 6.1 单测期望

| Case | 测试 | 关键断言 |
|---|---|---|
| OST-T1 | `test_oauth_subprocess_timeout_module_constant_exists` | `from autoteam.manager import OAUTH_SUBPROCESS_TIMEOUT_S; assert OAUTH_SUBPROCESS_TIMEOUT_S == 200`(默认值守恒) |
| OST-T2 | `test_oauth_subprocess_timeout_env_override` | 设 `OAUTH_SUBPROCESS_TIMEOUT_S=300`,reload module,常量等于 300 |
| OST-T3 | `test_oauth_subprocess_timeout_invalid_env_raises` | 设 `OAUTH_SUBPROCESS_TIMEOUT_S=abc`,reload module 抛 `ValueError`(fail-fast) |
| OST-T4 | `test_no_hardcode_60s_in_subprocess_callsites`(锚 grep) | `grep -n "subprocess.run.*timeout=60" src/autoteam/ scripts/` 应无命中 |
| OST-T5 | `test_subprocess_callsites_import_constant`(锚 grep) | 任何 `subprocess.run(...timeout=...)` 包裹 `login_codex_via_browser` 的位点必含 `OAUTH_SUBPROCESS_TIMEOUT_S` 字符串引用 |

### 6.2 锚 grep 防回归

```bash
# 期望:命中位点全部使用本常量
rg "subprocess\.run.*login_codex_via_browser" src/autoteam/ scripts/

# 期望:无命中(任何硬编码 60s 都是回归)
rg "timeout=60" src/autoteam/ scripts/ | rg -v "MAIL_TIMEOUT|test_"

# 期望:无命中(任何硬编码 200s 都是反例,即使等于默认)
rg "timeout=200" src/autoteam/ scripts/ | rg -v "OAUTH_SUBPROCESS_TIMEOUT_S|test_"
```

---

## 7. 不变量(`OST-Ix`)

> **OST-I1(强制)**:`subprocess.*` 包裹 `login_codex_via_browser` 或任何完整 OAuth 链路时,`timeout=` 参数**必须**等于 `OAUTH_SUBPROCESS_TIMEOUT_S`(从 `autoteam.manager` import)。

> **OST-I2(强制)**:`OAUTH_SUBPROCESS_TIMEOUT_S` 默认值不得低于 180(Round 11 五轮 P95 实测下界);若需调低,必须先在新一轮实测中验证 P95 不变,并同步更新本 spec §5 实证依据。

> **OST-I3(强制)**:`_pw_executor.run` 的 300s 默认 timeout 是**生产同步路径的独立保护**,**不**应被本常量替换;两者维度不同(单步 Playwright vs 子进程全链路 OAuth)。

> **OST-I4(允许)**:特定 OAuth 任务在已知慢路径(如 stage 2 fresh re-login + Cloudflare 长 challenge)下可通过 `subprocess.run(..., timeout=OAUTH_SUBPROCESS_TIMEOUT_S * 2)` 显式扩大 — 但**禁止**直接传数字;倍数必须基于该常量。

> **OST-I5(允许)**:运维侧通过 `OAUTH_SUBPROCESS_TIMEOUT_S=...` 环境变量临时调整不需要发版;但调整决策应在 round backlog / runbook 留迹,避免无据漂移。

---

## 8. 部署 / 运维指引

### 8.1 默认部署

无需任何环境变量,默认 `OAUTH_SUBPROCESS_TIMEOUT_S=200` 即可。

### 8.2 临时调整

```bash
# Linux / WSL
export OAUTH_SUBPROCESS_TIMEOUT_S=300
python -m autoteam.scripts.oauth_probe ...

# Windows PowerShell
$env:OAUTH_SUBPROCESS_TIMEOUT_S = "300"
python -m autoteam.scripts.oauth_probe ...
```

### 8.3 调整时机

| 场景 | 建议 |
|---|---|
| 网络环境差(企业代理 / 跨境延迟)| 调高至 300-400s |
| 容器资源吃紧(headless Chromium 慢)| 调高至 300s |
| CI / 单元测试要求快速失败 | 调低至 90-120s |
| 调试 stage 2 fresh re-login 路径 | 调高至 400-500s |

### 8.4 与生产同步路径的协同

| 场景 | 生效项 |
|---|---|
| 巡检 fill 主流程(`_run_post_register_oauth_team`)| `_pw_executor.run` 的 300s,**不**走本常量 |
| 异步 worker / probe 脚本(子进程 OAuth)| `OAUTH_SUBPROCESS_TIMEOUT_S` 200s 默认 |
| 调高生产 fill 容忍度 | 改 `_pw_executor` 的 300s 默认(目前未参数化,需代码改动)|
| 调高 probe 工具容忍度 | 改 `OAUTH_SUBPROCESS_TIMEOUT_S` 环境变量,重启即生效 |

---

## 9. 关联文档

- `oauth-workspace-selection.md` v1.5.0 §4.4 — issuer ledger TTL 与 retry backoff 关系(本常量是 retry 内单次上界)
- `account-state-machine.md` v2.1.2 §4.7 — 状态机层面的 ledger TTL 现象(本常量是 OAuth 重试链路单步保护)
- `master-subscription-health.md` v1.4 §15 — M-OA-backoff 失败堆积保护(独立维度,见 §4.3 对比)
- `manager.py:77-82` — 常量定义实现锚点
- `api.py:465-467` — `_pw_executor.run` 300s 生产路径保护(明确不引用本常量的边界)
- `.trellis/tasks/04-28-round11-master-resub-models-validate/research/p1-p2-execution-report.md` — 实证 71.3s 数据来源

---

## 附录 A:修订记录

| 版本 | 时间 | 变更 |
|---|---|---|
| **v1.0.0** | **2026-04-29 Round 11 五轮 spec-update** — 初版。文档化 `OAUTH_SUBPROCESS_TIMEOUT_S = 200` 模块级常量(`src/autoteam/manager.py:82`,Round 11 二轮收尾引入但未独立文档化)。**为什么独立成 spec**:Round 11 五轮三号探测发现 60s 子进程硬超时太短(P95 实测 < 180s,详见 P1 报告 fd3b5ccae1 71.3s),先前 D1/D2 timeout 是工具配置不当假象;200s safety margin 的依据(~2.8 × P50 = ~1.1 × P95 上限)需要独立文档作为后续调整的事实基线;subprocess 包裹方契约(必须 import 不许 hardcode)需要可锚定的不变量(OST-I1~I5)。**正文 9 节**:§0 元数据 / §1 概念定义(子进程 vs 生产同步路径)/ §2 常量定义 + 字段契约 / §3 调用方契约(必须 import + 反例对比)/ §4 不引用本常量的场景(`_pw_executor.run` 300s / 内部细粒度 timeout / M-OA-backoff)/ §5 实证依据(P1 报告 71.3s)/ §6 测试 OST-T1~T5(单测 + 锚 grep 防回归)/ §7 不变量 OST-I1~I5 / §8 部署运维指引(默认值 + 环境变量覆盖 + 调整时机) / §9 关联文档。**纯 spec 增量**,无代码改动 — manager.py:77-82 常量定义已在 Round 11 二轮落地。 |

---

**文档结束。** 工程师据此可直接在新增 / 改造 OAuth subprocess 包裹方时引用此常量,无需额外决策;运维据此可决定是否设置环境变量覆盖。
