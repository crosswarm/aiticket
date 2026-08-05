# AITicket 测试与日志规范

面向所有在本仓（及其三条血缘）上开发、调试、修复问题的人和 Agent。
参考 `ycc-approve-inbox` 的工程约定，但按本仓实际踩过的坑做了定制。

---

## 一、测试

### 1.1 怎么跑

**永远用统一入口，不要直接敲 `pytest`：**

```bash
bash scripts/dev/run-tests.sh                    # 全量
bash scripts/dev/run-tests.sh tests/test_x.py    # 单个文件
AITICKET_PYTHON=/path/to/python3 bash scripts/dev/run-tests.sh   # 指定解释器
```

它按「版本 ≥3.10 且有 pytest」自动探测解释器，三层降级：
`/data/pytools`（容器/172）→ 本机 pytest → 标准库 `unittest`（兜底）。

**为什么不能写死解释器**——两个真实教训：

| 环境 | 坑 |
|---|---|
| 172 服务器 | 离线，且镜像 `requirements.txt` 不含任何测试依赖 → 直接 `pytest` 是 `No module named pytest` |
| 本机 Mac | 唯一自带 pytest 的 `/usr/bin/python3` 是 **3.9**，而代码用了 PEP 604 的 `X \| None`（需 3.10+）→ 它连 app 代码都 import 不了 |

### 1.2 离线机器怎么装 pytest

**不改 `requirements.txt`、不重建镜像、不影响运行时**：

```bash
# 有网的机器上抓 wheel（按容器的 py3.11 / manylinux 平台）
bash scripts/dev/install-pytest-offline.sh fetch

# 拷到目标机后装进容器的持久卷
bash scripts/dev/install-pytest-offline.sh install _local/wheels aiticket

# 容器本身有网时一步到位
bash scripts/dev/install-pytest-offline.sh direct aiticket
```

装到 `/data/pytools`。`/data` 是命名卷，**容器 recreate 不丢**。

### 1.3 写在哪、怎么命名

- 1:1 命名：`APP/backend/<路径>/<module>.py` ↔ `APP/backend/tests/test_<module>.py`
- `conftest.py` 已统一处理 `sys.path`，**新测试不要再写 `sys.path.insert`**，直接 `import auth_service` / `from services.x import y`

### 1.4 可测性铁律

> **纯逻辑不要塞进 `main.py`。**

`main.py` 有 7600+ 行，`import main` 会**拉起 Chroma、启动 5 个后台线程**
（session-keepalive、claude-task-scanner、cleanup…）。任何为了测一个纯函数
而 `from main import` 的测试，都会连带启动真实后台作业。

实测对照：`_shrink_badcase_context` 原本写在 `main.py` 里，单测在只装了标准库的
环境里**根本跑不起来**；抽成 `services/badcase_context.py`（只依赖标准库）后，
同样的测试 **0.02 秒**跑完。

新增纯函数请放 `services/` 下的独立模块。

### 1.5 按任务范围的最小验证

| 改动范围 | 最小验证 |
|---|---|
| 文档 / 治理 / git 规则 | `git diff --check`；改了 hook 就把 hook 跑一遍 |
| Python 逻辑 | `python3 -m py_compile <改动文件>` + 相关 `run-tests.sh tests/test_<module>.py` |
| 前端 HTML 内联脚本 | 抽出 `<script>` 块逐个 `node --check`（改版式不算验证） |
| 涉及五道闸 / 回复链路 | 相关单测 + 在 172 上真实调一次端点，核对日志里的 trace |
| 涉及 172 部署 | 容器内文件哈希 vs 仓库、真实 HTTP 状态码、`git status` 干净 |

### 1.6 两条红线

1. **测试失败时记录真实阻断原因**，不得靠放宽断言、改无关文件、伪造数据蒙混通过。
2. **不要只看「页面能打开」「接口返回 200」**就判定通过；要核对实际落库/落盘内容。
   （本会话真实案例：`/api/badcase/mark` 一直返回 `ok:true`，但负配对文件一行没写，
   因为 numpy `float32` 序列化失败被 `except` 吞成了一条 warning。）

### 1.7 当前状态与待办

- 基线（2026-08-05）：**17 failed / 404 passed / 1 skipped**。
  那 17 个是**既有失败**，分布在 `test_board_query` / `test_guide_system` /
  `test_kb_analysis_api` / `test_llm_service` / `test_priority_queue` /
  `test_session_isolation`，**不是本次改动引入的**，也未通过放宽断言"修绿"。
