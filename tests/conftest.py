"""pytest 夹具：隔离的内存 SQLite + 不播种的干净环境。"""

from __future__ import annotations

import os

# 必须在导入应用模块前设置环境变量
os.environ["RETENTION_DB_PATH"] = ":memory:"
os.environ["RETENTION_SEED_SAMPLE"] = "false"
os.environ["RETENTION_DEFAULT_DAYS"] = "45"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client() -> TestClient:
    """每个测试用例使用全新的内存数据库与已初始化的应用。"""
    app.state.db = db.connect(":memory:")
    db.init_db(app.state.db)
    with TestClient(app) as test_client:
        yield test_client
    app.state.db.close()
