import httpx
import pytest
import yaml
from app.agent_runtime.tool_catalog import NativeToolCatalog, NativeToolEntry
from app.config import Settings
from app.main import create_app


@pytest.fixture
def legacy_native_catalog(monkeypatch):
    """Explicit two-tool compatibility coverage; new tests use the real default."""
    from pathlib import Path
    entries = yaml.safe_load((Path(__file__).parent / "fixtures/legacy-voice-tools.yaml").read_text())["tools"]
    monkeypatch.setattr(NativeToolCatalog, "load_file",
        lambda self: [NativeToolEntry.model_validate(item) for item in entries])


@pytest.fixture
async def app(tmp_path):
    settings = Settings(
        _env_file=None,
        auto_create_schema=True,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        dev_admin=True,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.fixture
async def conversation(client):
    response = await client.post("/api/v1/conversations", json={"title": "测试会话"})
    assert response.status_code == 201
    return response.json()["id"]
