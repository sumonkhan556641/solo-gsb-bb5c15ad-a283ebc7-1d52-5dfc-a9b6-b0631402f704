# 数据保留策略评估服务镜像
FROM python:3.11-slim

# 不生成 .pyc、日志不缓冲
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RETENTION_DB_PATH=/data/policies.db \
    SEED_ON_STARTUP=true

WORKDIR /app

# 先装依赖，利用层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用代码
COPY app ./app

# 持久化 SQLite 的数据卷
RUN mkdir -p /data && useradd -r -u 10001 appuser \
    && chown -R appuser:appuser /data /app
USER appuser
VOLUME ["/data"]

EXPOSE 8000

# 容器启动即完成依赖安装（镜像层）、建表与种子数据初始化（lifespan）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
