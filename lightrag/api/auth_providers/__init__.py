"""Pluggable auth providers.

Ships one concrete remote example (LDAP). To add another — OIDC, SAML,
a custom IdP gateway — subclass ``AuthProvider`` from
``lightrag.api.auth_provider`` and register it in
``lightrag.api.dependencies.install_platform_state`` behind the same
``LIGHTRAG_AUTH_PROVIDER`` env switch.
"""

from __future__ import annotations

from lightrag.api.auth_providers.ldap_provider import LDAPAuthProvider

__all__ = ["LDAPAuthProvider"]
