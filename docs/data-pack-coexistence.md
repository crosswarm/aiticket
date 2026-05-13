# 官方资料包与私有资料包并存指南

适用对象：已同时部署官方资料包 + 自建私有文档，想保证**升级官方包时私有内容不被覆盖**的部署管理员。

---

## 核心原则

> **官方包只动官方目录；私有内容统一放 `_local/` 命名空间，tar 解压天然隔离。**

`tar -xzf` 只覆盖压缩包内明确列出的路径。官方资料包的 tarball 里只含官方 INVENTORY 中列出的目录（`KB/打印/`、`KB/UI模板/` 等），它**物理上无法碰到** `KB/_local/`。

---

## 目录约定一览

| 类型 | 路径 | 管理方 | 官方包升级时 |
|------|------|--------|-------------|
| 官方 KB | `KB/打印/`、`KB/UI模板/`、`KB/业务流/`、`KB/规则/`、`KB/流程中心/`、`KB/组织/`、`KB/元数据/`、`KB/公式/`、`KB/权限/`、`KB/配置迁移/`、`KB/导入导出/`、`KB/YPD开发框架/`、`KB/MDD开发框架/`、`KB/消息/`、`KB/云平台/` 等（见资料包 INVENTORY.md） | 官方资料包 | 被覆盖（旧内容替换为新版本） |
| 私有 KB | `KB/_local/**` | 客户 | **保留，不被触碰** |
| 检索索引 | `KB/INDEX/` | 系统自动生成 | 升级后跑一次 sync 重建 |
| 官方报告 | `conclusion/WeeklyReports/`、`conclusion/MonthlyReports/` 等 | 官方资料包 | 被覆盖 |
| 私有报告 | `conclusion/_local/**` | 客户 | **保留，不被触碰** |
| 官方设计 | `design/spec/`、`design/template/`、`design/ticket-reduction/` 等 | 官方资料包 | 被覆盖 |
| 私有设计 | `design/_local/**` | 客户 | **保留，不被触碰** |

---

## 升级流程

### 第 1 步：（推荐）备份私有内容

```bash
cd /path/to/aiticket/
tar -czf ~/private-backup-$(date +%F).tar.gz \
    KB/_local/ \
    conclusion/_local/ \
    design/_local/
```

> 备份只需秒级，但如果升级出现意外（如磁盘满、网络中断），能快速恢复。

### 第 2 步：校验新资料包

```bash
cd /path/to/新资料包目录/
shasum -a 256 -c aiticket-data-pack-YYYY-MM-DD.sha256
# 输出 OK 再继续
```

### 第 3 步：解压新官方包

```bash
tar -xzf aiticket-data-pack-YYYY-MM-DD.tar.gz -C /path/to/aiticket/
```

此步骤：
- ✅ 覆盖官方目录（`KB/打印/` 等）
- ✅ 覆盖 `conclusion/`、`design/` 下的官方子目录
- ✅ 不碰 `KB/_local/`（不在 tar 里）
- ✅ 不碰 `conclusion/_local/`、`design/_local/`

### 第 4 步：重建检索索引

索引需要重建，把官方新内容 + 你的私有内容一并纳入：

```bash
# 触发全量同步（含官方新文档 + 你的私有文档）
curl -X POST http://localhost:18000/api/kb/sync -b /tmp/cookies.txt
```

### 第 5 步：验证并存

```bash
# 查看总词条数（应 = 官方词条 + 你的私有词条）
curl http://localhost:18000/api/kb/manifest -b /tmp/cookies.txt | python3 -c "
import sys,json; m=json.load(sys.stdin)
items=m.get('items',m) if isinstance(m,dict) else m
print(f'总词条数: {len(items)}')
"

# 搜索一条官方词条，确认官方内容正常
curl "http://localhost:18000/api/kb/search?q=审批流" -b /tmp/cookies.txt | python3 -m json.tool | head -20

# 搜索一条你自己的私有词条，确认未丢失
curl "http://localhost:18000/api/kb/search?q=你私有文档里的关键词" -b /tmp/cookies.txt
```

---

## 边界条件 & 注意事项

**不要把私有文档放进官方目录子树**

```
# 危险做法 ❌
KB/流程中心/我的补充说明.md   ← 若官方包修改了 KB/流程中心/，有同名文件时会被覆盖

# 安全做法 ✅
KB/_local/流程补充/我的补充说明.md
```

**不要给私有文件起与官方相同的相对路径**

tar 的覆盖逻辑是路径匹配。路径相同 = 被覆盖，没有任何警告。

**`KB/INDEX/` 始终由 builder 重建，不要手动放文件**

同步前后，`KB/INDEX/` 会被完整重写。不要在这里存任何私有内容。

**如果忘记用 `_local/`，私有文档放错了地方怎么办**

在升级之前，先迁移到 `_local/` 下：

```bash
mkdir -p KB/_local/迁移/
mv KB/官方目录/你的文件.md KB/_local/迁移/
# 然后再解压新官方包
```

---

## 灾难恢复

如果升级时意外丢失了私有内容：

```bash
# 从备份恢复
tar -xzf ~/private-backup-2026-05-13.tar.gz -C /path/to/aiticket/

# 重跑同步
curl -X POST http://localhost:18000/api/kb/sync -b /tmp/cookies.txt
```

如果没有备份，私有内容的唯一来源是你自己的原始文件服务器/wiki。所以**强烈建议在每次升级前跑一次备份命令**（第 1 步）。

---

## 完整目录结构示意

升级 + 自建后，预期目录结构如下：

```
aiticket/
├── KB/
│   ├── 打印/          ← 官方（v0.2.0 新版本）
│   ├── UI模板/         ← 官方
│   ├── 流程中心/       ← 官方
│   ├── ...
│   ├── INDEX/          ← 系统自动，勿手动改
│   └── _local/          ← 你的，永久保留
│       ├── 财务流程/
│       └── HR制度/
├── conclusion/
│   ├── WeeklyReports/ ← 官方
│   ├── MonthlyReports/ ← 官方
│   └── _local/          ← 你自己的报告归档
└── design/
    ├── spec/           ← 官方
    ├── template/       ← 官方
    └── _local/          ← 你自己的设计文档
```
