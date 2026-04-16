import importlib
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
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
def clear_membership_modules():
    module_names = (
        "lightrag.api.config",
        "lightrag.api.auth_accounts",
        "lightrag.api.auth",
        "lightrag.api.db",
        "lightrag.api.dependencies",
        "lightrag.api.identity_store",
        "lightrag.api.permissions",
        "lightrag.api.routers",
        "lightrag.api.routers.auth_routes",
        "lightrag.api.routers.membership_routes",
        "lightrag.api.models",
        "lightrag.api.models.user",
        "lightrag.api.models.membership",
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
        "auth_accounts": "owner:owner_pass,alice:alice_pass,bob:bob_pass",
        "whitelist_paths": "/health,/api/*",
        "token_auto_renew": True,
        "token_renew_threshold": 0.5,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def build_request(
    app: FastAPI,
    *,
    path: str,
    method: str = "POST",
    scheme: str = "http",
) -> Request:
    scope = {
        "type": "http",
        "app": app,
        "method": method,
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "scheme": scheme,
        "http_version": "1.1",
        "path_params": {},
    }
    return Request(scope)


def make_platform_app(dependencies_module, *, use_db_auth: bool, db_ready: bool = True):
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
    app.state.db_ready = db_ready
    return app


async def setup_membership_test_env(monkeypatch, tmp_path):
    config = import_real_api_module("lightrag.api.config")
    monkeypatch.setattr(config, "global_args", build_global_args())

    auth = import_real_api_module("lightrag.api.auth")
    auth = importlib.reload(auth)
    db = import_real_api_module("lightrag.api.db")
    identity_store = import_real_api_module("lightrag.api.identity_store")
    dependencies = import_real_api_module("lightrag.api.dependencies")
    auth_routes = import_real_api_module("lightrag.api.routers.auth_routes")
    membership_routes = import_real_api_module("lightrag.api.routers.membership_routes")

    db_path = tmp_path / "membership_auth.db"
    db_url = f"sqlite+aiosqlite:///{db_path.as_posix()}"

    await db.init_db(db_url)
    await identity_store.bootstrap_identity_store(build_global_args().auth_accounts)

    return auth, db, identity_store, dependencies, auth_routes, membership_routes


def build_auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_workspace_owner_can_invite_user_and_login_claims_include_membership(
    monkeypatch, tmp_path
):
    auth, db, _identity_store, dependencies, auth_routes, membership_routes = (
        await setup_membership_test_env(monkeypatch, tmp_path)
    )

    try:
        app = make_platform_app(dependencies, use_db_auth=True, db_ready=True)
        app.include_router(membership_routes.create_membership_routes())
        client = TestClient(app)

        owner_token = auth.auth_handler.create_token(
            "owner",
            role="user",
            metadata={"auth_mode": "enabled"},
        )
        create_response = client.post(
            "/workspaces/default/members",
            headers=build_auth_header(owner_token),
            json={"username": "alice", "role": "viewer"},
        )
        list_response = client.get(
            "/workspaces/default/members",
            headers=build_auth_header(owner_token),
        )

        assert create_response.status_code == 200
        assert create_response.json()["status"] == "created"
        assert create_response.json()["member"]["username"] == "alice"
        assert list_response.status_code == 200
        assert list_response.json()["total_count"] == 1

        alice_login = await auth_routes.issue_login_tokens(
            build_request(app, path="/login"),
            Response(),
            username="alice",
            metadata={"auth_mode": "enabled"},
        )
        token_info = auth.auth_handler.validate_token(alice_login["access_token"])

        assert any(
            claim == {"workspace_id": "default", "kb_id": None, "role": "viewer"}
            for claim in token_info["memberships"]
        )
    finally:
        await db.close_db()


@pytest.mark.asyncio
async def test_kb_membership_requires_workspace_membership_first(monkeypatch, tmp_path):
    auth, db, _identity_store, dependencies, _auth_routes, membership_routes = (
        await setup_membership_test_env(monkeypatch, tmp_path)
    )

    try:
        app = make_platform_app(dependencies, use_db_auth=True, db_ready=True)
        app.include_router(membership_routes.create_membership_routes())
        client = TestClient(app)

        owner_token = auth.auth_handler.create_token(
            "owner",
            role="user",
            metadata={"auth_mode": "enabled"},
        )
        response = client.post(
            "/workspaces/default/kb/default/members",
            headers=build_auth_header(owner_token),
            json={"username": "alice", "role": "editor"},
        )

        assert response.status_code == 400
        assert "must already be a workspace member" in response.json()["detail"]
    finally:
        await db.close_db()


@pytest.mark.asyncio
async def test_owner_can_manage_kb_membership_and_claims_show_on_login(
    monkeypatch, tmp_path
):
    auth, db, _identity_store, dependencies, auth_routes, membership_routes = (
        await setup_membership_test_env(monkeypatch, tmp_path)
    )

    try:
        app = make_platform_app(dependencies, use_db_auth=True, db_ready=True)
        app.include_router(membership_routes.create_membership_routes())
        client = TestClient(app)

        owner_token = auth.auth_handler.create_token(
            "owner",
            role="user",
            metadata={"auth_mode": "enabled"},
        )
        headers = build_auth_header(owner_token)

        workspace_response = client.post(
            "/workspaces/default/members",
            headers=headers,
            json={"username": "alice", "role": "viewer"},
        )
        kb_response = client.post(
            "/workspaces/default/kb/default/members",
            headers=headers,
            json={"username": "alice", "role": "editor"},
        )
        kb_list_response = client.get(
            "/workspaces/default/kb/default/members",
            headers=headers,
        )

        assert workspace_response.status_code == 200
        assert kb_response.status_code == 200
        assert kb_response.json()["member"]["role"] == "editor"
        assert kb_list_response.status_code == 200
        assert kb_list_response.json()["total_count"] == 1

        alice_login = await auth_routes.issue_login_tokens(
            build_request(app, path="/login"),
            Response(),
            username="alice",
            metadata={"auth_mode": "enabled"},
        )
        token_info = auth.auth_handler.validate_token(alice_login["access_token"])

        assert any(
            claim == {"workspace_id": "default", "kb_id": None, "role": "viewer"}
            for claim in token_info["memberships"]
        )
        assert any(
            claim == {"workspace_id": "default", "kb_id": "default", "role": "editor"}
            for claim in token_info["memberships"]
        )
    finally:
        await db.close_db()


@pytest.mark.asyncio
async def test_viewer_cannot_manage_workspace_memberships(monkeypatch, tmp_path):
    auth, db, _identity_store, dependencies, _auth_routes, membership_routes = (
        await setup_membership_test_env(monkeypatch, tmp_path)
    )

    try:
        app = make_platform_app(dependencies, use_db_auth=True, db_ready=True)
        app.include_router(membership_routes.create_membership_routes())
        client = TestClient(app)

        viewer_token = auth.auth_handler.create_token(
            "alice",
            role="user",
            metadata={"auth_mode": "enabled"},
            memberships=[
                {"workspace_id": "default", "kb_id": None, "role": "viewer"},
            ],
        )
        response = client.post(
            "/workspaces/default/members",
            headers=build_auth_header(viewer_token),
            json={"username": "bob", "role": "viewer"},
        )

        assert response.status_code == 403
        assert response.json()["detail"] == (
            "Permission denied for action 'workspace:invite_member'"
        )
    finally:
        await db.close_db()


@pytest.mark.asyncio
async def test_membership_routes_require_db_auth(monkeypatch, tmp_path):
    auth, db, _identity_store, dependencies, _auth_routes, membership_routes = (
        await setup_membership_test_env(monkeypatch, tmp_path)
    )

    try:
        app = make_platform_app(dependencies, use_db_auth=False, db_ready=False)
        app.include_router(membership_routes.create_membership_routes())
        client = TestClient(app)

        owner_token = auth.auth_handler.create_token(
            "owner",
            role="user",
            metadata={"auth_mode": "enabled"},
        )
        response = client.get(
            "/workspaces/default/members",
            headers=build_auth_header(owner_token),
        )

        assert response.status_code == 501
        assert response.json()["detail"] == (
            "Membership management requires USE_DB_AUTH=true"
        )
    finally:
        await db.close_db()
