---
name: protocol
description: Use PROTOCOL.md as the source of truth for the tinystream wire format (DATA/NACK packets, TLV control channel, codec bitstream, sequence arithmetic, transport rules) and know when a change needs a PROTOCOL_VERSION bump.
---

# Working with PROTOCOL.md

`PROTOCOL.md` specifies everything that crosses the wire between `tinystreamd`
and `tsrecv`. `include/ts_proto.h`, `include/ts_codec.h` and
`include/ts_config.h` implement it; `scripts/protocol_vectors.py` is an
independent Python implementation that generates the conformance vectors
(`tests/vectors/*.json`, `tests/vectors/ts_vectors.{h,c}`) and the fuzz seeds
(`fuzz/corpus/`).

Sections:

| Section | Topic |
|---|---|
| 1 | Constants (sizes, limits, timers) |
| 2 | CRC32 |
| 3 | UDP DATA packet, 3.1 validation, 3.2 sequence rules |
| 4 | UDP NACK packet |
| 5 | TCP control channel (TLV), 5.1 STATS layout |
| 6 | Codec bitstream (6.1 header ... 6.6 changed tiles) |
| 7 | Serial arithmetic for u16 counters |
| 8 | Transport behaviour summary |
| 9 | Test vectors |

How to use it:
1. Before changing wire-related code, find the governing section and quote it
   in the plan or the code comment (`/* PROTOCOL.md 3.1 */`).
2. When code and PROTOCOL.md disagree, the code is wrong. Do not edit
   PROTOCOL.md to describe the code.
3. Test against the vectors (`tests/vectors/ts_vectors.h`): they come from the
   Python implementation, not from the C code under test.
4. The wire format is closed for agents. Changing a layout, a constant or the
   bitstream, or adding a TLV type, needs a human decision; if it is approved
   it must bump `PROTOCOL_VERSION` in PROTOCOL.md **and** `TS_PROTOCOL_VERSION`
   in `include/ts_config.h`, then `python3 scripts/protocol_vectors.py`
   regenerates the vectors. The compliance gate fails a PROTOCOL.md or DATA
   header change without the bump; the diff-policy gate fails a new TLV type.
