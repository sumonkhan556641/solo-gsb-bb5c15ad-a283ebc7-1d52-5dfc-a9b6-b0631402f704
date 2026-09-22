# 数据保留策略评估服务（FastAPI + SQLite）

客户端提交 **事件类型、租户、敏感级别、发生时间**，服务从 SQLite 中的策略集合里
按 **条件匹配度（specificity）优先、其次策略优先级** 解析出最终生效规则，
计算数据 **到期时间**，并返回 **命中的规则链** 与逐维度的中文解释；同时提供不落库的
**策略校验接口** 与完整的策略 CRUD。

---

## 一条命令启动

需要已安装 Docker 与 Docker Compose v2：

```bash
docker compose up --build -d
```

- 端口：`http://localhost:8000`
- 交互式 API 文档（Swagger UI）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>
- SQLite 数据通过命名卷 `retention-data` 持久化到容器内 `/data/retention.db`
- 首次启动自动建表并写入 5 条演示策略（见下文“内置种子策略”）；
  之后重启不会重复播种，已创建/修改的策略保持不变。

停止：`docker compose down`（加 `-v` 会同时删除数据卷）。

---

## 快速试用

评估一个金融租户的机密交易事件：

```bash
curl -s -X POST http://localhost:8000/evaluate \
  -H 'Content-Type: application/json' \
  -d '{
    "event_type": "transaction",
    "tenant": "fin",
    "sensitivity": "confidential",
    "occurred_at": "2025-03-15T10:00:00Z"
  }'
```

返回（节选）：`金融租户交易记录-7年` 因条件更具体（同时限定事件类型与租户）
排在规则链头部，尽管只看数字优先级它并非最高；到期时间按发生时间 + 7 年做日历运算：

```json
{
  "expires_at": "2032-03-15T10:00:00Z",
  "action": "archive",
  "retention_period": "P7Y",
  "fallback_applied": false,
  "rule_chain": [ { "name": "金融租户交易记录-7年", "specificity": 20, "rank": 1, "selected": true, "...": "..." } ],
  "unmatched": [ { "name": "...", "matched": false, "reason": "未命中：租户（...）" } ],
  "explanation": ["收到事件：...", "共有 N 条策略命中，按“匹配度优先、其次优先级”排序...", "生效动作：归档；保留 7年..."]
}
```

诉讼冻结（无限期保留）示例：

```bash
curl -s -X POST http://localhost:8000/evaluate -H 'Content-Type: application/json' -d '{
  "event_type": "security_alert", "tenant": "legal",
  "sensitivity": "secret", "occurred_at": "2025-03-15T10:00:00Z" }'
# -> action=legal_hold, expires_at=null, legal_hold=true
```

只校验策略、不写数据库：

```bash
curl -s -X POST http://localhost:8000/policies/validate \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "新规则",
    "conditions": {"event_type": ["click"], "tenant": ["acme"]},
    "action": "delete",
    "retention_period": "6mo"
  }'
# valid=true 时 canonical_policy.retention_period 会归一化为 "P6M"；
# issues 为错误、warnings 为提示（全通配策略、被高优先级宽条件策略遮蔽等）
```

---

## 策略模型

| 字段 | 说明 |
| --- | --- |
| `name` | 唯一名称（必填） |
| `conditions.event_type` | 事件类型候选集合；空数组 = 通配 |
| `conditions.tenant` | 租户候选集合；空数组 = 通配 |
| `conditions.sensitivity` | 敏感级别候选集合（任一命中即可）；空 = 不限定 |
| `conditions.min_sensitivity` | 最低敏感级别阈值（含），与 `sensitivity` 为“且”关系 |
| `action` | `delete` / `anonymize` / `archive` / `legal_hold` |
| `retention_period` | 保留时长；`legal_hold` 动作可省略（视为无限期） |
| `legal_hold` | 是否诉讼冻结（无限期、不得自动删除） |
| `priority` | 0–1000000，数字越大优先级越高（匹配度相同时才比较） |
| `enabled` | 是否启用；停用的策略不参与评估 |

### 保留时长写法

- ISO-8601：`P7Y`、`P6M`、`P90D`、`P1Y2M30D`、`PT12H30M`
- 简写：`90`（=90 天）、`90d`、`6mo`、`1y`、`2w`、`12h`、`30mi`、`60s`
- 无限期：`indefinite`
- 服务端会统一归一化为 ISO-8601 标准形式后存储。
- 月/年按日历加法：1/31 + 1 个月 → 2/28，闰年 2/29 + 1 年 → 次年 2/28。

