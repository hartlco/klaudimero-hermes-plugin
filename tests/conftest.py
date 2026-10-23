import json
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from klaudimero_plugin.api import router, service


@pytest.fixture
def avatar():
    chick = {k: {"rgb": 12345} for k in ["body", "shell", "eyes", "beak", "accent", "background"]}
    chick.update(expression="wink", beakStyle="smile", shellStyle="speckled", accessory="glasses", backdrop="confetti", cheeks=True)
    return {"symbol": "terminal.fill", "color": "purple", "style": "chick", "chick": chick}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("KLAUDIMERO_DATA_DIR", str(tmp_path / "plugin"))
    monkeypatch.delenv("KLAUDIMERO_PUSH_RELAY_URL", raising=False)
    monkeypatch.delenv("KLAUDIMERO_PUSH_RELAY_TOKEN", raising=False)
    app = FastAPI()
    @app.middleware("http")
    async def identity(request, next):
        # Stand-in for Hermes's outer auth middleware, never shipped by plugin.
        value = request.headers.get("Authorization", "")
        if value in ["Bearer alice", "Bearer bob"]:
            request.state.session = SimpleNamespace(provider="fixture", issuer="", user_id=value)
        return await next(request)
    app.include_router(router, prefix="/api/plugins/klaudimero")
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer alice"
        yield client
    service(str(tmp_path / "plugin"))[1].close()
    service.cache_clear()

