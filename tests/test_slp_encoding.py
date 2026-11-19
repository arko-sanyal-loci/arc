"""SLP register codec: heat survives uint16 encoding and cache lines round-trip."""
import pytest

from loci.slp_encoding import (
    ALPHA, H_MAX, UINT16_MAX, CACHE_LINE_BYTES, ClassFlags, RoutingHint, SLPRegister,
    heat_to_u16, pack_cache_line, u16_to_heat, unpack_cache_line,
)

STEP = (H_MAX - ALPHA) / UINT16_MAX


@pytest.mark.parametrize("h", [ALPHA, 0.35, 0.70, 0.80, H_MAX])
def test_heat_round_trip(h):
    assert abs(u16_to_heat(heat_to_u16(h)) - h) <= STEP


def test_heat_clamped_to_open_interval():
    assert u16_to_heat(heat_to_u16(0.0)) == pytest.approx(ALPHA)
    assert u16_to_heat(heat_to_u16(2.0)) == pytest.approx(H_MAX)


def test_cache_line_round_trip():
    regs = [
        SLPRegister(node_id=i, category_id=7, timestamp_s=1_700_000_000 + i, heat=0.5 + i / 10,
                    class_flags=ClassFlags(0), routing_hint=RoutingHint(0), content_sha_lo32=0xABCD + i)
        for i in range(3)
    ]
    line = pack_cache_line(regs, seq=42)
    assert len(line) == CACHE_LINE_BYTES
    seq, out = unpack_cache_line(line)
    assert seq == 42
    assert [r.node_id for r in out] == [0, 1, 2]
    for a, b in zip(regs, out):
        assert abs(a.heat - b.heat) <= STEP
        assert a.content_sha_lo32 == b.content_sha_lo32