### 匹配度如何计算

每个命中的非通配维度计分：事件类型 10 分、租户 10 分、敏感级别集合 5 分、
敏感级别阈值 5 分。设计上 **任何两个具体维度的组合分都高于任何单一维度**，
即“同时限定租户与敏感级别”比“只限定事件类型”更具体。

裁决排序：`匹配度降序 → priority 降序 → id 升序`，链首规则生效。
所有命中策略都会在 `rule_chain` 中给出，并且每条都附带按其自身保留期算出的
`expires_at` 供对比；`unmatched` 给出未命中策略及具体在哪个维度失配。
没有任何策略命中时回退到 `RETENTION_DEFAULT_DAYS`（默认 90 天）并置
`fallback_applied=true`。

### 内置种子策略

1. 安全审计日志-90天（security_alert → P90D）
2. 金融租户交易记录-7年（transaction + fin → 归档 P7Y，优先级 200）
3. 机密以上用户数据-1年（min_sensitivity=confidential → P1Y）
4. 诉讼冻结-无限期（legal 租户 + confidential 以上 → legal_hold，优先级 500）
5. 默认保留-30天（全通配兜底，优先级 0）

---

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/evaluate` | 评估事件，返回到期时间、规则链、解释 |
| `POST` | `/policies/validate` | 仅校验策略（不落库），始终 200，看 `valid` |
| `GET` | `/policies` | 列出全部策略 |
| `POST` | `/policies` | 创建策略（语义错误 422，重名 409） |
| `GET` | `/policies/{id}` | 获取单条策略 |
| `PATCH` | `/policies/{id}` | 局部更新（conditions 整体替换） |
| `DELETE` | `/policies/{id}` | 删除策略（204） |
| `GET` | `/health` | 健康检查 |

完整字段与示例以 `/docs`（Swagger）或 `/openapi.json` 为准。

### 校验规则摘要

- 非 `legal_hold` 动作必须给出可解析的 `retention_period`；
- 敏感级别名称必须存在于 `RETENTION_SENSITIVITY_LEVELS`；
- `sensitivity` 集合若全部低于 `min_sensitivity`，条件永不可能成立（报错）；
- 警告（不阻断）：全通配策略、`legal_hold` 标志与动作不一致、
  会被更高优先级且条件覆盖本策略的既有策略遮蔽。

---

## 配置（环境变量）

所有配置都有默认值，开箱即用：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `RETENTION_DB_PATH` | `/data/retention.db` | SQLite 文件路径（容器内挂卷） |
| `RETENTION_SEED_SAMPLE` | `true` | 空库启动时是否写入演示策略 |
| `RETENTION_DEFAULT_DAYS` | `90` | 无策略命中时的回退保留天数 |
| `RETENTION_SENSITIVITY_LEVELS` | `public,internal,confidential,secret` | 敏感级别从低到高，逗号分隔；调整后建议使用空库 |

在 compose 里修改 `services.retention-api.environment` 即可，例如：

```bash
RETENTION_DEFAULT_DAYS=30 RETENTION_SEED_SAMPLE=false docker compose up --build -d
```

---

## 本地开发（不用 Docker）

要求 Python 3.11+：

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export RETENTION_DB_PATH=./data/retention.db   # 默认 /data/retention.db
uvicorn app.main:app --reload --port 8000
```

启动时（FastAPI lifespan）会自动完成 SQLite 建表与（可选的）种子写入，无需手动迁移。

### 运行测试

```bash
pip install -r requirements-dev.txt   # 额外包含 pytest、httpx
pytest -q
```

测试使用内存 SQLite、关闭种子数据，并覆盖保留期解析/日历运算、CRUD、
校验接口（含遮蔽误报回归）与评估裁决（匹配度优先于优先级、冻结、回退）。

---

## 目录结构

```
app/
  main.py          # FastAPI 入口，lifespan 中初始化 SQLite
  config.py        # 环境变量配置
  db.py            # SQLite 建表、种子策略、CRUD
  schemas.py       # 请求/响应模型
  retention.py     # 保留期解析与到期时间日历运算
  matching.py      # 匹配度评分与规则链裁决
  validation.py    # 策略语义校验与遮蔽分析
  routers/
    policies.py    # 策略 CRUD + /policies/validate
    evaluate.py    # /evaluate
tests/             # pytest 测试
Dockerfile         # 单容器镜像，启动即初始化 DB
docker-compose.yml # 单命令编排 + 命名卷 + 健康检查
```
