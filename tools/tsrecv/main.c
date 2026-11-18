/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tools/tsrecv/main.c
 * Laptop receiver / viewer around libtsr (include/ts_receiver.h).
 *
 *   tsrecv --server IP[:PORT] [--udp-port N] [--announce-port N] [--kbps N] [--seconds N]
 *          [--metrics PATH] [--frame PATH] [--out-ms N]
 *          [--truth synth[:SEED[:CUT_FRAMES]]] [--seq-start N] [--ref CLIP.y8]
 *          [--late drop|decode] [--no-nack] [--sdl] [--scale N] [--log] [--quiet]
 *          [--heartbeat-ms N] [--press KEYS:MS] [--status]
 *   tsrecv --server IP[:PORT] --send keyframe | --send set-rate=N   (one-shot, then exit)
 *
 * Every --out-ms (500) the last decoded frame is written to --frame (binary
 * PGM) and the metrics to --metrics (JSON), both atomically (tmp + rename).
 * Keys on stdin (raw mode when it is a terminal) or in the SDL window:
 *   k = KEYFRAME_REQ, r = reset metrics, + / - = SET_RATE x2 / /2, q = quit.
 * Ground truth: "--truth synth" regenerates the synthetic source for every
 * decoded frame_seq (frame index = frame_seq - --seq-start, unwrapped), "--ref"
 * compares against a raw 320x240 gray8 clip; both feed exact-match % and PSNR.
 * Without truth, PSNR is measured between the last P-frame and the next keyframe.
 * The SDL2 window is compiled in with -DTS_WITH_SDL=ON only.
 * Scripted control traffic (workshop opening, scripts/opening_demo.sh):
 * --heartbeat-ms N sends a SET_RATE at the current rate every N ms (a rate
 * heartbeat, no change of rate); --press KEYS:MS presses the keys of KEYS
 * in turn every MS ms (scripted, no terminal needed); --status prints one
 * line per second (fps, kbps, CRC / NACK / retransmit counters, keyframe
 * requests answered or ignored). A 'k' counts as answered only when a
 * keyframe sent for a request arrives within RX_KEY_ANSWER_MS, else as
 * ignored; metrics.json carries the same numbers under "commands". The
 * wire carries no keyframe reason (PROTOCOL.md 6.1), so
 * the daemon's own keyframes are told apart by their timing: one that comes at
 * least TS_KEYFRAME_PERIOD_MS - RX_KEY_SCHED_SLACK_MS (7.5 s) after the
 * previous keyframe is on the daemon's schedule (periodic, or the synth scene
 * cut on the same 10 s cadence), and the first keyframe after a (re)connect
 * starts the stream; neither answers a 'k'. Presses more than 7.5 s apart can
 * therefore be counted as ignored even when the daemon answers them.
 * --announce-port puts another UDP port into HELLO than the one bound with
 * --udp-port: run tsnetem --listen A --forward 127.0.0.1:B and tsrecv
 * --udp-port B --announce-port A, so DATA goes through the relay.
 * Tear detector (rx_tear.h): every decoded frame the viewer loop sees is
 * checked for new row-pair discontinuities; metrics.json carries the counts
 * under "tear" (frames_checked, tear_frames, tear_rows, picture_changes,
 * last_row). Frames decoded within one 5 ms wait are checked once (the last).
 */
#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <poll.h>
#include <unistd.h>
#include <termios.h>
#include "ts_config.h"
#include "ts_proto.h"
#include "ts_io.h"
#include "ts_capture.h"
#include "ts_receiver.h"
#include "ts_log.h"
#include "rx_args.h"
#include "rx_tear.h"

#ifdef TS_WITH_SDL
#define SDL_MAIN_HANDLED
#include <SDL.h>
#endif

#define RX_PATH_MAX        512U
#define RX_REF_MAX_FRAMES  64U
#define RX_JSON_CAP        16384U
#define RX_WAIT_MS         5U
#define RX_SEND_TIMEOUT_MS 3000U
#define RX_STATS_WAIT_MS   2500U
#define RX_BYE_WAIT_MS     300U
#define RX_PRESS_MAX       8U       /* keys in one --press cycle */
#define RX_KEY_ANSWER_MS   1250U    /* a 'k' is answered when a requested keyframe arrives within this */
/* A keyframe >= period - this after the previous one is on the daemon's schedule. The slack
 * covers the scene-cut cadence after a reconnect (it runs from the daemon's start, the
 * viewer's first keyframe comes a second or two later); a requested keyframe follows the
 * previous one by at most the press interval (7 s in the opening), so it stays below. */
#define RX_KEY_SCHED_SLACK_MS 2500U
#define RX_STATUS_MS       1000U

typedef enum send_kind {
    SEND_NONE = 0,
    SEND_KEYFRAME,
    SEND_SET_RATE
} send_kind_t;

typedef enum truth_kind {
    TRUTH_NONE = 0,
    TRUTH_SYNTH,
    TRUTH_REF
} truth_kind_t;

typedef struct opts {
    tsr_cfg_t    cfg;
    bool         udp_port_set;
    uint16_t     announce_port;   /**< HELLO udp_port when != 0 (relay in between) */
    uint32_t     seconds;
    uint32_t     out_ms;
    char         metrics_path[RX_PATH_MAX];
    char         frame_path[RX_PATH_MAX];
    char         ref_path[RX_PATH_MAX];
    truth_kind_t truth;
    uint32_t     synth_seed;
    uint32_t     synth_cut;
    uint16_t     seq_start;
    send_kind_t  send;
    uint32_t     send_kbps;
    bool         sdl;
    uint32_t     scale;
    bool         log;
    bool         quiet;
    bool         help;
    bool         version;
    uint32_t     heartbeat_ms;    /**< SET_RATE at the current rate every N ms (rate heartbeat) */
    char         press_keys[RX_PRESS_MAX + 1U];   /**< --press KEYS:MS, pressed in turn */
    uint32_t     press_ms;
    bool         status;          /**< one status line per second on stderr */
} opts_t;

