"""Durable owner memory — survives /new chats and process restarts.

Preferences live in SQLite, *outside* the model's context window. At the start
of every agent session the current preference set is injected into the system
prompt, and the model can update it through tools mid-conversation.

Examples the task calls out: "always assume UPI unless I say cash",
"default atta = Aashirvaad 5kg", shop name / GSTIN printed on invoices.
"""

from __future__ import annotations

from ..db.database import get_conn, transaction

KNOWN_KEYS_HINT = (
    "Common keys: default_payment_mode, default_atta, shop_name, shop_address, "
    "shop_phone, gstin, language — but any key the owner wants is fine."
)


def set_preference(key: str, value: str) -> dict:
    key = key.strip().lower().replace(" ", "_")
    with transaction() as tx:
        tx.execute(
            """INSERT INTO preferences (scope, key, value, updated_at)
               VALUES ('store', ?, ?, datetime('now'))
               ON CONFLICT(scope, key) DO UPDATE SET value=excluded.value,
                   updated_at=datetime('now')""",
            (key, value),
        )
    return {"saved": {key: value}}


def delete_preference(key: str) -> dict:
    with transaction() as tx:
        tx.execute("DELETE FROM preferences WHERE scope='store' AND key=?",
                   (key.strip().lower().replace(" ", "_"),))
    return {"deleted": key}


def get_preferences() -> dict:
    rows = get_conn().execute(
        "SELECT key, value FROM preferences WHERE scope='store' ORDER BY key"
    ).fetchall()
    return {r["key"]: r["value"] for r in rows}
