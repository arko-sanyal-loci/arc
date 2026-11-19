"""Local Antigravity MCP tools for one explicitly configured LOCI identity."""

import json
import os
from pathlib import Path
import re
import subprocess
from urllib.request import Request, urlopen

from mcp.server.fastmcp import FastMCP

server = FastMCP("mr-calendar")
locus = os.getenv("MR_CALENDAR_LOCUS", "mr-calendar")
if not re.fullmatch(r"[a-z][a-z0-9-]{0,49}", locus):
    raise ValueError("Configure a valid LOCI name")
state = Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local/state")) / "arc-mr-calendar" / locus
os.umask(0o077)


@server.tool()
def scratchpad_add(note: str) -> str:
    """Append a private working note for this configured LOCI identity."""
    if not 0 < len(note) <= 2000:
        raise ValueError("Supply 1–2000 characters")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = state / "scratchpad.jsonl"
    if path.exists() and path.stat().st_size >= 128_000:
        raise ValueError("Scratchpad is full. The owner can archive it locally.")
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(note, ensure_ascii=False) + "\n")
    return locus


@server.tool()
def scratchpad_recent() -> dict:
    """Read recent private working notes for this configured LOCI identity."""
    path = state / "scratchpad.jsonl"
    notes = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()[-8:]] if path.exists() else []
    return {"locus": locus, "notes": notes}


@server.tool()
def calendar_chat(question: str, parallel: bool = False) -> dict:
    """Ask the local Mr Calendar runtime a text question."""
    if not 0 < len(question) <= 4000:
        raise ValueError("Supply 1–4000 characters")
    payload = json.dumps({"stream": False, "messages": [{"role": "user", "content": question}],
                          "options": {"num_predict": 640}}).encode()
    request = Request("http://127.0.0.1:11437/api/chat", data=payload, headers={
        "Content-Type": "application/json", "X-LOCI-Execution": "parallel" if parallel else "single",
    })
    with urlopen(request, timeout=300) as response:
        return {"locus": locus, "model": response.headers.get("X-LOCI-Model"),
                "answer": json.load(response)["message"]["content"]}


@server.tool()
def review_scene(start_seconds: int = 90) -> dict:
    """Capture and compare three visual fidelities for the configured YouTube clip."""
    if not 0 <= start_seconds <= 200:
        raise ValueError("Seek must be between 0 and 200 seconds")
    python = os.environ["MR_CALENDAR_VISION_PYTHON"]
    script = Path(__file__).with_name("live.py")
    output = subprocess.check_output(
        [python, str(script), "--frames", "20", "--start", str(start_seconds),
         "--ask", "--", "-nxb3VCH7Vw"], text=True, timeout=180,
    )
    rows = [json.loads(line) for line in output.splitlines()]
    return {"locus": locus, "samples": sum(row["track"] == "video" for row in rows),
            "review": next((row for row in rows if row["track"] == "review"), None)}


if __name__ == "__main__":
    server.run()
