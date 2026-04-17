"""
End-to-end smoke test for the three workspace-isolation guarantees.

Each guarantee maps to one assertion block below:

    1. Each user can create multiple workspaces.
    2. Each workspace can host multiple knowledge bases.
    3. A user cannot query knowledge bases that live in another user's
       workspace — either by enumeration (``GET /workspaces/{foreign}/kb``)
       or by hijacking the ``X-Workspace-Id`` header on ``POST /query``.

Run against a live server. The script registers two throwaway accounts,
creates 2 workspaces × 2 KBs per user, and verifies isolation. It exits
non-zero if any guarantee is violated — wire it into CI or run it
manually after deployment changes that touch auth / permissions /
federation.

Requires:
- ``LIGHTRAG_ALLOW_SELF_REGISTRATION=true`` on the target server.
- ``ENABLE_KB_ISOLATION=true`` so KB CRUD is exposed.

Usage:
    uv run python scripts/verify_workspace_isolation.py
    uv run python scripts/verify_workspace_isolation.py --base-url https://stage.example.com
"""

from __future__ import annotations

import argparse
import secrets
import sys
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_BASE_URL = "http://localhost:9621"


class VerificationError(RuntimeError):
    """Raised when an isolation guarantee fails."""


@dataclass
class Actor:
    username: str
    password: str
    access_token: str
    personal_workspace_id: str

    @property
    def auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}


def _log(message: str) -> None:
    print(f"[verify] {message}")


def _unique_suffix() -> str:
    # 8-hex-char suffix is plenty to avoid collisions across concurrent runs
    # and is short enough to read in server logs when debugging a failure.
    return secrets.token_hex(4)


def register(client: httpx.Client, username: str, password: str) -> Actor:
    response = client.post(
        "/auth/register", json={"username": username, "password": password}
    )
    if response.status_code != 200:
        raise VerificationError(
            f"register({username}) expected 200, got "
            f"{response.status_code}: {response.text}"
        )
    payload = response.json()
    token = payload.get("access_token")
    personal_ws = payload.get("personal_workspace_id")
    if not token or not personal_ws:
        raise VerificationError(
            f"register({username}) response missing access_token / "
            f"personal_workspace_id: {payload}"
        )
    return Actor(
        username=username,
        password=password,
        access_token=token,
        personal_workspace_id=personal_ws,
    )


def create_workspace(client: httpx.Client, actor: Actor, name: str) -> str:
    response = client.post(
        "/workspaces",
        json={"name": name},
        headers=actor.auth_header,
    )
    if response.status_code != 201:
        raise VerificationError(
            f"{actor.username}: POST /workspaces -> {response.status_code}: "
            f"{response.text}"
        )
    return response.json()["id"]


def create_kb(
    client: httpx.Client,
    actor: Actor,
    workspace_id: str,
    name: str,
    *,
    category: str = "",
) -> str:
    response = client.post(
        f"/workspaces/{workspace_id}/kb",
        json={"name": name, "category": category},
        headers={**actor.auth_header, "X-Workspace-Id": workspace_id},
    )
    if response.status_code != 200:
        raise VerificationError(
            f"{actor.username}: POST /workspaces/{workspace_id}/kb -> "
            f"{response.status_code}: {response.text}"
        )
    return response.json()["kb"]["id"]


def list_kbs(
    client: httpx.Client, actor: Actor, workspace_id: str
) -> list[dict[str, Any]]:
    response = client.get(
        f"/workspaces/{workspace_id}/kb",
        headers={**actor.auth_header, "X-Workspace-Id": workspace_id},
    )
    if response.status_code != 200:
        raise VerificationError(
            f"{actor.username}: GET /workspaces/{workspace_id}/kb -> "
            f"{response.status_code}: {response.text}"
        )
    return response.json().get("items", [])


def list_kbs_status(
    client: httpx.Client, actor: Actor, workspace_id: str
) -> int:
    """Like list_kbs but returns only the HTTP status code (for negative tests)."""
    response = client.get(
        f"/workspaces/{workspace_id}/kb",
        headers={**actor.auth_header, "X-Workspace-Id": workspace_id},
    )
    return response.status_code


def post_query_status(
    client: httpx.Client,
    actor: Actor,
    workspace_id: str,
) -> int:
    """Try to run a query against ``workspace_id`` with the actor's token.

    Returns the HTTP status code. Negative tests assert 403.
    """
    response = client.post(
        "/query",
        json={"query": "what is rag?", "mode": "mix"},
        headers={**actor.auth_header, "X-Workspace-Id": workspace_id},
    )
    return response.status_code


