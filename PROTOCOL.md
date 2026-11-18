# tinystream v2 wire protocol

PROTOCOL_VERSION: 2

This file is the single source of truth for everything that crosses the wire
between `tinystreamd` (the Pi daemon) and `tsrecv` (the laptop receiver).
`include/ts_proto.h` and `include/ts_codec.h` implement exactly what is written
here; `scripts/protocol_vectors.py` is an independent Python implementation that
produces the test vectors in `tests/vectors/*.json`. Any change to a packet
layout, a constant or the bitstream MUST bump `PROTOCOL_VERSION` above and the
`TS_PROTOCOL_VERSION` constant in `include/ts_config.h` (the compliance gate checks that
`PROTOCOL.md` changed together with a version bump when a header changed).

All multi-byte integers are **little-endian**. `uN` means an unsigned N-bit
integer, `iN` a two's-complement signed one. Byte offsets are written `[a..b)`.

---

## 1. Constants

| Name | Value | Meaning |
|---|---|---|
| `TS_PROTOCOL_VERSION` | 2 | value of the `ver` byte in every DATA header |
| `TS_WIDTH` | 320 | frame width in pixels |
| `TS_HEIGHT` | 240 | frame height in pixels |
| `TS_FRAME_BYTES` | 76800 | `TS_WIDTH * TS_HEIGHT`, one gray8 pixel per byte, row-major |
| `TS_MTU_PAYLOAD` | 1400 | maximum DATA payload bytes per UDP packet |
| `TS_HDR_LEN` | 24 | DATA header size |
| `TS_PKT_MAX` | 1424 | `TS_HDR_LEN + TS_MTU_PAYLOAD` |
| `TS_MAX_FRAGS` | 110 | maximum fragments per frame (`110 * 1400 = 154000 >= TS_ENC_MAX`) |
| `TS_ENC_MAX` | 153664 | `2 * TS_WIDTH * TS_HEIGHT + 64`, upper bound of an encoded frame |
| `TS_RING_SLOTS` | 448 | sender history ring (>= 4 frames of 110 fragments) |
| `TS_MAX_CLIENTS` | 4 | simultaneous viewers |
| `TS_NACK_MAX` | 32 | entries per NACK packet |
| `TS_NACK_PKT_MAX` | 132 | `4 + 4 * TS_NACK_MAX` |
| `TS_TLV_HDR_LEN` | 4 | control TLV header size |
| `TS_TLV_MAX_LEN` | 512 | maximum TLV value length accepted by either side |
| `TS_TLV_ACC_CAP` | 1024 | per-connection accumulate buffer (>= `TS_TLV_HDR_LEN + TS_TLV_MAX_LEN`) |
| `TS_TILE` | 16 | tile edge in pixels, `20 x 15 = 300` tiles per frame |
| `TS_TILES` | 300 | tiles per frame |
| `TS_TILE_THRESH` | 16 | a tile is "changed" when any pixel differs from the reference by more than this |
| `TS_QUALITY_MAX` | 4 | quality steps are 1, 2, 4 (row subsampling factor) |
| `TS_KEYFRAME_PERIOD_MS` | 10000 | periodic keyframe interval |
| `TS_KEYFRAME_COOLDOWN_MS` | 1000 | minimum spacing between requested keyframes |
| `TS_CLIENT_TIMEOUT_MS` | 2000 | a client is dropped after this silence on TCP and UDP |
| `TS_REASM_FRAMES` | 8 | receiver frames in flight |
| `TS_FREEZE_MS` | 500 | receiver counts a freeze after this long without a decoded frame |
| `TS_NACK_DELAY_MS` | 20 | receiver waits this long for reordering before NACKing a gap |
| `TS_NACK_RETRY_MS` | 200 | receiver requests a keyframe when a NACK stays unanswered this long |
| `TS_RETX_PER_TICK` | 16 | maximum retransmissions per client per daemon tick |
| `TS_STATS_PERIOD_MS` | 1000 | daemon sends STATS this often |
| `TS_MAGIC_DATA` | 0x5453 | DATA packet magic (u16) |
| `TS_MAGIC_NACK` | 0x4E4B | NACK packet magic (u16) |

---

## 2. CRC32

Standard reflected CRC-32 (IEEE 802.3, as used by zlib and PNG):

