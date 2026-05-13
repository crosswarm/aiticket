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
COPY APP/backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY APP/backend/ ./
COPY APP/frontend/ ./frontend/

# 数据目录（通过 volume 挂载）
RUN mkdir -p /data/sqlite /data/chroma /data/kb /data/reply_trainer \
    && mkdir -p /app/logs

# 首次启动自动 bootstrap（初始化 DB schema）
COPY APP/backend/bootstrap/ ./bootstrap/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 18000

ENTRYPOINT ["/entrypoint.sh"]
