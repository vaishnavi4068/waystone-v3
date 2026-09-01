"""Seed a local dev workspace: one member + a shared strategy.

Reads WAYSTONE_DB and WAYSTONE_ADMIN_TOKEN from the environment (set by install.sh),
registers a "Demo" member, sets a default momentum strategy, and writes the member's
bearer token to .devdata/token.txt so it can be pasted into the dashboard login.

Offline-safe: the default StubDataSource and in-process PaperBroker need no credentials.
"""

from __future__ import annotations

import os
from pathlib import Path

from waystone3.workspace.runtime import build_service_from_env

DEV_MEMBER = "Demo"


def main() -> None:
    db = os.environ["WAYSTONE_DB"]
    service = build_service_from_env()

    member = service.register(service.admin_token, DEV_MEMBER)
    token = member["token"]

    service.set_strategy(
        token,
        weights={"ma_crossover": 0.5, "price_action": 0.3, "volume": 0.2},
        watchlist=["AAPL", "MSFT", "NVDA"],
    )

    token_path = Path(db).parent / "token.txt"
    token_path.write_text(token + "\n")
    print(f"Seeded member {DEV_MEMBER!r}; token written to {token_path}")


if __name__ == "__main__":
    main()
