"""
slp_encoding.py — SLP (SymLinkPrev) register: 20-byte, L1-cache-aligned format.

Layout (3 registers = 60 bytes + 4-byte header = 64 bytes = one L1 cache line):

  offset  field             type     meaning
  0x00    node_id           uint32   local node id
  0x04    category_id       uint32   hashed category key
  0x08    timestamp_s       uint32   seconds-since-epoch (B11)
  0x0C    heat_u16          uint16   H mapped to [α=0.0500, H_max=0.9999]
  0x0E    class_flags       uint8    HOT|MILD|COLD|SCAR|GOAL|PLAYGROUND
  0x0F    routing_hint      uint8    local_req|speculative|ignore|force_dualwrite
  0x10    content_sha_lo32  uint32   first 32 bits of SHA-256

Heat encoding:
  H_u16   = round((H_float − α) / (H_max − α) × 65535)
  H_float = α + (H_u16 / 65535) × (H_max − α)
  ΔH ≈ 1.45×10⁻⁵  (~4.82 decimal places precision)
  Invariant: 0.0500 ≤ H_float ≤ 0.9999  (Constant of Hope floor + ceiling)
"""

from __future__ import annotations

import hashlib
import struct
import time
from dataclasses import dataclass, field
from enum import IntFlag
from typing import Optional

# ── Heat constants ────────────────────────────────────────────────────────────

ALPHA: float = 0.0500        # Constant of Hope floor — heat never reaches 0
H_MAX: float = 0.9999        # ceiling — heat never reaches 1.0 (sovereignty exception)
H_RANGE: float = H_MAX - ALPHA  # 0.9499
UINT16_MAX: int = 65535


# ── Flag enums ────────────────────────────────────────────────────────────────

class ClassFlags(IntFlag):
    """class_flags byte: heat class + routing shortcuts."""
    HOT        = 0x01
    MILD       = 0x02
    COLD       = 0x04
    SCAR       = 0x08  # B2 sticky trace — routes direct to ledger, never clamped to 1.0
    GOAL       = 0x10  # active Goal (GCSD, dH/dt = 0) — skip tagging
    PLAYGROUND = 0x20  # unmatched hypothesis held at α floor

    @classmethod
    def heat_class(cls, h: float) -> "ClassFlags":
        """Assign HOT/MILD/COLD from a heat float."""
        if h >= 0.70:
            return cls.HOT
        if h >= 0.35:
            return cls.MILD
        return cls.COLD


class RoutingHint(IntFlag):
    """routing_hint byte: how CAR should dispatch this register."""
    LOCAL_REQ      = 0x01  # standard local request
    SPECULATIVE    = 0x02  # speculative prefetch (K-hypothesis)
    IGNORE         = 0x04  # CAR should drop silently
    FORCE_DUALWRITE = 0x08  # bypass triage, write to both Drive + Compendium immediately


# ── Struct definition ─────────────────────────────────────────────────────────
# Format: little-endian  uint32 uint32 uint32 uint16 uint8 uint8 uint32
# Sizes:                 4      4      4      2      1     1     4      = 20 bytes
_SLP_STRUCT = struct.Struct("<IIIHBBI")

assert _SLP_STRUCT.size == 20, "SLP register must be exactly 20 bytes"


# ── Core functions ────────────────────────────────────────────────────────────

def heat_to_u16(h: float) -> int:
    """
    Map H_float ∈ [α, H_max] to uint16 [0, 65535].
    Clamps to invariant range before encoding.
    """
    h = max(ALPHA, min(H_MAX, h))
    return round((h - ALPHA) / H_RANGE * UINT16_MAX)


def u16_to_heat(h_u16: int) -> float:
    """
    Decode uint16 → H_float ∈ [α, H_max].
    Inverse of heat_to_u16; always within invariant range.
    """
    h_u16 = max(0, min(UINT16_MAX, h_u16))
    return ALPHA + (h_u16 / UINT16_MAX) * H_RANGE


def sha_lo32(content: bytes) -> int:
    """First 32 bits of SHA-256 of content, as little-endian uint32."""
    digest = hashlib.sha256(content).digest()
    return struct.unpack_from("<I", digest)[0]


def category_hash(category: str) -> int:
    """Stable uint32 hash for a category string."""
    digest = hashlib.sha256(category.encode()).digest()
    return struct.unpack_from("<I", digest)[0]


def heat_decay(h: float, ticks: int, gamma: float = 0.995) -> float:
    """
    Hyperbolic heat decay over N ticks.
    Approaches α asymptotically — never reaches 0 (Constant of Hope).
    gamma < 1.0; smaller gamma = faster decay.
    """
    decayed = ALPHA + (h - ALPHA) * (gamma ** ticks)
    return max(ALPHA, min(H_MAX, decayed))


# ── SLPRegister dataclass ─────────────────────────────────────────────────────