/* What happened to the commands the viewer sent (keys, --press, heartbeats). */
typedef struct cmd_track {
    uint32_t heartbeats;          /**< SET_RATE heartbeats sent */
    uint32_t keys_sent;           /**< manual KEYFRAME_REQ ('k', --press) */
    uint32_t keys_answered;       /**< ... followed by a requested keyframe within RX_KEY_ANSWER_MS */
    uint32_t keys_ignored;        /**< ... not followed by one (scheduled keyframes do not count) */
    uint32_t rates_sent;          /**< SET_RATE from '+' / '-' */
    bool     key_pending;
    uint32_t key_ms;
    uint32_t kf_seen;             /**< tsr_metrics()->keyframes at the last tick */
    uint32_t kf_last_ms;          /**< when the last keyframe arrived */
    uint32_t kf_reconnects;       /**< tsr_metrics()->reconnects when it arrived */
    bool     kf_have;             /**< a keyframe arrived since the start */
    uint32_t press_idx;
    uint32_t last_press_ms;
    uint32_t last_hb_ms;
    uint32_t last_status_ms;
} cmd_track_t;

/* truth provider state: unwraps the u16 frame_seq into a frame index */
typedef struct truth_ctx {
    truth_kind_t kind;
    uint32_t     seed;
    uint32_t     cut;
    uint16_t     seq_start;
    bool         have_last;
    uint16_t     last_seq;
    uint32_t     last_idx;
    uint32_t     ref_frames;
} truth_ctx_t;

static volatile sig_atomic_t g_stop = 0;
static uint8_t g_ref[RX_REF_MAX_FRAMES][TS_FRAME_BYTES];
static char g_json[RX_JSON_CAP];
static ts_io_posix_t g_io;
static truth_ctx_t g_truth;
static cmd_track_t g_cmd;
static rx_tear_t g_tear;
static uint32_t g_tear_seen;   /* tsr_metrics()->frames_decoded at the last check */
static struct termios g_tty_saved;
static bool g_tty_raw = false;

static void on_signal(int sig);
static bool parse_u32(const char *s, uint32_t *out);
static bool parse_server(const char *s, ts_addr_t *a);
static bool parse_args(int argc, char **argv, opts_t *o);
static void usage(void);
static bool truth_fn(void *ctx, uint16_t frame_seq, uint8_t *truth);
static bool load_ref(const char *path, uint32_t *frames);
static bool write_atomic(const char *path, const uint8_t *hdr, size_t hdr_len, const uint8_t *data, size_t len);
static void write_outputs(const tsr_t *r, const opts_t *o);
static void log_drain(ts_log_t *l);
static void tty_raw(void);
static void tty_restore(void);
static int32_t key_poll(void);
static void handle_key(tsr_t *r, int32_t key, const opts_t *o, uint32_t now);
static void send_heartbeat(tsr_t *r);
static void commands_tick(tsr_t *r, const opts_t *o, uint32_t now, uint32_t t0);
static void tear_tick(const tsr_t *r);
static size_t commands_json(char *buf, size_t len, size_t cap);
static int run_send(tsr_t *r, const opts_t *o);
static int run_viewer(tsr_t *r, const opts_t *o);

/* ---- argument parsing ------------------------------------------------------------- */
static void on_signal(int sig)
{
    (void)sig;
    g_stop = 1;
}

/* Strict decimal (rx_args.h): digits only, no sign or blanks, no overflow. */
static bool parse_u32(const char *s, uint32_t *out)
{
    return rx_parse_u32(s, 0U, UINT32_MAX, out);
}

/* "a.b.c.d[:port]" or "localhost[:port]" */
static bool parse_server(const char *s, ts_addr_t *a)
{
    bool ok = true;
    uint32_t ip = 0U;
    uint32_t parts = 0U;
    size_t i = 0U;
    if (strncmp(s, "localhost", 9U) == 0) {
        ip = 0x7F000001U;
        parts = 4U;
        i = 9U;
    } else {
        uint32_t cur = 0U;
        bool digits = false;
        while (ok && (s[i] != '\0') && (s[i] != ':')) {
            char c = s[i];
            if ((c >= '0') && (c <= '9')) {
                uint8_t uc = (uint8_t)c;
                cur = (cur * 10U) + ((uint32_t)uc - 48U);
                digits = true;
                ok = (cur <= 255U);
            } else if ((c == '.') && digits && (parts < 3U)) {
                ip = (ip << 8) | cur;
                parts++;
                cur = 0U;
                digits = false;
            } else {
                ok = false;
            }
            i++;
        }
        if (ok && digits && (parts == 3U)) {
            ip = (ip << 8) | cur;
            parts = 4U;
        }
    }
    ok = ok && (parts == 4U);
    if (ok) {
        a->ip = ip;
        if (s[i] == ':') {
            uint32_t port = 0U;
            ok = parse_u32(&s[i + 1U], &port) && (port > 0U) && (port <= 65535U);
            if (ok) {
                a->port = (uint16_t)port;
            }
        } else {
            ok = (s[i] == '\0');
        }
    }
    return ok;
}

