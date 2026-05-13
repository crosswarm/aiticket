# 官方资料包部署指南

适用对象：拿到 **代码包 + 官方资料包** 的私有化部署管理员。

> 如果你是客户，想把自己的内部文档也导入进来，见 [data-pack-byo.md](data-pack-byo.md)。
> 如果你担心自己导入的内容会被官方包更新覆盖，见 [data-pack-coexistence.md](data-pack-coexistence.md)。

---

## 前置条件

- 已在服务器上 clone 代码：`git clone https://github.com/crosswarm/aiticket.git`
- 已获得官方资料包文件（以 `aiticket-data-pack-YYYY-MM-DD.tar.gz` 命名）
- 服务器磁盘剩余空间 ≥ 8 GB（解压后约 2.6 GB，加上 ChromaDB 索引约 5 GB）

---

## 步骤一：校验资料包完整性

```bash
# 用随包提供的 .sha256 文件校验
cd /path/to/你存放资料包的目录/
shasum -a 256 -c aiticket-data-pack-2026-05-13.sha256
# 输出：aiticket-data-pack-2026-05-13.tar.gz: OK
```

如果显示 `FAILED`，说明下载不完整，重新下载。

---

## 步骤二：解压到部署目录

```bash
# 解压到 aiticket 代码根目录
tar -xzf aiticket-data-pack-2026-05-13.tar.gz -C /path/to/aiticket/

# 解压完成后，应能看到：
ls /path/to/aiticket/KB/
# 打印  UI模板  业务流  规则  流程中心  组织  元数据  公式  权限  ...（共 20+ 个目录）

ls /path/to/aiticket/conclusion/
# WeeklyReports  MonthlyReports  requirements  specs  daily_reports  exports

ls /path/to/aiticket/design/
# spec  template  ticket-reduction
```

> **注意**：如果目录下有你自己存放的 `_local/` 子目录，它们不会被资料包覆盖。详见 [data-pack-coexistence.md](data-pack-coexistence.md)。

---

## 步骤三：启动服务

```bash
cd /path/to/aiticket/
cp .env.example .env       # 首次部署：填写 JIRA_BASE_URL、LLM API Key 等
docker compose up -d
```

---

## 步骤四：触发 KB 重建

资料包里是原始文档，需要跑一次构建流程生成检索索引。

**方式 A：API 调用（推荐）**

```bash
# 先拿到 session cookie（用 admin 账号登录一次）
SESSION=$(curl -s -c /tmp/cookies.txt -X POST http://localhost:18000/api/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin"}' | python3 -c "import sys,json; print(json.load(sys.stdin).get('session',''))")

# 触发全量同步（先把原始文档导入索引）
curl -X POST http://localhost:18000/api/kb/sync -b /tmp/cookies.txt

# 然后触发 AI 编译（把文档综合成高质量词条，比较慢，可选）
curl -X POST http://localhost:18000/api/kb/compile-all -b /tmp/cookies.txt
```

**方式 B：容器内脚本（适合首次全量重建）**

```bash
docker compose exec backend python -m scripts.batch_compile_kb
```

> KB 同步一般 2-10 分钟（视文档数量）。compile-all 全量编译可能需要 30-60 分钟，取决于 LLM 速度。

---

## 步骤五：验证

```bash
# 查询索引状态
curl http://localhost:18000/api/kb/manifest | python3 -m json.tool | head -20

# 搜索一条已知词条（用任意你知道在 KB 里的关键词）
curl "http://localhost:18000/api/kb/search?q=审批流" -b /tmp/cookies.txt | python3 -m json.tool
```

浏览器访问 `http://localhost:18000`，用 `admin / admin` 登录，进入知识库页面，搜索任意词条能返回结果即为成功。

---

## 常见问题

**Q：解压报 `Permission denied`**
```bash
sudo chown -R $(whoami) /path/to/aiticket/KB/ /path/to/aiticket/conclusion/ /path/to/aiticket/design/
```

**Q：sync 调用后 KB 词条仍为 0**
- 检查 `docker compose logs backend` 里有没有 ChromaDB 锁报错
- 解决 ChromaDB 锁：`docker compose restart backend`，等 30 秒再重试

**Q：磁盘不够**
- ChromaDB 向量索引约占原始文档体积的 1.5-2x，2.5 GB 文档需要约 4-5 GB 索引空间
- 减少 LLM 编译批次可降低存储，在 `.env` 设置 `KB_COMPILE_ENABLED=false` 仅用基础检索

---

## 升级到新版资料包

见 [data-pack-coexistence.md — 升级流程](data-pack-coexistence.md#升级流程) 节，核心是：解压新包 → 保留 `_local/` 子树 → 重跑 sync。