- polynomial (reflected) `0xEDB88320`
- initial value `0xFFFFFFFF`
- input and output reflected
- final XOR `0xFFFFFFFF`

Check value: `crc32("123456789") = 0xCBF43926`. `crc32("") = 0x00000000`.

The CRC in a DATA packet covers the header bytes `[0..20)` followed by the
payload bytes `[0..payload_len)` as one continuous message; it does **not**
cover the `crc32` field itself.

---

## 3. UDP DATA packet

One encoded frame is split into `frag_count` fragments of at most
`TS_MTU_PAYLOAD` bytes. Every fragment is one UDP datagram:

```
offset  size  field        type  notes
 0      2     magic        u16   TS_MAGIC_DATA = 0x5453 (bytes 53 54)
 2      1     ver          u8    TS_PROTOCOL_VERSION = 2
 3      1     flags        u8    bit0 TS_F_KEYFRAME, bit1 TS_F_RETRANSMIT, bits 2..7 reserved = 0
 4      2     frame_seq    u16   serial counter of frames, shared by all fragments of a frame
 6      2     pkt_seq      u16   serial counter of transmitted DATA packets (fresh ones only, see 3.2)
 8      1     frag_idx     u8    0 .. frag_count-1
 9      1     frag_count   u8    1 .. TS_MAX_FRAGS
10      4     frame_len    u32   total encoded frame length, 1 .. TS_ENC_MAX
14      2     payload_len  u16   bytes of payload in this packet, 1 .. TS_MTU_PAYLOAD
16      2     ts_ms        u16   capture timestamp of the frame in ms, modulo 65536
18      2     reserved     u16   0 on send, ignored on receive (but covered by the CRC)
20      4     crc32        u32   CRC32 over bytes [0..20) + payload[0..payload_len)
24      ...   payload            payload_len bytes
```

Datagram length is exactly `24 + payload_len`.

### 3.1 Validation (receiver and fuzz targets)

A packet is rejected, in this order, when:

1. the datagram is shorter than 24 bytes (`TS_E_TRUNC`),
2. `magic != 0x5453` (`TS_E_BADMAGIC`),
3. `ver != 2` (`TS_E_BADVER`),
4. `payload_len == 0`, `payload_len > TS_MTU_PAYLOAD`, or the datagram length is
   not `24 + payload_len` (`TS_E_TRUNC` when shorter, `TS_E_INVAL` otherwise),
5. `frag_count == 0`, `frag_count > TS_MAX_FRAGS`, `frag_idx >= frag_count`,
   `frame_len == 0`, `frame_len > TS_ENC_MAX`,
   `frag_idx * TS_MTU_PAYLOAD + payload_len > frame_len`,
   the fragment is not the last one and `payload_len != TS_MTU_PAYLOAD`,
   or the fragment is the last one and `frag_idx * TS_MTU_PAYLOAD + payload_len != frame_len` (`TS_E_INVAL`),
6. the CRC does not match (`TS_E_BADCRC`).

Fragment `i` carries frame bytes `[i * 1400 .. i * 1400 + payload_len)`; every
fragment except the last carries exactly 1400 bytes.

### 3.2 Sequence rules

- `frame_seq` increments by one per encoded frame (keyframe or P-frame) and
  wraps modulo 65536.
- `pkt_seq` is assigned once per fragment when it enters the sender ring
  (consecutive, wrapping modulo 65536) and is shared by all clients: every
  client receives the same packed packet. Fragments a client's cursor skipped
  (a dropped backlog, a ring wrap) therefore appear as `pkt_seq` gaps at that
  receiver. A retransmitted fragment keeps its `pkt_seq` and sets
  `TS_F_RETRANSMIT`; the CRC is recomputed because `flags` changed. The
  receiver uses `pkt_seq` gaps to detect loss and `(frame_seq, frag_idx)` to
  reassemble.
- `ts_ms` is the capture time in milliseconds modulo 65536 and is compared with
  serial arithmetic (section 7).
- The daemon option `--seq-start N` sets the initial `frame_seq` and `pkt_seq`
  so that wrap-around happens within seconds (HIL test).

---

## 4. UDP NACK packet (receiver -> daemon)

