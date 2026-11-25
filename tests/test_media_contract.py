from pathlib import Path

from app.server import MCP_TOOLS


ROOT = Path(__file__).resolve().parents[1]


def test_music_search_is_an_explicit_mcp_tool():
    tools = {tool["name"]: tool for tool in MCP_TOOLS}
    assert "arc_music_search" in tools
    schema = tools["arc_music_search"]["inputSchema"]
    assert schema["required"] == ["query"]
    assert schema["properties"]["limit"]["maximum"] == 50


def test_service_orders_arc_after_media_monitor():
    unit = (ROOT / "systemd/arc-interface.service").read_text()
    assert "Wants=arc-nomic-tunnel.service arc-monitor.service" in unit
    assert "After=default.target arc-nomic-tunnel.service arc-monitor.service" in unit
    assert "ARC_MUSIC_MONITOR_URL=http://127.0.0.1:11500" in unit


def test_status_poll_is_non_overlapping_and_visibility_aware():
    page = (ROOT / "app/static/index.html").read_text()
    assert "async function pollStatus()" in page
    assert "document.hidden ? 15000 : 3000" in page
    assert "setInterval(pollStatus" not in page
    assert "setTimeout(pollStatus" in page


def test_media_runtime_contract_documents_explicit_permission_and_proof():
    document = (ROOT / "docs/ARC-MEDIA-RUNTIME.md").read_text()
    assert "Share tab audio" in document
    assert "response says `indexed`" in document
    assert "raw source audio is not stored" in document