static void usage(void)
{
    (void)fprintf(stderr,
        "usage: tsrecv --server IP[:PORT] [--udp-port N] [--announce-port N] [--kbps N] [--seconds N]\n"
        "              [--metrics PATH] [--frame PATH] [--out-ms N]\n"
        "              [--truth synth[:SEED[:CUT_FRAMES]]] [--seq-start N] [--ref CLIP.y8]\n"
        "              [--late drop|decode] [--no-nack] [--sdl] [--scale N] [--log] [--quiet]\n"
        "              [--heartbeat-ms N] [--press KEYS:MS] [--status]\n"
        "       tsrecv --server IP[:PORT] --send keyframe | --send set-rate=N\n"
        "keys: k keyframe request, r reset metrics, +/- rate x2 or /2, q quit\n");
}

static bool parse_args(int argc, char **argv, opts_t *o)
{
    bool ok = true;
    int i = 1;
    (void)memset(o, 0, sizeof(*o));
    tsr_cfg_default(&o->cfg);
    o->out_ms = 500U;
    o->synth_seed = 1U;
    o->synth_cut = (10000U * TS_DEFAULT_FPS) / 1000U;
    o->scale = 2U;
    (void)strcpy(o->metrics_path, "metrics.json");
    (void)strcpy(o->frame_path, "frame.pgm");
    while (ok && (i < argc)) {
        const char *a = argv[i];
        const char *v = ((i + 1) < argc) ? argv[i + 1] : NULL;
        uint32_t n = 0U;
        bool takes = true;
        if (strcmp(a, "--server") == 0) {
            ok = (v != NULL) && parse_server(v, &o->cfg.server);
        } else if (strcmp(a, "--udp-port") == 0) {
            ok = parse_u32(v, &n) && (n <= 65535U);
            o->cfg.udp_port = (uint16_t)n;
            o->udp_port_set = true;
        } else if (strcmp(a, "--announce-port") == 0) {
            ok = parse_u32(v, &n) && (n > 0U) && (n <= 65535U);
            o->announce_port = (uint16_t)n;
        } else if (strcmp(a, "--kbps") == 0) {
            ok = parse_u32(v, &o->cfg.kbps);
        } else if (strcmp(a, "--seconds") == 0) {
            /* seconds * 1000 must fit the u32 millisecond clock difference */
            ok = rx_parse_u32(v, 0U, RX_SECONDS_MAX, &o->seconds);
        } else if (strcmp(a, "--out-ms") == 0) {
            ok = parse_u32(v, &o->out_ms) && (o->out_ms > 0U);
        } else if (strcmp(a, "--metrics") == 0) {
            ok = (v != NULL) && (strlen(v) < (RX_PATH_MAX - 8U));
            if (ok) {
                (void)strcpy(o->metrics_path, v);
            }
        } else if (strcmp(a, "--frame") == 0) {
            ok = (v != NULL) && (strlen(v) < (RX_PATH_MAX - 8U));
            if (ok) {
                (void)strcpy(o->frame_path, v);
            }
        } else if (strcmp(a, "--ref") == 0) {
            ok = (v != NULL) && (strlen(v) < RX_PATH_MAX);
            if (ok) {
                (void)strcpy(o->ref_path, v);
                o->truth = TRUTH_REF;
            }
        } else if (strcmp(a, "--truth") == 0) {
            ok = (v != NULL) && (strncmp(v, "synth", 5U) == 0);
            if (ok) {
                o->truth = TRUTH_SYNTH;
                if (v[5] == ':') {
                    char tmp[64];
                    char *colon;
                    ok = (strlen(&v[6]) < sizeof(tmp));
                    if (ok) {
                        (void)strcpy(tmp, &v[6]);
                        colon = strchr(tmp, (int)':');
                        if (colon != NULL) {
                            *colon = '\0';
                            ok = parse_u32(&colon[1], &o->synth_cut) && (o->synth_cut > 0U);
                        }
                        ok = ok && parse_u32(tmp, &o->synth_seed);
                    }
                } else {
                    ok = (v[5] == '\0');
                }
            }
        } else if (strcmp(a, "--seq-start") == 0) {
            ok = parse_u32(v, &n) && (n <= 65535U);
            o->seq_start = (uint16_t)n;
        } else if (strcmp(a, "--late") == 0) {
            ok = (v != NULL);
            if (ok && (strcmp(v, "decode") == 0)) {
                o->cfg.late_policy = TSR_LATE_DECODE;
            } else if (ok && (strcmp(v, "drop") == 0)) {
                o->cfg.late_policy = TSR_LATE_DROP;
            } else {
                ok = false;
            }
        } else if (strcmp(a, "--send") == 0) {
            ok = (v != NULL);
            if (ok && (strcmp(v, "keyframe") == 0)) {
                o->send = SEND_KEYFRAME;
            } else if (ok && (strncmp(v, "set-rate=", 9U) == 0)) {
                o->send = SEND_SET_RATE;
                ok = parse_u32(&v[9], &o->send_kbps) && (o->send_kbps > 0U);
            } else {
                ok = false;
            }
        } else if (strcmp(a, "--heartbeat-ms") == 0) {
            ok = rx_parse_u32(v, 1U, 3600000U, &o->heartbeat_ms);
        } else if (strcmp(a, "--press") == 0) {
            const char *colon = (v != NULL) ? strchr(v, (int)':') : NULL;
            ok = (colon != NULL) && (colon != v);
            if (ok) {
                ptrdiff_t klen = colon - v;
                ok = ((size_t)klen <= RX_PRESS_MAX) && rx_parse_u32(&colon[1], 100U, 3600000U, &o->press_ms);
                if (ok) {
                    (void)memcpy(o->press_keys, v, (size_t)klen);
                    o->press_keys[klen] = '\0';
                    ok = (strspn(o->press_keys, "kK+-") == (size_t)klen);
                }
            }
        } else if (strcmp(a, "--scale") == 0) {
            ok = parse_u32(v, &o->scale) && (o->scale >= 1U) && (o->scale <= 8U);
        } else {
            takes = false;
            if (strcmp(a, "--no-nack") == 0) {
                o->cfg.no_nack = true;
            } else if (strcmp(a, "--sdl") == 0) {
                o->sdl = true;
            } else if (strcmp(a, "--log") == 0) {
                o->log = true;
            } else if (strcmp(a, "--quiet") == 0) {
                o->quiet = true;
            } else if (strcmp(a, "--status") == 0) {
                o->status = true;
            } else if ((strcmp(a, "--help") == 0) || (strcmp(a, "-h") == 0)) {
                o->help = true;
            } else if (strcmp(a, "--version") == 0) {
                o->version = true;
            } else {
                ok = false;
            }
        }
        if (!ok) {
            (void)fprintf(stderr, "tsrecv: bad or incomplete option '%s'\n", a);
        }
        i += takes ? 2 : 1;
    }
    if (ok && (o->send != SEND_NONE) && (!o->udp_port_set)) {
        /* one-shot: an ephemeral UDP port, so the HELLO never restarts a running viewer */
        o->cfg.udp_port = 0U;
    }
    return ok;
}

