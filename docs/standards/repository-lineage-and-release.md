# AITicket 仓库血缘与发布管理规范

本文件是 AITicket 跨项目、跨 Agent 的仓库血缘、分支同步、环境发布和认证数据变更规范。任何 Agent 在执行相关任务前都必须先读本文件。

## 适用范围与维护规则

- 触发场景：跨仓移植、分支同步、向 Mini 或 172 发布、认证数据库或密钥变更、用户数据批量变更、发布验收与交接。
- 本规范在以下两个仓库中各有一份内容完全相同的镜像；修改时必须在同一变更中同步更新，禁止只改一份：
  - `/Users/cfone/Studio/aiticket/docs/standards/repository-lineage-and-release.md`
  - `/Users/cfone/Studio/aiticket-deployable/docs/standards/repository-lineage-and-release.md`
- `.claude/rules/aiticket-repository-lineage-and-release.md` 是 Claude Code 的自动发现入口；`AGENTS.md` 是 Codex 和通用 Agent 的强制入口。
- Codex ad-hoc 记忆只保存索引和阶段快照，本文件是稳定规范的权威来源。
- 本文件不得保存用户明文密码、API token、SSH 密钥或 Cookie。

## 1. 仓库与分支血缘

- 主项目工作区：`/Users/cfone/Studio/aiticket`。
  - 本地主开发分支为 `main`。
  - `origin` 指向 `git@github.com:crosswarm/aiticket.git`。
  - `deploy` 指向 `qcl:/opt/ai-ticket/repo.git`。
  - 工作区经常存在用户未提交改动。任何 Agent 必须先读取 `git status`，只暂存本任务的精确文件或 hunk，不得覆盖、清理或顺带提交其他改动。
- 可部署项目工作区：`/Users/cfone/Studio/aiticket-deployable`。
  - 本地 `main` 是 deployable 主线。
  - `origin` 指向 `git@github.com:crosswarm/aiticket.git`。
  - `yyrd` 指向 `git@git.yyrd.com:hushuq/aiticket.git`，生产离线分支为 `offline-deploy`。
  - `main_repo` 指向 `/Users/cfone/Studio/aiticket`，仅用于只读比较或明确的跨仓移植。
- 截至 2026-07-18，deployable 本地 `main` 与 yyrd `offline-deploy` 没有共同 merge-base，属于不同根历史。禁止直接 merge；同一功能必须形成单一、可审查提交，再分别 cherry-pick 或外科式移植。
- 标准发布落点：
  1. `/Users/cfone/Studio/aiticket` 本地 `main`；
  2. `/Users/cfone/Studio/aiticket-deployable` 本地 `main`；
  3. `git.yyrd.com:hushuq/aiticket.git` 的 `offline-deploy`；
  4. Mini 运行环境；
  5. 172 运行环境。
- 不得默认推送 GitHub `origin/main`。只有用户明确授权并完成待推送集合审查后才能推送。

## 2. 跨仓同步规则

- 优先在与目标生产分支同血缘的功能分支实现、测试和审查，形成边界清晰的功能提交。
- 推送 yyrd `offline-deploy` 前必须 fetch 或 `ls-remote`，确认远端仍是预期基点，只允许 fast-forward；远端已移动时停止并重新评估。
- deployable 本地 `main` 使用临时 worktree cherry-pick，禁止为了同步而切换或污染当前脏工作区；验证后删除临时 worktree。
- 主项目 `aiticket/main` 可能与 deployable 有主线差异。不得整文件覆盖共享核心文件，应基于当前主线语义手工合并，并精确暂存 hunk。
- 交接必须报告：源提交、每个落点的实际 SHA、是否 push、测试证据、冲突处理、保留的用户脏文件及未完成项。

## 3. Mini 发布规范

- Mini 为 `CrossMini.local`；Compose 项目 `deployable`，容器 `aiticket`，镜像 `deployable-aiticket`。
- 宿主 `18090` 映射容器 `18000`。标准命令：`PORT=18090 docker compose -p deployable up -d --no-deps --build --wait aiticket`。
- 容器没有源码 bind mount，restart 不会发布新代码，必须生成新镜像并重建容器。
- 全量构建仅因外部包代理失败且依赖未变化时，才可使用受控增量镜像：先留不可变回滚标签，再只 COPY 已审查的运行时代码，记录功能提交 label，然后 `--no-build --force-recreate --wait`。
- 发布后验证：容器 `running/healthy`、`restarts=0`，并真实 GET `/health`、`/api/board/stats`、`/settings.html`。
- 人员范围：pmlist 人员只创建在 172；Mini 不创建该名单账号，只保留原管理员。Mini 仍部署用户管理功能代码。