```
offset  size  field     type  notes
 0      2     magic     u16   TS_MAGIC_NACK = 0x4E4B (bytes 4B 4E)
 2      1     count     u8    0 .. TS_NACK_MAX (32)
 3      1     reserved  u8    0
 4      4*n   entries         count entries, each:
          2   frame_seq u16
          1   frag_idx  u8
          1   pad       u8    0
```

Datagram length is exactly `4 + 4 * count`. A NACK with `count > 32`, a wrong
magic, or a length that does not match is dropped (`TS_E_INVAL`,
`TS_E_BADMAGIC`, `TS_E_TRUNC`). The source address `(ip, udp_port)` identifies
the client; NACKs from unknown clients are ignored. The daemon answers each
entry it still holds in its ring with a retransmission (`TS_F_RETRANSMIT`),
within the rate rules of section 8; entries older than the ring answer
nothing (`TS_E_TOO_OLD`, counted in `nack_too_old`) and the receiver must
request a keyframe after it has waited `TS_NACK_RETRY_MS` without an answer.

---

## 5. TCP control channel (TLV)

One TCP connection per client, opened by the receiver. The byte stream is a
sequence of TLVs; **a TLV may be split across any number of `recv()` calls and
several TLVs may arrive in one** `recv()`.

```
offset  size  field  type  notes
 0      1     type   u8    see table
 1      1     flags  u8    reserved, 0 on send, ignored on receive
 2      2     len    u16   value length, 0 .. TS_TLV_MAX_LEN
 4      len   value
```

A `len > TS_TLV_MAX_LEN` or an unknown type with a valid length is a protocol
error: the daemon closes the connection (unknown types are *not* skipped, so an
injected `CTRL_SHELL` TLV is a visible failure); the receiver reconnects.

| type | name | direction | value |
|---|---|---|---|
| 1 | HELLO | receiver -> daemon | `udp_port u16`, `kbps u32` (requested rate, 0 = daemon default) |
| 2 | WELCOME | daemon -> receiver | `w u16`, `h u16`, `mtu u16`, `stream_id u32`, `ring_slots u16` |
| 3 | STATS | daemon -> receiver | see 5.1 |
| 4 | KEYFRAME_REQ | receiver -> daemon | empty |
| 5 | SET_RATE | receiver -> daemon | `kbps u32` (kilobits per second, 1 kbit = 1000 bit) |
| 6 | BYE | both | empty |

Lifecycle: TCP connect -> `HELLO` -> `WELCOME` -> data flows over UDP to
`(peer ip, udp_port)`; `STATS` is sent by the daemon once per second to every
active client; `BYE` or 2 s (`TS_CLIENT_TIMEOUT_MS`) without any TCP or UDP
packet from the client frees the slot. A `HELLO` on a new connection from an
already-known `(ip, udp_port)` restarts that client (the old slot is closed).
A repeated `HELLO` on the same connection is a rate change only: the cursor and
the tokens already earned are kept (a different `udp_port` restarts the cursor).
A new cursor starts on the newest keyframe when that keyframe is younger than
the 1 s keyframe cooldown and still in the ring; otherwise the next keyframe is
forced, or deferred until the cooldown has passed, so neither `HELLO` churn nor
`KEYFRAME_REQ` spam produces more than one keyframe per cooldown
(`KEYFRAME_REQ` is also limited to one accepted request per client per 500 ms).
The daemon refuses the fifth client by closing the TCP connection without
`WELCOME`.

### 5.1 STATS value layout

Global counters (44 bytes) followed by `client_count` per-client blocks of 36
bytes. Total length `44 + 36 * client_count`, at most 188 bytes.

Global block:

```
offset size field            notes
 0     4    uptime_ms        u32 ms since daemon start
 4     4    frames_captured  u32
 8     4    frames_sent      u32 frames handed to the transmit ring
12     4    keyframes_sent   u32
16     4    pkts_sent        u32 fresh DATA packets, all clients
20     4    bytes_sent       u32 UDP payload+header bytes, all clients, incl. retransmits
24     4    nacks_rx         u32 NACK entries received
28     4    retx_sent        u32 retransmitted packets
32     4    nack_too_old     u32 NACK entries not in the ring any more
36     4    ctrl_errors      u32 TLV parse errors / connections closed on protocol error
40     1    client_count     u8  0 .. TS_MAX_CLIENTS
41     3    reserved         0
```