/* ---- ground truth -------------------------------------------------------------------- */
static bool truth_fn(void *ctx, uint16_t frame_seq, uint8_t *truth)
{
    /* opaque callback context: always the truth_ctx_t main() registered */
    /* cppcheck-suppress misra-c2012-11.5 */
    truth_ctx_t *t = (truth_ctx_t *)ctx;
    bool ok = false;
    uint32_t idx;
    if (!t->have_last) {
        idx = (uint32_t)(uint16_t)(frame_seq - t->seq_start);
    } else {
        int32_t d = (int32_t)ts_seq_diff(frame_seq, t->last_seq);
        idx = t->last_idx + (uint32_t)d;   /* modulo 2^32 also handles small negative steps */
    }
    t->have_last = true;
    t->last_seq = frame_seq;
    t->last_idx = idx;
    if (t->kind == TRUTH_SYNTH) {
        ts_cap_synth_frame(truth, idx, t->seed, t->cut);
        ok = true;
    } else if ((t->kind == TRUTH_REF) && (t->ref_frames > 0U)) {
        (void)memcpy(truth, g_ref[idx % t->ref_frames], TS_FRAME_BYTES);
        ok = true;
    } else {
        ok = false;
    }
    return ok;
}

static bool load_ref(const char *path, uint32_t *frames)
{
    bool ok = false;
    FILE *f = fopen(path, "rb");
    *frames = 0U;
    if (f != NULL) {
        bool more = true;
        while (more && (*frames < RX_REF_MAX_FRAMES)) {
            size_t n = fread(g_ref[*frames], 1U, TS_FRAME_BYTES, f);
            if (n == sizeof(g_ref[0])) {
                (*frames)++;
            } else {
                more = false;
            }
        }
        (void)fclose(f);
        ok = (*frames > 0U);
    }
    return ok;
}

/* ---- outputs --------------------------------------------------------------------------- */
static bool write_atomic(const char *path, const uint8_t *hdr, size_t hdr_len, const uint8_t *data, size_t len)
{
    char tmp[RX_PATH_MAX];
    bool ok = false;
    FILE *f;
    (void)strcpy(tmp, path);
    (void)strcat(tmp, ".tmp");
    f = fopen(tmp, "wb");
    if (f != NULL) {
        ok = (fwrite(hdr, 1U, hdr_len, f) == hdr_len) && (fwrite(data, 1U, len, f) == len);
        ok = (fclose(f) == 0) && ok;
        if (ok) {
            ok = (rename(tmp, path) == 0);
        }
    }
    return ok;
}

static void write_outputs(const tsr_t *r, const opts_t *o)
{
    static const char pgm_hdr[] = "P5\n320 240\n255\n";
    const uint8_t *frame = tsr_frame(r);
    size_t n = tsr_metrics_json(tsr_metrics(r), g_json, sizeof(g_json));
    n = commands_json(g_json, n, sizeof(g_json));
    if ((n > 0U) && (strlen(o->metrics_path) > 0U)) {
        static const uint8_t nl[1] = { (uint8_t)'\n' };
        if (!write_atomic(o->metrics_path, (const uint8_t *)g_json, n, nl, 1U)) {
            (void)fprintf(stderr, "tsrecv: cannot write %s\n", o->metrics_path);
        }
    }
    if ((frame != NULL) && (strlen(o->frame_path) > 0U)) {
        if (!write_atomic(o->frame_path, (const uint8_t *)pgm_hdr, sizeof(pgm_hdr) - 1U, frame, TS_FRAME_BYTES)) {
            (void)fprintf(stderr, "tsrecv: cannot write %s\n", o->frame_path);
        }
    }
}