- 覆盖：154 个源文件 / 37 个测试文件。核心链路已补齐：
  `query_builder`、`reply_reuse_evaluator`、`gate_decision_log`、
  `reply_diff_analyzer`、`reply_supervisor_agent`、`badcase_context`、`logging_setup`。
- **待办**：`board_service_chroma.py`（5261 行）尚无测试。它 import 即拉起 Chroma
  与后台线程，硬写只会得到又慢又脆的空壳。正确做法是先把其中的纯函数
  （评分、归一化、列分配）抽到 `services/` 下，再按 1:1 补测试。

---

## 二、日志

### 2.1 三流拆分

| 文件 | 收什么 | 轮转 |
|---|---|---|
| `main.log` | 应用与模块日志（INFO+），**不含请求流水** | 10MB × 5 |
| `access.log` | 请求流水（method/path/status/耗时） | 20MB × 3 |
| `error.log` | 只收 WARNING+ | 5MB × **20** |

`error.log` 留存份数给得多，是因为改造前的实测数据：**3 小时 49 分产生 33,096 行，
99.1% 是 INFO 访问流水，全窗口只有 2 条 ERROR**；`main.log` 5 个轮转文件约 4 小时
就写满 —— 等你想查问题时，现场往往已经被冲掉了。

落点由 `services/logging_setup.resolve_log_dir()` 决定：
`AITICKET_LOG_DIR` → `/app/logs`（容器挂载卷）→ `APP/backend/logs`（本机开发）。

### 2.2 两个曾经的盲区（已修，勿再引入）

1. **不要静音 `uvicorn.error`。** 曾经 `handlers=[]` + `propagate=False`，
   导致启动期 import 崩溃的栈只在容器 stdout。`/api/agents/*` 全 404 那次排查困难，
   根因就是这个 —— `main.log` 里一个字都没有。
   （现在只静音 `uvicorn.access`，因为流水已由 `access.log` 承担。）
2. **root logger 必须有 handler。** 曾经只有 `ai_ticket` 配了 handler，
   各模块 `logging.getLogger(__name__)` 冒泡到无 handler 的 root，**写了等于没写**。
   `[BadcaseMark] float32 序列化失败` 那条告警当时就只出现在 `docker logs` 里。

`tests/test_logging_setup.py` 已把这两条钉成回归测试。

### 2.3 trace_id

- 中间件为每个请求生成（或沿用请求头传入的）`X-Trace-Id`，写进 contextvar。
- 该请求产生的**所有**日志行都带 `[trace]`，响应头也回传。
- badcase 上报会把 trace 一并带回，因此可以直接：

```bash
grep '<trace-id>' APP/backend/logs/main.log     # 或容器内 /app/logs/main.log
```

一把捞出那次请求的全链路日志。没有请求上下文时（启动期、后台线程）显示 `-`。

### 2.4 加日志的习惯

- 高频、低价值的流水 → `access_logger`，不要进 `main.log`。
- 失败路径**必须**留下可判定的证据。如果一个失败被 `except` 吞成 warning，
  就应同时在**响应里暴露状态**（例如 `/api/badcase/mark` 的 `negative_pair_recorded`），
  否则调用方永远不知道出了事。

---

## 三、提交闸门

```bash
bash scripts/dev/install-hooks.sh     # 每个 clone 跑一次
```

> 仓库里一直有 `.githooks/pre-commit`，但从没人设过 `core.hooksPath`，
> 所以它**从未生效过**。

四道检查：

1. `git diff --cached --check`（空白 / 冲突标记）
2. **运行时数据与凭据拦截** —— 拒绝暂存 `APP/backend/data/*.jsonl|*.db`、
   `eval/`、`logs/`、`.env`、含 `cookie|token|secret` 的文件，以及 patch 里的
   私钥 / GitHub / GitLab / Bearer token / `JSESSIONID`
3. schedule 注册一致性（原有）
4. **测试伴随检查** —— 改了源文件却没有对应 `tests/test_<module>.py` 时列出清单

第 4 条目前**只告警不阻断**：存量 122 个源文件没有测试，一上来就阻断只会让所有人
习惯性 `--no-verify`，闸门反而作废。核心链路补齐后再对白名单目录升级为强制。

为什么第 2 条重要：172 上 `<repo>/APP` 是 bind-mount 进容器的，服务一写就落到宿主
git 工作区。没被忽略的运行时文件会让下次 `git pull --ff-only` 被脏工作区挡住、
**发布中断**。本会话已因这类问题踩过三次。