def verify(base_url: str) -> int:
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        # Preflight: server reachable?
        try:
            status = client.get("/auth-status")
            status.raise_for_status()
        except httpx.HTTPError as exc:
            _log(f"FAIL: server at {base_url} not reachable ({exc})")
            return 2

        auth_status = status.json()
        if not auth_status.get("supports_self_registration"):
            _log(
                "FAIL: target server does not expose self-registration. "
                "Set LIGHTRAG_ALLOW_SELF_REGISTRATION=true and retry."
            )
            return 2

        # --- register two throwaway accounts ---
        suffix = _unique_suffix()
        alice_name = f"verify_alice_{suffix}"
        bob_name = f"verify_bob_{suffix}"
        alice = register(client, alice_name, "alice_pw_00000000")
        bob = register(client, bob_name, "bob_pw_00000000")
        _log(
            f"registered: {alice.username} (ws={alice.personal_workspace_id[:8]}…), "
            f"{bob.username} (ws={bob.personal_workspace_id[:8]}…)"
        )

        # ========================================================
        # Guarantee 1: each user can create multiple workspaces
        # ========================================================
        alice_ws_extra_a = create_workspace(client, alice, "alice-research")
        alice_ws_extra_b = create_workspace(client, alice, "alice-product")
        bob_ws_extra = create_workspace(client, bob, "bob-private")

        # Alice should now see 3 workspaces: her personal one + the two she
        # just created. Bob's one-extra is not in her list.
        list_response = client.get("/workspaces", headers=alice.auth_header)
        if list_response.status_code != 200:
            raise VerificationError(
                f"GET /workspaces (alice) -> {list_response.status_code}"
            )
        alice_ws_ids = {w["id"] for w in list_response.json()["items"]}
        expected_alice = {
            alice.personal_workspace_id,
            alice_ws_extra_a,
            alice_ws_extra_b,
        }
        if not expected_alice.issubset(alice_ws_ids):
            raise VerificationError(
                f"alice missing workspaces: expected superset of "
                f"{expected_alice}, got {alice_ws_ids}"
            )
        if bob_ws_extra in alice_ws_ids:
            raise VerificationError(
                "alice's workspace list leaked bob's workspace id"
            )
        _log(f"PASS [1/3] alice owns {len(alice_ws_ids)} workspaces, bob's hidden")

        # ========================================================
        # Guarantee 2: each workspace can host multiple KBs
        # ========================================================
        alice_kb_a1 = create_kb(client, alice, alice_ws_extra_a, "papers")
        alice_kb_a2 = create_kb(client, alice, alice_ws_extra_a, "notes")
        alice_kb_b1 = create_kb(client, alice, alice_ws_extra_b, "specs")
        alice_kb_b2 = create_kb(client, alice, alice_ws_extra_b, "meetings")
        bob_kb = create_kb(client, bob, bob_ws_extra, "bob-docs")

        alice_kbs_in_a = {kb["id"] for kb in list_kbs(client, alice, alice_ws_extra_a)}
        if {alice_kb_a1, alice_kb_a2}.difference(alice_kbs_in_a):
            raise VerificationError(
                f"alice-research missing KBs: expected "
                f"{{{alice_kb_a1}, {alice_kb_a2}}}, got {alice_kbs_in_a}"
            )
        if alice_kb_b1 in alice_kbs_in_a or alice_kb_b2 in alice_kbs_in_a:
            raise VerificationError(
                "alice-research leaked KBs from alice-product (same-user "
                "workspace-to-workspace bleed)"
            )
        _log(
            f"PASS [2/3] alice-research lists {len(alice_kbs_in_a)} KBs, "
            f"no bleed from alice-product"
        )

        # ========================================================
        # Guarantee 3: cross-user isolation
        # ========================================================

        # 3a: bob lists kbs in alice-research via header hijack → 403
        hijack_status = list_kbs_status(client, bob, alice_ws_extra_a)
        if hijack_status != 403:
            raise VerificationError(
                f"bob reading alice's KB list should 403, got {hijack_status}"
            )

        # 3b: bob runs a query scoped to alice's workspace → 403
        query_status = post_query_status(client, bob, alice_ws_extra_a)
        if query_status != 403:
            raise VerificationError(
                f"bob querying alice's workspace should 403, got {query_status}"
            )

        # 3c: alice's own list does NOT include bob's KB (ensures the
        # federation fanout stays workspace-local)
        alice_kbs_all = [
            kb["id"]
            for ws in alice_ws_ids
            for kb in list_kbs(client, alice, ws)
        ]
        if bob_kb in alice_kbs_all:
            raise VerificationError(
                f"alice's cross-workspace KB list leaked bob's KB {bob_kb}"
            )

        _log(
            "PASS [3/3] bob 403's on alice's workspace list + query, "
            "alice never sees bob's KBs"
        )

    _log("All three isolation guarantees hold.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify per-user workspace and KB isolation against a running server."
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Server to test (default: {DEFAULT_BASE_URL})",
    )
    args = parser.parse_args()

    try:
        return verify(args.base_url)
    except VerificationError as exc:
        _log(f"FAIL: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
