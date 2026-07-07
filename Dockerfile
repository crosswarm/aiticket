FROM python:3.11-slim

# 系统依赖：chromadb / sentence-transformers / SQLite
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    libsqlite3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安装 Python 依赖（先 copy requirements 利用层缓存）
COPY APP/backend/requirements.txt ./APP/backend/requirements.txt
RUN pip install --no-cache-dir -r APP/backend/requirements.txt

# 复制应用代码——必须保留 APP/ 布局，不能拍平。
# 代码 hardwired 依赖仓库结构：kb_runtime_service.py 用 project_root=Path(__file__).parents[2]
# 定位 <root>/APP/backend/data、<root>/APP/frontend；main.py 用 _CODE_DIR/../frontend。
# 旧版 `COPY APP/backend/ ./`（拍平到 /app）会让 parents[2] 越界 IndexError、
# 前端算成 /frontend 找不到，导致容器启动即崩、restart 死循环。
COPY APP/ ./APP/

# 镜像内置默认配置：未挂载 host 配置时也能开箱启动
# （config/loader.py 会自查 __file__.parent/deployment.yaml）
RUN cp -n APP/backend/config/deployment.yaml.example APP/backend/config/deployment.yaml || true

# 数据目录（通过 volume 挂载持久化）
RUN mkdir -p /data/sqlite /data/chroma /data/kb /data/reply_trainer \
    && mkdir -p /app/logs

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 18000

ENTRYPOINT ["/entrypoint.sh"]
