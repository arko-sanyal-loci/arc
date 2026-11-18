"""
glassbox_ingest.py — Gmail → Glassbox Compendium ingest pipeline

Normalises raw Gmail messages into Glassbox Package records,
seeds the SLP heat from message signals, stamps provenance hash,
and hands off to dual_write for atomic Drive + Compendium storage.

Pipeline per message:
  1. Parse Gmail payload → NormalizedRecord
  2. Derive initial heat from: recency, thread depth, label signals
  3. Assign stream (I/Q/D/M/S) from content classifier stub
  4. Build SLPRegister + Package
  5. Dispatch to dual_write
"""

from __future__ import annotations

import base64
import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Protocol

from slp_encoding import (
    ALPHA, H_MAX,
    ClassFlags, RoutingHint,
    SLPRegister, heat_to_u16,
)
from dual_write import Package, DriveClient, CompendiumDB, dual_write, DualWriteResult


# ── Gmail message dict (minimal subset we use) ───────────────────────────────

GmailMessage = dict  # from gmail_service.users().messages().get(...)


# ── Normalized record ─────────────────────────────────────────────────────────

@dataclass
class NormalizedRecord:
    """
    Canonical form of one Gmail message for Glassbox storage.
    """
    message_id:    str
    thread_id:     str
    subject:       str
    sender:        str
    recipients:    list[str]
    body_text:     str          # decoded, plain-text
    timestamp_s:   int          # epoch seconds (internalDate / 1000)
    labels:        list[str]    # e.g. ["INBOX", "IMPORTANT", "CATEGORY_PERSONAL"]
    thread_depth:  int          # 0-indexed position in thread
    has_attachment: bool


# ── Heat seeding from message signals ─────────────────────────────────────────

_RECENCY_HALF_LIFE_SECONDS = 7 * 24 * 3600   # 1 week

# Label weights: bump heat for high-signal labels
_LABEL_HEAT_BUMPS: dict[str, float] = {
    "STARRED":    +0.15,
    "IMPORTANT":  +0.10,
    "INBOX":      +0.05,
    "SENT":       +0.02,
    "SPAM":       -0.20,
    "TRASH":      -0.30,
}

# Thread depth decay: deep threads are less likely to be top-signal
_THREAD_DEPTH_DECAY = 0.03   # subtract per extra message after the first


def _recency_score(timestamp_s: int, now_s: int) -> float:
    """
    Exponential decay from 1.0 → 0 based on age relative to half-life.
    Returns value in [0, 1].
    """
    import math
    age = max(0, now_s - timestamp_s)
    decay = math.exp(-math.log(2) * age / _RECENCY_HALF_LIFE_SECONDS)
    return decay


def seed_heat(record: NormalizedRecord, now_s: int | None = None) -> float:
    """
    Derive initial Heat value for a normalized email record.

    Formula:
        H_seed = clamp(
            0.40 * recency
            + 0.30 * label_boost
            + 0.20 * body_length_score
            + 0.10 * thread_shallow_bonus,
            ALPHA, H_MAX
        )

    Returns float in [ALPHA, H_MAX].
    """
    if now_s is None:
        now_s = int(time.time())

    # Recency component [0, 1]
    recency = _recency_score(record.timestamp_s, now_s)

    # Label boost/penalty [−0.30, +0.30 roughly]
    label_bump = sum(
        _LABEL_HEAT_BUMPS.get(lbl.upper(), 0.0)
        for lbl in record.labels
    )
    label_score = max(0.0, min(1.0, 0.5 + label_bump))  # center on 0.5

    # Body length score: more content → more signal (soft cap at 2000 chars)
    body_len = min(len(record.body_text), 2000) / 2000.0

    # Thread depth: shallow = higher signal for new context
    depth_bonus = max(0.0, 1.0 - record.thread_depth * _THREAD_DEPTH_DECAY)

    raw = (
        0.40 * recency
        + 0.30 * label_score
        + 0.20 * body_len
        + 0.10 * depth_bonus
    )
    return max(ALPHA, min(H_MAX, raw))


# ── Stream classification stub ─────────────────────────────────────────────────

_SCALAR_KEYWORDS = re.compile(
    r"\b(\d[\d,]*\.?\d*\s*(USD|EUR|BDT|%|percent|million|billion|units?|rows?|ms|sec|GB|MB|TB))\b",
    re.IGNORECASE,
)

def classify_stream(record: NormalizedRecord) -> str:
    """
    Assign SymPrev stream label based on message content heuristics.
    Production version should use a small classifier.

    Returns one of: I (narrative), Q (scalars), M (VAD/session)
    """
    text = record.body_text.lower()

    # Q-stream: messages that are predominantly quantitative
    if len(_SCALAR_KEYWORDS.findall(record.body_text)) >= 3:
        return "Q"

    # M-stream: strong emotional/personal signals
    mood_signals = sum(
        text.count(kw)
        for kw in ("feel", "worried", "excited", "frustrated", "happy", "sad", "urgent")
    )
    if mood_signals >= 2:
        return "M"

    # Default: narrative
    return "I"


# ── Gmail body decoder ────────────────────────────────────────────────────────

def _decode_body(payload: dict) -> str:
    """
    Extract plain-text body from Gmail API message payload dict.
    Handles both simple and multipart MIME.
    """
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")

    if mime_type.startswith("multipart/"):
        for part in payload.get("parts", []):
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")

    return ""


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _has_attachment(payload: dict) -> bool:
    for part in payload.get("parts", []):
        if part.get("filename"):
            return True
    return False


# ── Normalize Gmail message ───────────────────────────────────────────────────

