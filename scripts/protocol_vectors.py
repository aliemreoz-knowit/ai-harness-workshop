#!/usr/bin/env python3
"""
protocol_vectors.py -- independent Python implementation of PROTOCOL.md that
generates the conformance vectors used by the C unit tests and the fuzz seeds.

Outputs (all committed):
  tests/vectors/crc.json, header.json, nack.json, tlv.json, rle.json
  tests/vectors/ts_vectors.h / ts_vectors.c   (same data as C arrays)
  fuzz/corpus/{pkt_parse,ctrl_parse,codec_decode}/*.bin

Run from the repository root:  python3 scripts/protocol_vectors.py
The implementation here is deliberately written from PROTOCOL.md only; it
shares no code with src/.  Keep it that way.
"""
import json
import os
import struct
import sys

PROTOCOL_VERSION = 2
W, H = 320, 240
FRAME_BYTES = W * H
MTU = 1400
HDR_LEN = 24
MAX_FRAGS = 110
ENC_MAX = 2 * W * H + 64
NACK_MAX = 32
TLV_MAX_LEN = 512
TILE = 16
TILE_THRESH = 16
MAGIC_DATA = 0x5453
MAGIC_NACK = 0x4E4B
F_KEYFRAME = 0x01
F_RETRANSMIT = 0x02
KIND_K = 0x4B
KIND_P = 0x50

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VEC_DIR = os.path.join(ROOT, "tests", "vectors")
CORPUS_DIR = os.path.join(ROOT, "fuzz", "corpus")


# ---------------------------------------------------------------- CRC32 ----
_CRC_TABLE = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ 0xEDB88320 if (_c & 1) else (_c >> 1)
    _CRC_TABLE.append(_c)


def crc32(data: bytes) -> int:
    c = 0xFFFFFFFF
    for b in data:
        c = _CRC_TABLE[(c ^ b) & 0xFF] ^ (c >> 8)
    return c ^ 0xFFFFFFFF


# ------------------------------------------------------- serial arithmetic ----
def seq_diff(a: int, b: int) -> int:
    d = (a - b) & 0xFFFF
    return d - 0x10000 if d >= 0x8000 else d


def seq_after(a: int, b: int) -> bool:
    return seq_diff(a, b) > 0


# ---------------------------------------------------------- DATA header ----
def hdr_pack(ver, flags, frame_seq, pkt_seq, frag_idx, frag_count, frame_len,
             payload_len, ts_ms, payload, reserved=0, crc_override=None, magic=MAGIC_DATA):
    head = struct.pack("<HBBHHBBIHHH", magic, ver, flags, frame_seq, pkt_seq,
                       frag_idx, frag_count, frame_len, payload_len, ts_ms, reserved)
    assert len(head) == 20
    crc = crc32(head + payload[:payload_len]) if crc_override is None else crc_override
    return head + struct.pack("<I", crc) + payload, crc


def hdr_validate(pkt: bytes):
    """Returns (error_name or None, fields)."""
    if len(pkt) < HDR_LEN:
        return "TS_E_TRUNC", None
    (magic, ver, flags, frame_seq, pkt_seq, frag_idx, frag_count, frame_len,
     payload_len, ts_ms, _res, crc) = struct.unpack("<HBBHHBBIHHHI", pkt[:HDR_LEN])
    if magic != MAGIC_DATA:
        return "TS_E_BADMAGIC", None
    if ver != PROTOCOL_VERSION:
        return "TS_E_BADVER", None
    if payload_len == 0 or payload_len > MTU:
        return "TS_E_INVAL", None
    if len(pkt) < HDR_LEN + payload_len:
        return "TS_E_TRUNC", None
    if len(pkt) != HDR_LEN + payload_len:
        return "TS_E_INVAL", None
    if frag_count == 0 or frag_count > MAX_FRAGS or frag_idx >= frag_count:
        return "TS_E_INVAL", None
    if frame_len == 0 or frame_len > ENC_MAX:
        return "TS_E_INVAL", None
    if frag_idx * MTU + payload_len > frame_len:
        return "TS_E_INVAL", None
    if frag_idx != frag_count - 1 and payload_len != MTU:
        return "TS_E_INVAL", None
    if frag_idx == frag_count - 1 and frag_idx * MTU + payload_len != frame_len:
        return "TS_E_INVAL", None
    if crc32(pkt[:20] + pkt[HDR_LEN:HDR_LEN + payload_len]) != crc:
        return "TS_E_BADCRC", None
    return None, dict(ver=ver, flags=flags, frame_seq=frame_seq, pkt_seq=pkt_seq,
                      frag_idx=frag_idx, frag_count=frag_count, frame_len=frame_len,
                      payload_len=payload_len, ts_ms=ts_ms, crc32=crc)


# ------------------------------------------------------------------ NACK ----
def nack_pack(entries, count_override=None, magic=MAGIC_NACK):
    count = len(entries) if count_override is None else count_override
    out = struct.pack("<HBB", magic, count, 0)
    for fs, fi in entries:
        out += struct.pack("<HBB", fs, fi, 0)
    return out