/* libtsd is built with TS_LOG_ENABLED=0, so ts_log_dump() is not linked: render here. */
static void log_drain(ts_log_t *l)
{
    ts_log_entry_t e;
    while (ts_log_pop(l, &e)) {
        (void)fprintf(stderr, "%10u %-4s %-8s code=%u a=%u b=%u\n", (unsigned)e.ms,
                      ts_log_level_name((ts_log_level_t)e.level), ts_log_module_name((ts_module_t)e.module),
                      (unsigned)e.code, (unsigned)e.a, (unsigned)e.b);
    }
}

/* ---- keyboard ----------------------------------------------------------------------------- */
static void tty_raw(void)
{
    if (isatty(STDIN_FILENO) != 0) {
        struct termios t;
        if (tcgetattr(STDIN_FILENO, &g_tty_saved) == 0) {
            t = g_tty_saved;
            t.c_lflag &= ~((tcflag_t)ICANON | (tcflag_t)ECHO);
            t.c_cc[VMIN] = 0U;
            t.c_cc[VTIME] = 0U;
            g_tty_raw = (tcsetattr(STDIN_FILENO, TCSANOW, &t) == 0);
        }
    }
}

static void tty_restore(void)
{
    if (g_tty_raw) {
        (void)tcsetattr(STDIN_FILENO, TCSANOW, &g_tty_saved);
        g_tty_raw = false;
    }
}

/* Next key from stdin, 0 when none, -1 on EOF. */
static int32_t key_poll(void)
{
    int32_t key = 0;
    struct pollfd p;
    p.fd = STDIN_FILENO;
    p.events = POLLIN;
    p.revents = 0;
    if ((poll(&p, 1U, 0) > 0) && ((p.revents & (POLLIN | POLLHUP)) != 0)) {
        uint8_t c = 0U;
        ssize_t n = read(STDIN_FILENO, &c, 1U);
        if (n == 1) {
            key = (int32_t)c;
        } else if (n == 0) {
            key = -1;
        } else {
            key = 0;
        }
    }
    return key;
}

/* SET_RATE at the current rate (the rate heartbeat): no change of rate for the daemon */
static void send_heartbeat(tsr_t *r)
{
    uint32_t kbps = (r->cfg.kbps == 0U) ? TS_DEFAULT_KBPS : r->cfg.kbps;
    if (tsr_send_set_rate(r, kbps) == TS_OK) {
        g_cmd.heartbeats++;
    }
}

static void handle_key(tsr_t *r, int32_t key, const opts_t *o, uint32_t now)
{
    uint32_t kbps = (r->cfg.kbps == 0U) ? TS_DEFAULT_KBPS : r->cfg.kbps;
    bool command = (key == (int32_t)'k') || (key == (int32_t)'K') || (key == (int32_t)'+') ||
                   ((key == (int32_t)'-') && (kbps >= 2U));
    if (command && (o->heartbeat_ms != 0U)) {
        send_heartbeat(r);
    }
    if ((key == (int32_t)'k') || (key == (int32_t)'K')) {
        if (tsr_send_keyframe_req(r) != TS_OK) {
            (void)fprintf(stderr, "tsrecv: not connected, keyframe request not sent\n");
        } else {
            g_cmd.keys_sent++;
            if (!g_cmd.key_pending) {
                g_cmd.key_pending = true;
                g_cmd.key_ms = now;
            }
        }
    } else if ((key == (int32_t)'r') || (key == (int32_t)'R')) {
        tsr_reset_metrics(r);
    } else if (key == (int32_t)'+') {
        if (tsr_send_set_rate(r, kbps * 2U) == TS_OK) {
            g_cmd.rates_sent++;
        }
    } else if ((key == (int32_t)'-') && (kbps >= 2U)) {
        if (tsr_send_set_rate(r, kbps / 2U) == TS_OK) {
            g_cmd.rates_sent++;
        }
    } else if ((key == (int32_t)'q') || (key == (int32_t)'Q')) {
        g_stop = 1;
    } else {
        /* ignored */
    }
}

