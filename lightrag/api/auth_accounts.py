"""
Helpers for parsing legacy AUTH_ACCOUNTS entries during the staged auth migration.
"""

from __future__ import annotations


def parse_auth_accounts(auth_accounts: str | None) -> dict[str, str]:
    """Parse comma-separated ``username:password`` pairs into a mapping."""
    entries = {}
    raw_value = (auth_accounts or "").strip()
    if not raw_value:
        return entries

    invalid_accounts: list[str] = []
    for account in raw_value.split(","):
        account = account.strip()
        if not account:
            invalid_accounts.append("<empty>")
            continue
        try:
            username, password = account.split(":", 1)
            if not username or not password:
                raise ValueError
            entries[username] = password
        except ValueError:
            invalid_accounts.append(account)

    if invalid_accounts:
        invalid_entries = ", ".join(invalid_accounts)
        raise ValueError(
            f"AUTH_ACCOUNTS must use comma-separated user:password pairs. Invalid entries: {invalid_entries}"
        )

    return entries
