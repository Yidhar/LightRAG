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
def clear_permission_modules():
    module_names = (
        "lightrag.api.config",
        "lightrag.api.auth_accounts",
        "lightrag.api.auth",
        "lightrag.api.auth_provider",
        "lightrag.api.dependencies",
        "lightrag.api.kb_registry",
        "lightrag.api.permissions",
        "lightrag.api.utils_api",
        "lightrag.api.routers",
        "lightrag.api.routers.query_routes",
        "lightrag.api.routers.graph_routes",
        "lightrag.api.routers.document_routes",
        "lightrag.api.routers.image_routes",
        "lightrag.api.routers.ollama_api",
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
        "auth_accounts": "admin:admin_pass",
        "use_db_auth": False,
        "whitelist_paths": "/health,/api/*",
        "token_auto_renew": True,
        "token_renew_threshold": 0.5,
        "max_upload_size": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def load_modules(monkeypatch, *, auth_accounts: str = "admin:admin_pass"):
    config = import_real_api_module("lightrag.api.config")
    monkeypatch.setattr(
        config,
        "global_args",
        build_global_args(auth_accounts=auth_accounts),
    )

    auth = import_real_api_module("lightrag.api.auth")
    auth = importlib.reload(auth)
    dependencies = import_real_api_module("lightrag.api.dependencies")
    permissions = import_real_api_module("lightrag.api.permissions")
    query_routes = import_real_api_module("lightrag.api.routers.query_routes")
    graph_routes = import_real_api_module("lightrag.api.routers.graph_routes")
    document_routes = import_real_api_module("lightrag.api.routers.document_routes")
    return auth, dependencies, permissions, query_routes, graph_routes, document_routes


def make_platform_app(dependencies_module, *, use_db_auth: bool = False):
    app = FastAPI()
    dependencies_module.install_platform_state(
        app,
        SimpleNamespace(
            default_workspace_id="default",
            default_kb_id="default",
            use_db_auth=use_db_auth,
            enable_kb_isolation=True,
        ),
    )
    app.state.db_ready = use_db_auth
    return app


def async_return(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner


class FakeRag:
    def __init__(self, *, prefix: str = "", workspace: str = "default"):
        self.prefix = prefix
        self.workspace = workspace

    async def aquery_llm(self, query, param):
        return {
            "llm_response": {
                "content": f"{self.prefix}response:{query}",
                "is_streaming": False,
            },
            "data": {"references": [], "chunks": []},
        }

    async def get_graph_labels(self):
        if self.prefix:
            return [f"{self.prefix}entity-a", f"{self.prefix}entity-b"]
        return ["entity-a", "entity-b"]


def test_resolve_effective_role_prefers_exact_kb_membership(monkeypatch):
    _auth, _dependencies, permissions, *_ = load_modules(monkeypatch)

    role = permissions.resolve_effective_role(
        {
            "role": "user",
            "memberships": [
                {"workspace_id": "default", "kb_id": None, "role": "viewer"},
                {"workspace_id": "default", "kb_id": "default", "role": "editor"},
            ],
        },
        workspace_id="default",
        kb_id="default",
    )

    assert role == "editor"


def test_resolve_effective_role_denies_cross_workspace_access(monkeypatch):
    """A user whose memberships do not include the requested workspace
    must fall to ``no_access``, not ``viewer`` (which still has kb:query)."""
    _auth, _dependencies, permissions, *_ = load_modules(monkeypatch)

    role = permissions.resolve_effective_role(
        {
            "role": "user",
            "memberships": [
                {"workspace_id": "workspace_a", "kb_id": None, "role": "owner"},
            ],
        },
        workspace_id="workspace_b",
        kb_id="default",
    )

    assert role == permissions.NO_ACCESS_ROLE
    # Sanity: no_access has no entries in ROLE_PERMISSIONS so every
    # permission check fails — this is what keeps workspace B invisible
    # to a token that only has membership in workspace A.
    assert permissions.has_permission(role, permissions.Action.KB_VIEW) is False
    assert permissions.has_permission(role, permissions.Action.KB_QUERY) is False
    assert permissions.has_permission(role, permissions.Action.WORKSPACE_VIEW) is False


def test_legacy_user_role_maps_to_owner_permissions(monkeypatch):
    auth, dependencies, _permissions, query_routes, _graph_routes, _document_routes = load_modules(
        monkeypatch
    )

    app = make_platform_app(dependencies)
    app.include_router(query_routes.create_query_routes(FakeRag()))
    client = TestClient(app)

    token = auth.auth_handler.create_token(
        "admin",
        role="user",
        metadata={"auth_mode": "enabled"},
    )
    response = client.post(
        "/query",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "hello world", "mode": "mix"},
    )

    assert response.status_code == 200
    assert response.json()["response"] == "response:hello world"


def test_viewer_membership_can_query_but_cannot_upload_or_edit_graph(monkeypatch):
    auth, dependencies, _permissions, query_routes, graph_routes, document_routes = load_modules(
        monkeypatch
    )

    app = make_platform_app(dependencies)
    app.include_router(query_routes.create_query_routes(FakeRag()))
    app.include_router(graph_routes.create_graph_routes(FakeRag()))
    app.include_router(
        document_routes.create_document_routes(
            FakeRag(),
            SimpleNamespace(),
        )
    )
    client = TestClient(app)

    viewer_token = auth.auth_handler.create_token(
        "viewer-user",
        role="user",
        metadata={"auth_mode": "enabled"},
        memberships=[
            {"workspace_id": "default", "kb_id": None, "role": "viewer"},
        ],
    )
    headers = {"Authorization": f"Bearer {viewer_token}"}

    query_response = client.post(
        "/query",
        headers=headers,
        json={"query": "viewer query", "mode": "mix"},
    )
    upload_response = client.post(
        "/documents/upload",
        headers=headers,
        files={"file": ("sample.txt", b"hello", "text/plain")},
    )
    graph_edit_response = client.post(
        "/graph/entity/edit",
        headers=headers,
        json={
            "entity_name": "EntityA",
            "updated_data": {"description": "updated"},
            "allow_rename": False,
            "allow_merge": False,
        },
    )

    assert query_response.status_code == 200
    assert upload_response.status_code == 403
    assert upload_response.json()["detail"] == (
        "Permission denied for action 'kb:upload_document'"
    )
    assert graph_edit_response.status_code == 403
    assert graph_edit_response.json()["detail"] == (
        "Permission denied for action 'kb:edit_graph'"
    )


def test_api_key_retains_owner_level_access(monkeypatch):
    _auth, dependencies, _permissions, query_routes, _graph_routes, _document_routes = load_modules(
        monkeypatch
    )

    app = make_platform_app(dependencies)
    app.include_router(query_routes.create_query_routes(FakeRag(), api_key="secret-key"))
    client = TestClient(app)

    response = client.post(
        "/query",
        headers={"X-API-Key": "secret-key"},
        json={"query": "api key query", "mode": "mix"},
    )

    assert response.status_code == 200
    assert response.json()["response"] == "response:api key query"


def test_routes_require_credentials_when_guest_mode_is_removed(monkeypatch):
    _auth, dependencies, _permissions, query_routes, _graph_routes, _document_routes = load_modules(
        monkeypatch,
        auth_accounts="",
    )

    app = make_platform_app(dependencies)
    app.include_router(query_routes.create_query_routes(FakeRag()))
    client = TestClient(app)

    response = client.post(
        "/query",
        json={"query": "open mode query", "mode": "mix"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "No credentials provided. Please login."


def test_query_and_graph_routes_can_resolve_request_scoped_runtimes(
    monkeypatch, tmp_path
):
    (
        auth,
        dependencies,
        _permissions,
        query_routes,
        graph_routes,
        _document_routes,
    ) = load_modules(
        monkeypatch,
        auth_accounts="",
    )
    kb_registry = import_real_api_module("lightrag.api.kb_registry")

    app = make_platform_app(dependencies)
    app.state.kb_registry = kb_registry.KnowledgeBaseRegistry(tmp_path)
    app.state.kb_registry.ensure_default_kb("team_alpha", "finance_primary")
    app.state.kb_registry.ensure_default_kb("team_beta", "support_hub")

    class FakeRagFactory:
        def __init__(self):
            self.calls = []

        async def get(self, workspace_id, kb_id):
            self.calls.append((workspace_id, kb_id))
            return FakeRag(prefix=f"{workspace_id}/{kb_id}:")

    rag_factory = FakeRagFactory()
    app.state.rag_factory = rag_factory

    app.include_router(query_routes.create_query_routes())
    app.include_router(graph_routes.create_graph_routes())
    client = TestClient(app)
    token = auth.auth_handler.create_token(
        "platform-owner",
        role="user",
        metadata={"auth_mode": "enabled"},
    )
    headers = {
        "Authorization": f"Bearer {token}",
    }

    query_response = client.post(
        "/query",
        headers={
            **headers,
            "LIGHTRAG-WORKSPACE": "team-alpha",
            "LIGHTRAG-KB": "finance.primary",
        },
        json={"query": "route query", "mode": "mix"},
    )
    graph_response = client.get(
        "/graph/label/list",
        headers={
            **headers,
            "LIGHTRAG-WORKSPACE": "team-beta",
            "LIGHTRAG-KB": "support.hub",
        },
    )

    assert query_response.status_code == 200
    assert query_response.json()["response"] == (
        "team_alpha/finance_primary:response:route query"
    )
    assert graph_response.status_code == 200
    assert graph_response.json() == [
        "team_beta/support_hub:entity-a",
        "team_beta/support_hub:entity-b",
    ]
    assert rag_factory.calls == [
        ("team_alpha", "finance_primary"),
        ("team_beta", "support_hub"),
    ]


def test_document_upload_route_resolves_request_scoped_runtime_and_doc_manager(
    monkeypatch, tmp_path
):
    (
        auth,
        dependencies,
        _permissions,
        _query_routes,
        _graph_routes,
        document_routes,
    ) = load_modules(
        monkeypatch,
        auth_accounts="",
    )
    kb_registry = import_real_api_module("lightrag.api.kb_registry")

    app = make_platform_app(dependencies)
    app.state.kb_registry = kb_registry.KnowledgeBaseRegistry(tmp_path / "registry")
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

        async def get(self, workspace_id, kb_id):
            self.calls.append((workspace_id, kb_id))
            return FakeUploadRag(f"{workspace_id}__{kb_id}")

    rag_factory = FakeRagFactory()
    app.state.rag_factory = rag_factory

    app.include_router(document_routes.create_document_routes())
    client = TestClient(app)
    token = auth.auth_handler.create_token(
        "platform-owner",
        role="user",
        metadata={"auth_mode": "enabled"},
    )

    response = client.post(
        "/documents/upload",
        headers={
            "Authorization": f"Bearer {token}",
            "LIGHTRAG-WORKSPACE": "team-alpha",
            "LIGHTRAG-KB": "finance.primary",
        },
        files={"file": ("sample.txt", b"hello world", "text/plain")},
    )

    assert response.status_code == 200
    assert response.json()["track_id"] == "upload-track"
    assert rag_factory.calls == [("team_alpha", "finance_primary")]
    assert captured == [
        (
            "team_alpha__finance_primary",
            tmp_path / "inputs" / "team_alpha__finance_primary" / "sample.txt",
            "upload-track",
            True,
        )
    ]
    assert (
        tmp_path / "inputs" / "team_alpha__finance_primary" / "sample.txt"
    ).read_bytes() == b"hello world"


def test_image_metadata_route_resolves_request_scoped_runtime(monkeypatch, tmp_path):
    (
        auth,
        dependencies,
        _permissions,
        _query_routes,
        _graph_routes,
        _document_routes,
    ) = load_modules(
        monkeypatch,
        auth_accounts="",
    )
    kb_registry = import_real_api_module("lightrag.api.kb_registry")
    image_routes = import_real_api_module("lightrag.api.routers.image_routes")

    app = make_platform_app(dependencies)
    app.state.kb_registry = kb_registry.KnowledgeBaseRegistry(tmp_path / "registry")
    app.state.kb_registry.ensure_default_kb("team_alpha", "finance_primary")

    class FakeImageRag:
        def __init__(self, workspace: str, kb_id: str):
            self.workspace = workspace
            self.image_blob_store = SimpleNamespace(
                get_metadata=async_return({"content_type": "image/png"}),
                get_reference=async_return(None),
                get=async_return(b"png-bytes"),
            )
            self.image_metadata = SimpleNamespace(
                get_by_id=async_return(
                    {
                        "blob_ref": "memory://img-1",
                        "content_type": "image/png",
                        "source_doc_id": f"{workspace}:{kb_id}:doc-1",
                    }
                )
            )

    class FakeRagFactory:
        def __init__(self):
            self.calls = []

        async def get(self, workspace_id, kb_id):
            self.calls.append((workspace_id, kb_id))
            return FakeImageRag(workspace_id, kb_id)

    rag_factory = FakeRagFactory()
    app.state.rag_factory = rag_factory

    app.include_router(image_routes.create_image_routes())
    client = TestClient(app)
    token = auth.auth_handler.create_token(
        "platform-owner",
        role="user",
        metadata={"auth_mode": "enabled"},
    )

    response = client.get(
        "/images/img-1/metadata",
        headers={
            "Authorization": f"Bearer {token}",
            "LIGHTRAG-WORKSPACE": "team-alpha",
            "LIGHTRAG-KB": "finance.primary",
        },
    )

    assert response.status_code == 200
    assert response.json()["source_doc_id"] == "team_alpha:finance_primary:doc-1"
    assert rag_factory.calls == [("team_alpha", "finance_primary")]


def test_ollama_chat_route_resolves_request_scoped_runtime(monkeypatch, tmp_path):
    (
        auth,
        dependencies,
        _permissions,
        _query_routes,
        _graph_routes,
        _document_routes,
    ) = load_modules(
        monkeypatch,
        auth_accounts="",
    )
    kb_registry = import_real_api_module("lightrag.api.kb_registry")
    ollama_api_module = import_real_api_module("lightrag.api.routers.ollama_api")

    app = make_platform_app(dependencies)
    app.state.kb_registry = kb_registry.KnowledgeBaseRegistry(tmp_path / "registry")
    app.state.kb_registry.ensure_default_kb("team_alpha", "finance_primary")

    class FakeOllamaRag:
        def __init__(self, workspace: str):
            self.workspace = workspace
            self.llm_model_kwargs = {"temperature": 0}
            self.last_llm_kwargs = None

        async def aquery(self, query, param):
            return f"{self.workspace}:answer:{query}"

        async def llm_model_func(
            self, query, stream=False, history_messages=None, **kwargs
        ):
            self.last_llm_kwargs = dict(kwargs)
            return f"{self.workspace}:llm:{query}"

    class FakeRagFactory:
        def __init__(self):
            self.calls = []
            self.instances = []

        async def get(self, workspace_id, kb_id):
            self.calls.append((workspace_id, kb_id))
            rag = FakeOllamaRag(f"{workspace_id}__{kb_id}")
            self.instances.append(rag)
            return rag

    rag_factory = FakeRagFactory()
    app.state.rag_factory = rag_factory

    server_infos = SimpleNamespace(
        LIGHTRAG_MODEL="lightrag:latest",
        LIGHTRAG_CREATED_AT="2026-04-16T00:00:00Z",
        LIGHTRAG_SIZE=1,
        LIGHTRAG_DIGEST="digest",
        LIGHTRAG_NAME="lightrag",
    )
    ollama_api = ollama_api_module.OllamaAPI(
        top_k=17,
        api_key=None,
        ollama_server_infos=server_infos,
    )
    app.include_router(ollama_api.router, prefix="/api")
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        headers={
            "Authorization": f"Bearer {auth.auth_handler.create_token('platform-owner', role='user', metadata={'auth_mode': 'enabled'})}",
            "LIGHTRAG-WORKSPACE": "team-alpha",
            "LIGHTRAG-KB": "finance.primary",
        },
        json={
            "model": "lightrag:latest",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
            "system": "be concise",
        },
    )

    assert response.status_code == 200
    assert response.json()["message"]["content"] == (
        "team_alpha__finance_primary:answer:hello"
    )
    assert rag_factory.calls == [("team_alpha", "finance_primary")]
    assert len(rag_factory.instances) == 1
    assert rag_factory.instances[0].llm_model_kwargs == {"temperature": 0}
