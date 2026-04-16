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
def clear_kb_isolation_modules():
    module_names = (
        "lightrag.api.config",
        "lightrag.api.auth_accounts",
        "lightrag.api.auth",
        "lightrag.api.dependencies",
        "lightrag.api.kb_registry",
        "lightrag.api.permissions",
        "lightrag.api.utils_api",
        "lightrag.api.routers",
        "lightrag.api.routers.query_routes",
        "lightrag.api.routers.document_routes",
        "lightrag.api.routers.kb_routes",
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
        "auth_accounts": "",
        "whitelist_paths": "/health,/api/*",
        "token_auto_renew": True,
        "token_renew_threshold": 0.5,
        "max_upload_size": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def load_modules(monkeypatch):
    config = import_real_api_module("lightrag.api.config")
    monkeypatch.setattr(
        config,
        "global_args",
        build_global_args(),
    )

    import_real_api_module("lightrag.api.auth")
    dependencies = import_real_api_module("lightrag.api.dependencies")
    kb_registry = import_real_api_module("lightrag.api.kb_registry")
    query_routes = import_real_api_module("lightrag.api.routers.query_routes")
    document_routes = import_real_api_module("lightrag.api.routers.document_routes")
    kb_routes = import_real_api_module("lightrag.api.routers.kb_routes")
    return dependencies, kb_registry, query_routes, document_routes, kb_routes


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
            kb_separator="__",
        ),
    )
    app.state.doc_manager_cache = {}
    return app