Per-client block (one per active client, in slot order):

```
offset size field          notes
 0     1    id             u8  slot index 0..3
 1     1    state          u8  1 = active (0 = free never appears, 2 = closing)
 2     2    udp_port       u16
 4     4    kbps           u32 current token-bucket rate
 8     4    pkts_sent      u32 fresh packets to this client
12     4    bytes_sent     u32
16     4    nacks_rx       u32
20     4    retx_sent      u32
24     4    retx_dropped   u32 retransmits refused by bucket/cap
28     4    keyframe_reqs  u32
32     4    last_seen_ms   u32 ms since the last packet from this client
```

---

## 6. Codec bitstream

An encoded frame (`frame_len` bytes, the unit that is fragmented) is an 8-byte
codec header followed by an RLE stream.

### 6.1 Codec header

```
offset size field      notes
 0     1    kind       u8  0x4B 'K' keyframe, 0x50 'P' P-frame
 1     1    quality    u8  row subsampling step q: 1, 2 or 4
 2     2    frame_seq  u16 the frame's own sequence number
 4     2    ref_seq    u16 P: frame_seq of the reference frame; K: equal to frame_seq
 6     2    rows       u16 number of encoded rows = TS_HEIGHT / q (240, 120 or 60)
```

Any other `kind`, a `quality` not in {1, 2, 4}, or `rows != TS_HEIGHT / quality`
is `TS_E_CORRUPT`.

### 6.2 Row subsampling (quality step)

With step `q` the encoder encodes only rows `0, q, 2q, ...` (`rows` rows of
`TS_WIDTH` symbols each, `n = rows * TS_WIDTH` symbols in total). The decoder
reconstructs row `r` (a multiple of `q`) and copies it to rows
`r+1 .. r+q-1`. The last encoded row is `TS_HEIGHT - q`.

### 6.3 Symbols

- Keyframe: symbol `j` of encoded row `r` is the pixel value `cur[r][j]`.
- P-frame: symbol `j` of encoded row `r` is `cur[r][j] XOR ref[r][j]`, where
  `ref` is the **reconstructed reference** (6.5).

Decoding row `r` from symbols `s[0..W)`:

- Keyframe: `out[r+k][j] = s[j]` for `k = 0..q-1`.
- P-frame: `out[r+k][j] = ref[r][j] XOR s[j]` for `k = 0..q-1` (the reference row
  `r` is used for all replicated rows).

### 6.4 RLE stream

The `n` symbols are serialised with the following tokens:

```
run token:      [len u8 = 1..255] [value u8]           -> len copies of value
escape/literal: [0x00] [len u8 = 1..255] [len bytes]   -> the bytes verbatim
```

The stream ends exactly when `n` symbols have been produced. Decoders MUST
reject (`TS_E_CORRUPT`) a run or literal that would exceed `n`, a literal `len`
of 0, a truncated token, and trailing bytes after the `n`-th symbol.

Upper bound: a run-only encoder needs at most 2 bytes per symbol, hence
`TS_ENC_MAX = 2 * TS_WIDTH * TS_HEIGHT + 64` bounds every valid frame including
the codec header.

**Canonical encoder** (normative for the vectors; any valid stream must decode):

```
pos = 0
while pos < n:
    run = number of equal symbols starting at pos, capped at 255
    if run >= 2:
        emit [run][s[pos]]; pos += run
    else:
        lit = 1
        while pos + lit < n and lit < 255 and runlen(pos + lit) < 3:
            lit += 1
        emit [0x00][lit][s[pos .. pos+lit)]; pos += lit
```

(`runlen(p)` is the number of equal symbols starting at `p`, capped at 255.)
The canonical encoder never produces more than `(5/3) * n + 2` bytes.

### 6.5 Reconstructed reference rule

Both sides keep one reference frame of `TS_FRAME_BYTES`:

- After encoding a frame, the **encoder** sets its reference to the frame as
  the decoder will reconstruct it (rows replicated per 6.2), **not** to the raw
  captured frame. Encoder and decoder references are bit-identical after every
  frame.
- After decoding a frame the decoder sets its reference to its output.
- A P-frame with `ref_seq != last decoded frame_seq`, or arriving when the
  decoder holds no reference, is rejected with `TS_E_NOREF`; the receiver then
  requests a keyframe and drops P-frames until one arrives.
