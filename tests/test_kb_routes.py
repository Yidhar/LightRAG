import importlib
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.offline


def import_real_api_module(module_name: str):
    sys.modules.pop(module_name, None)

    package_name, _, child_name = module_name.rpartition(".")
    package = sys.modules.get(package_name)
    if package is not None and hasattr(package, child_name):
        delattr(package, child_name)

    return importlib.import_module(module_name)


@pytest.fixture(autouse=True)
def clear_kb_route_modules():
    module_names = (
        "lightrag.api.config",
        "lightrag.api.auth_accounts",
        "lightrag.api.auth",
        "lightrag.api.dependencies",
        "lightrag.api.kb_registry",
        "lightrag.api.permissions",
        "lightrag.api.rag_factory",
        "lightrag.api.routers",
        "lightrag.api.routers.kb_routes",
        "lightrag.api.utils_api",
    )
    for module_name in module_names:
        sys.modules.pop(module_name, None)

    yield

    for module_name in module_names:
        sys.modules.pop(module_name, None)


def build_global_args(**overrides):
    defaults = {
        "token_secret": "test-jwt-secret",
        "jwt_algorithm": "HS256",
        "token_expire_hours": 48,
        "guest_token_expire_hours": 24,
        "refresh_token_expire_hours": 168,
        "auth_accounts": "owner:owner_pass,alice:alice_pass",
        "whitelist_paths": "/health,/api/*",
        "token_auto_renew": True,
        "token_renew_threshold": 0.5,
        "max_upload_size": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def load_modules(monkeypatch, *, auth_accounts: str = "owner:owner_pass,alice:alice_pass"):
    config = import_real_api_module("lightrag.api.config")
    monkeypatch.setattr(
        config,
        "global_args",
        build_global_args(auth_accounts=auth_accounts),
    )

    auth = import_real_api_module("lightrag.api.auth")
    auth = importlib.reload(auth)
    dependencies = import_real_api_module("lightrag.api.dependencies")
    kb_registry = import_real_api_module("lightrag.api.kb_registry")
    kb_routes = import_real_api_module("lightrag.api.routers.kb_routes")
    return auth, dependencies, kb_registry, kb_routes


def make_platform_app(
    dependencies_module,
    *,
    enable_kb_isolation: bool = True,
    default_workspace_id: str = "default",
    default_kb_id: str = "default",
):
    app = FastAPI()
    dependencies_module.install_platform_state(
        app,
        SimpleNamespace(
            default_workspace_id=default_workspace_id,
            default_kb_id=default_kb_id,
            use_db_auth=False,
            enable_kb_isolation=enable_kb_isolation,
        ),
    )
    app.state.doc_manager_cache = {}
    return app


def build_auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_workspace_admin_can_create_list_and_get_knowledge_bases_even_with_kb_viewer_claim(
    monkeypatch, tmp_path
):
    auth, dependencies, kb_registry_module, kb_routes = load_modules(monkeypatch)

    app = make_platform_app(dependencies, enable_kb_isolation=True)
    app.state.kb_registry = kb_registry_module.KnowledgeBaseRegistry(tmp_path)
    app.state.kb_registry.ensure_default_kb("default", "default")
    app.include_router(kb_routes.create_kb_routes())
    client = TestClient(app)

    token = auth.auth_handler.create_token(
        "owner",
        role="user",
        metadata={"auth_mode": "enabled"},
        memberships=[
            {"workspace_id": "default", "kb_id": None, "role": "admin"},
            {"workspace_id": "default", "kb_id": "default", "role": "viewer"},
        ],
    )
    headers = build_auth_header(token)

    create_response = client.post(
        "/workspaces/default/kb",
        headers=headers,
        json={
            "kb_id": "finance.primary",
            "name": "Finance KB",
            "description": "Quarterly documents",
        },
    )
    list_response = client.get("/workspaces/default/kb", headers=headers)
    get_response = client.get(
        "/workspaces/default/kb/finance.primary",
        headers=headers,
    )

    assert create_response.status_code == 200
    assert create_response.json()["kb"]["id"] == "finance_primary"
    assert list_response.status_code == 200
    assert [item["id"] for item in list_response.json()["items"]] == [
        "default",
        "finance_primary",
    ]
    assert get_response.status_code == 200
    assert get_response.json()["name"] == "Finance KB"


def test_kb_admin_can_update_metadata_and_trigger_runtime_eviction(
    monkeypatch, tmp_path
):
    auth, dependencies, kb_registry_module, kb_routes = load_modules(monkeypatch)

    app = make_platform_app(dependencies, enable_kb_isolation=True)
    app.state.kb_registry = kb_registry_module.KnowledgeBaseRegistry(tmp_path)
    app.state.kb_registry.ensure_default_kb("default", "default")
    app.state.kb_registry.create_kb("default", kb_id="finance.primary", name="Finance")

    class FakeRagFactory:
        def __init__(self):
            self.calls = []

        async def evict(self, workspace_id, kb_id):
            self.calls.append((workspace_id, kb_id))
            return True

    rag_factory = FakeRagFactory()
    app.state.rag_factory = rag_factory
    app.include_router(kb_routes.create_kb_routes())
    client = TestClient(app)

    token = auth.auth_handler.create_token(
        "owner",
        role="user",
        metadata={"auth_mode": "enabled"},
        memberships=[
            {"workspace_id": "default", "kb_id": None, "role": "viewer"},
            {"workspace_id": "default", "kb_id": "finance_primary", "role": "admin"},
        ],
    )

    response = client.patch(
        "/workspaces/default/kb/finance.primary",
        headers=build_auth_header(token),
        json={
            "description": "Updated metadata",
            "config_override": {"rerank": True},
            "status": "archived",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "updated"
    assert response.json()["kb"]["description"] == "Updated metadata"
    assert response.json()["kb"]["config_override"] == {"rerank": True}
    assert response.json()["kb"]["status"] == "archived"
    assert rag_factory.calls == [("default", "finance_primary")]


def test_delete_knowledge_base_evicts_runtime_and_cleans_doc_manager_cache(
    monkeypatch, tmp_path
):
    auth, dependencies, kb_registry_module, kb_routes = load_modules(monkeypatch)

    app = make_platform_app(dependencies, enable_kb_isolation=True)
    app.state.kb_registry = kb_registry_module.KnowledgeBaseRegistry(tmp_path)
    app.state.kb_registry.ensure_default_kb("default", "default")
    app.state.kb_registry.create_kb("default", kb_id="finance.primary", name="Finance")
    app.state.doc_manager_cache = {
        "default__default": object(),
        "default__finance_primary": object(),
    }

    class FakeRagFactory:
        def __init__(self):
            self.calls = []

        async def evict(self, workspace_id, kb_id):
            self.calls.append((workspace_id, kb_id))
            return True

    rag_factory = FakeRagFactory()
    app.state.rag_factory = rag_factory
    app.include_router(kb_routes.create_kb_routes())
    client = TestClient(app)

    token = auth.auth_handler.create_token(
        "owner",
        role="user",
        metadata={"auth_mode": "enabled"},
        memberships=[
            {"workspace_id": "default", "kb_id": None, "role": "admin"},
            {"workspace_id": "default", "kb_id": "default", "role": "viewer"},
        ],
    )

    response = client.delete(
        "/workspaces/default/kb/finance.primary",
        headers=build_auth_header(token),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "deleted"
    assert app.state.kb_registry.exists("default", "finance_primary") is False
    assert rag_factory.calls == [("default", "finance_primary")]
    assert "default__finance_primary" not in app.state.doc_manager_cache
    assert "default__default" in app.state.doc_manager_cache


def test_default_knowledge_base_cannot_be_deleted(monkeypatch, tmp_path):
    auth, dependencies, kb_registry_module, kb_routes = load_modules(monkeypatch)

    app = make_platform_app(dependencies, enable_kb_isolation=True)
    app.state.kb_registry = kb_registry_module.KnowledgeBaseRegistry(tmp_path)
    app.state.kb_registry.ensure_default_kb("default", "default")
    app.include_router(kb_routes.create_kb_routes())
    client = TestClient(app)

    token = auth.auth_handler.create_token(
        "owner",
        role="user",
        metadata={"auth_mode": "enabled"},
        memberships=[
            {"workspace_id": "default", "kb_id": None, "role": "admin"},
        ],
    )

    response = client.delete(
        "/workspaces/default/kb/default",
        headers=build_auth_header(token),
    )

    assert response.status_code == 400
    assert "cannot be deleted" in response.json()["detail"]


def test_kb_routes_require_kb_isolation_feature(monkeypatch, tmp_path):
    auth, dependencies, _kb_registry_module, kb_routes = load_modules(monkeypatch)

    app = make_platform_app(dependencies, enable_kb_isolation=False)
    app.include_router(kb_routes.create_kb_routes())
    client = TestClient(app)

    token = auth.auth_handler.create_token(
        "owner",
        role="user",
        metadata={"auth_mode": "enabled"},
    )

    response = client.get(
        "/workspaces/default/kb",
        headers=build_auth_header(token),
    )

    assert response.status_code == 501
    assert response.json()["detail"] == (
        "Knowledge base management requires ENABLE_KB_ISOLATION=true"
    )