@dataclass
class SLPRegister:
    """
    20-byte SymLinkPrev register.
    Pack 3 of these + a 4-byte header = 64 bytes = one L1 cache line.
    """
    node_id: int
    category_id: int
    timestamp_s: int
    heat: float                    # stored as float; encoded to uint16 on pack()
    class_flags: ClassFlags
    routing_hint: RoutingHint
    content_sha_lo32: int

    # ── serialisation ──────────────────────────────────────────────────────

    def pack(self) -> bytes:
        """Serialise to 20-byte wire format."""
        return _SLP_STRUCT.pack(
            self.node_id & 0xFFFFFFFF,
            self.category_id & 0xFFFFFFFF,
            self.timestamp_s & 0xFFFFFFFF,
            heat_to_u16(self.heat),
            int(self.class_flags) & 0xFF,
            int(self.routing_hint) & 0xFF,
            self.content_sha_lo32 & 0xFFFFFFFF,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "SLPRegister":
        """Deserialise from 20-byte wire format."""
        if len(data) < 20:
            raise ValueError(f"SLP data too short: {len(data)} bytes, need 20")
        node_id, cat_id, ts, h_u16, flags, hint, sha = _SLP_STRUCT.unpack(data[:20])
        return cls(
            node_id=node_id,
            category_id=cat_id,
            timestamp_s=ts,
            heat=u16_to_heat(h_u16),
            class_flags=ClassFlags(flags),
            routing_hint=RoutingHint(hint),
            content_sha_lo32=sha,
        )

    # ── convenience constructors ───────────────────────────────────────────

    @classmethod
    def new(
        cls,
        node_id: int,
        category: str,
        heat: float,
        content: bytes,
        routing_hint: RoutingHint = RoutingHint.LOCAL_REQ,
        extra_flags: ClassFlags = ClassFlags(0),
    ) -> "SLPRegister":
        """Create a new register, deriving category_id and sha from strings."""
        h_clamped = max(ALPHA, min(H_MAX, heat))
        flags = ClassFlags.heat_class(h_clamped) | extra_flags
        return cls(
            node_id=node_id,
            category_id=category_hash(category),
            timestamp_s=int(time.time()),
            heat=h_clamped,
            class_flags=flags,
            routing_hint=routing_hint,
            content_sha_lo32=sha_lo32(content),
        )

    # ── mutations ─────────────────────────────────────────────────────────

    def decay(self, ticks: int = 1, gamma: float = 0.995) -> "SLPRegister":
        """Return a new register with heat decayed by N ticks."""
        from dataclasses import replace
        return replace(
            self,
            heat=heat_decay(self.heat, ticks, gamma),
            class_flags=ClassFlags.heat_class(self.heat) | (
                self.class_flags & (ClassFlags.SCAR | ClassFlags.GOAL | ClassFlags.PLAYGROUND)
            ),
        )

    def with_heat(self, new_heat: float) -> "SLPRegister":
        """Return a new register with updated heat (and re-derived heat class)."""
        from dataclasses import replace
        h = max(ALPHA, min(H_MAX, new_heat))
        sticky = self.class_flags & (ClassFlags.SCAR | ClassFlags.GOAL | ClassFlags.PLAYGROUND)
        return replace(
            self,
            heat=h,
            class_flags=ClassFlags.heat_class(h) | sticky,
        )

    # ── inspection ────────────────────────────────────────────────────────

    def is_hot(self) -> bool:
        return bool(self.class_flags & ClassFlags.HOT)

    def is_scar(self) -> bool:
        return bool(self.class_flags & ClassFlags.SCAR)

    def is_goal(self) -> bool:
        return bool(self.class_flags & ClassFlags.GOAL)

    def is_playground(self) -> bool:
        return bool(self.class_flags & ClassFlags.PLAYGROUND)

    def age_seconds(self) -> int:
        return int(time.time()) - self.timestamp_s

    def __repr__(self) -> str:
        return (
            f"SLP(node={self.node_id:#010x} cat={self.category_id:#010x} "
            f"H={self.heat:.4f} flags={self.class_flags.name} "
            f"hint={self.routing_hint.name} ts={self.timestamp_s})"
        )


# ── L1-cache-line batch ───────────────────────────────────────────────────────

CACHE_LINE_BYTES = 64
HEADER_BYTES = 4
SLP_PER_LINE = 3  # (64 - 4) // 20 = 3

assert HEADER_BYTES + SLP_PER_LINE * 20 == 64, "Cache line packing check failed"


def pack_cache_line(registers: list[SLPRegister], seq: int = 0) -> bytes:
    """
    Pack up to 3 SLPRegisters + a 4-byte header into one 64-byte cache line.
    Header: [uint16 seq_lo, uint8 count, uint8 reserved]
    """
    assert len(registers) <= SLP_PER_LINE
    header = struct.pack("<HBB", seq & 0xFFFF, len(registers), 0)
    payload = b"".join(r.pack() for r in registers)
    # zero-pad to fill the line
    payload = payload.ljust(SLP_PER_LINE * 20, b"\x00")
    return header + payload


def unpack_cache_line(data: bytes) -> tuple[int, list[SLPRegister]]:
    """
    Unpack a 64-byte cache line.
    Returns (seq, list_of_registers).
    """
    assert len(data) == CACHE_LINE_BYTES
    seq, count, _ = struct.unpack_from("<HBB", data, 0)
    registers = []
    for i in range(count):
        offset = HEADER_BYTES + i * 20
        registers.append(SLPRegister.unpack(data[offset:offset + 20]))
    return seq, registers