/* Heartbeats, scripted presses, the answered / ignored bookkeeping and the status line. */
static void commands_tick(tsr_t *r, const opts_t *o, uint32_t now, uint32_t t0)
{
    const tsr_metrics_t *m = tsr_metrics(r);
    bool streaming = (tsr_state(r) == TSR_STREAMING);
    if (m->keyframes > g_cmd.kf_seen) {
        /* a keyframe arrived: does it answer the pending 'k'? */
        bool restart = (!g_cmd.kf_have) || (m->reconnects != g_cmd.kf_reconnects);
        bool scheduled = (now - g_cmd.kf_last_ms) >= (TS_KEYFRAME_PERIOD_MS - RX_KEY_SCHED_SLACK_MS);
        if (g_cmd.key_pending && (!restart) && (!scheduled)) {
            g_cmd.keys_answered++;
            g_cmd.key_pending = false;
        }
        g_cmd.kf_have = true;
        g_cmd.kf_last_ms = now;
        g_cmd.kf_reconnects = m->reconnects;
    }
    g_cmd.kf_seen = m->keyframes;          /* also after 'r' reset the metrics */
    if (g_cmd.key_pending && ((now - g_cmd.key_ms) >= RX_KEY_ANSWER_MS)) {
        g_cmd.keys_ignored++;
        g_cmd.key_pending = false;
    }
    if (streaming && (o->press_ms != 0U) && ((now - g_cmd.last_press_ms) >= o->press_ms)) {
        size_t nkeys = strlen(o->press_keys);
        g_cmd.last_press_ms = now;
        if (nkeys > 0U) {
            handle_key(r, (int32_t)(uint8_t)o->press_keys[g_cmd.press_idx % (uint32_t)nkeys], o, now);
            g_cmd.press_idx++;
        }
    } else if (streaming && (o->heartbeat_ms != 0U) && ((now - g_cmd.last_hb_ms) >= o->heartbeat_ms)) {
        send_heartbeat(r);
    } else {
        /* nothing due */
    }
    if (o->heartbeat_ms != 0U) {
        g_cmd.last_hb_ms = ((now - g_cmd.last_hb_ms) >= o->heartbeat_ms) ? now : g_cmd.last_hb_ms;
    }
    if (o->status && ((now - g_cmd.last_status_ms) >= RX_STATUS_MS)) {
        const ts_tlv_stats_t *st = &m->last_stats;
        g_cmd.last_status_ms = now;
        (void)fprintf(stderr,
                      "tsrecv %5us fps %3u.%u rx %5u kbps | crc %6u nack %6u retx %6u lost %4u | freeze %2u "
                      "reconn %2u | tear %u | k %u: answered %u ignored %u, rate %u | daemon clients %u kf %u\n",
                      (unsigned)((now - t0) / 1000U), (unsigned)(m->fps_x100 / 100U),
                      (unsigned)((m->fps_x100 % 100U) / 10U), (unsigned)m->kbps, (unsigned)m->crc_fail,
                      (unsigned)m->nacks_sent, (unsigned)m->retx_rx, (unsigned)m->pkts_lost, (unsigned)m->freezes,
                      (unsigned)m->reconnects, (unsigned)g_tear.tear_frames, (unsigned)g_cmd.keys_sent, (unsigned)g_cmd.keys_answered,
                      (unsigned)g_cmd.keys_ignored, (unsigned)g_cmd.rates_sent, (unsigned)st->g.client_count,
                      (unsigned)st->g.keyframes_sent);
    }
}

/* A new decoded frame since the last check: run the tear detector on it. */
static void tear_tick(const tsr_t *r)
{
    const tsr_metrics_t *m = tsr_metrics(r);
    const uint8_t *frame = tsr_frame(r);
    if ((frame != NULL) && (m->frames_decoded != g_tear_seen)) {
        (void)rx_tear_check(&g_tear, frame);
    }
    g_tear_seen = m->frames_decoded;   /* also after 'r' reset the metrics */
}

/* Append "commands": {...} and "tear": {...} to the metrics JSON object in buf (len bytes, ends with '}'). */
static size_t commands_json(char *buf, size_t len, size_t cap)
{
    size_t out = len;
    size_t end = len;
    while ((end > 0U) && (buf[end - 1U] != '}')) {
        end--;
    }
    if (end > 0U) {
        int w = snprintf(&buf[end - 1U], cap - (end - 1U),
                         ",\n  \"commands\": {\"heartbeats\": %u, \"keyframe_reqs\": %u, \"keyframe_answered\": %u, "
                         "\"keyframe_ignored\": %u, \"set_rate\": %u},\n"
                         "  \"tear\": {\"frames_checked\": %u, \"tear_frames\": %u, \"tear_rows\": %u, "
                         "\"picture_changes\": %u, \"last_row\": %u}\n}",
                         (unsigned)g_cmd.heartbeats, (unsigned)g_cmd.keys_sent, (unsigned)g_cmd.keys_answered,
                         (unsigned)g_cmd.keys_ignored, (unsigned)g_cmd.rates_sent,
                         (unsigned)g_tear.frames_checked, (unsigned)g_tear.tear_frames,
                         (unsigned)g_tear.tear_rows_total, (unsigned)g_tear.picture_changes,
                         (unsigned)g_tear.last_row);
        if ((w > 0) && ((size_t)w < (cap - (end - 1U)))) {
            out = (end - 1U) + (size_t)w;
        } else {
            buf[end - 1U] = '}';          /* no room: keep the object as it was */
            out = len;
        }
    }
    return out;
}

/* ---- SDL2 viewer (optional) --------------------------------------------------------------- */
#ifdef TS_WITH_SDL
typedef struct view {
    SDL_Window   *win;
    SDL_Renderer *ren;
    SDL_Texture  *tex;
    uint32_t      px[TS_FRAME_BYTES];
} view_t;

static view_t g_view;

static bool view_open(view_t *v, uint32_t scale);
static void view_frame(view_t *v, const tsr_t *r);
static int32_t view_poll(void);
static void view_close(view_t *v);

static bool view_open(view_t *v, uint32_t scale)
{
    bool ok = false;
    (void)memset(v, 0, sizeof(*v));
    SDL_SetMainReady();
    if (SDL_Init(SDL_INIT_VIDEO) == 0) {
        v->win = SDL_CreateWindow("tsrecv", SDL_WINDOWPOS_UNDEFINED, SDL_WINDOWPOS_UNDEFINED,
                                  (int)(TS_WIDTH * scale), (int)(TS_HEIGHT * scale), 0U);
        if (v->win != NULL) {
            v->ren = SDL_CreateRenderer(v->win, -1, 0U);
        }
        if (v->ren != NULL) {
            v->tex = SDL_CreateTexture(v->ren, (Uint32)SDL_PIXELFORMAT_ARGB8888,
                                       (int)SDL_TEXTUREACCESS_STREAMING, (int)TS_WIDTH, (int)TS_HEIGHT);
        }
        ok = (v->tex != NULL);
    }
    if (!ok) {
        (void)fprintf(stderr, "tsrecv: SDL: %s\n", SDL_GetError());
    }
    return ok;
}

