"""Tiny health endpoint, so the bot can live on a free web-service host.

Free tiers (Hugging Face Spaces, Render) run *web* services: they expect
something listening on `$PORT` and they idle a service that gets no traffic.
This agent is a long-polling worker with no web surface of its own, so it
serves one page — a status summary — purely to satisfy that contract and to
give an uptime pinger something to hit.

Stdlib only, one daemon thread, no framework. It never touches the store's
write path: it reports counts and nothing else.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

log = logging.getLogger(__name__)

STARTED_AT = datetime.now(timezone.utc)


def _status() -> dict:
    """Read-only snapshot. Errors here must never take the bot down."""
    payload = {
        "service": "kirana-ops-agent",
        "status": "ok",
        "started_at": STARTED_AT.isoformat(timespec="seconds"),
        "uptime_seconds": int((datetime.now(timezone.utc) - STARTED_AT).total_seconds()),
    }
    try:
        from .config import MODEL_CHAIN
        from .db.database import get_conn

        conn = get_conn()
        payload.update(
            models=MODEL_CHAIN,
            products=conn.execute(
                "SELECT COUNT(*) c FROM products WHERE active=1").fetchone()["c"],
            bills_finalized=conn.execute(
                "SELECT COUNT(*) c FROM bills WHERE status='finalized'").fetchone()["c"],
        )
    except Exception as e:  # noqa: BLE001
        payload["status"] = "degraded"
        payload["detail"] = str(e)
    return payload


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = json.dumps(_status(), indent=2).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence per-request noise from uptime pings
        pass


def start_health_server() -> None:
    """Start the health server if the host gave us a PORT. No-op locally."""
    port = os.environ.get("PORT")
    if not port:
        return
    try:
        server = HTTPServer(("0.0.0.0", int(port)), _Handler)
    except Exception:  # noqa: BLE001
        log.exception("health server could not bind port %s — continuing without it", port)
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("health endpoint listening on :%s", port)
