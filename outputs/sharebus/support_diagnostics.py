"""Pure, bounded workroom diagnostic projection shared by broker and help."""
from __future__ import annotations


def business_runtime(runtime: dict) -> dict:
    return {"service": "sharebus-business-diagnostics-v1", "key": runtime["workspace_id"],
            "nonce": runtime["nonce"], "pid": runtime["pid"], "url": runtime["url"].rstrip("/")}


def project_diagnostic(state: dict | None, connection: str = "unknown") -> dict:
    """Registration/verified channels do not attest current desktop liveness."""
    paused = None
    agents = []
    if isinstance(state, dict):
        auto = state.get("auto") if isinstance(state.get("auto"), dict) else {}
        if type(state.get("paused")) is bool:
            paused = state["paused"]
        if auto.get("paused") is True or auto.get("suspended") is True:
            paused = True
        connection = "paused" if paused else "connected"
        agents = state.get("agents") if isinstance(state.get("agents"), list) else []
    counts = {name: 0 for name in ("claude", "codex", "antigravity", "other")}
    registered = connected = working = attention = paused_apps = stopped_apps = 0
    for agent in agents[:10000]:
        if not isinstance(agent, dict):
            continue
        provider = agent.get("provider")
        counts[provider if isinstance(provider, str) and provider in counts else "other"] += 1
        registered += 1
        status = agent.get("state")
        if not isinstance(status, str):
            status = "unknown"
        verified = (agent.get("channel_verified") is True and agent.get("wake_channel_available") is True
                    and agent.get("attachment_status") == "native_delivery_verified"
                    and status in {"idle", "busy", "running", "working", "awaiting_native_reply"})
        connected += verified
        working += verified and status in {"busy", "running", "working", "awaiting_native_reply"}
        paused_apps += status == "paused"
        stopped_apps += status == "stopped"
        attention += status in {"needs_human", "error", "unknown", "stale", "unavailable"}
    return {"connection": connection, "control": {"paused": paused, "stopped": None},
            "apps": {"registered": registered, "connected": connected, "working": working,
                     "paused": paused_apps, "stopped": stopped_apps, "needs_attention": attention,
                     "liveness": "not_verified", "by_provider": counts}, "can_configure": False}