static void view_frame(view_t *v, const tsr_t *r)
{
    const uint8_t *f = tsr_frame(r);
    const tsr_metrics_t *m = tsr_metrics(r);
    char title[160];
    if (f != NULL) {
        for (uint32_t i = 0U; i < TS_FRAME_BYTES; i++) {
            uint32_t g = (uint32_t)f[i];
            v->px[i] = 0xFF000000U | (g << 16) | (g << 8) | g;
        }
        (void)SDL_UpdateTexture(v->tex, NULL, v->px, (int)(TS_WIDTH * 4U));
    }
    (void)SDL_RenderClear(v->ren);
    (void)SDL_RenderCopy(v->ren, v->tex, NULL, NULL);
    SDL_RenderPresent(v->ren);
    (void)snprintf(title, sizeof(title), "tsrecv  %u.%02u fps  %u kbps  loss %u.%02u%%  freezes %u",
                   (unsigned)(m->fps_x100 / 100U), (unsigned)(m->fps_x100 % 100U), (unsigned)m->kbps,
                   (unsigned)(m->loss_pct_x100 / 100U), (unsigned)(m->loss_pct_x100 % 100U),
                   (unsigned)m->freezes);
    SDL_SetWindowTitle(v->win, title);
}

/* Key from the window: 0 none, 'q' on close. */
static int32_t view_poll(void)
{
    int32_t key = 0;
    SDL_Event ev;
    while (SDL_PollEvent(&ev) != 0) {
        if (ev.type == (Uint32)SDL_QUIT) {
            key = (int32_t)'q';
        } else if ((ev.type == (Uint32)SDL_KEYDOWN) && (key == 0)) {
            SDL_Keycode k = ev.key.keysym.sym;
            if ((k >= 32) && (k < 127)) {
                key = (int32_t)k;
            }
        } else {
            /* other events ignored */
        }
    }
    return key;
}

static void view_close(view_t *v)
{
    if (v->tex != NULL) {
        SDL_DestroyTexture(v->tex);
    }
    if (v->ren != NULL) {
        SDL_DestroyRenderer(v->ren);
    }
    if (v->win != NULL) {
        SDL_DestroyWindow(v->win);
    }
    SDL_Quit();
}
#endif

/* ---- modes ---------------------------------------------------------------------------------- */
static int run_send(tsr_t *r, const opts_t *o)
{
    int rc = 1;
    uint32_t phase = 0U;      /* 0 wait WELCOME, 1 wait STATS, 2 BYE sent, 3 done */
    uint32_t phase_ms = ts_io_now_ms(&g_io.io);
    uint32_t stats0 = 0U;
    while ((phase < 3U) && (g_stop == 0)) {
        uint32_t now;
        (void)ts_io_posix_wait(&g_io, RX_WAIT_MS);
        now = ts_io_now_ms(&g_io.io);
        (void)tsr_tick(r, now);
        if ((phase == 0U) && (tsr_state(r) == TSR_STREAMING)) {
            ts_err_t e = (o->send == SEND_KEYFRAME) ? tsr_send_keyframe_req(r) : tsr_send_set_rate(r, o->send_kbps);
            if (e == TS_OK) {
                (void)printf("tsrecv: %s sent to the daemon (stream_id 0x%08x)\n",
                             (o->send == SEND_KEYFRAME) ? "KEYFRAME_REQ" : "SET_RATE",
                             (unsigned)tsr_metrics(r)->stream_id);
                rc = 0;
            } else {
                (void)fprintf(stderr, "tsrecv: send failed: %s\n", ts_err_str(e));
            }
            stats0 = tsr_metrics(r)->stats_rx;
            phase_ms = now;
            if (o->send == SEND_SET_RATE) {
                phase = 1U;
            } else {
                phase = 2U;
                (void)tsr_send_bye(r);
            }
        } else if ((phase == 0U) && ((now - phase_ms) >= RX_SEND_TIMEOUT_MS)) {
            (void)fprintf(stderr, "tsrecv: no WELCOME from the daemon within %u ms\n", (unsigned)RX_SEND_TIMEOUT_MS);
            phase = 3U;
        } else if (phase == 1U) {
            const tsr_metrics_t *m = tsr_metrics(r);
            bool got = (m->stats_rx != stats0) && ((now - phase_ms) > TS_STATS_PERIOD_MS / 4U);
            if (got || ((now - phase_ms) >= RX_STATS_WAIT_MS)) {
                for (uint32_t i = 0U; got && (i < (uint32_t)m->last_stats.g.client_count) && (i < TS_MAX_CLIENTS); i++) {
                    if (m->last_stats.c[i].udp_port == r->cfg.udp_port) {
                        (void)printf("tsrecv: daemon reports client %u at %u kbps\n",
                                     (unsigned)m->last_stats.c[i].id, (unsigned)m->last_stats.c[i].kbps);
                    }
                }
                (void)tsr_send_bye(r);
                phase = 2U;
                phase_ms = now;
            }
        } else if ((phase == 2U) &&
                   ((tsr_state(r) == TSR_CLOSED) || ((now - phase_ms) >= RX_BYE_WAIT_MS))) {
            phase = 3U;
        } else {
            /* keep ticking */
        }
    }
    return rc;
}