def async_return(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner


class FakeQueryRag:
    def __init__(self, prefix: str):
        self.prefix = prefix

    async def aquery_llm(self, query, param):
        return {
            "llm_response": {
                "content": f"{self.prefix}response:{query}",
                "is_streaming": False,
            },
            "data": {"references": [], "chunks": []},
        }


def test_query_routes_isolate_same_workspace_across_two_kbs(monkeypatch, tmp_path):
    dependencies, kb_registry_module, query_routes, _document_routes, _kb_routes = (
        load_modules(monkeypatch)
    )

    app = make_platform_app(dependencies)
    app.state.kb_registry = kb_registry_module.KnowledgeBaseRegistry(tmp_path)
    app.state.kb_registry.ensure_default_kb("team_alpha", "finance_primary")
    app.state.kb_registry.ensure_default_kb("team_alpha", "legal_ops")

    class FakeRagFactory:
        def __init__(self):
            self.calls = []

        def compose_workspace(self, workspace_id, kb_id):
            return f"isolated_scope__{workspace_id}__{kb_id}"

        async def get(self, workspace_id, kb_id):
            self.calls.append((workspace_id, kb_id))
            return FakeQueryRag(prefix=f"{workspace_id}/{kb_id}:")

    rag_factory = FakeRagFactory()
    app.state.rag_factory = rag_factory

    app.include_router(query_routes.create_query_routes())
    client = TestClient(app)

    finance_response = client.post(
        "/query",
        headers={
            "LIGHTRAG-WORKSPACE": "team-alpha",
            "LIGHTRAG-KB": "finance.primary",
        },
        json={"query": "shared workspace", "mode": "mix"},
    )
    legal_response = client.post(
        "/query",
        headers={
            "LIGHTRAG-WORKSPACE": "team-alpha",
            "LIGHTRAG-KB": "legal.ops",
        },
        json={"query": "shared workspace", "mode": "mix"},
    )

    assert finance_response.status_code == 200
    assert legal_response.status_code == 200
    assert finance_response.json()["response"] == (
        "team_alpha/finance_primary:response:shared workspace"
    )
    assert legal_response.json()["response"] == (
        "team_alpha/legal_ops:response:shared workspace"
    )
    assert rag_factory.calls == [
        ("team_alpha", "finance_primary"),
        ("team_alpha", "legal_ops"),
    ]


def test_document_upload_uses_factory_composed_runtime_workspace(
    monkeypatch, tmp_path
):
    (
        dependencies,
        kb_registry_module,
        _query_routes,
        document_routes,
        _kb_routes,
    ) = load_modules(monkeypatch)

    app = make_platform_app(dependencies)
    app.state.kb_registry = kb_registry_module.KnowledgeBaseRegistry(tmp_path / "registry")
    app.state.kb_registry.ensure_default_kb("team_alpha", "finance_primary")
    app.state.doc_manager_base_input_dir = str(tmp_path / "inputs")
    app.state.default_runtime_workspace = "default__default"
    app.state.default_doc_manager = document_routes.DocumentManager(
        str(tmp_path / "inputs"), workspace="default__default"
    )
    app.state.doc_manager_cache = {}

    class FakeUploadRag:
        def __init__(self, workspace: str):
            self.workspace = workspace
            self.doc_status = SimpleNamespace(
                get_doc_by_file_path=async_return(None),
            )
            self.image_embedding_func = None
            self.images_vdb = None
            self.image_blob_store = None

    captured = []

    async def fake_pipeline_index_file(rag, file_path, track_id, preclaimed):
        captured.append((rag.workspace, file_path, track_id, preclaimed))
        return True

    async def fake_claim(_rag, _filename, _owner):
        return True, None

    monkeypatch.setattr(
        document_routes,
        "generate_track_id",
        lambda prefix: f"{prefix}-track",
    )
    monkeypatch.setattr(document_routes, "pipeline_index_file", fake_pipeline_index_file)
    monkeypatch.setattr(document_routes, "_claim_input_file", fake_claim)

    class FakeRagFactory:
        def __init__(self):
            self.calls = []

        def compose_workspace(self, workspace_id, kb_id):
            return f"factory_scope__{workspace_id}__{kb_id}"

        async def get(self, workspace_id, kb_id):
            self.calls.append((workspace_id, kb_id))
            return FakeUploadRag(self.compose_workspace(workspace_id, kb_id))

    rag_factory = FakeRagFactory()
    app.state.rag_factory = rag_factory

    app.include_router(document_routes.create_document_routes())
    client = TestClient(app)

    response = client.post(
        "/documents/upload",
        headers={
            "LIGHTRAG-WORKSPACE": "team-alpha",
            "LIGHTRAG-KB": "finance.primary",
        },
        files={"file": ("sample.txt", b"hello world", "text/plain")},
    )

    expected_workspace = "factory_scope__team_alpha__finance_primary"
    expected_path = tmp_path / "inputs" / expected_workspace / "sample.txt"

    assert response.status_code == 200
    assert response.json()["track_id"] == "upload-track"
    assert rag_factory.calls == [("team_alpha", "finance_primary")]
    assert captured == [
        (
            expected_workspace,
            expected_path,
            "upload-track",
            True,
        )
    ]
    assert expected_path.read_bytes() == b"hello world"
    assert set(app.state.doc_manager_cache.keys()) == {expected_workspace}


def test_kb_delete_cleans_doc_manager_cache_with_factory_composed_workspace(
    monkeypatch, tmp_path
):
    dependencies, kb_registry_module, _query_routes, _document_routes, kb_routes = (
        load_modules(monkeypatch)
    )

    app = make_platform_app(dependencies)
    app.state.kb_registry = kb_registry_module.KnowledgeBaseRegistry(tmp_path)
    app.state.kb_registry.ensure_default_kb("team_alpha", "default")
    app.state.kb_registry.create_kb("team_alpha", kb_id="finance.primary", name="Finance")
    app.state.doc_manager_cache = {
        "factory_scope__team_alpha__default": object(),
        "factory_scope__team_alpha__finance_primary": object(),
    }

    class FakeRagFactory:
        def __init__(self):
            self.calls = []

        def compose_workspace(self, workspace_id, kb_id):
            return f"factory_scope__{workspace_id}__{kb_id}"

        async def evict(self, workspace_id, kb_id):
            self.calls.append((workspace_id, kb_id))
            return True

    rag_factory = FakeRagFactory()
    app.state.rag_factory = rag_factory

    app.include_router(kb_routes.create_kb_routes())
    client = TestClient(app)

    response = client.delete("/workspaces/team-alpha/kb/finance.primary")

    assert response.status_code == 200
    assert response.json()["status"] == "deleted"
    assert rag_factory.calls == [("team_alpha", "finance_primary")]
    assert "factory_scope__team_alpha__finance_primary" not in app.state.doc_manager_cache
    assert "factory_scope__team_alpha__default" in app.state.doc_manager_cache
