"""Entrypoint: initialize DB (+seed if empty) and start the Telegram bot."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kirana.db.database import get_conn, init_db  # noqa: E402


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # Fail loudly at startup, not on the owner's first message.
    from kirana.config import check_startup_config
    check_startup_config()

    init_db()
    if get_conn().execute("SELECT COUNT(*) c FROM products").fetchone()["c"] == 0:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from scripts.seed import seed
        seed()

    # Only binds if the host sets $PORT (free web-service tiers do).
    from kirana.health import start_health_server
    start_health_server()

    from kirana.telegram.bot import KiranaBot
    KiranaBot().run()


if __name__ == "__main__":
    main()
