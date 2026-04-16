import importlib
import hashlib
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import bcrypt
import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
from starlette.requests import Request

from lightrag.api.passwords import BCRYPT_PASSWORD_PREFIX, hash_password, verify_password
from lightrag.tools.hash_password import main as hash_password_main
from lightrag.utils import logger as lightrag_logger


def import_real_api_module(module_name: str):
    sys.modules.pop(module_name, None)

    package_name, _, child_name = module_name.rpartition(".")
    package = sys.modules.get(package_name)
    if package is not None and hasattr(package, child_name):
        delattr(package, child_name)

    return importlib.import_module(module_name)


@pytest.fixture(autouse=True)
def clear_auth_related_modules():
    module_names = (
        "lightrag.api.config",
        "lightrag.api.auth_accounts",
        "lightrag.api.auth",
        "lightrag.api.auth_provider",
        "lightrag.api.db",
        "lightrag.api.dependencies",
        "lightrag.api.identity_store",
        "lightrag.api.models",
        "lightrag.api.models.user",
        "lightrag.api.models.membership",
        "lightrag.api.routers.auth_routes",
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
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture
def auth_module(monkeypatch):
    config = import_real_api_module("lightrag.api.config")
    mock_global_args = build_global_args()

    monkeypatch.setattr(config, "global_args", mock_global_args)

    module = import_real_api_module("lightrag.api.auth")
    module = importlib.reload(module)
    yield module
    sys.modules.pop("lightrag.api.auth", None)


def build_bcrypt_value(password: str) -> str:
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    return f"{BCRYPT_PASSWORD_PREFIX}{hashed}"


def build_request(
    app: FastAPI,
    *,
    path: str,
    method: str = "POST",
    scheme: str = "http",
    cookies: dict[str, str] | None = None,
) -> Request:
    header_items = []
    if cookies:
        cookie_header = "; ".join(f"{name}={value}" for name, value in cookies.items())
        header_items.append((b"cookie", cookie_header.encode("latin-1")))

    scope = {
        "type": "http",
        "app": app,
        "method": method,
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": b"",
        "headers": header_items,
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
            enable_kb_isolation=False,
        ),
    )
    app.state.db_ready = db_ready
    return app


