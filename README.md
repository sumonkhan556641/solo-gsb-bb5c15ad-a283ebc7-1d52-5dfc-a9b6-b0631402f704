# 数据保留策略评估服务 (Data Retention Policy Evaluation Service)

基于 **FastAPI + SQLite** 的数据保留（Data Retention）策略评估服务。客户端提交
**事件类型、租户、敏感级别、发生时间**，服务从 SQLite 中加载全部启用的策略，按
**条件匹配度（精确维度数）→ 优先级** 解析出生效规则，计算数据到期时间，并返回
完整的命中规则链与中文决策解释。另提供**策略校验接口**与策略 CRUD。

## 一条命令启动（需要 Docker + Compose v2）

```bash
docker compose up --build -d
```

- 服务端口：**8000**
- 接口文档（Swagger UI）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>
- 数据持久化：SQLite 文件位于容器内 `/data/policies.db`，挂载到 Docker 命名卷
  `retention-data`；删除卷才会清空数据（`docker compose down -v`）。

容器启动时自动完成：安装依赖（镜像层）→ **创建 SQLite 库表** → 库为空时
**写入 4 条示例策略**（可用环境变量关闭，见下）。

停止服务：

```bash
docker compose down        # 保留数据
docker compose down -v     # 同时删除数据卷
```

## 无 Docker 时本地运行

需要 Python 3.11+：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

默认数据库为 `/data/policies.db`（需写权限）。本地可改为当前目录：

```bash
RETENTION_DB_PATH=./policies.db uvicorn app.main:app --reload
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `RETENTION_DB_PATH` | `/data/policies.db` | SQLite 数据库文件路径，父目录不存在会自动创建 |
| `SEED_ON_STARTUP` | `true` | 数据库为空时是否自动写入示例策略（`true/1/yes` 开启） |
| `RETENTION_TIMEZONE` | `UTC` | 展示用时区；所有时间统一以 **UTC** 存储与返回 |

在 Compose 中通过 `docker-compose.yml` 的 `environment` 修改即可。

## 策略匹配与决策规则

策略含三个匹配维度，任一维度为 `null`（或空串）表示该维度**通配**：

- `event_type` 事件类型（如 `login`、`audit`）
- `tenant_id` 租户 ID
- `sensitivity` 敏感级别：`low` / `medium` / `high` / `critical`

一条策略命中 = 它所有非通配维度与事件**精确相等**。多条命中时按以下顺序解析生效规则：

1. **匹配度降序**：精确匹配的维度越多越优先（3 > 2 > 1 > 0，全通配为兜底）；
2. **优先级 `priority` 升序**：匹配度相同时，数值小者优先（取值 1–10000，1 最高）；
3. `id` 升序：仍相同时先创建者优先，保证结果确定。

到期时间：

- `expires_at = occurred_at + retain_duration × retain_unit`
- 单位支持 `days` / `months` / `years`（月、年按日历计算，如 1 月 31 日 + 3 月 = 4 月 30 日）；
- `keep_forever=true`（如法律保全 `legal_hold`）时 `expires_at` 为 `null`；
- 朴素时间（无时区）按 UTC 处理，带时区时间统一转换为 UTC 返回。

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/evaluate` | 评估事件，返回生效策略、到期时间、规则链与解释 |
| POST | `/policies/validate` | 校验策略（不落库），`?check_name=true` 可同时检查名称唯一 |
| GET | `/policies` | 列出全部策略 |
| POST | `/policies` | 创建策略（名称唯一冲突返回 409） |
| GET | `/policies/{id}` | 获取单条策略 |
| PUT | `/policies/{id}` | 全量更新策略 |
| DELETE | `/policies/{id}` | 删除策略 |
| GET | `/health` | 健康检查 |

### 评估示例

```bash
curl -s -X POST http://localhost:8000/evaluate \
  -H 'Content-Type: application/json' \
  -d '{
    "event_type": "login",
    "tenant_id": "tenant-a",
    "sensitivity": "high",
    "occurred_at": "2026-09-22T08:00:00+08:00"
  }'
```

响应（节选）：

```json
{
  "matched": true,
  "winning_policy_name": "tenant-a-login-90d",
  "expires_at": "2026-12-21T00:00:00Z",
  "retain_period": "90 days",
  "action": "delete",
  "match_count": 2,
  "rule_chain": [
    { "rank": 1, "name": "tenant-a-login-90d", "specificity_score": 2,
      "matched_dimensions": {"event_type": "exact", "tenant_id": "exact", "sensitivity": "wildcard"},
      "explanation": "精确匹配 2/3 个维度（事件类型=login、租户=tenant-a）；通配维度：敏感级别=*；保留 90 days…" },
    { "rank": 2, "name": "default-30-days", "specificity_score": 0, "...": "兜底策略" }
  ],
  "decision_explanation": "共命中 2 条策略。按“精确匹配维度数（匹配度）降序、优先级 priority 升序、id 升序”解析，规则 `tenant-a-login-90d` (id=3) 胜出。…到期时间为 2026-12-21T00:00:00Z…"
}
```

### 策略校验示例

```bash
curl -s -X POST 'http://localhost:8000/policies/validate?check_name=true' \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "my-rule",
    "event_type": "audit",
    "sensitivity": "high",
    "retain_duration": 365,
    "retain_unit": "days",
    "action": "anonymize"
  }'
```

合法返回 `200 {"valid": true, ...}`；以下情况返回 `422` 并在 `detail.errors`
中逐条列出字段级原因：

- `keep_forever=true` 同时提供了 `retain_duration`/`retain_unit`（互斥）；
- 非永久保留却缺少时长或单位；
- 枚举非法、优先级越界、空字符串等（Pydantic 校验，同样为 422）；
- `check_name=true` 且名称已被占用（`name_available=false`）。

### 创建策略示例

```bash
curl -s -X POST http://localhost:8000/policies \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "eu-gdpr-login",
    "description": "欧盟租户登录事件保留 6 个月",
    "event_type": "login",
    "tenant_id": "tenant-eu",
    "sensitivity": null,
    "priority": 20,
    "retain_duration": 6,
    "retain_unit": "months",
    "action": "delete"
  }'
```

永久保留策略：

```json
{ "name": "legal-hold-x", "sensitivity": "critical", "priority": 1,
  "keep_forever": true, "action": "legal_hold" }
```

## 内置示例策略

首次启动写入（优先级数值越小越优先）：

| 名称 | 匹配 | priority | 保留 |
| --- | --- | --- | --- |
| `legal-hold-critical` | sensitivity=critical | 10 | 永久保留（legal_hold） |
| `tenant-a-login-90d` | event=login 且 tenant=tenant-a | 50 | 90 天 |
| `pci-audit-high` | event=audit 且 sensitivity=high | 100 | 1 年，到期匿名化 |
| `default-30-days` | 全通配兜底 | 1000 | 30 天 |

## 项目结构

```
.
├── app/
│   ├── main.py            # FastAPI 入口，lifespan 中建表 + 种子初始化
│   ├── config.py          # 环境变量配置
│   ├── database.py        # SQLite 连接、建表 DDL
│   ├── schemas.py         # Pydantic 模型与跨字段语义校验
│   ├── retention.py       # 匹配引擎：匹配度打分、优先级解析、到期计算、解释
│   ├── crud.py            # 策略增删改查
│   ├── seed.py            # 内置示例策略
│   └── routers/
│       ├── evaluation.py  # POST /evaluate
│       └── policies.py    # /policies 与 /policies/validate
├── tests/test_api.py      # 端到端测试（10 个场景）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 运行测试

```bash
pip install -r requirements-dev.txt
pytest -q
```
