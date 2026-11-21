#!/usr/bin/env python3
"""Private VPS chat and outage journal for Arc; binds to loopback only."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
import uuid


ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("ARC_CHAT_PORT", "8765"))
DATA_DIR = Path(os.environ.get("ARC_CHAT_DATA_DIR", "/var/lib/arc-remote-chat"))
DB_PATH = DATA_DIR / "journal.sqlite3"
LATEST_PATH = DATA_DIR / "workstation-latest.json"
OLLAMA_URL = os.environ.get("ARC_CHAT_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
CHAT_MODEL = os.environ.get("ARC_CHAT_MODEL", "qwen2.5:1.5b")
CHAT_USERNAME = os.environ.get("ARC_CHAT_USERNAME", "arko")
CHAT_PASSWORD_HASH = os.environ.get("ARC_CHAT_PASSWORD_HASH", "")
TELEMETRY_TOKEN = os.environ.get("ARC_CHAT_TELEMETRY_TOKEN", "")
AGE_RECIPIENT = os.environ.get("ARC_CHAT_AGE_RECIPIENT", "")
MAX_BODY = 4 * 1024 * 1024
_auth_failures: dict[str, tuple[int, float]] = {}
_auth_lock = threading.Lock()
_whisper = None
_whisper_lock = threading.Lock()


@contextmanager
def _db():
    DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(DATA_DIR, 0o700)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("""CREATE TABLE IF NOT EXISTS journal (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            created_at REAL NOT NULL,
            event_type TEXT NOT NULL,
            actor TEXT NOT NULL,
            payload BLOB NOT NULL,
            encrypted INTEGER NOT NULL,
            previous_hash TEXT NOT NULL,
            record_hash TEXT NOT NULL
        )""")
        conn.execute("""CREATE TRIGGER IF NOT EXISTS journal_no_update
            BEFORE UPDATE ON journal BEGIN SELECT RAISE(ABORT,'append-only journal'); END""")
        conn.execute("""CREATE TRIGGER IF NOT EXISTS journal_no_delete
            BEFORE DELETE ON journal BEGIN SELECT RAISE(ABORT,'append-only journal'); END""")
        conn.commit()
        os.chmod(DB_PATH, 0o600)
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _append_event(event_type: str, actor: str, payload: dict, *, encrypt: bool) -> dict:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    if encrypt:
        if not AGE_RECIPIENT.startswith("age1") or not shutil.which("age"):
            raise RuntimeError("encrypted journal is not configured")
        result = subprocess.run(["age", "--encrypt", "--recipient", AGE_RECIPIENT],
                                input=encoded, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, check=True, timeout=20)
        stored = result.stdout
    else:
        stored = encoded
    event_id = str(uuid.uuid4())
    created_at = time.time()
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        prior = conn.execute("SELECT record_hash FROM journal ORDER BY sequence DESC LIMIT 1").fetchone()
        previous_hash = prior[0] if prior else "0" * 64
        digest = hashlib.sha256()
        digest.update(previous_hash.encode())
        digest.update(event_id.encode())
        digest.update(event_type.encode())
        digest.update(stored)
        record_hash = digest.hexdigest()
        cursor = conn.execute(
            "INSERT INTO journal (event_id,created_at,event_type,actor,payload,encrypted,previous_hash,record_hash) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (event_id, created_at, event_type, actor, stored, int(encrypt), previous_hash, record_hash),
        )
        sequence = cursor.lastrowid
    return {"event_id": event_id, "sequence": sequence, "record_hash": record_hash,
            "created_at": created_at, "event_type": event_type}


def _verify_journal() -> dict:
    previous_hash = "0" * 64
    count = 0
    with _db() as conn:
        rows = conn.execute("SELECT event_id,event_type,payload,previous_hash,record_hash FROM journal ORDER BY sequence").fetchall()
    for event_id, event_type, payload, stored_previous, stored_hash in rows:
        if stored_previous != previous_hash:
            return {"valid": False, "checked": count, "failed_event": event_id}
        digest = hashlib.sha256(previous_hash.encode() + event_id.encode()
                                + event_type.encode() + payload).hexdigest()
        if not hmac.compare_digest(digest, stored_hash):
            return {"valid": False, "checked": count, "failed_event": event_id}
        previous_hash = stored_hash
        count += 1
    return {"valid": True, "checked": count, "head": previous_hash}


def _scrypt_matches(password: bytes) -> bool:
    try:
        algorithm, n_value, r_value, p_value, salt64, expected64 = CHAT_PASSWORD_HASH.split("$")
        if algorithm != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt64 + "=" * (-len(salt64) % 4))
        expected = base64.urlsafe_b64decode(expected64 + "=" * (-len(expected64) % 4))
        actual = hashlib.scrypt(password, salt=salt, n=int(n_value), r=int(r_value),
                                p=int(p_value), dklen=len(expected), maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, MemoryError):
        return False


def _basic_user(header: str) -> tuple[str, bytes] | None:
    if not header.startswith("Basic "):
        return None
    try:
        raw = base64.b64decode(header[6:], validate=True)
        username, password = raw.split(b":", 1)
        return username.decode("utf-8"), password
    except (ValueError, UnicodeDecodeError):
        return None


def _write_latest_status(payload: dict) -> None:
    temp_path = LATEST_PATH.with_name(LATEST_PATH.name + ".tmp")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    temp_path.write_bytes(encoded)
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, LATEST_PATH)


def _read_latest_status() -> dict | None:
    try:
        return json.loads(LATEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _workstation_status() -> dict:
    item = _read_latest_status()
    if not item:
        return {"online": False, "last_update": None, "reason": "No workstation heartbeat received yet."}
    age_seconds = max(0, int(time.time() - float(item.get("received_at", 0))))
    result = dict(item)
    result["age_seconds"] = age_seconds
    result["online"] = age_seconds <= 75
    if not result["online"]:
        result["reason"] = "No heartbeat in the last 75 seconds; showing the last received state."
    return result


def _dispatch_events(limit: int = 25) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT sequence,event_id,created_at,event_type,actor,payload,encrypted,record_hash "
            "FROM journal WHERE encrypted=0 ORDER BY sequence DESC LIMIT ?", (limit,),
        ).fetchall()
    items = []
    for sequence, event_id, created_at, event_type, actor, payload, _encrypted, record_hash in reversed(rows):
        try:
            public_payload = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            public_payload = {"error": "invalid journal payload"}
        items.append({"sequence": sequence, "event_id": event_id, "created_at": created_at,
                      "event_type": event_type, "actor": actor, "payload": public_payload,
                      "record_hash": record_hash})
    return items


def _ollama_json(path: str, payload: dict | None = None, timeout: int = 5) -> dict:
    request = Request(OLLAMA_URL + path,
                      data=json.dumps(payload).encode() if payload is not None else None,
                      headers={"Content-Type": "application/json"} if payload is not None else {},
                      method="POST" if payload is not None else "GET")
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _transcriber():
    global _whisper
    if _whisper is None:
        with _whisper_lock:
            if _whisper is None:
                from faster_whisper import WhisperModel
                _whisper = WhisperModel(
                    os.environ.get("ARC_CHAT_WHISPER_MODEL", "tiny"),
                    device="cpu", compute_type="int8",
                    download_root=str(DATA_DIR / "whisper-models"),
                )
    return _whisper


def _transcribe_audio(data: bytes) -> str:
    import av
    import numpy as np
    with av.open(io.BytesIO(data)) as container:
        if not container.streams.audio:
            raise ValueError("audio track missing")
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=16_000)
        frames = []
        for frame in container.decode(audio=0):
            for converted in resampler.resample(frame):
                frames.append(converted.to_ndarray().reshape(-1))
        for converted in resampler.resample(None):
            frames.append(converted.to_ndarray().reshape(-1))
    audio = np.concatenate(frames).astype(np.float32, copy=False) if frames else np.empty(0, dtype=np.float32)
    if audio.size == 0 or float(np.sqrt(np.mean(np.square(audio)))) < 0.0015:
        return ""
    segments, _info = _transcriber().transcribe(audio, beam_size=1, vad_filter=True,
                                                 condition_on_previous_text=False)
    return " ".join(segment.text.strip() for segment in segments).strip()[:4000]


class Handler(BaseHTTPRequestHandler):
    server_version = "ArcRemoteChat/0.1"

    def log_message(self, *_args):
        # Do not log chat bodies, voice payloads, auth headers or tokens.
        return

    def _send(self, status: int, body, content_type: str = "application/json; charset=utf-8"):
        if isinstance(body, bytes):
            data = body
        else:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        credential = _basic_user(self.headers.get("Authorization", ""))
        if not credential:
            return False
        username, password = credential
        return (hmac.compare_digest(username.encode(), CHAT_USERNAME.encode())
                and _scrypt_matches(password))

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        address = self.client_address[0]
        now = time.monotonic()
        with _auth_lock:
            count, until = _auth_failures.get(address, (0, now + 3600))
            if now > until:
                count, until = 0, now + 3600
            count += 1
            _auth_failures[address] = (count, until)
        self.send_response(429 if count > 12 else 401)
        self.send_header("WWW-Authenticate", 'Basic realm="Arc remote chat", charset="UTF-8"')
        self.send_header("Cache-Control", "no-store")
        if count > 12:
            self.send_header("Retry-After", str(max(1, int(until - now))))
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _body(self) -> dict:
        try:
            size = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            size = -1
        if size <= 0 or size > MAX_BODY:
            raise ValueError(f"request body must be 1..{MAX_BODY} bytes")
        value = json.loads(self.rfile.read(size))
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def do_GET(self):
        path = urlsplit(self.path)
        if path.path in {"/arko/chat", "/arko/chat/"}:
            if not self._require_auth():
                return
            return self._send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        if not path.path.startswith("/arko/chat/api/"):
            return self._send(404, {"error": "not found"})
        if not self._require_auth():
            return
        if path.path == "/arko/chat/api/status":
            try:
                models = _ollama_json("/api/tags", timeout=3).get("models", [])
                model_names = [item.get("name") for item in models if isinstance(item, dict)]
                ollama_status = {"online": True, "models": model_names}
            except (OSError, ValueError, HTTPError, URLError):
                ollama_status = {"online": False, "models": []}
            return self._send(200, {"workstation": _workstation_status(), "ollama": ollama_status,
                                    "model": CHAT_MODEL, "journal": _verify_journal(),
                                    "events": _dispatch_events(15)})
        if path.path == "/arko/chat/api/events":
            query = parse_qs(path.query)
            try:
                limit = min(100, max(1, int(query.get("limit", ["25"])[0])))
            except ValueError:
                limit = 25
            return self._send(200, {"items": _dispatch_events(limit)})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/arko/chat/api/telemetry":
            if not TELEMETRY_TOKEN or not hmac.compare_digest(
                    self.headers.get("Authorization", ""), f"Bearer {TELEMETRY_TOKEN}"):
                return self._send(401, {"error": "telemetry credential rejected"})
            try:
                payload = self._body()
                sequence = int(payload["sequence"])
                if not 1 <= sequence < 2**63:
                    raise ValueError
                if payload.get("agent_id") != "workstation":
                    raise ValueError
                sent_at = str(payload["sent_at"])
                arc_api = payload["arc_api"]
                heartbeat_tick = payload.get("heartbeat_tick")
                lanes = payload.get("inference_lanes", {})
                if not isinstance(arc_api, bool) or not isinstance(lanes, dict):
                    raise ValueError
                if heartbeat_tick is not None and (not isinstance(heartbeat_tick, int) or heartbeat_tick < 0):
                    raise ValueError
                clean_lanes = {}
                for name in ("rtx", "arc"):
                    lane = lanes.get(name, {})
                    if not isinstance(lane, dict) or not isinstance(lane.get("ready"), bool):
                        raise ValueError
                    models = lane.get("models", [])
                    if not isinstance(models, list) or any(not isinstance(x, str) or len(x) > 120 for x in models[:12]):
                        raise ValueError
                    clean_lanes[name] = {"ready": lane["ready"], "models": models[:12]}
                device_counts = payload.get("devices", {})
                if not isinstance(device_counts, dict):
                    raise ValueError
                clean = {
                    "agent_id": "workstation", "sequence": sequence, "sent_at": sent_at,
                    "arc_api": arc_api, "heartbeat_tick": heartbeat_tick,
                    "inference_lanes": clean_lanes,
                    "devices": {key: max(0, min(32, int(device_counts.get(key, 0))))
                                for key in ("camera_count", "microphone_count")},
                    "received_at": time.time(),
                }
                old = _read_latest_status()
                previous_sequence = int(old.get("sequence", 0)) if old else 0
                if sequence < previous_sequence:
                    return self._send(409, {"error": "telemetry sequence replay"})
                if sequence == previous_sequence:
                    if old and all(old.get(key) == clean.get(key) for key in
                                   ("agent_id", "sequence", "sent_at", "arc_api", "heartbeat_tick", "inference_lanes", "devices")):
                        clean["received_at"] = time.time()
                        _write_latest_status(clean)
                        return self._send(200, {"status": "duplicate", "sequence": sequence})
                    return self._send(409, {"error": "sequence reused with different content"})
                _write_latest_status(clean)
                state_fields = {key: clean[key] for key in
                                ("arc_api", "heartbeat_tick", "inference_lanes", "devices")}
                old_fields = {key: old.get(key) for key in state_fields} if old else None
                if old_fields != state_fields:
                    _append_event("system.status", "workstation", state_fields, encrypt=False)
                return self._send(200, {"status": "accepted", "sequence": sequence})
            except (KeyError, TypeError, ValueError, OverflowError, json.JSONDecodeError):
                return self._send(422, {"error": "invalid sanitized workstation telemetry"})

        if not self._require_auth():
            return
        try:
            payload = self._body()
        except (ValueError, json.JSONDecodeError) as exc:
            return self._send(400, {"error": str(exc)})

        if path == "/arko/chat/api/notices":
            reason = payload.get("reason")
            device = payload.get("device", "workstation")
            eta = payload.get("eta", "")
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
                return self._send(422, {"error": "reason must contain 1..1000 characters"})
            if device not in {"workstation", "laptop", "vps", "other"}:
                return self._send(422, {"error": "unsupported device"})
            if not isinstance(eta, str) or len(eta) > 200:
                return self._send(422, {"error": "eta must be 200 characters or fewer"})
            event = _append_event("maintenance.notice", "arko",
                                  {"device": device, "reason": reason.strip(), "eta": eta.strip()},
                                  encrypt=False)
            return self._send(201, {"status": "recorded", **event})

        if path == "/arko/chat/api/transcribe":
            audio_text = payload.get("audio")
            if not isinstance(audio_text, str) or len(audio_text) > 4_000_000:
                return self._send(413, {"error": "audio chunk is missing or too large"})
            try:
                audio_bytes = base64.b64decode(audio_text, validate=True)
                if not audio_bytes or len(audio_bytes) > 3_000_000:
                    raise ValueError
                transcript = _transcribe_audio(audio_bytes)
                return self._send(200, {"transcript": transcript,
                                        "speech_model": os.environ.get("ARC_CHAT_WHISPER_MODEL", "tiny"),
                                        "stored": False})
            except ImportError:
                return self._send(503, {"error": "local VPS speech model is not installed"})
            except (ValueError, TypeError, RuntimeError) as exc:
                return self._send(422, {"error": f"audio transcription failed: {type(exc).__name__}"})
            except Exception as exc:
                return self._send(503, {"error": f"local speech service failed: {type(exc).__name__}"})

        if path == "/arko/chat/api/chat":
            messages = payload.get("messages")
            if not isinstance(messages, list) or not 1 <= len(messages) <= 18:
                return self._send(422, {"error": "messages must contain 1..18 user/assistant turns"})
            clean_messages = []
            total_chars = 0
            for item in messages:
                if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
                    return self._send(422, {"error": "only user and assistant messages are accepted"})
                content = item.get("content")
                if not isinstance(content, str) or not content.strip() or len(content) > 8000:
                    return self._send(422, {"error": "each message must contain 1..8000 characters"})
                total_chars += len(content)
                clean_messages.append({"role": item["role"], "content": content})
            if total_chars > 32_000 or clean_messages[-1]["role"] != "user":
                return self._send(422, {"error": "conversation is too large or does not end with a user message"})
            status = _workstation_status()
            events = _dispatch_events(8)
            context = {"latest_workstation": status,
                       "recent_dispatch_events": events,
                       "guidance": "Use these as time-stamped evidence. Distinguish current online telemetry from stale last-known state. Never claim access to workstation files or devices unless the telemetry says they are online. If a device is offline, help the user troubleshoot from the records available here."}
            messages_for_model = [{"role": "system", "content":
                "You are Arc's VPS continuity assistant. Your only local context is the status and dispatch journal below. "
                "Be candid about uncertainty and distinguish observations from inference. Do not claim to control local devices. "
                "Keep private details in this protected session. Context: " + json.dumps(context, ensure_ascii=False)}]
            messages_for_model.extend(clean_messages)
            try:
                response = _ollama_json("/api/chat", {
                    "model": CHAT_MODEL, "messages": messages_for_model,
                    "stream": False, "keep_alive": "3m",
                    "options": {"num_ctx": 4096, "num_predict": 512, "temperature": 0.3},
                }, timeout=180)
                answer = response.get("message", {}).get("content", "")
                if not isinstance(answer, str) or not answer.strip():
                    return self._send(502, {"error": "local Ollama returned an empty answer"})
            except (OSError, ValueError, HTTPError, URLError, TimeoutError) as exc:
                return self._send(503, {"error": "VPS local model is unavailable", "detail": type(exc).__name__})
            try:
                _append_event("chat.turn", "arko", {"messages": clean_messages, "answer": answer}, encrypt=True)
                journal_saved = True
            except Exception:
                journal_saved = False
            return self._send(200, {"answer": answer, "model": CHAT_MODEL,
                                    "journal_saved_encrypted": journal_saved})

        return self._send(404, {"error": "not found"})


def main() -> None:
    if (not CHAT_PASSWORD_HASH or not TELEMETRY_TOKEN or not AGE_RECIPIENT.startswith("age1")
            or not shutil.which("age")):
        raise SystemExit("Arc remote chat credentials and age recipient must be configured")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    print(f"Arc VPS chat listening on loopback port {PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