# ------------------------------------------------------------------- TLV ----
TLV = dict(HELLO=1, WELCOME=2, STATS=3, KEYFRAME_REQ=4, SET_RATE=5, BYE=6)


def tlv_pack(t, value: bytes, flags=0):
    return struct.pack("<BBH", t, flags, len(value)) + value


def hello(udp_port, kbps):
    return tlv_pack(TLV["HELLO"], struct.pack("<HI", udp_port, kbps))


def welcome(w, h, mtu, stream_id, ring_slots):
    return tlv_pack(TLV["WELCOME"], struct.pack("<HHHIH", w, h, mtu, stream_id, ring_slots))


def stats(g, clients):
    v = struct.pack("<10I", *[g[k] for k in ("uptime_ms", "frames_captured", "frames_sent",
                                              "keyframes_sent", "pkts_sent", "bytes_sent",
                                              "nacks_rx", "retx_sent", "nack_too_old",
                                              "ctrl_errors")])
    v += struct.pack("<B3x", len(clients))
    for c in clients:
        v += struct.pack("<BBH8I", c["id"], c["state"], c["udp_port"], c["kbps"], c["pkts_sent"],
                         c["bytes_sent"], c["nacks_rx"], c["retx_sent"], c["retx_dropped"],
                         c["keyframe_reqs"], c["last_seen_ms"])
    assert len(v) == 44 + 36 * len(clients)
    return tlv_pack(TLV["STATS"], v)


def set_rate(kbps):
    return tlv_pack(TLV["SET_RATE"], struct.pack("<I", kbps))


# ------------------------------------------------------------------- RLE ----
def _runlen(s, p, n):
    v = s[p]
    r = 1
    while p + r < n and s[p + r] == v and r < 255:
        r += 1
    return r


def rle_encode(s: bytes) -> bytes:
    n = len(s)
    out = bytearray()
    pos = 0
    while pos < n:
        run = _runlen(s, pos, n)
        if run >= 2:
            out += bytes((run, s[pos]))
            pos += run
        else:
            lit = 1
            while pos + lit < n and lit < 255 and _runlen(s, pos + lit, n) < 3:
                lit += 1
            out += bytes((0, lit)) + s[pos:pos + lit]
            pos += lit
    return bytes(out)


def rle_decode(data: bytes, n: int):
    """Returns (error or None, symbols)."""
    out = bytearray()
    i = 0
    while len(out) < n:
        if i >= len(data):
            return "TS_E_TRUNC", None
        t = data[i]
        i += 1
        if t == 0:
            if i >= len(data):
                return "TS_E_TRUNC", None
            ln = data[i]
            i += 1
            if ln == 0 or len(out) + ln > n:
                return "TS_E_CORRUPT", None
            if i + ln > len(data):
                return "TS_E_TRUNC", None
            out += data[i:i + ln]
            i += ln
        else:
            if i >= len(data):
                return "TS_E_TRUNC", None
            if len(out) + t > n:
                return "TS_E_CORRUPT", None
            out += bytes((data[i],)) * t
            i += 1
    if i != len(data):
        return "TS_E_CORRUPT", None
    return None, bytes(out)


# ----------------------------------------------------------------- codec ----
def codec_hdr(kind, quality, frame_seq, ref_seq, rows):
    return struct.pack("<BBHHH", kind, quality, frame_seq, ref_seq, rows)


def encode_frame(cur: bytes, ref, kind, q, frame_seq, ref_seq):
    rows = H // q
    syms = bytearray()
    for r in range(0, H, q):
        row = cur[r * W:(r + 1) * W]
        if kind == KIND_P:
            rrow = ref[r * W:(r + 1) * W]
            row = bytes(a ^ b for a, b in zip(row, rrow))
        syms += row
    enc = codec_hdr(kind, q, frame_seq, ref_seq if kind == KIND_P else frame_seq, rows) + rle_encode(bytes(syms))
    # reconstructed reference
    rec = bytearray(FRAME_BYTES)
    for r in range(0, H, q):
        row = cur[r * W:(r + 1) * W]
        for k in range(q):
            rec[(r + k) * W:(r + k + 1) * W] = row
    return enc, bytes(rec)