## 4. 172 发布规范

- 目标：`root@172.20.46.75`。诊断入口为 deployable 的 `_local/remote_diag.sh`，显式设置 `RD_TARGET=root@172.20.46.75`。
- 服务器仓库：`/root/aiTicket/aiticket`，分支 `offline-deploy`；容器名 `aiticket`。
- 实际 Compose 控制面是 `/opt/aiticket/docker-compose.yml`（项目 `aiticket`），不是服务器仓库根目录的 Compose 文件。
- 宿主 `80` 映射容器 `18000`。宿主验证用 `http://127.0.0.1/...`；容器内验证用 `http://127.0.0.1:18000/...`。
- 标准顺序：确认 yyrd SHA → `git pull --ff-only origin offline-deploy` → 核验 HEAD 等于目标 SHA → `docker restart aiticket` → 等待健康 → 检查日志和真实 HTTP 端点。
- 每次发布必须核对容器内目标文件内容或哈希，不能只相信 `git pull` 输出。

## 5. 认证数据变更与回滚

- 认证数据库为 `/data/app_auth.db`，密钥为 `/data/app_auth.key`；二者位于持久卷，严禁提交 Git 或烘入镜像。
- `APP_AUTH_DB_PATH=/data/app_auth.db` 与 `APP_AUTH_SECRET_PATH=/data/app_auth.key` 必须同时设置。只持久化数据库会导致容器重建后 Jira、PM、LLM 密文无法解密。
- 用户变更前必须用 SQLite `Connection.backup()` 在线一致性备份到 `/data/backups/`；WAL 模式不得只复制主 `.db`。
- `.dockerignore` 必须排除 `app_auth.db`、`app_auth.db-shm`、`app_auth.db-wal` 和密钥。
- 名单同步必须幂等：邮箱前缀为 username，姓名为 display_name；缺失账号创建为 `member`，使用密码学安全随机密码。
- 已有账号只更新已确认字段。必须断言 password_hash、role、is_active、created_by、项目配置、各类绑定、session 和 token 等非目标数据不变；不得调用重置密码接口。
- 初始密码只在交付响应中一次性提供，不写入仓库、日志或跨 Agent 记忆。
- 当前人员范围：仅在 172 同步 pmlist；既有 `jiaah` 保留原密码、管理员角色、状态和绑定，只更新确认的显示名。

## 6. 发布门禁与验证

- 代码门禁至少包括：Python 编译、定向 pytest、相关 ruff、JavaScript 或内联脚本语法检查、`git diff --check`。
- 认证写接口必须验证 JSON、严格同源、CSRF header、普通用户 403，且响应不含 `password_hash`。
- 密码变更、管理员重置与停用必须原子撤销 Web session、device token、skill token；最后有效管理员约束必须在单事务内保护。
- 每个环境真实执行 HTTP E2E：管理员列表、创建、修改、重置；普通用户改密；旧密码失效；会话撤销；普通用户访问管理员接口 403；跨 Origin 写请求 403；设置页 200。
- E2E 使用唯一临时账号，并在成功或失败后清理用户、会话和测试审计记录，验证零残留。
- 只有代码门禁、分支同步、部署、数据范围和线上 E2E 全部满足后，功能故事才能标记 `passes: true`。

## 7. 2026-07-18 已验证快照

- 用户管理与验收最终落点：deployable/yyrd `0cc510a2`；deployable 本地 `main` 为 `02e503a9`；主项目 `aiticket/main` 为 `d3edd199`。
- 172 已同步 pmlist：23 个缺失账号新建为普通用户；既有 `jiaah` 只更新显示名并保留原密码和管理员属性。
- Mini 已移除范围澄清前创建的 24 个名单账号，仅保留原管理员；功能代码仍部署在 Mini 与 172。
- Mini 和 172 均已使用持久 `/data/app_auth.key`；Jira 绑定状态不再因密钥漂移报 500 或误显示 HTTP 400 过期。
