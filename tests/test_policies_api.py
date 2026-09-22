"""策略 CRUD 与校验接口测试。"""

from __future__ import annotations


def _make_policy(**overrides) -> dict:
    payload = {
        "name": "测试策略-90天",
        "description": "测试用",
        "conditions": {
            "event_type": ["security_alert"],
            "tenant": ["acme"],
            "sensitivity": [],
            "min_sensitivity": None,
        },
        "action": "delete",
        "retention_period": "90d",
        "legal_hold": False,
        "priority": 100,
        "enabled": True,
    }
    payload.update(overrides)
    return payload


def test_health_and_empty_list(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/policies").json() == []


def test_create_policy_canonicalizes_period(client):
    resp = client.post("/policies", json=_make_policy())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["retention_period"] == "P90D"
    assert body["id"] == 1


def test_create_duplicate_name_conflicts(client):
    client.post("/policies", json=_make_policy())
    resp = client.post("/policies", json=_make_policy(conditions={"event_type": ["x"]}))
    assert resp.status_code == 409


def test_invalid_period_rejected_by_schema(client):
    resp = client.post("/policies", json=_make_policy(retention_period="nonsense"))
    assert resp.status_code == 422


def test_delete_action_requires_period(client):
    resp = client.post("/policies", json=_make_policy(retention_period=None))
    assert resp.status_code == 422
    assert any(
        issue["code"] == "missing_retention_period"
        for issue in resp.json()["detail"]["issues"]
    )


def test_legal_hold_can_omit_period(client):
    payload = _make_policy(
        name="冻结策略", action="legal_hold", legal_hold=True, retention_period=None
    )
    resp = client.post("/policies", json=payload)
    assert resp.status_code == 201, resp.text
    assert resp.json()["retention_period"] is None


def test_unknown_sensitivity_semantic_error(client):
    payload = _make_policy(
        conditions={"event_type": ["security_alert"], "sensitivity": ["top_secret"]}
    )
    resp = client.post("/policies", json=payload)
    assert resp.status_code == 422
    codes = {issue["code"] for issue in resp.json()["detail"]["issues"]}
    assert "unknown_sensitivity" in codes


def test_validate_endpoint_does_not_persist(client):
    payload = _make_policy(retention_period="6mo")
    resp = client.post("/policies/validate", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True
    assert body["canonical_policy"]["retention_period"] == "P6M"
    # 未落库
    assert client.get("/policies").json() == []


def test_validate_reports_invalid_payload(client):
    resp = client.post(
        "/policies/validate", json=_make_policy(retention_period=None)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert body["canonical_policy"] is None


def test_validate_warns_wildcard_and_shadow(client):
    client.post("/policies", json=_make_policy(name="宽泛策略", conditions={}))
    resp = client.post(
        "/policies/validate",
        json=_make_policy(
            name="狭窄策略",
            conditions={"event_type": ["security_alert"], "tenant": ["acme"]},
            priority=50,
        ),
    )
    body = resp.json()
    assert body["valid"] is True
    codes = {w["code"] for w in body["warnings"]}
    assert "shadowed_policy" in codes

    # 全通配策略本身会收到 wildcard 警告
    resp_wild = client.post(
        "/policies/validate", json=_make_policy(name="另一个兜底", conditions={}, priority=0)
    )
    assert "wildcard_policy" in {w["code"] for w in resp_wild.json()["warnings"]}


def test_validate_no_false_shadow_for_disjoint_or_narrower(client):
    # 已有只针对 security_alert 的策略；新策略针对另一事件类型，不应被遮蔽
    client.post(
        "/policies",
        json=_make_policy(
            name="仅安全事件",
            conditions={"event_type": ["security_alert"], "tenant": ["acme"]},
        ),
    )
    resp = client.post(
        "/policies/validate",
        json=_make_policy(
            name="点击事件",
            conditions={"event_type": ["click"], "tenant": ["zzz"]},
            priority=0,
        ),
    )
    codes = {w["code"] for w in resp.json()["warnings"]}
    assert "shadowed_policy" not in codes


def test_validate_shadowed_by_higher_priority_broader_policy(client):
    # 高优先级的阈值策略会遮蔽低优先级、仅限定敏感集合的新策略吗？
    # 这里构造：已有“confidential 阈值 + 高优先级”，新建“仅 secret 集合 + 低优先级”
    client.post(
        "/policies",
        json=_make_policy(
            name="机密阈值-高优先",
            conditions={"min_sensitivity": "confidential"},
            priority=500,
        ),
    )
    resp = client.post(
        "/policies/validate",
        json=_make_policy(
            name="绝密集合-低优先",
            conditions={"sensitivity": ["secret"]},
            priority=10,
        ),
    )
    codes = {w["code"] for w in resp.json()["warnings"]}
    assert "shadowed_policy" in codes


def test_patch_update_and_null_retention(client):
    created = client.post("/policies", json=_make_policy()).json()
    resp = client.patch(
        f"/policies/{created['id']}",
        json={"action": "legal_hold", "legal_hold": True, "retention_period": None},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["action"] == "legal_hold"
    assert body["retention_period"] is None
    assert body["legal_hold"] is True


def test_patch_unknown_id_404(client):
    assert client.patch("/policies/999", json={"priority": 1}).status_code == 404


def test_delete_policy(client):
    created = client.post("/policies", json=_make_policy()).json()
    assert client.delete(f"/policies/{created['id']}").status_code == 204
    assert client.delete(f"/policies/{created['id']}").status_code == 404


def test_disabled_policy_excluded_from_default_listing_flag(client):
    client.post("/policies", json=_make_policy(enabled=False))
    policies = client.get("/policies").json()
    assert policies[0]["enabled"] is False
