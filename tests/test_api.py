"""端到端测试：基于 FastAPI TestClient + 临时 SQLite 数据库。"""

from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("RETENTION_DB_PATH", tmp.name)
    monkeypatch.setenv("SEED_ON_STARTUP", "true")

    import importlib

    import app.config as config
    importlib.reload(config)
    import app.database as database
    importlib.reload(database)
    import app.seed as seed
    importlib.reload(seed)
    import app.routers.evaluation as ev_router
    importlib.reload(ev_router)
    import app.routers.policies as pol_router
    importlib.reload(pol_router)
    import app.main as main
    importlib.reload(main)

    # lifespan 完成建表与种子
    with pytest.MonkeyPatch.context():
        from fastapi.testclient import TestClient

        with TestClient(main.app) as c:
            yield c

    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(tmp.name + suffix)
        except OSError:
            pass


def _event(**overrides):
    payload = {
        "event_type": "login",
        "tenant_id": "tenant-a",
        "sensitivity": "high",
        "occurred_at": "2026-01-01T00:00:00Z",
    }
    payload.update(overrides)
    return payload


def test_health_and_seed(client):
    assert client.get("/health").json()["status"] == "ok"
    policies = client.get("/policies").json()
    assert len(policies) == 4
    # 空描述必须序列化为 "" 而不是 null
    assert policies[0]["description"] is not None
    assert isinstance(policies[0]["description"], str)


def test_create_policy_empty_description_stays_string(client):
    r = client.post(
        "/policies",
        json={"name": "no-desc", "retain_duration": 1, "retain_unit": "days"},
    )
    assert r.status_code == 201
    pid = r.json()["id"]
    assert r.json()["description"] == ""
    assert client.get(f"/policies/{pid}").json()["description"] == ""
    # 显式 null 描述经 PUT 后仍应为字符串
    body = client.get(f"/policies/{pid}").json()
    body = {k: v for k, v in body.items() if k in (
        "description", "event_type", "tenant_id", "sensitivity", "priority",
        "retain_duration", "retain_unit", "keep_forever", "action", "enabled")}
    body["description"] = None
    upd = client.put(f"/policies/{pid}", json=body)
    assert upd.status_code == 200
    assert upd.json()["description"] == ""


def test_specificity_beats_priority(client):
    """tenant-a login 高敏：双维度精确规则(priority 50) 战胜
    单维度精确但优先级数值更小的规则(priority 100) 与 critical 通配(不命中)。"""
    resp = client.post("/evaluate", json=_event())
    assert resp.status_code == 200
    data = resp.json()
    assert data["matched"] is True
    assert data["winning_policy_name"] == "tenant-a-login-90d"
    assert data["expires_at"] == "2026-04-01T00:00:00Z"
    assert data["retain_period"] == "90 days"
    # 规则链：双维精确 > 单维精确(audit 不匹配 login)，再到全通配
    chain = data["rule_chain"]
    assert [r["rank"] for r in chain] == [1, 2]
    assert chain[0]["specificity_score"] == 2
    assert chain[1]["name"] == "default-30-days"
    assert chain[1]["specificity_score"] == 0
    assert "规则链" in data["decision_explanation"] or "命中" in data["decision_explanation"]


def test_priority_tiebreak_within_same_specificity(client):
    """同匹配度时 priority 数值小者胜出：新增一条同为双维精确、priority 10 的规则。"""
    created = client.post(
        "/policies",
        json={
            "name": "urgent-login",
            "event_type": "login",
            "tenant_id": "tenant-a",
            "sensitivity": None,
            "priority": 10,
            "retain_duration": 7,
            "retain_unit": "days",
            "action": "archive",
        },
    )
    assert created.status_code == 201
    data = client.post("/evaluate", json=_event()).json()
    assert data["winning_policy_name"] == "urgent-login"
    assert data["expires_at"] == "2026-01-08T00:00:00Z"
    assert data["action"] == "archive"


def test_keep_forever_critical(client):
    data = client.post(
        "/evaluate", json=_event(sensitivity="critical", event_type="whatever")
    ).json()
    assert data["winning_policy_name"] == "legal-hold-critical"
    assert data["keep_forever"] is True
    assert data["expires_at"] is None
    assert data["retain_period"] is None
    assert data["action"] == "legal_hold"


