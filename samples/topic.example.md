<!--
主题树配置文件 — 复制到 APP/backend/data/topic.md 后按你的业务自定义

规则：
1. 每个主题带 [TOP-...] 标识，格式固定
2. 用缩进表示父子层级，末级主题供 AI 精确分类用
3. 每个项目用 ## [PROJECT:<KEY>] 分区，KEY 与 Jira 项目键一致
-->

# 主题

## [PROJECT:MYPROJECT]

- [TOP-INFRA] 基础设施
    - [TOP-INFRA.DEPLOY] 部署与运维
    - [TOP-INFRA.PERF] 性能与稳定性
    - [TOP-INFRA.SECURITY] 安全与权限

- [TOP-API] API 与集成
    - [TOP-API.REST] REST 接口
    - [TOP-API.WEBHOOK] Webhook / 事件推送
    - [TOP-API.AUTH] 认证与授权

- [TOP-UI] 前端与交互
    - [TOP-UI.BUG] 界面 Bug
    - [TOP-UI.UX] 交互体验
    - [TOP-UI.MOBILE] 移动端适配

- [TOP-DATA] 数据与报表
    - [TOP-DATA.EXPORT] 数据导出
    - [TOP-DATA.REPORT] 报表统计
    - [TOP-DATA.MIGRATION] 数据迁移

- [TOP-DOC] 文档与帮助
    - [TOP-DOC.FAQ] 常见问题
    - [TOP-DOC.GUIDE] 操作指南
