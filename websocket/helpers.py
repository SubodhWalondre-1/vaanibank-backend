"""
VaaniBank AI — WebSocket Helpers
PSBs Hackathon 2026 | Team Vectora
"""

from datetime import datetime, timezone
import logging
from typing import Any, Dict, Optional
from fastapi import WebSocket

logger = logging.getLogger("vaanibank.websocket.helpers")

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _event(event_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap payload in standard envelope."""
    return {
        "type": event_type,
        "data": data,
        "timestamp": _now_iso(),
    }

async def _safe_send(ws: Optional[WebSocket], payload: Dict[str, Any]) -> bool:
    """Send JSON to a WebSocket; swallow errors if connection is gone."""
    if ws is None:
        return False
    try:
        await ws.send_json(payload)
        return True
    except Exception as exc:
        logger.warning("WebSocket send failed: %s", exc)
        return False
