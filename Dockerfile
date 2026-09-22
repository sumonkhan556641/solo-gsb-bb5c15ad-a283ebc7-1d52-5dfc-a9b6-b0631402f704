# 数据保留策略评估服务镜像
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 先装依赖，利用层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用代码
COPY app ./app

# SQLite 数据目录（Compose 中挂载为命名卷）
RUN mkdir -p /data
ENV RETENTION_DB_PATH=/data/retention.db \
    RETENTION_SEED_SAMPLE=true \
    RETENTION_DEFAULT_DAYS=90 \
    RETENTION_SENSITIVITY_LEVELS=public,internal,confidential,secret

EXPOSE 8000

# 容器级健康检查：slim 镜像无 curl，用标准库实现
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=5 \
    CMD python -c "import json,urllib.request,sys; \
r=urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=2); \
sys.exit(0 if r.status==200 and json.load(r)['status']=='ok' else 1)"

# 启动即完成 SQLite 初始化（建表+种子），随后启动 uvicorn
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