def changed_tiles(a, b):
    n = 0
    for ty in range(H // TILE):
        for tx in range(W // TILE):
            changed = False
            for y in range(ty * TILE, (ty + 1) * TILE):
                base = y * W + tx * TILE
                for x in range(TILE):
                    if abs(a[base + x] - b[base + x]) > TILE_THRESH:
                        changed = True
                        break
                if changed:
                    break
            n += 1 if changed else 0
    return n


def gen_gradient(shift):
    return bytes(((x + 2 * y + shift) & 0xFF) for y in range(H) for x in range(W))


def gen_noise(seed):
    s = seed or 1
    out = bytearray(FRAME_BYTES)
    for i in range(FRAME_BYTES):
        s ^= (s << 13) & 0xFFFFFFFF
        s ^= s >> 17
        s ^= (s << 5) & 0xFFFFFFFF
        out[i] = s & 0xFF
    return bytes(out)


def gen_bars(width):
    return bytes((255 if (x // width) % 2 else 0) for y in range(H) for x in range(W))


GENERATORS = {
    "gradient0": lambda: gen_gradient(0),
    "gradient1": lambda: gen_gradient(1),
    "gradient40": lambda: gen_gradient(40),
    "noise7": lambda: gen_noise(7),
    "bars8": lambda: gen_bars(8),
}


# --------------------------------------------------------------- vectors ----
def hx(b: bytes) -> str:
    return b.hex()


def build_crc():
    cases = [
        ("empty", b""),
        ("check", b"123456789"),
        ("a", b"a"),
        ("zeros32", bytes(32)),
        ("bytes0_255", bytes(range(256))),
        ("fox", b"The quick brown fox jumps over the lazy dog"),
    ]
    return [dict(name=n, data=hx(d), crc32="%08x" % crc32(d)) for n, d in cases]


def build_header():
    payload = bytes(i & 0xFF for i in range(MTU))
    vec = []

    def add(name, pkt, **kw):
        err, fields = hdr_validate(pkt)
        entry = dict(name=name, packet=hx(pkt))
        if err:
            entry["error"] = err
        else:
            entry.update(fields)
        vec.append(entry)

    p, _ = hdr_pack(2, F_KEYFRAME, 1, 100, 0, 3, 3000, MTU, 1234, payload)
    add("key_first_fragment", p)
    p, _ = hdr_pack(2, F_KEYFRAME, 1, 102, 2, 3, 3000, 200, 1234, payload[:200])
    add("key_last_fragment", p)
    p, _ = hdr_pack(2, F_RETRANSMIT, 7, 55, 0, 1, 10, 10, 65000, payload[:10])
    add("retransmit_single", p)
    p, _ = hdr_pack(2, 0, 0xFFFF, 0xFFFF, 0, 1, 1, 1, 0xFFFF, b"\x42")
    add("all_counters_at_wrap", p)
    p, _ = hdr_pack(2, 0, 10, 20, 109, 110, 110 * MTU, MTU, 5, payload)
    add("max_fragments", p)
    p, _ = hdr_pack(2, 0, 3, 4, 0, 1, 16, 16, 9, payload[:16], reserved=0xBEEF)
    add("reserved_nonzero_ignored", p)
    # negatives
    p, _ = hdr_pack(2, 0, 1, 1, 0, 1, 5, 5, 0, payload[:5])
    add("truncated_header", p[:23])
    add("truncated_payload", p[:-1])
    add("extra_bytes", p + b"\x00")
    p, _ = hdr_pack(2, 0, 1, 1, 0, 1, 5, 5, 0, payload[:5], magic=0x5454)
    add("bad_magic", p)
    p, _ = hdr_pack(3, 0, 1, 1, 0, 1, 5, 5, 0, payload[:5])
    add("bad_version", p)
    p, _ = hdr_pack(2, 0, 1, 1, 0, 1, 5, 0, 0, b"")
    add("payload_len_zero", p)
    p, _ = hdr_pack(2, 0, 1, 1, 0, 1, 1401, 1401, 0, payload + b"\x00")
    add("payload_len_over_mtu", p)
    p, _ = hdr_pack(2, 0, 1, 1, 1, 1, 5, 5, 0, payload[:5])
    add("frag_idx_ge_count", p)
    p, _ = hdr_pack(2, 0, 1, 1, 0, 0, 5, 5, 0, payload[:5])
    add("frag_count_zero", p)
    p, _ = hdr_pack(2, 0, 1, 1, 0, 111, 111 * MTU, MTU, 0, payload)
    add("frag_count_over_max", p)
    p, _ = hdr_pack(2, 0, 1, 1, 0, 1, 0, 5, 0, payload[:5])
    add("frame_len_zero", p)
    p, _ = hdr_pack(2, 0, 1, 1, 0, 1, ENC_MAX + 1, 5, 0, payload[:5])
    add("frame_len_over_max", p)
    p, _ = hdr_pack(2, 0, 1, 1, 0, 2, 3000, 100, 0, payload[:100])
    add("middle_fragment_short", p)
    p, _ = hdr_pack(2, 0, 1, 1, 1, 2, 3000, 100, 0, payload[:100])
    add("last_fragment_len_mismatch", p)
    p, _ = hdr_pack(2, 0, 1, 1, 0, 1, 5, 5, 0, payload[:5], crc_override=0x12345678)
    add("bad_crc", p)
    p, _ = hdr_pack(2, 0, 1, 1, 0, 1, 5, 5, 0, payload[:5])
    add("payload_bitflip_bad_crc", p[:-1] + bytes((p[-1] ^ 0x01,)))
    return vec


def build_nack():
    vec = []

    def add(name, pkt, entries=None, error=None):
        e = dict(name=name, packet=hx(pkt))
        if error:
            e["error"] = error
        else:
            e["entries"] = [dict(frame_seq=fs, frag_idx=fi) for fs, fi in entries]
        vec.append(e)

    add("empty", nack_pack([]), [])
    add("one", nack_pack([(1, 2)]), [(1, 2)])
    thirty_two = [((i * 977) & 0xFFFF, i % MAX_FRAGS) for i in range(32)]
    add("max32", nack_pack(thirty_two), thirty_two)
    add("wrap_entries", nack_pack([(0xFFFF, 109), (0, 0)]), [(0xFFFF, 109), (0, 0)])
    add("count_33", nack_pack([(1, 1)] * 33), error="TS_E_INVAL")
    add("truncated_entry", nack_pack([(1, 1), (2, 2)])[:-2], error="TS_E_TRUNC")
    add("truncated_header", nack_pack([])[:3], error="TS_E_TRUNC")
    add("bad_magic", nack_pack([(1, 1)], magic=0x4E4C), error="TS_E_BADMAGIC")
    add("extra_bytes", nack_pack([(1, 1)]) + b"\x00", error="TS_E_INVAL")
    add("count_mismatch_long", nack_pack([(1, 1), (2, 2)], count_override=1), error="TS_E_INVAL")
    return vec


def build_tlv():
    g = dict(uptime_ms=12345, frames_captured=300, frames_sent=299, keyframes_sent=31,
             pkts_sent=4000, bytes_sent=5000000, nacks_rx=12, retx_sent=11, nack_too_old=1,
             ctrl_errors=0)
    c0 = dict(id=0, state=1, udp_port=6000, kbps=4000, pkts_sent=2000, bytes_sent=2500000,
              nacks_rx=7, retx_sent=7, retx_dropped=0, keyframe_reqs=1, last_seen_ms=3)
    c1 = dict(id=2, state=2, udp_port=6001, kbps=2000, pkts_sent=2000, bytes_sent=2500000,
              nacks_rx=5, retx_sent=4, retx_dropped=1, keyframe_reqs=0, last_seen_ms=1999)
    msgs = [
        ("hello", hello(6000, 4000), TLV["HELLO"], 6),
        ("welcome", welcome(W, H, MTU, 0xC0FFEE42, 448), TLV["WELCOME"], 12),
        ("stats_no_clients", stats(g, []), TLV["STATS"], 44),
        ("stats_two_clients", stats(g, [c0, c1]), TLV["STATS"], 116),
        ("keyframe_req", tlv_pack(TLV["KEYFRAME_REQ"], b""), TLV["KEYFRAME_REQ"], 0),
        ("set_rate", set_rate(1500), TLV["SET_RATE"], 4),
        ("bye", tlv_pack(TLV["BYE"], b""), TLV["BYE"], 0),
        ("flags_ignored", tlv_pack(TLV["KEYFRAME_REQ"], b"", flags=0xFF), TLV["KEYFRAME_REQ"], 0),
    ]
    vec = [dict(name=n, data=hx(d), type=t, len=l) for n, d, t, l in msgs]
    vec.append(dict(name="len_over_max", data=hx(tlv_pack(TLV["HELLO"], bytes(TLV_MAX_LEN + 1))),
                    error="TS_E_TOOLONG"))
    vec.append(dict(name="max_len_ok", data=hx(tlv_pack(99, bytes(TLV_MAX_LEN))), type=99, len=TLV_MAX_LEN))
    stream = hello(6000, 0) + tlv_pack(TLV["KEYFRAME_REQ"], b"") + set_rate(2500) + tlv_pack(TLV["BYE"], b"")
    vec.append(dict(name="glued_stream", data=hx(stream),
                    chunks=[1, 2, 3, 5, 4, 6, 2, 1],
                    expect=[dict(type=TLV["HELLO"], len=6), dict(type=TLV["KEYFRAME_REQ"], len=0),
                            dict(type=TLV["SET_RATE"], len=4), dict(type=TLV["BYE"], len=0)]))
    return vec, dict(hello=hello(6000, 4000), welcome=welcome(W, H, MTU, 0xC0FFEE42, 448),
                     stats=stats(g, [c0, c1]), set_rate=set_rate(1500), stream=stream)


def build_rle():
    sym_cases = [
        ("empty", b""),
        ("run4", b"aaaa"),
        ("lit3", b"abc"),
        ("run2_lit1", b"aab"),
        ("lit1_run3", b"abbb"),
        ("lit4_with_pair", b"abbc"),
        ("run300", b"x" * 300),
        ("lit256", bytes(range(256))),
        ("run255_boundary", b"z" * 255),
        ("alternating", b"ab" * 150),
        ("aab_pattern_worst", b"aab" * 100),
    ]
    vec = []
    for n, s in sym_cases:
        enc = rle_encode(s)
        err, dec = rle_decode(enc, len(s))
        assert err is None and dec == s, n
        vec.append(dict(name=n, symbols=hx(s), n=len(s), encoded=hx(enc)))
    negatives = [
        ("literal_len_zero", b"\x00\x00", 1, "TS_E_CORRUPT"),
        ("run_exceeds_n", b"\x05\x41", 4, "TS_E_CORRUPT"),
        ("literal_exceeds_n", b"\x00\x03abc", 2, "TS_E_CORRUPT"),
        ("truncated_run_token", b"\x05", 5, "TS_E_TRUNC"),
        ("truncated_literal", b"\x00\x03ab", 3, "TS_E_TRUNC"),
        ("trailing_bytes", b"\x02\x41\x00", 2, "TS_E_CORRUPT"),
        ("empty_input_n1", b"", 1, "TS_E_TRUNC"),
        ("run_to_255_then_stop", b"\xff\x41\x01", 256, "TS_E_TRUNC"),
    ]
    for n, d, cnt, err in negatives:
        got, _ = rle_decode(d, cnt)
        assert got == err, (n, got, err)
        vec.append(dict(name=n, encoded=hx(d), n=cnt, error=err))
    # codec header
    hdrs = [
        ("hdr_key_q1", codec_hdr(KIND_K, 1, 1, 1, 240), None),
        ("hdr_p_q2", codec_hdr(KIND_P, 2, 3, 2, 120), None),
        ("hdr_p_q4_wrap", codec_hdr(KIND_P, 4, 0, 0xFFFF, 60), None),
        ("hdr_bad_kind", codec_hdr(0x51, 1, 1, 1, 240), "TS_E_CORRUPT"),
        ("hdr_bad_quality", codec_hdr(KIND_K, 3, 1, 1, 80), "TS_E_CORRUPT"),
        ("hdr_rows_mismatch", codec_hdr(KIND_K, 2, 1, 1, 240), "TS_E_CORRUPT"),
        ("hdr_truncated", codec_hdr(KIND_K, 1, 1, 1, 240)[:7], "TS_E_TRUNC"),
    ]
    hvec = []
    for n, d, err in hdrs:
        e = dict(name=n, data=hx(d))
        if err:
            e["error"] = err
        else:
            k, q, fs, rs, rows = struct.unpack("<BBHHH", d)
            e.update(kind=k, quality=q, frame_seq=fs, ref_seq=rs, rows=rows)
        hvec.append(e)
    # full frames
    frames = []
    seqs = [
        ("f1_key_gradient", "gradient0", KIND_K, 1, 1),
        ("f2_p_gradient_plus1", "gradient1", KIND_P, 1, 2),
        ("f3_p_q2_gradient_plus40", "gradient40", KIND_P, 2, 3),
        ("f4_key_q4_noise", "noise7", KIND_K, 4, 4),
        ("f5_p_bars", "bars8", KIND_P, 1, 5),
    ]
    ref = None
    last_seq = 0
    encs = {}
    for name, gen, kind, q, seq in seqs:
        cur = GENERATORS[gen]()
        enc, rec = encode_frame(cur, ref, kind, q, seq, last_seq)
        # PROTOCOL.md 6.6: the frame as reconstructed at its quality vs the reconstructed reference
        tiles = changed_tiles(rec, ref) if ref is not None else (W // TILE) * (H // TILE)
        assert len(enc) <= ENC_MAX
        # decode check with the independent decoder
        k, qq, fs, rs, rows = struct.unpack("<BBHHH", enc[:8])
        err, syms = rle_decode(enc[8:], rows * W)
        assert err is None
        out = bytearray(FRAME_BYTES)
        for i, r in enumerate(range(0, H, qq)):
            row = syms[i * W:(i + 1) * W]
            if k == KIND_P:
                row = bytes(a ^ b for a, b in zip(row, ref[r * W:(r + 1) * W]))
            for kk in range(qq):
                out[(r + kk) * W:(r + kk + 1) * W] = row
        assert bytes(out) == rec
        frames.append(dict(name=name, generator=gen, kind="K" if kind == KIND_K else "P",
                           quality=q, frame_seq=seq, ref_seq=(last_seq if kind == KIND_P else seq),
                           enc_len=len(enc), enc_crc32="%08x" % crc32(enc),
                           dec_crc32="%08x" % crc32(rec), changed_tiles=tiles,
                           raw_crc32="%08x" % crc32(cur)))
        encs[name] = enc
        ref = rec
        last_seq = seq
    return vec, hvec, frames, encs


# ------------------------------------------------------------- C output ----
def c_bytes(b: bytes, per_line=16):
    if not b:
        return "    0U"
    lines = []
    for i in range(0, len(b), per_line):
        lines.append("    " + ", ".join("0x%02XU" % x for x in b[i:i + per_line]))
    return ",\n".join(lines)


def write_c(crc, hdr, nack, tlv, rle, hvec, frames):
    h = []
    c = []
    h.append("/* SPDX-License-Identifier: MIT */")
    h.append("/* GENERATED by scripts/protocol_vectors.py -- do not edit. PROTOCOL_VERSION %d */" % PROTOCOL_VERSION)
    h.append("#ifndef TS_VECTORS_H\n#define TS_VECTORS_H\n#include <stdint.h>\n#include <stddef.h>\n")
    h.append("#define TSV_PROTOCOL_VERSION %dU\n" % PROTOCOL_VERSION)
    h.append("typedef struct { const char *name; const uint8_t *data; size_t len; uint32_t crc; } tsv_crc_t;")
    h.append("typedef struct { const char *name; const uint8_t *pkt; size_t len; const char *error;\n"
             "    uint8_t ver; uint8_t flags; uint16_t frame_seq; uint16_t pkt_seq; uint8_t frag_idx; uint8_t frag_count;\n"
             "    uint32_t frame_len; uint16_t payload_len; uint16_t ts_ms; uint32_t crc32; } tsv_hdr_t;")
    h.append("typedef struct { uint16_t frame_seq; uint8_t frag_idx; } tsv_nack_entry_t;")
    h.append("typedef struct { const char *name; const uint8_t *pkt; size_t len; const char *error;\n"
             "    uint8_t count; const tsv_nack_entry_t *entries; } tsv_nack_t;")
    h.append("typedef struct { const char *name; const uint8_t *data; size_t len; const char *error;\n"
             "    uint8_t type; uint16_t vlen; } tsv_tlv_t;")
    h.append("typedef struct { uint8_t type; uint16_t len; } tsv_tlv_expect_t;")
    h.append("typedef struct { const uint8_t *data; size_t len; const uint8_t *chunks; size_t nchunks;\n"
             "    const tsv_tlv_expect_t *expect; size_t nexpect; } tsv_tlv_stream_t;")
    h.append("typedef struct { const char *name; const uint8_t *symbols; size_t n; const uint8_t *encoded; size_t enc_len;\n"
             "    const char *error; } tsv_rle_t;")
    h.append("typedef struct { const char *name; const uint8_t *data; size_t len; const char *error;\n"
             "    uint8_t kind; uint8_t quality; uint16_t frame_seq; uint16_t ref_seq; uint16_t rows; } tsv_codec_hdr_t;")
    h.append("typedef struct { const char *name; const char *generator; char kind; uint8_t quality;\n"
             "    uint16_t frame_seq; uint16_t ref_seq; uint32_t enc_len; uint32_t enc_crc32; uint32_t dec_crc32;\n"
             "    uint32_t raw_crc32; uint16_t changed_tiles; } tsv_frame_t;\n")
    h.append("/* Frame generators (320x240 gray8), replicated by tests/support/ts_vectors_gen.c:\n"
             " *  gradientS : px(x,y) = (x + 2*y + S) & 0xFF\n"
             " *  noiseS    : xorshift32 seeded with S, px = state & 0xFF per pixel (state updated before each pixel)\n"
             " *  barsN     : px = 255 when (x / N) is odd else 0\n"
             " * changed_tiles of a keyframe without reference = TS_TILES (300). */")

    c.append("/* SPDX-License-Identifier: MIT */")
    c.append("/* GENERATED by scripts/protocol_vectors.py -- do not edit. */")
    c.append('#include "ts_vectors.h"\n')

    # crc
    for i, v in enumerate(crc):
        c.append("static const uint8_t crc_%d[] = {\n%s\n};" % (i, c_bytes(bytes.fromhex(v["data"]))))
    c.append("const tsv_crc_t tsv_crc[] = {")
    for i, v in enumerate(crc):
        c.append('    { "%s", crc_%d, %dU, 0x%sU },' % (v["name"], i, len(bytes.fromhex(v["data"])), v["crc32"].upper()))
    c.append("};\nconst size_t tsv_crc_count = %d;\n" % len(crc))
    h.append("#define TSV_CRC_COUNT %dU\nextern const tsv_crc_t tsv_crc[%d];\nextern const size_t tsv_crc_count;" % (len(crc), len(crc)))

    # header
    for i, v in enumerate(hdr):
        c.append("static const uint8_t hdr_%d[] = {\n%s\n};" % (i, c_bytes(bytes.fromhex(v["packet"]))))
    c.append("const tsv_hdr_t tsv_hdr[] = {")
    for i, v in enumerate(hdr):
        ln = len(bytes.fromhex(v["packet"]))
        if "error" in v:
            c.append('    { "%s", hdr_%d, %dU, "%s", 0U, 0U, 0U, 0U, 0U, 0U, 0U, 0U, 0U, 0U },' % (v["name"], i, ln, v["error"]))
        else:
            c.append('    { "%s", hdr_%d, %dU, NULL, %dU, %dU, %dU, %dU, %dU, %dU, %dU, %dU, %dU, 0x%08XU },' % (
                v["name"], i, ln, v["ver"], v["flags"], v["frame_seq"], v["pkt_seq"], v["frag_idx"],
                v["frag_count"], v["frame_len"], v["payload_len"], v["ts_ms"], v["crc32"]))
    c.append("};\nconst size_t tsv_hdr_count = %d;\n" % len(hdr))
    h.append("#define TSV_HDR_COUNT %dU\nextern const tsv_hdr_t tsv_hdr[%d];\nextern const size_t tsv_hdr_count;" % (len(hdr), len(hdr)))

    # nack
    for i, v in enumerate(nack):
        c.append("static const uint8_t nack_%d[] = {\n%s\n};" % (i, c_bytes(bytes.fromhex(v["packet"]))))
        if "entries" in v and v["entries"]:
            c.append("static const tsv_nack_entry_t nack_e_%d[] = { %s };" % (
                i, ", ".join("{ %dU, %dU }" % (e["frame_seq"], e["frag_idx"]) for e in v["entries"])))
    c.append("const tsv_nack_t tsv_nack[] = {")
    for i, v in enumerate(nack):
        ln = len(bytes.fromhex(v["packet"]))
        if "error" in v:
            c.append('    { "%s", nack_%d, %dU, "%s", 0U, NULL },' % (v["name"], i, ln, v["error"]))
        else:
            ent = "nack_e_%d" % i if v["entries"] else "NULL"
            c.append('    { "%s", nack_%d, %dU, NULL, %dU, %s },' % (v["name"], i, ln, len(v["entries"]), ent))
    c.append("};\nconst size_t tsv_nack_count = %d;\n" % len(nack))
    h.append("#define TSV_NACK_COUNT %dU\nextern const tsv_nack_t tsv_nack[%d];\nextern const size_t tsv_nack_count;" % (len(nack), len(nack)))

    # tlv
    single = [v for v in tlv if "chunks" not in v]
    streams = [v for v in tlv if "chunks" in v]
    for i, v in enumerate(single):
        c.append("static const uint8_t tlv_%d[] = {\n%s\n};" % (i, c_bytes(bytes.fromhex(v["data"]))))
    c.append("const tsv_tlv_t tsv_tlv[] = {")
    for i, v in enumerate(single):
        ln = len(bytes.fromhex(v["data"]))
        if "error" in v:
            c.append('    { "%s", tlv_%d, %dU, "%s", 0U, 0U },' % (v["name"], i, ln, v["error"]))
        else:
            c.append('    { "%s", tlv_%d, %dU, NULL, %dU, %dU },' % (v["name"], i, ln, v["type"], v["len"]))
    c.append("};\nconst size_t tsv_tlv_count = %d;\n" % len(single))
    h.append("#define TSV_TLV_COUNT %dU\nextern const tsv_tlv_t tsv_tlv[%d];\nextern const size_t tsv_tlv_count;" % (len(single), len(single)))
    for i, v in enumerate(streams):
        c.append("static const uint8_t tlvs_%d[] = {\n%s\n};" % (i, c_bytes(bytes.fromhex(v["data"]))))
        c.append("static const uint8_t tlvs_chunks_%d[] = { %s };" % (i, ", ".join("%dU" % x for x in v["chunks"])))
        c.append("static const tsv_tlv_expect_t tlvs_expect_%d[] = { %s };" % (
            i, ", ".join("{ %dU, %dU }" % (e["type"], e["len"]) for e in v["expect"])))
    c.append("const tsv_tlv_stream_t tsv_tlv_stream[] = {")
    for i, v in enumerate(streams):
        c.append("    { tlvs_%d, %dU, tlvs_chunks_%d, %dU, tlvs_expect_%d, %dU }," % (
            i, len(bytes.fromhex(v["data"])), i, len(v["chunks"]), i, len(v["expect"])))
    c.append("};\nconst size_t tsv_tlv_stream_count = %d;\n" % len(streams))
    h.append("#define TSV_TLV_STREAM_COUNT %dU\nextern const tsv_tlv_stream_t tsv_tlv_stream[%d];\nextern const size_t tsv_tlv_stream_count;" % (len(streams), len(streams)))

    # rle
    for i, v in enumerate(rle):
        if "symbols" in v:
            c.append("static const uint8_t rle_s_%d[] = {\n%s\n};" % (i, c_bytes(bytes.fromhex(v["symbols"]))))
        c.append("static const uint8_t rle_e_%d[] = {\n%s\n};" % (i, c_bytes(bytes.fromhex(v["encoded"]))))
    c.append("const tsv_rle_t tsv_rle[] = {")
    for i, v in enumerate(rle):
        el = len(bytes.fromhex(v["encoded"]))
        if "error" in v:
            c.append('    { "%s", NULL, %dU, rle_e_%d, %dU, "%s" },' % (v["name"], v["n"], i, el, v["error"]))
        else:
            c.append('    { "%s", rle_s_%d, %dU, rle_e_%d, %dU, NULL },' % (v["name"], i, v["n"], i, el))
    c.append("};\nconst size_t tsv_rle_count = %d;\n" % len(rle))
    h.append("#define TSV_RLE_COUNT %dU\nextern const tsv_rle_t tsv_rle[%d];\nextern const size_t tsv_rle_count;" % (len(rle), len(rle)))

    # codec header
    for i, v in enumerate(hvec):
        c.append("static const uint8_t chdr_%d[] = {\n%s\n};" % (i, c_bytes(bytes.fromhex(v["data"]))))
    c.append("const tsv_codec_hdr_t tsv_codec_hdr[] = {")
    for i, v in enumerate(hvec):
        ln = len(bytes.fromhex(v["data"]))
        if "error" in v:
            c.append('    { "%s", chdr_%d, %dU, "%s", 0U, 0U, 0U, 0U, 0U },' % (v["name"], i, ln, v["error"]))
        else:
            c.append('    { "%s", chdr_%d, %dU, NULL, %dU, %dU, %dU, %dU, %dU },' % (
                v["name"], i, ln, v["kind"], v["quality"], v["frame_seq"], v["ref_seq"], v["rows"]))
    c.append("};\nconst size_t tsv_codec_hdr_count = %d;\n" % len(hvec))
    h.append("#define TSV_CODEC_HDR_COUNT %dU\nextern const tsv_codec_hdr_t tsv_codec_hdr[%d];\nextern const size_t tsv_codec_hdr_count;" % (len(hvec), len(hvec)))

    # frames
    c.append("const tsv_frame_t tsv_frame[] = {")
    for v in frames:
        c.append("    { \"%s\", \"%s\", '%s', %dU, %dU, %dU, %dU, 0x%sU, 0x%sU, 0x%sU, %dU }," % (
            v["name"], v["generator"], v["kind"], v["quality"], v["frame_seq"], v["ref_seq"],
            v["enc_len"], v["enc_crc32"].upper(), v["dec_crc32"].upper(), v["raw_crc32"].upper(), v["changed_tiles"]))
    c.append("};\nconst size_t tsv_frame_count = %d;\n" % len(frames))
    h.append("#define TSV_FRAME_COUNT %dU\nextern const tsv_frame_t tsv_frame[%d];\nextern const size_t tsv_frame_count;" % (len(frames), len(frames)))

    h.append("\n#endif /* TS_VECTORS_H */")
    with open(os.path.join(VEC_DIR, "ts_vectors.h"), "w", newline="\n") as f:
        f.write("\n".join(h) + "\n")
    with open(os.path.join(VEC_DIR, "ts_vectors.c"), "w", newline="\n") as f:
        f.write("\n".join(c) + "\n")


def write_json(name, obj):
    with open(os.path.join(VEC_DIR, name), "w", newline="\n") as f:
        json.dump(dict(protocol_version=PROTOCOL_VERSION, generator="scripts/protocol_vectors.py",
                       vectors=obj), f, indent=1)
        f.write("\n")


def write_corpus(hdr, nack, tlvbin, encs):
    for d in ("pkt_parse", "ctrl_parse", "codec_decode"):
        os.makedirs(os.path.join(CORPUS_DIR, d), exist_ok=True)
    for v in hdr:
        with open(os.path.join(CORPUS_DIR, "pkt_parse", "hdr_%s.bin" % v["name"]), "wb") as f:
            f.write(bytes.fromhex(v["packet"]))
    for v in nack:
        with open(os.path.join(CORPUS_DIR, "pkt_parse", "nack_%s.bin" % v["name"]), "wb") as f:
            f.write(bytes.fromhex(v["packet"]))
    for k, b in tlvbin.items():
        with open(os.path.join(CORPUS_DIR, "ctrl_parse", "%s.bin" % k), "wb") as f:
            f.write(b)
    # codec seeds: two vector encodings (77 KB, full-size runs) plus small valid
    # full frames (a constant frame RLE-encodes to < 1 KB): a keyframe, P-frames
    # against it (ref_seq 0x0101 = the keyframe fuzz_codec_decode primes with),
    # and q2 / q4 variants.
    for k in ("f1_key_gradient", "f5_p_bars"):
        with open(os.path.join(CORPUS_DIR, "codec_decode", "%s.bin" % k), "wb") as f:
            f.write(encs[k])
    const = bytes([0x80]) * FRAME_BYTES
    box = bytearray(const)
    for y in range(64, 80):
        for x in range(16, 32):
            box[y * W + x] = 0x20
    box = bytes(box)
    k_enc, k_rec = encode_frame(const, None, KIND_K, 1, 0x0101, 0x0101)
    small = {
        "small_key_const": k_enc,
        "small_p_box": encode_frame(box, k_rec, KIND_P, 1, 0x0102, 0x0101)[0],
        "small_p_q2_box": encode_frame(box, k_rec, KIND_P, 2, 0x0102, 0x0101)[0],
        "small_key_q4_box": encode_frame(box, None, KIND_K, 4, 0x0103, 0x0103)[0],
    }
    for k, b in small.items():
        assert len(b) < 4096
        with open(os.path.join(CORPUS_DIR, "codec_decode", "%s.bin" % k), "wb") as f:
            f.write(b)


def main():
    os.makedirs(VEC_DIR, exist_ok=True)
    assert crc32(b"123456789") == 0xCBF43926
    assert seq_after(1, 0xFFFF) and seq_diff(1, 0xFFFF) == 2 and not seq_after(0xFFFF, 1)
    assert seq_diff(0x8000, 0) == -32768
    crc = build_crc()
    hdr = build_header()
    nack = build_nack()
    tlv, tlvbin = build_tlv()
    rle, hvec, frames, encs = build_rle()
    write_json("crc.json", crc)
    write_json("header.json", hdr)
    write_json("nack.json", nack)
    write_json("tlv.json", tlv)
    write_json("rle.json", dict(rle=rle, codec_header=hvec, frames=frames))
    write_c(crc, hdr, nack, tlv, rle, hvec, frames)
    write_corpus(hdr, nack, tlvbin, encs)
    print("vectors: crc=%d header=%d nack=%d tlv=%d rle=%d codec_hdr=%d frames=%d" % (
        len(crc), len(hdr), len(nack), len(tlv), len(rle), len(hvec), len(frames)))
    for fr in frames:
        print("  %-28s enc_len=%6d  tiles=%3d" % (fr["name"], fr["enc_len"], fr["changed_tiles"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