def test_years_and_months_expiry(client):
    # 高敏 audit：保留 1 年
    data = client.post(
        "/evaluate",
        json=_event(event_type="audit", sensitivity="high", tenant_id="other"),
    ).json()
    assert data["winning_policy_name"] == "pci-audit-high"
    assert data["expires_at"] == "2027-01-01T00:00:00Z"

    # 新增按月保留的规则
    client.post(
        "/policies",
        json={
            "name": "monthly",
            "event_type": "report",
            "tenant_id": "tenant-z",
            "sensitivity": "low",
            "priority": 1,
            "retain_duration": 3,
            "retain_unit": "months",
        },
    )
    data = client.post(
        "/evaluate",
        json=_event(
            event_type="report",
            tenant_id="tenant-z",
            sensitivity="low",
            occurred_at="2026-01-31T12:00:00Z",
        ),
    ).json()
    # relativedelta 处理月末：1月31日 + 3 月 = 4月30日
    assert data["expires_at"] == "2026-04-30T12:00:00Z"


def test_naive_datetime_treated_as_utc(client):
    data = client.post(
        "/evaluate", json=_event(occurred_at="2026-01-01T08:30:00")
    ).json()
    assert data["event"]["occurred_at"] == "2026-01-01T08:30:00+00:00"
    assert data["expires_at"] in ("2026-04-01T08:30:00Z", "2026-04-01T08:30:00+00:00")


def test_no_match_scenario(client):
    # 禁用兜底策略后，评估一个不会命中任何具体规则的组合
    policies = client.get("/policies").json()
    default_id = next(p["id"] for p in policies if p["name"] == "default-30-days")
    legal_id = next(p["id"] for p in policies if p["name"] == "legal-hold-critical")
    for pid in (default_id, legal_id):
        p = next(x for x in policies if x["id"] == pid)
        body = {k: v for k, v in p.items() if k in (
            "description", "event_type", "tenant_id", "sensitivity", "priority",
            "retain_duration", "retain_unit", "keep_forever", "action", "enabled")}
        body["enabled"] = False
        client.put(f"/policies/{pid}", json=body)

    data = client.post(
        "/evaluate", json=_event(event_type="nope", sensitivity="low")
    ).json()
    assert data["matched"] is False
    assert data["winning_policy_id"] is None
    assert data["expires_at"] is None
    assert data["rule_chain"] == []
    assert "没有任何" in data["decision_explanation"]


def test_validate_ok_and_semantic_errors(client):
    ok = client.post(
        "/policies/validate",
        json={"name": "new-rule", "retain_duration": 30, "retain_unit": "days"},
    )
    assert ok.status_code == 200
    assert ok.json()["valid"] is True

    # 永久保留又给了时长 -> 语义错误
    bad = client.post(
        "/policies/validate",
        json={
            "name": "bad-rule",
            "keep_forever": True,
            "retain_duration": 30,
            "retain_unit": "days",
        },
    )
    assert bad.status_code == 422
    detail = bad.json()["detail"]
    assert detail["valid"] is False
    assert any("keep_forever" in e["message"] for e in detail["errors"])

    # 缺少时长单位
    missing = client.post(
        "/policies/validate", json={"name": "m", "retain_duration": 10}
    )
    assert missing.status_code == 422

    # 名称占用检查
    dup = client.post(
        "/policies/validate?check_name=true",
        json={"name": "default-30-days", "retain_duration": 1, "retain_unit": "days"},
    )
    assert dup.status_code == 422
    assert dup.json()["detail"]["name_available"] is False


def test_crud_and_unique_name(client):
    # 类型错误 -> 422
    r = client.post("/policies", json={"name": "x", "retain_duration": -5})
    assert r.status_code == 422

    r = client.post(
        "/policies",
        json={"name": "dup", "retain_duration": 1, "retain_unit": "days"},
    )
    assert r.status_code == 201
    r = client.post(
        "/policies",
        json={"name": "dup", "retain_duration": 1, "retain_unit": "days"},
    )
    assert r.status_code == 409

    pid = client.get("/policies").json()[-1]["id"]
    upd = client.put(
        f"/policies/{pid}",
        json={
            "description": "updated",
            "event_type": None,
            "tenant_id": None,
            "sensitivity": None,
            "priority": 5,
            "retain_duration": 2,
            "retain_unit": "years",
            "keep_forever": False,
            "action": "delete",
            "enabled": True,
        },
    )
    assert upd.status_code == 200
    assert upd.json()["retain_duration"] == 2

    assert client.delete(f"/policies/{pid}").status_code == 204
    assert client.delete(f"/policies/{pid}").status_code == 404
    assert client.get(f"/policies/{pid}").status_code == 404


def test_invalid_event_rejected(client):
    r = client.post(
        "/evaluate",
        json={"event_type": "", "tenant_id": "t", "sensitivity": "low",
              "occurred_at": "2026-01-01T00:00:00Z"},
    )
    assert r.status_code == 422

    r = client.post(
        "/evaluate",
        json={"event_type": "login", "tenant_id": "t", "sensitivity": "secret",
              "occurred_at": "2026-01-01T00:00:00Z"},
    )
    assert r.status_code == 422
