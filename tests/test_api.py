"""API 集成测试：无模型环境下验证全链路降级行为（不依赖 LLM/存储真实可用）。"""
import time

import pytest
from fastapi.testclient import TestClient

from reviewhive.api.app import create_app
from reviewhive.config import load_settings

SAMPLE_CODE = """
public class UserService {
    public User find(String id) {
        String sql = "SELECT * FROM t_user WHERE id = '" + id + "'";
        return dao.query(sql);
    }
}
"""


@pytest.fixture()
def client(tmp_path):
    settings = load_settings()
    settings.app.db_path = str(tmp_path / "test.db")
    settings.app.data_dir = str(tmp_path / "data")
    settings.models.vision.enabled = False
    settings.models.llm.base_url = "http://127.0.0.1:9/v1"  # 必然拒绝连接，隔离真实模型
    settings.models.llm.timeout_seconds = 3
    settings.models.embedding.provider = "openai"
    settings.models.embedding.base_url = "http://127.0.0.1:1/v1"  # 必然不可达，跳过本地模型加载
    settings.models.reranker.enabled = False
    settings.observability.enabled = False  # 测试不产生追踪数据
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def test_agents_and_skills_introspection(client):
    agents = client.get("/api/agents").json()
    names = {agent["name"] for agent in agents}
    assert {"security", "performance", "style", "test", "vision"} <= names

    skills = client.get("/api/skills").json()
    assert any(skill["name"] == "search_kb" for skill in skills)


def test_review_session_full_flow_degraded(client):
    resp = client.post(
        "/api/reviews",
        json={"code": SAMPLE_CODE, "filename": "UserService.java", "language": "java"},
    )
    assert resp.status_code == 200
    session_id = resp.json()["session_id"]

    deadline = time.time() + 30
    session = None
    while time.time() < deadline:
        session = client.get(f"/api/reviews/{session_id}").json()
        if session["status"] != "running":
            break
        time.sleep(1)

    assert session is not None and session["status"] == "done"
    report = session["report"]
    assert report["plan"]["sub_agents"]  # 无 LLM 时使用默认调度计划
    assert report["findings"] == []      # 无 LLM 时子 Agent 全部优雅失败
    assert report["duration_ms"] >= 0

    events = client.get(f"/api/reviews/{session_id}/events").text
    assert "report" in events


def test_unknown_session_returns_404(client):
    assert client.get("/api/reviews/nope").status_code == 404