- A keyframe always resets the reference. The encoder emits a keyframe when it
  has no reference, when `force_keyframe` was called, or when the keyframe
  scheduler (periodic 10 s, `KEYFRAME_REQ`, `NACK_TOO_OLD`, scene change >60 %
  of tiles; 1 s cooldown for requested ones) asks for one.

### 6.6 Changed tiles

The frame is divided into 16x16 tiles (20 columns x 15 rows = 300 tiles). A
tile counts as changed when any of its pixels, taken from the frame **as it is
reconstructed at the frame's quality step** (rows replicated per 6.2), differs
from the reconstructed reference by more than `TS_TILE_THRESH` (16). Rows that
a quality step does not transmit therefore never mark a tile: a static detailed
scene at quality 2 or 4 reports 0 changed tiles, exactly as at quality 1.
(Clarification only: the bitstream, the constants and the test vectors are
unchanged, so `PROTOCOL_VERSION` stays 2.) The count is reported by the
encoder for the keyframe scheduler and is exported by the receiver as a tile
bitmap (300 bits, 38 bytes, tile `t` = bit `t & 7` of byte `t >> 3`).

---

## 7. Serial arithmetic for u16 counters

`frame_seq`, `pkt_seq` and `ts_ms` wrap modulo 65536 and MUST only be compared
with serial arithmetic (RFC 1982 style):

```
int16_t  ts_seq_diff(uint16_t a, uint16_t b)  = (int16_t)(uint16_t)(a - b)
bool     ts_seq_after(uint16_t a, uint16_t b) = ts_seq_diff(a, b) > 0
```

- `diff` is positive when `a` is ahead of `b` by less than 32768, negative when
  behind; a distance of exactly 32768 yields `-32768`, i.e. "not after".
- Example: `seq_after(0x0001, 0xFFFF) == true`, `seq_diff(0x0001, 0xFFFF) == 2`,
  `seq_after(0xFFFF, 0x0001) == false`.
- `ts_ms` latency: `(uint16_t)(rx_ms - ts_ms)` interpreted through `ts_seq_diff`.

---

## 8. Transport behaviour summary

- Sender: capture -> encode -> fragment into the ring -> each active client has
  a cursor into the ring; fresh packets consume its token bucket; NACKed
  fragments are retransmitted from the ring (max `TS_RETX_PER_TICK` per client
  per tick, charged to the same bucket). Ring slots are reused oldest-first; a
  NACK for a slot already overwritten is `NACK_TOO_OLD`.
- Receiver: up to `TS_REASM_FRAMES` (8) frames in flight keyed by `frame_seq`;
  duplicate fragments are dropped; a `pkt_seq` gap older than
  `TS_NACK_DELAY_MS` (20 ms) produces NACK entries; a complete frame is decoded
  in `frame_seq` order; a frame older than the last decoded one is dropped
  (late-frame policy "drop"); a frame still incomplete when the window is full
  is dropped and counted as lost.
- Rate: token bucket per client (bytes, refilled from `kbps`, burst = 1 frame)
  plus a global cap; NACK ratio above 10 % over the last second steps the
  quality down (1 -> 2 -> 4) and a clean second steps it up.
- Congestion: once per second the stream rate (estimated from the P-frames)
  is compared with the slowest viewer's `kbps` (or its share of the global
  cap). Above 90 % of it, or with a standing backlog (the smallest backlog in
  the second above 200 ms), quality steps down; it steps up again when twice
  the estimate fits below 85 %. The coarser of loss and congestion quality is
  used. A client backlog that needs more than 1 s to drain is dropped: the
  cursor jumps to the newest keyframe (or a keyframe is requested).

---

## 9. Test vectors

`scripts/protocol_vectors.py` writes `tests/vectors/{crc,header,nack,tlv,rle}.json`
and `tests/vectors/ts_vectors.h`. Every vector is `{"name", "in"/fields, "out"}`
with byte strings in lowercase hex. Negative vectors carry `"error": "TS_E_..."`.
Unit tests in `tests/unit/` load the generated C header, so a change in this
file must be followed by `python3 scripts/protocol_vectors.py` and a commit of
the regenerated vectors.
