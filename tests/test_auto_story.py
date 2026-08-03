import pytest
from fastapi.testclient import TestClient

from polyglo.chat import OfflineChatCompleter, generate_story_from_description
from polyglo.web import app
from tests.test_api import safe_providers


@pytest.fixture()
def client(tmp_path, monkeypatch):
    safe_providers(monkeypatch)
    return TestClient(app)


def test_generate_story_from_description_offline() -> None:
    completer = OfflineChatCompleter()
    title, text = generate_story_from_description(
        description="A brave cat exploring the ancient castle",
        cefr="B1",
        completer=completer,
        model="test-model",
    )

    assert "Cat" in title or "Story" in title
    assert "brave cat" in text
    assert len(text) > 20


def test_api_generate_story_endpoint(client) -> None:
    resp = client.post(
        "/api/generate-story",
        json={"prompt": "A friendly dragon learning to bake cakes", "cefr": "A2"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "title" in data
    assert "source_text" in data
    assert len(data["source_text"]) > 0


def test_api_generate_story_empty_prompt_400(client) -> None:
    resp = client.post(
        "/api/generate-story",
        json={"prompt": "  ", "cefr": "A2"},
    )
    assert resp.status_code == 400
