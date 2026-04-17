"""Unit tests for the LDAP auth provider's pure-python helpers.

These don't talk to an LDAP server — that would need a fixture most
test runs can't provide. We cover the bits most likely to regress:

- env-var parsing into ``LDAPSettings``
- RFC 4515 filter-escape fallback when ``ldap3`` isn't installed
- role-precedence resolution across multiple mapped groups

The actual bind / search codepath is exercised by integration harnesses
in environments that do have an LDAP server wired up.
"""

from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.offline


def _fresh_ldap_module(monkeypatch):
    """Re-import ``ldap_provider`` with a canned ``config.global_args``.

    Touching ``lightrag.api.auth`` at module level evaluates
    ``AuthHandler()`` which reads ``config.global_args`` which triggers
    argparse. Under pytest argparse sees the pytest CLI and aborts.
    Pattern matches ``tests/test_permissions.py::import_real_api_module``.
    """
    # Wipe any cached modules so the monkeypatched global_args is the
    # one the freshly-imported auth.py sees on first attribute access.
    for name in (
        "lightrag.api.config",
        "lightrag.api.auth",
        "lightrag.api.auth_provider",
        "lightrag.api.auth_providers",
        "lightrag.api.auth_providers.ldap_provider",
    ):
        sys.modules.pop(name, None)

    config = importlib.import_module("lightrag.api.config")
    monkeypatch.setattr(
        config,
        "global_args",
        SimpleNamespace(
            token_secret="test-jwt-secret",
            jwt_algorithm="HS256",
            token_expire_hours=48,
            guest_token_expire_hours=24,
            refresh_token_expire_hours=168,
            auth_accounts="admin:admin_pass",
            use_db_auth=False,
            whitelist_paths="/health,/api/*",
            token_auto_renew=True,
            token_renew_threshold=0.5,
            max_upload_size=None,
        ),
    )

    return importlib.import_module("lightrag.api.auth_providers.ldap_provider")


@pytest.fixture
def ldap_env(monkeypatch):
    """Minimum viable LDAP_* env set for LDAPSettings.from_env() to parse."""
    monkeypatch.setenv("LDAP_SERVER_URL", "ldaps://ldap.example.com:636")
    monkeypatch.setenv("LDAP_BIND_DN", "cn=svc,dc=example,dc=com")
    monkeypatch.setenv("LDAP_BIND_PASSWORD", "svc-pw")
    monkeypatch.setenv("LDAP_USER_SEARCH_BASE", "ou=people,dc=example,dc=com")
    # Clear any optional vars that might leak from the parent shell.
    for opt in (
        "LDAP_USER_FILTER",
        "LDAP_USER_ATTR_USERNAME",
        "LDAP_GROUP_SEARCH_BASE",
        "LDAP_GROUP_FILTER",
        "LDAP_GROUP_ATTR",
        "LDAP_ROLE_MAPPING",
        "LDAP_DEFAULT_ROLE",
        "LDAP_USE_START_TLS",
    ):
        monkeypatch.delenv(opt, raising=False)


def test_settings_defaults(ldap_env, monkeypatch):
    mod = _fresh_ldap_module(monkeypatch)
    settings = mod.LDAPSettings.from_env()

    assert settings.server_url == "ldaps://ldap.example.com:636"
    assert settings.user_filter == "(&(objectClass=inetOrgPerson)(uid={username}))"
    assert settings.username_attr == "uid"
    assert settings.group_search_base is None
    assert settings.group_filter is None
    assert settings.role_mapping == {}
    assert settings.default_role == "viewer"
    assert settings.use_start_tls is False


def test_settings_missing_required_raises(monkeypatch):
    mod = _fresh_ldap_module(monkeypatch)

    monkeypatch.setenv("LDAP_SERVER_URL", "ldap://example.com")
    # LDAP_BIND_DN deliberately absent — parser must refuse to construct
    # a provider with a hole in its required set (otherwise the bind
    # later fails with a less legible runtime error).
    monkeypatch.delenv("LDAP_BIND_DN", raising=False)
    monkeypatch.setenv("LDAP_BIND_PASSWORD", "pw")
    monkeypatch.setenv("LDAP_USER_SEARCH_BASE", "ou=people,dc=example,dc=com")

    with pytest.raises(RuntimeError, match="LDAP_BIND_DN"):
        mod.LDAPSettings.from_env()


def test_role_mapping_parses_and_normalises_case(ldap_env, monkeypatch):
    mod = _fresh_ldap_module(monkeypatch)

    monkeypatch.setenv(
        "LDAP_ROLE_MAPPING",
        "Lightrag-Admins:Admin , lightrag-editors:editor,badentry, :orphan",
    )
    settings = mod.LDAPSettings.from_env()

    # Keys + values both lowercased; malformed entries silently ignored.
    assert settings.role_mapping == {
        "lightrag-admins": "admin",
        "lightrag-editors": "editor",
    }


def test_pick_best_role_prefers_higher_privilege(monkeypatch):
    mod = _fresh_ldap_module(monkeypatch)

    # Admin wins over editor/viewer regardless of list order — the
    # no_access sentinel elsewhere means we want to grant the greatest
    # role the user actually qualifies for, not the alphabetically first.
    assert mod._pick_best_role(["viewer", "editor", "admin"]) == "admin"
    assert mod._pick_best_role(["viewer", "editor"]) == "editor"
    assert mod._pick_best_role(["viewer"]) == "viewer"


def test_pick_best_role_unknown_falls_through(monkeypatch):
    mod = _fresh_ldap_module(monkeypatch)

    # Custom role names outside the precedence list get returned as-is
    # (first element). Lets deployments introduce a bespoke role without
    # silently downgrading the user to "viewer".
    assert mod._pick_best_role(["mystery-role"]) == "mystery-role"
    assert mod._pick_best_role([]) == "viewer"


def test_escape_ldap_filter_blocks_injection(monkeypatch):
    mod = _fresh_ldap_module(monkeypatch)

    # Classic filter-injection payload: if any of these characters slip
    # into `(uid={username})` unescaped, an attacker can break out of
    # the template and re-open the search with `(objectClass=*)`.
    raw = "alice*)(uid=admin"
    escaped = mod._escape_ldap_filter(raw)

    assert "*" not in escaped
    assert "(" not in escaped
    assert ")" not in escaped
    # RFC 4515 requires hex escapes — either ldap3's helper or our
    # fallback must produce one. We check for the sentinel escape on
    # any of the dangerous characters.
    assert any(seq in escaped for seq in (r"\2a", r"\28", r"\29"))