def normalize_gmail_message(
    msg: GmailMessage,
    thread_depth: int = 0,
) -> NormalizedRecord:
    """
    Convert a raw Gmail API message dict into a NormalizedRecord.
    """
    payload = msg.get("payload", {})
    headers = payload.get("headers", [])

    timestamp_ms = int(msg.get("internalDate", "0"))
    timestamp_s = timestamp_ms // 1000

    recipients_raw = _header(headers, "To") + "," + _header(headers, "Cc")
    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]

    return NormalizedRecord(
        message_id=msg.get("id", ""),
        thread_id=msg.get("threadId", ""),
        subject=_header(headers, "Subject"),
        sender=_header(headers, "From"),
        recipients=recipients,
        body_text=_decode_body(payload),
        timestamp_s=timestamp_s,
        labels=msg.get("labelIds", []),
        thread_depth=thread_depth,
        has_attachment=_has_attachment(payload),
    )


# ── Category inference ────────────────────────────────────────────────────────

def infer_category(record: NormalizedRecord, default_category: str = "inbox") -> str:
    """
    Map Gmail labels to Glassbox category strings.
    Extend with your own domain taxonomy.
    """
    labels_upper = {lbl.upper() for lbl in record.labels}

    if "CATEGORY_PROMOTIONS" in labels_upper:
        return "marketing"
    if "CATEGORY_SOCIAL" in labels_upper:
        return "social"
    if "CATEGORY_UPDATES" in labels_upper:
        return "updates"
    if "STARRED" in labels_upper:
        return "starred"

    # Subject-line category hints
    subject_lower = record.subject.lower()
    for keyword, cat in [
        ("invoice", "finance"),
        ("payment", "finance"),
        ("contract", "legal"),
        ("nda", "legal"),
        ("meeting", "calendar"),
        ("urgent", "priority"),
        ("loci", "loci-engine"),
        ("billo", "billo-tech"),
        ("glassbox", "loci-engine"),
    ]:
        if keyword in subject_lower:
            return cat

    return default_category


# ── Build Glassbox Package ────────────────────────────────────────────────────

def build_package(
    record: NormalizedRecord,
    project: str = "glassbox",
    category: str | None = None,
    now_s: int | None = None,
) -> Package:
    """
    Convert a NormalizedRecord into a Glassbox Package ready for dual_write.
    """
    if now_s is None:
        now_s = int(time.time())

    resolved_category = category or infer_category(record)
    heat = seed_heat(record, now_s)
    stream = classify_stream(record)

    # Build SLPRegister
    flags = ClassFlags.MILD
    if heat >= 0.70:
        flags = ClassFlags.HOT
    elif heat < 0.30:
        flags = ClassFlags.COLD

    routing = RoutingHint(0)
    if "STARRED" in [lbl.upper() for lbl in record.labels]:
        routing |= RoutingHint.FORCE_DUALWRITE

    # category_id: stable hash of category string → uint32
    cat_id = int(hashlib.sha256(resolved_category.encode()).hexdigest()[:8], 16)

    slp = SLPRegister.new(
        category_id=cat_id & 0xFFFFFFFF,
        source_id=int(hashlib.sha256(record.message_id.encode()).hexdigest()[:8], 16) & 0xFFFFFFFF,
        heat_raw=heat_to_u16(heat),
        class_flags=int(flags),
        routing_hints=int(routing),
        stream={"I": 0, "Q": 1, "D": 2, "M": 3, "S": 4}.get(stream, 0),
    )

    content = (
        f"Subject: {record.subject}\n"
        f"From: {record.sender}\n"
        f"To: {', '.join(record.recipients)}\n\n"
        f"{record.body_text}"
    )

    return Package(
        commit_id=record.message_id,
        category=resolved_category,
        project=project,
        source_id=record.message_id,
        author_id=record.sender,
        role="email",
        content=content,
        slp=slp,
        metadata={
            "thread_id":      record.thread_id,
            "thread_depth":   record.thread_depth,
            "has_attachment": record.has_attachment,
            "labels":         record.labels,
            "stream":         stream,
        },
        timestamp_s=record.timestamp_s,
    )


# ── Main ingest function ──────────────────────────────────────────────────────

def ingest_gmail_message(
    msg:      GmailMessage,
    drive:    DriveClient,
    compendium: CompendiumDB,
    project:  str = "glassbox",
    category: str | None = None,
    thread_depth: int = 0,
    now_s:    int | None = None,
) -> DualWriteResult:
    """
    Full pipeline: raw Gmail message → Glassbox Compendium entry.

    Returns DualWriteResult. Caller should check .success and handle
    failures (e.g. retry queue).
    """
    record  = normalize_gmail_message(msg, thread_depth=thread_depth)
    package = build_package(record, project=project, category=category, now_s=now_s)
    return dual_write(package, drive, compendium)


def ingest_batch(
    messages: list[GmailMessage],
    drive:    DriveClient,
    compendium: CompendiumDB,
    project:  str = "glassbox",
    now_s:    int | None = None,
) -> list[DualWriteResult]:
    """
    Ingest a list of Gmail messages.
    Thread depth is approximated by thread_id grouping.
    """
    if now_s is None:
        now_s = int(time.time())

    # Group by thread to calculate depth
    thread_counts: dict[str, int] = {}
    results: list[DualWriteResult] = []

    for msg in messages:
        tid = msg.get("threadId", "")
        depth = thread_counts.get(tid, 0)
        thread_counts[tid] = depth + 1

        result = ingest_gmail_message(
            msg=msg,
            drive=drive,
            compendium=compendium,
            project=project,
            thread_depth=depth,
            now_s=now_s,
        )
        results.append(result)

    return results
