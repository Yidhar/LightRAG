import importlib
import sqlite3
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.requests import Request

pytestmark = pytest.mark.offline


def import_real_api_module(module_name: str):
    sys.modules.pop(module_name, None)

    package_name, _, child_name = module_name.rpartition(".")
    package = sys.modules.get(package_name)
    if package is not None and hasattr(package, child_name):
        delattr(package, child_name)

    return importlib.import_module(module_name)


@pytest.fixture(autouse=True)
def clear_backend_scaffold_modules():
    module_names = (
        "lightrag.api.config",
        "lightrag.api.db",
        "lightrag.api.dependencies",
        "lightrag.api.kb_registry",
        "lightrag.api.rag_factory",
        "lightrag.api.models",
        "lightrag.api.models.kb",
    )
    for module_name in module_names:
        sys.modules.pop(module_name, None)

    yield

    for module_name in module_names:
        sys.modules.pop(module_name, None)


def build_request(
    app: FastAPI,
    *,
    path_params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> Request:
    header_items = [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "app": app,
        "method": "GET",
        "path": "/health",
        "raw_path": b"/health",
        "query_string": b"",
        "headers": header_items,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "scheme": "http",
        "http_version": "1.1",
        "path_params": path_params or {},
    }
    return Request(scope)


def test_parse_args_reads_platform_feature_flags_from_env(monkeypatch):
    config = import_real_api_module("lightrag.api.config")

    monkeypatch.setenv("USE_DB_AUTH", "true")
    monkeypatch.setenv("ENABLE_KB_ISOLATION", "true")
    monkeypatch.setenv("KB_SEPARATOR", "--")
    monkeypatch.setenv("DB_URL", "sqlite+aiosqlite:///./custom_auth.db")
    monkeypatch.setenv("DEFAULT_WORKSPACE_ID", "team-alpha")
    monkeypatch.setenv("DEFAULT_KB_ID", "sales.primary")
    monkeypatch.setattr(sys, "argv", ["lightrag-server"])

    args = config.parse_args()

    assert args.use_db_auth is True
    assert args.enable_kb_isolation is True
    assert args.kb_separator == "--"
    assert args.db_url == "sqlite+aiosqlite:///./custom_auth.db"
    assert args.default_workspace_id == "team_alpha"
    assert args.default_kb_id == "sales_primary"


def test_parse_args_rejects_unsafe_kb_separator(monkeypatch):
    config = import_real_api_module("lightrag.api.config")

    monkeypatch.setenv("KB_SEPARATOR", "../")
    monkeypatch.setattr(sys, "argv", ["lightrag-server"])

    args = config.parse_args()

    assert args.kb_separator == "__"


def test_collect_manual_kb_isolation_migration_backends_flags_operator_managed_stores():
    config = import_real_api_module("lightrag.api.config")

    args = SimpleNamespace(
        kv_storage="RedisKVStorage",
        doc_status_storage="MongoDocStatusStorage",
        graph_storage="PGGraphStorage",
        vector_storage="FaissVectorDBStorage",
    )

    assert config.collect_manual_kb_isolation_migration_backends(args) == [
        "kv_storage=RedisKVStorage",
        "doc_status_storage=MongoDocStatusStorage",
        "graph_storage=PGGraphStorage",
        "vector_storage=FaissVectorDBStorage",
    ]


def test_request_context_prefers_path_params_then_headers():
    dependencies = import_real_api_module("lightrag.api.dependencies")
    app = FastAPI()
    dependencies.install_platform_state(
        app,
        SimpleNamespace(
            default_workspace_id="default-workspace",
            default_kb_id="default-kb",
            use_db_auth=False,
            enable_kb_isolation=False,
            kb_separator="__",
        ),
    )
    request = build_request(
        app,
        path_params={"workspace_id": "core-team"},
        headers={"LIGHTRAG-KB": "finance.primary"},
    )

    context = dependencies.get_request_context(request)

    assert context.workspace_id == "core_team"
    assert context.kb_id == "finance_primary"
    assert context.source == {"workspace": "path", "kb": "header"}
    assert request.state.request_context is context


def test_request_context_uses_platform_defaults_when_identifiers_are_missing():
    dependencies = import_real_api_module("lightrag.api.dependencies")
    app = FastAPI()
    dependencies.install_platform_state(
        app,
        SimpleNamespace(
            default_workspace_id="ops-main",
            default_kb_id="kb-root",
            use_db_auth=True,
            enable_kb_isolation=True,
            kb_separator="::",
        ),
    )
    request = build_request(app)

    context = dependencies.get_request_context(request)

    assert context.workspace_id == "ops_main"
    assert context.kb_id == "kb_root"
    assert context.source == {"workspace": "default", "kb": "default"}
    assert app.state.kb_separator == "::"
    assert app.state.kb_registry is None
    assert app.state.rag_factory is None


def test_get_current_kb_validates_registry_when_isolation_enabled(tmp_path):
    dependencies = import_real_api_module("lightrag.api.dependencies")
    registry_module = import_real_api_module("lightrag.api.kb_registry")

    app = FastAPI()
    dependencies.install_platform_state(
        app,
        SimpleNamespace(
            default_workspace_id="ops-main",
            default_kb_id="kb-root",
            use_db_auth=False,
            enable_kb_isolation=True,
            kb_separator="__",
        ),
    )
    app.state.kb_registry = registry_module.KnowledgeBaseRegistry(tmp_path)
    app.state.kb_registry.ensure_default_kb("ops_main", "finance_primary")

    existing_request = build_request(
        app,
        path_params={"workspace_id": "ops-main", "kb_id": "finance.primary"},
    )
    assert dependencies.get_current_kb(existing_request) == "finance_primary"

    missing_request = build_request(
        app,
        path_params={"workspace_id": "ops-main", "kb_id": "missing.kb"},
    )
    with pytest.raises(Exception) as exc_info:
        dependencies.get_current_kb(missing_request)
    assert getattr(exc_info.value, "status_code", None) == 404


@pytest.mark.asyncio
async def test_get_current_rag_returns_default_when_isolation_disabled():
    dependencies = import_real_api_module("lightrag.api.dependencies")
    app = FastAPI()
    dependencies.install_platform_state(
        app,
        SimpleNamespace(
            default_workspace_id="default",
            default_kb_id="default",
            use_db_auth=False,
            enable_kb_isolation=False,
            kb_separator="__",
        ),
    )
    sentinel_rag = object()
    app.state.default_rag = sentinel_rag

    request = build_request(
        app,
        path_params={"workspace_id": "team-alpha", "kb_id": "sales.primary"},
    )

    resolved = await dependencies.get_current_rag(request)

    assert resolved is sentinel_rag


@pytest.mark.asyncio
async def test_get_current_rag_uses_factory_when_isolation_enabled(tmp_path):
    dependencies = import_real_api_module("lightrag.api.dependencies")
    registry_module = import_real_api_module("lightrag.api.kb_registry")

    app = FastAPI()
    dependencies.install_platform_state(
        app,
        SimpleNamespace(
            default_workspace_id="default",
            default_kb_id="default",
            use_db_auth=False,
            enable_kb_isolation=True,
            kb_separator="__",
        ),
    )

    registry = registry_module.KnowledgeBaseRegistry(tmp_path)
    registry.ensure_default_kb("team_alpha", "sales_primary")
    app.state.kb_registry = registry

    class FakeFactory:
        def __init__(self):
            self.calls = []

        async def get(self, workspace_id, kb_id):
            self.calls.append((workspace_id, kb_id))
            return {"workspace_id": workspace_id, "kb_id": kb_id}

    factory = FakeFactory()
    app.state.rag_factory = factory

    request = build_request(
        app,
        path_params={"workspace_id": "team-alpha", "kb_id": "sales.primary"},
    )

    resolved = await dependencies.get_current_rag(request)

    assert resolved == {"workspace_id": "team_alpha", "kb_id": "sales_primary"}
    assert factory.calls == [("team_alpha", "sales_primary")]


@pytest.mark.asyncio
async def test_init_db_creates_bootstrap_table(tmp_path):
    db_module = import_real_api_module("lightrag.api.db")

    db_path = tmp_path / "platform_auth.db"
    db_url = f"sqlite+aiosqlite:///{db_path.as_posix()}"

    await db_module.init_db(db_url)

    try:
        assert db_module.is_db_initialized() is True
        assert db_path.exists()

        with sqlite3.connect(db_path) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }

        assert "platform_bootstrap_state" in tables
    finally:
        await db_module.close_db()

    assert db_module.is_db_initialized() is False


@pytest.mark.asyncio
async def test_get_db_requires_init():
    db_module = import_real_api_module("lightrag.api.db")
    db_generator = db_module.get_db()

    with pytest.raises(RuntimeError, match="Database session factory is not initialized"):
        await db_generator.__anext__()