def fetch_refresh_rows(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, user_id, token_jti, token_hash, issued_at, expires_at,
                   revoked_at, rotated_from_token_id
            FROM refresh_tokens
            ORDER BY created_at, id
            """
        ).fetchall()
    return [dict(row) for row in rows]


async def setup_db_auth_environment(monkeypatch, tmp_path, *, auth_accounts="admin:admin_pass"):
    config = import_real_api_module("lightrag.api.config")
    monkeypatch.setattr(config, "global_args", build_global_args(auth_accounts=auth_accounts))

    auth = import_real_api_module("lightrag.api.auth")
    auth = importlib.reload(auth)
    db = import_real_api_module("lightrag.api.db")
    identity_store = import_real_api_module("lightrag.api.identity_store")
    dependencies = import_real_api_module("lightrag.api.dependencies")
    auth_routes = import_real_api_module("lightrag.api.routers.auth_routes")

    db_path = tmp_path / "auth_refresh.db"
    db_url = f"sqlite+aiosqlite:///{db_path.as_posix()}"

    await db.init_db(db_url)
    await identity_store.bootstrap_identity_store(auth_accounts)

    return auth, db, identity_store, dependencies, auth_routes, db_path


def test_verify_plaintext_password(auth_module):
    handler = auth_module.AuthHandler()
    handler.accounts = {"admin": "admin_pass"}

    assert handler.verify_password("admin", "admin_pass")
    assert not handler.verify_password("admin", "wrong_pass")


def test_verify_prefixed_bcrypt_password(auth_module):
    handler = auth_module.AuthHandler()
    handler.accounts = {"user": build_bcrypt_value("user_pass")}

    assert handler.verify_password("user", "user_pass")
    assert not handler.verify_password("user", "wrong_pass")


def test_plaintext_password_with_bcrypt_prefix_stays_plaintext(auth_module):
    handler = auth_module.AuthHandler()
    handler.accounts = {"user": "$2b$not-a-real-hash"}

    assert handler.verify_password("user", "$2b$not-a-real-hash")
    assert not handler.verify_password("user", "anything-else")


def test_invalid_auth_accounts_raises(monkeypatch):
    config = import_real_api_module("lightrag.api.config")
    mock_global_args = build_global_args(auth_accounts="admin")

    monkeypatch.setattr(config, "global_args", mock_global_args)

    with pytest.raises(ValueError, match="AUTH_ACCOUNTS must use"):
        import_real_api_module("lightrag.api.auth")

    sys.modules.pop("lightrag.api.auth", None)


def test_trailing_comma_in_auth_accounts_raises():
    parser_module = import_real_api_module("lightrag.api.auth_accounts")

    with pytest.raises(ValueError, match="AUTH_ACCOUNTS must use"):
        parser_module.parse_auth_accounts("admin:secret,")


def test_initialize_config_rejects_default_token_secret_with_auth_accounts():
    config = import_real_api_module("lightrag.api.config")

    insecure_args = SimpleNamespace(
        auth_accounts="admin:admin_pass",
        token_secret=config.DEFAULT_TOKEN_SECRET,
    )

    with pytest.raises(ValueError, match="TOKEN_SECRET must be explicitly set"):
        config.initialize_config(insecure_args, force=True)


def test_initialize_config_allows_custom_token_secret_with_auth_accounts():
    config = import_real_api_module("lightrag.api.config")

    secure_args = SimpleNamespace(
        auth_accounts="admin:admin_pass",
        token_secret="custom-jwt-secret",
    )

    initialized = config.initialize_config(secure_args, force=True)

    assert initialized is secure_args


def test_guest_tokens_fall_back_to_default_secret_when_token_secret_missing(
    monkeypatch,
):
    config = import_real_api_module("lightrag.api.config")
    mock_global_args = build_global_args(
        token_secret=None,
        auth_accounts="",
    )

    monkeypatch.setattr(config, "global_args", mock_global_args)
    warning_messages = []

    def capture_warning(message):
        warning_messages.append(message)

    monkeypatch.setattr(lightrag_logger, "warning", capture_warning)

    module = import_real_api_module("lightrag.api.auth")
    module = importlib.reload(module)
    handler = module.AuthHandler()

    token = handler.create_token("guest", role="guest")
    token_info = handler.validate_token(token)

    assert handler.secret == config.DEFAULT_TOKEN_SECRET
    assert token_info["username"] == "guest"
    assert token_info["role"] == "guest"
    assert any(
        "Falling back to the default development JWT secret" in msg
        for msg in warning_messages
    )

    sys.modules.pop("lightrag.api.auth", None)


def test_hash_password_returns_prefixed_value(auth_module):
    hashed = hash_password("new_password")

    assert hashed.startswith(BCRYPT_PASSWORD_PREFIX)
    raw_hash = hashed[len(BCRYPT_PASSWORD_PREFIX) :]
    assert bcrypt.checkpw("new_password".encode("utf-8"), raw_hash.encode("utf-8"))


def test_hash_password_cli_outputs_auth_accounts_entry(capsys):
    exit_code = hash_password_main(["--username", "admin", "secret"])

    assert exit_code == 0
    output = capsys.readouterr().out.strip()
    username, hashed = output.split(":", 1)
    assert username == "admin"
    assert hashed.startswith(BCRYPT_PASSWORD_PREFIX)
    raw_hash = hashed[len(BCRYPT_PASSWORD_PREFIX) :]
    assert bcrypt.checkpw("secret".encode("utf-8"), raw_hash.encode("utf-8"))


def test_validate_token_v1_shape_accepted(auth_module):
    handler = auth_module.AuthHandler()
    legacy_exp = datetime.now(timezone.utc) + timedelta(hours=1)
    legacy_token = auth_module.jwt.encode(
        {
            "sub": "legacy-user",
            "exp": legacy_exp,
            "role": "user",
            "metadata": {"legacy": True},
        },
        handler.secret,
        algorithm=handler.algorithm,
    )

    token_info = handler.validate_token(legacy_token)

    assert token_info["username"] == "legacy-user"
    assert token_info["user_id"] is None
    assert token_info["role"] == "user"
    assert token_info["memberships"] == []
    assert token_info["jti"] == ""
    assert token_info["metadata"] == {"legacy": True}


def test_validate_token_v2_shape_returns_memberships(auth_module):
    handler = auth_module.AuthHandler()
    token = handler.create_token(
        "admin",
        role="user",
        metadata={"auth_mode": "enabled"},
        memberships=[
            {
                "workspace_id": "ws1",
                "kb_id": None,
                "role": "admin",
            }
        ],
        jti="session-123",
        user_id="user-123",
    )

    token_info = handler.validate_token(token)

    assert token_info["username"] == "admin"
    assert token_info["user_id"] == "user-123"
    assert token_info["jti"] == "session-123"
    assert token_info["memberships"] == [
        {
            "workspace_id": "ws1",
            "kb_id": None,
            "role": "admin",
        }
    ]


@pytest.mark.asyncio
async def test_login_env_only_no_refresh_token(monkeypatch):
    config = import_real_api_module("lightrag.api.config")
    monkeypatch.setattr(config, "global_args", build_global_args())
    import_real_api_module("lightrag.api.auth")
    dependencies = import_real_api_module("lightrag.api.dependencies")
    auth_routes = import_real_api_module("lightrag.api.routers.auth_routes")

    app = make_platform_app(dependencies, use_db_auth=False, db_ready=False)
    request = build_request(app, path="/login")
    response = Response()

    payload = await auth_routes.issue_login_tokens(
        request,
        response,
        username="admin",
        metadata={"auth_mode": "enabled"},
    )

    assert "refresh_token" not in payload
    assert payload["token_type"] == "bearer"
    assert "set-cookie" not in response.headers


@pytest.mark.asyncio
async def test_login_db_mode_returns_refresh_token_and_db_row(monkeypatch, tmp_path):
    auth, db, _identity_store, dependencies, auth_routes, db_path = await setup_db_auth_environment(
        monkeypatch, tmp_path
    )

    try:
        app = make_platform_app(dependencies, use_db_auth=True, db_ready=True)
        request = build_request(app, path="/login")
        response = Response()

        payload = await auth_routes.issue_login_tokens(
            request,
            response,
            username="admin",
            metadata={"auth_mode": "enabled"},
        )

        rows = fetch_refresh_rows(db_path)
        assert payload["access_token"]
        assert payload["refresh_token"]
        assert len(rows) == 1
        assert rows[0]["token_hash"] == hashlib.sha256(
            payload["refresh_token"].encode("utf-8")
        ).hexdigest()
        assert rows[0]["revoked_at"] is None
        assert "lightrag_refresh_token=" in response.headers.get("set-cookie", "")

        token_info = auth.auth_handler.validate_token(payload["access_token"])
        assert token_info["username"] == "admin"
        assert token_info["jti"] == rows[0]["token_jti"]
    finally:
        await db.close_db()


@pytest.mark.asyncio
async def test_refresh_rotates_token_pair(monkeypatch, tmp_path):
    _auth, db, _identity_store, dependencies, auth_routes, db_path = await setup_db_auth_environment(
        monkeypatch, tmp_path
    )

    try:
        app = make_platform_app(dependencies, use_db_auth=True, db_ready=True)
        app.include_router(auth_routes.create_auth_routes())

        initial_response = Response()
        initial_payload = await auth_routes.issue_login_tokens(
            build_request(app, path="/login"),
            initial_response,
            username="admin",
            metadata={"auth_mode": "enabled"},
        )

        client = TestClient(app)
        refresh_response = client.post(
            "/auth/refresh",
            json={"refresh_token": initial_payload["refresh_token"]},
        )

        assert refresh_response.status_code == 200
        body = refresh_response.json()
        rows = fetch_refresh_rows(db_path)
        initial_hash = hashlib.sha256(
            initial_payload["refresh_token"].encode("utf-8")
        ).hexdigest()
        old_row = next(row for row in rows if row["token_hash"] == initial_hash)
        new_row = next(row for row in rows if row["token_hash"] != initial_hash)

        assert body["access_token"]
        assert body["refresh_token"] != initial_payload["refresh_token"]
        assert len(rows) == 2
        assert old_row["revoked_at"] is not None
        assert new_row["rotated_from_token_id"] == old_row["id"]
    finally:
        await db.close_db()


@pytest.mark.asyncio
async def test_refresh_reuse_returns_401(monkeypatch, tmp_path):
    _auth, db, _identity_store, dependencies, auth_routes, _db_path = await setup_db_auth_environment(
        monkeypatch, tmp_path
    )

    try:
        app = make_platform_app(dependencies, use_db_auth=True, db_ready=True)
        app.include_router(auth_routes.create_auth_routes())

        initial_payload = await auth_routes.issue_login_tokens(
            build_request(app, path="/login"),
            Response(),
            username="admin",
            metadata={"auth_mode": "enabled"},
        )

        client = TestClient(app)
        first_refresh = client.post(
            "/auth/refresh",
            json={"refresh_token": initial_payload["refresh_token"]},
        )
        second_refresh = client.post(
            "/auth/refresh",
            json={"refresh_token": initial_payload["refresh_token"]},
        )

        assert first_refresh.status_code == 200
        assert second_refresh.status_code == 401
        assert second_refresh.json()["detail"] == "Refresh token has been revoked"
    finally:
        await db.close_db()


def test_refresh_endpoint_requires_db_mode(monkeypatch):
    config = import_real_api_module("lightrag.api.config")
    monkeypatch.setattr(config, "global_args", build_global_args())
    dependencies = import_real_api_module("lightrag.api.dependencies")
    auth_routes = import_real_api_module("lightrag.api.routers.auth_routes")

    app = make_platform_app(dependencies, use_db_auth=False, db_ready=False)
    app.include_router(auth_routes.create_auth_routes())

    client = TestClient(app)
    response = client.post("/auth/refresh", json={"refresh_token": "anything"})

    assert response.status_code == 501
    assert response.json()["detail"] == "Refresh tokens require USE_DB_AUTH=true"


@pytest.mark.asyncio
async def test_auth_me_change_password_and_logout(monkeypatch, tmp_path):
    _auth, db, identity_store, dependencies, auth_routes, _db_path = await setup_db_auth_environment(
        monkeypatch, tmp_path
    )

    try:
        app = make_platform_app(dependencies, use_db_auth=True, db_ready=True)
        app.include_router(auth_routes.create_auth_routes())

        login_response = Response()
        login_payload = await auth_routes.issue_login_tokens(
            build_request(app, path="/login"),
            login_response,
            username="admin",
            role="user",
            metadata={"auth_mode": "enabled", "account_source": "env"},
        )
        cookie_header = login_response.headers.get("set-cookie", "")
        refresh_cookie = cookie_header.split(";", 1)[0].split("=", 1)[1]

        client = TestClient(app)
        headers = {"Authorization": f"Bearer {login_payload['access_token']}"}

        me_response = client.get("/auth/me", headers=headers)
        assert me_response.status_code == 200
        assert me_response.json()["username"] == "admin"
        assert me_response.json()["provider"] == "local"

        change_response = client.post(
            "/auth/change-password",
            headers=headers,
            json={
                "current_password": "admin_pass",
                "new_password": "new_admin_pass",
            },
        )
        assert change_response.status_code == 200
        assert change_response.json()["status"] == "success"

        updated_user = await identity_store.get_user_auth_by_username("admin")
        assert updated_user is not None
        assert verify_password("new_admin_pass", updated_user.password_secret)
        assert not verify_password("admin_pass", updated_user.password_secret)

        logout_response = client.post(
            "/auth/logout",
            headers=headers,
            cookies={auth_routes.REFRESH_TOKEN_COOKIE_NAME: refresh_cookie},
        )
        assert logout_response.status_code == 200
        assert logout_response.json()["status"] == "success"
    finally:
        await db.close_db()


@pytest.mark.asyncio
async def test_local_user_directory_management_endpoints(monkeypatch, tmp_path):
    _auth, db, identity_store, dependencies, auth_routes, _db_path = await setup_db_auth_environment(
        monkeypatch, tmp_path
    )

    try:
        app = make_platform_app(dependencies, use_db_auth=True, db_ready=True)
        app.include_router(auth_routes.create_auth_routes())

        login_payload = await auth_routes.issue_login_tokens(
            build_request(app, path="/login"),
            Response(),
            username="admin",
            role="user",
            metadata={"auth_mode": "enabled", "account_source": "env"},
        )
        headers = {"Authorization": f"Bearer {login_payload['access_token']}"}
        client = TestClient(app)

        create_response = client.post(
            "/auth/users",
            headers=headers,
            json={
                "username": "alice",
                "password": "alice_password",
                "is_active": True,
            },
        )
        assert create_response.status_code == 201
        created_user_id = create_response.json()["user"]["user_id"]

        list_response = client.get("/auth/users", headers=headers)
        assert list_response.status_code == 200
        usernames = [item["username"] for item in list_response.json()["items"]]
        assert "admin" in usernames
        assert "alice" in usernames

        update_response = client.patch(
            f"/auth/users/{created_user_id}",
            headers=headers,
            json={"username": "alice.renamed", "is_active": False},
        )
        assert update_response.status_code == 200
        assert update_response.json()["user"]["username"] == "alice.renamed"
        assert update_response.json()["user"]["is_active"] is False

        password_response = client.post(
            f"/auth/users/{created_user_id}/password",
            headers=headers,
            json={"password": "alice_password_next"},
        )
        assert password_response.status_code == 200

        updated_user = await identity_store.get_user_auth_by_username("alice.renamed")
        assert updated_user is not None
        assert verify_password("alice_password_next", updated_user.password_secret)
    finally:
        await db.close_db()
