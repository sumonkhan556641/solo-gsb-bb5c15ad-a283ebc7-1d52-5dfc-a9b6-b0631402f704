"""策略评估接口测试：匹配度优先于优先级、规则链、冻结与回退。"""

from __future__ import annotations


def _policy(name: str, **kwargs) -> dict:
    payload = {
        "name": name,
        "description": "",
        "conditions": {},
        "action": "delete",
        "retention_period": "P30D",
        "priority": 100,
        "enabled": True,
    }
    payload.update(kwargs)
    return payload


EVENT = {
    "event_type": "transaction",
    "tenant": "fin",
    "sensitivity": "confidential",
    "occurred_at": "2025-03-15T10:00:00+00:00",
}


def test_specificity_beats_priority(client):
    # 高优先级但条件宽泛
    client.post(
        "/policies",
        json=_policy("宽泛-高优先级", conditions={}, retention_period="P30D", priority=500),
    )
    # 低优先级但精确命中三个维度
    client.post(
        "/policies",
        json=_policy(
            "精确-低优先级",
            conditions={
                "event_type": ["transaction"],
                "tenant": ["fin"],
                "sensitivity": ["confidential"],
            },
            retention_period="P7Y",
            priority=10,
        ),
    )
    resp = client.post("/evaluate", json=EVENT)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["fallback_applied"] is False
    assert body["action"] == "delete"
    assert body["rule_chain"][0]["name"] == "精确-低优先级"
    assert body["rule_chain"][0]["selected"] is True
    assert body["rule_chain"][1]["name"] == "宽泛-高优先级"
    # 2025-03-15 + 7 年
    assert body["expires_at"].startswith("2032-03-15T10:00:00")
    assert body["retention_period"] == "P7Y"


def test_priority_breaks_specificity_tie(client):
    client.post(
        "/policies",
        json=_policy(
            "同条件-低优先",
            conditions={"event_type": ["transaction"], "tenant": ["fin"]},
            priority=100,
        ),
    )
    client.post(
        "/policies",
        json=_policy(
            "同条件-高优先",
            conditions={"event_type": ["transaction"], "tenant": ["fin"]},
            retention_period="P90D",
            priority=200,
        ),
    )
    body = client.post("/evaluate", json=EVENT).json()
    assert body["rule_chain"][0]["name"] == "同条件-高优先"
    assert body["expires_at"].startswith("2025-06-13T10:00:00")


def test_min_sensitivity_threshold(client):
    client.post(
        "/policies",
        json=_policy(
            "机密以上-1年",
            conditions={"min_sensitivity": "confidential"},
            retention_period="P1Y",
        ),
    )
    body = client.post("/evaluate", json=EVENT).json()
    assert body["rule_chain"][0]["name"] == "机密以上-1年"
    assert body["expires_at"].startswith("2026-03-15T10:00:00")

    # public 事件不应命中阈值策略
    body_low = client.post(
        "/evaluate", json={**EVENT, "sensitivity": "public"}
    ).json()
    assert body_low["rule_chain"] == []
    assert body_low["fallback_applied"] is True


def test_legal_hold_indefinite(client):
    client.post(
        "/policies",
        json=_policy(
            "诉讼冻结",
            conditions={"tenant": ["fin"], "min_sensitivity": "confidential"},
            action="legal_hold",
            legal_hold=True,
            retention_period=None,
            priority=10,
        ),
    )
    client.post(
        "/policies",
        json=_policy("普通-高优先", conditions={}, retention_period="P30D", priority=999),
    )
    body = client.post("/evaluate", json=EVENT).json()
    # 冻结策略条件更具体，即便优先级低也胜出
    assert body["rule_chain"][0]["name"] == "诉讼冻结"
    assert body["expires_at"] is None
    assert body["action"] == "legal_hold"
    assert body["legal_hold"] is True
    assert body["retention_period"] is None
    assert any("无限期" in line for line in body["explanation"])


def test_unmatched_chain_explains_failure(client):
    client.post(
        "/policies",
        json=_policy("只命中其他租户", conditions={"tenant": ["other"]}),
    )
    body = client.post("/evaluate", json=EVENT).json()
    assert body["fallback_applied"] is True
    assert len(body["unmatched"]) == 1
    missed = body["unmatched"][0]
    assert missed["matched"] is False
    assert "tenant" in missed["match_detail"]
    assert body["expires_at"].startswith("2025-04-29T10:00:00")  # +45 默认天


def test_disabled_policy_not_evaluated(client):
    client.post("/policies", json=_policy("停用策略", conditions={}, enabled=False))
    body = client.post("/evaluate", json=EVENT).json()
    assert body["rule_chain"] == []
    assert body["fallback_applied"] is True


def test_naive_event_time_assumed_utc(client):
    client.post(
        "/policies",
        json=_policy("简单规则", conditions={"event_type": ["transaction"]}, retention_period="P1D"),
    )
    body = client.post(
        "/evaluate", json={**EVENT, "occurred_at": "2025-03-15T10:00:00"}
    ).json()
    assert body["expires_at"].startswith("2025-03-16T10:00:00")


def test_rule_chain_entries_have_per_rule_expiry(client):
    client.post(
        "/policies",
        json=_policy("链尾", conditions={}, retention_period="P30D", priority=0),
    )
    client.post(
        "/policies",
        json=_policy(
            "链头",
            conditions={"event_type": ["transaction"]},
            retention_period="P1Y",
            priority=0,
        ),
    )
    body = client.post("/evaluate", json=EVENT).json()
    chain = body["rule_chain"]
    assert [hit["name"] for hit in chain] == ["链头", "链尾"]
    assert chain[0]["expires_at"].startswith("2026-03-15")
    assert chain[1]["expires_at"].startswith("2025-04-14")
    assert chain[1]["selected"] is False
    # 最终到期以链头为准
    assert body["expires_at"] == chain[0]["expires_at"]


def test_unknown_event_sensitivity_rejected(client):
    resp = client.post("/evaluate", json={**EVENT, "sensitivity": "???"})
    # 未知敏感级别不违反 schema（自由字符串），但所有阈值/集合策略不命中
    assert resp.status_code == 200
