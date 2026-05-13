# Docker 部署常见问题

## Q: 容器启动后 /api/health 返回 500

**原因**：ChromaDB 首次初始化或数据目录权限不足。

**解决**：
```bash
# 检查数据目录权限
ls -la /data/
# 修复权限
chown -R 1000:1000 /data/
docker compose restart
```

## Q: 嵌入模型加载很慢或失败

**原因**：首次启动需要下载 `sentence-transformers` 模型（约 90MB）。

**解决**：
- 确保服务器能访问 HuggingFace（或配置镜像源）
- 离线环境：设置 `ALLOW_EMBEDDING_DOWNLOAD=false`，提前手动下载模型

## Q: 多次重启后 ChromaDB 数据丢失

**原因**：`DATA_DIR` 未持久化挂载。

**解决**：docker compose 中确保挂载了 volume：
```yaml
volumes:
  - /host/data:/data
```