static int run_viewer(tsr_t *r, const opts_t *o)
{
    int rc = 0;
    uint32_t t0 = ts_io_now_ms(&g_io.io);
    uint32_t last_out = t0;
    rx_tear_init(&g_tear);
    g_tear_seen = 0U;
    g_cmd.last_press_ms = t0;
    g_cmd.last_hb_ms = t0;
    g_cmd.last_status_ms = t0;
    bool stdin_open = true;
    bool use_sdl = false;
#ifdef TS_WITH_SDL
    if (o->sdl) {
        use_sdl = view_open(&g_view, o->scale);
        rc = use_sdl ? 0 : 1;
    }
#endif
    if (rc == 0) {
        tty_raw();
        if (!o->quiet) {
            (void)fprintf(stderr, "tsrecv: udp port %u, keys: k keyframe, r reset, +/- rate, q quit\n",
                          (unsigned)r->cfg.udp_port);
        }
    }
    while ((rc == 0) && (g_stop == 0)) {
        uint32_t now;
        (void)ts_io_posix_wait(&g_io, RX_WAIT_MS);
        now = ts_io_now_ms(&g_io.io);
        (void)tsr_tick(r, now);
        if (stdin_open) {
            int32_t k = key_poll();
            if (k < 0) {
                stdin_open = false;
            } else if (k > 0) {
                handle_key(r, k, o, now);
            } else {
                /* no key */
            }
        }
#ifdef TS_WITH_SDL
        if (use_sdl) {
            int32_t k = view_poll();
            if (k > 0) {
                handle_key(r, k, o, now);
            }
        }
#endif
        tear_tick(r);
        commands_tick(r, o, now, t0);
        if ((now - last_out) >= o->out_ms) {
            last_out = now;
            write_outputs(r, o);
#ifdef TS_WITH_SDL
            if (use_sdl) {
                view_frame(&g_view, r);
            }
#endif
            if (o->log) {
                log_drain(&r->log);
            }
        }
        if (rx_seconds_done(now - t0, o->seconds)) {
            g_stop = 1;
        }
        if (tsr_state(r) == TSR_CLOSED) {
            g_stop = 1;
        }
    }
    /* polite shutdown: BYE, then a few ticks to flush it */
    if (tsr_send_bye(r) == TS_OK) {
        uint32_t t_bye = ts_io_now_ms(&g_io.io);
        uint32_t now = t_bye;
        while ((tsr_state(r) != TSR_CLOSED) && ((now - t_bye) < RX_BYE_WAIT_MS)) {
            (void)ts_io_posix_wait(&g_io, RX_WAIT_MS);
            now = ts_io_now_ms(&g_io.io);
            (void)tsr_tick(r, now);
        }
    }
    write_outputs(r, o);
    if (!o->quiet) {
        const tsr_metrics_t *m = tsr_metrics(r);
        (void)fprintf(stderr, "tsrecv: %u frames decoded, %u dropped, %u lost pkts, %u NACKs, %u freezes, "
                      "%u torn\n",
                      (unsigned)m->frames_decoded, (unsigned)m->frames_dropped, (unsigned)m->pkts_lost,
                      (unsigned)m->nacks_sent, (unsigned)m->freezes, (unsigned)g_tear.tear_frames);
    }
    tty_restore();
#ifdef TS_WITH_SDL
    if (use_sdl) {
        view_close(&g_view);
    }
#else
    (void)use_sdl;
#endif
    return rc;
}

int main(int argc, char **argv)
{
    int rc = 0;
    opts_t o;
    tsr_t *r = NULL;
    bool io_open = false;
    struct sigaction sa;

    if (!parse_args(argc, argv, &o)) {
        usage();
        rc = 2;
    } else if (o.help) {
        usage();
    } else if (o.version) {
        (void)printf("tsrecv protocol %u\n", (unsigned)TS_PROTOCOL_VERSION);
    } else {
#ifndef TS_WITH_SDL
        if (o.sdl) {
            (void)fprintf(stderr, "tsrecv: built without SDL2 (configure with -DTS_WITH_SDL=ON)\n");
            rc = 2;
        }
#endif
        if ((rc == 0) && (o.truth == TRUTH_REF) && (!load_ref(o.ref_path, &g_truth.ref_frames))) {
            (void)fprintf(stderr, "tsrecv: cannot read reference clip %s\n", o.ref_path);
            rc = 2;
        }
        if (rc == 0) {
            if (ts_io_posix_open(&g_io, o.cfg.udp_port, 0U) != TS_OK) {
                (void)fprintf(stderr, "tsrecv: cannot open UDP port %u\n", (unsigned)o.cfg.udp_port);
                rc = 1;
            } else {
                io_open = true;
                o.cfg.udp_port = (o.announce_port != 0U) ? o.announce_port : ts_io_posix_udp_port(&g_io);
                r = tsr_create(&o.cfg, &g_io.io);
                if (r == NULL) {
                    (void)fprintf(stderr, "tsrecv: out of memory\n");
                    rc = 1;
                }
            }
        }
        if (rc == 0) {
            (void)memset(&sa, 0, sizeof(sa));
            sa.sa_handler = on_signal;
            (void)sigemptyset(&sa.sa_mask);
            (void)sigaction(SIGINT, &sa, NULL);
            (void)sigaction(SIGTERM, &sa, NULL);
            if (o.truth != TRUTH_NONE) {
                g_truth.kind = o.truth;
                g_truth.seed = o.synth_seed;
                g_truth.cut = o.synth_cut;
                g_truth.seq_start = o.seq_start;
                tsr_set_truth_fn(r, truth_fn, &g_truth);
            }
            rc = (o.send != SEND_NONE) ? run_send(r, &o) : run_viewer(r, &o);
        }
        if (r != NULL) {
            tsr_destroy(r);
        }
        if (io_open) {
            ts_io_posix_close(&g_io);
        }
    }
    return rc;
}
