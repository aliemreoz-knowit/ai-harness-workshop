/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- src/main.c
 * tinystreamd: argument parsing, init order, poll loop, SIGTERM.
 *
 * Init order (nothing allocates after step 5):
 *   1. parse arguments (libc may allocate here)
 *   2. signals: SIGTERM/SIGINT set a flag, SIGPIPE ignored
 *   3. stdout unbuffered (stdio never needs a heap buffer later)
 *   4. ts_pool_init -> ts_io_posix_open(pool->io) -> tsd_create (opens the
 *      capture source and seals the pool)
 *   5. ts_init_done(): from here on src/malloc_guard.c aborts on malloc/free
 *   6. loop: tsd_tick(now) -> ts_io_posix_wait(until tsd_next_due_ms)
 *   7. SIGTERM: tsd_stop (BYE to every client) -> close sockets -> exit 0
 *
 * stdio is allowed in this file only (start-up and shutdown, never per tick).
 */
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <signal.h>
#include <time.h>
#include <unistd.h>
#include "ts_config.h"
#include "ts_daemon.h"
#include "ts_pool.h"

/* Exit codes (documented in --help). */
#define TSD_EXIT_OK       0   /* clean shutdown (SIGTERM / SIGINT), --help, --version */
#define TSD_EXIT_RUNTIME  1   /* fatal error while running (capture EOF / read error, poll failure) */
#define TSD_EXIT_USAGE    2   /* bad command line */
#define TSD_EXIT_INIT     3   /* start-up failure (socket bind / listen, capture open, bad config) */

#define TSD_MAX_WAIT_MS   100U
#define TSD_ARG_MAX_FPS   120U
#define TSD_ARG_MAX_KBPS  1000000U
#define TSD_ARG_MIN_KEY_MS 100U
#define TSD_ARG_MAX_KEY_MS 600000U
#define TSD_ARG_MAX_NOISE_S 86400U

static volatile sig_atomic_t g_stop = 0;

static void on_signal(int sig)
{
    (void)sig;
    g_stop = 1;
}

static void usage(FILE *out)
{
    (void)fprintf(out,
        "usage: tinystreamd [options]\n"
        "  --source S            synth | file:PATH | pipe[:PATH] | v4l2[:/dev/videoN]   (default synth)\n"
        "                        pipe reads rpicam-vid --codec yuv420 320x240 from stdin (or a FIFO)\n"
        "  --fps N               capture rate for synth/file, 1..%u (default %u)\n"
        "  --kbps N              default per-client rate in kbit/s (default %u)\n"
        "  --kbps-global N       global cap in kbit/s (default %u)\n"
        "  --udp-port N          DATA/NACK UDP port, 0 = ephemeral (default %u)\n"
        "  --tcp-port N          control TCP port (default %u)\n"
        "  --mtu N               link payload budget in bytes; protocol v2 sends %u-byte fragments,\n"
        "                        so N must be >= %u\n"
        "  --seq-start N         initial frame_seq and pkt_seq, 0..65535 (HIL: 65000)\n"
        "  --keyframe-interval MS  periodic keyframe interval, %u..%u ms (default %u)\n"
        "  --seed N              synth pattern seed (default 0)\n"
        "  --noise-after-s N     synth: no noise burst in the first N s (a noise frame repeats the\n"
        "                        frame before it), 0..%u (default 0 = noise from the start)\n"
        "  --stream-id N         WELCOME stream id (default: random per start)\n"
        "  --no-loss-adapt       keep quality step 1 whatever the NACK ratio\n"
        "  --version, --help\n"
        "exit codes: %d clean stop (SIGTERM/SIGINT), %d runtime failure, %d usage, %d start-up failure\n",
        (unsigned)TSD_ARG_MAX_FPS, (unsigned)TS_DEFAULT_FPS, (unsigned)TS_DEFAULT_KBPS, (unsigned)TS_GLOBAL_KBPS,
        (unsigned)TS_DEFAULT_UDP_PORT, (unsigned)TS_DEFAULT_TCP_PORT, (unsigned)TS_MTU_PAYLOAD,
        (unsigned)TS_MTU_PAYLOAD, (unsigned)TSD_ARG_MIN_KEY_MS, (unsigned)TSD_ARG_MAX_KEY_MS,
        (unsigned)TS_KEYFRAME_PERIOD_MS, (unsigned)TSD_ARG_MAX_NOISE_S,
        TSD_EXIT_OK, TSD_EXIT_RUNTIME, TSD_EXIT_USAGE, TSD_EXIT_INIT);
}

/* Strict decimal parser: digits only, no sign, no overflow, value in [lo, hi]. */
static bool parse_u32(const char *s, uint32_t lo, uint32_t hi, uint32_t *out)
{
    bool ok = (s != NULL) && (s[0] != '\0');
    uint64_t v = 0U;
    for (size_t i = 0U; ok && (s[i] != '\0'); i++) {
        char ch = s[i];
        if ((ch >= '0') && (ch <= '9') && (i < 10U)) {
            uint32_t digit = (uint32_t)(uint8_t)ch - (uint32_t)(uint8_t)'0';
            v = (v * 10U) + (uint64_t)digit;
        } else {
            ok = false;
        }
    }
    if (ok && (v >= (uint64_t)lo) && (v <= (uint64_t)hi)) {
        *out = (uint32_t)v;
    } else {
        ok = false;
    }
    return ok;
}

static bool set_path(ts_cap_cfg_t *cap, const char *path)
{
    bool ok = false;
    size_t n = strlen(path);
    if (n < sizeof(cap->path)) {
        (void)memcpy(cap->path, path, n + 1U);
        ok = true;
    }
    return ok;
}

/* "synth" | "file:PATH" | "pipe" | "pipe:PATH" | "v4l2" | "v4l2:/dev/videoN" */
static bool parse_source(const char *arg, ts_cap_cfg_t *cap)
{
    bool ok = false;
    char kind[8];
    const char *colon = strchr(arg, ':');
    size_t klen = strlen(arg);
    if (colon != NULL) {
        ptrdiff_t at = colon - arg;
        klen = (size_t)at;
    }
    if (klen < sizeof(kind)) {
        (void)memcpy(kind, arg, klen);
        kind[klen] = '\0';
        if (ts_cap_kind_parse(kind, &cap->kind) == TS_OK) {
            const char *path = (colon != NULL) ? &colon[1] : "";
            switch (cap->kind) {
            case TS_CAP_SYNTH:
                ok = (colon == NULL);
                break;
            case TS_CAP_FILE:
                ok = (path[0] != '\0') && set_path(cap, path);
                break;
            case TS_CAP_PIPE:
                ok = set_path(cap, path);   /* "" = stdin */
                break;
            case TS_CAP_V4L2:
                ok = set_path(cap, (path[0] != '\0') ? path : "/dev/video0");
                break;
            default:
                ok = false;
                break;
            }
        }
    }
    return ok;
}

typedef enum parse_res {
    PARSE_RUN = 0,
    PARSE_EXIT_OK,
    PARSE_EXIT_USAGE
} parse_res_t;

static parse_res_t parse_args(int argc, char **argv, tsd_cfg_t *cfg)
{
    parse_res_t res = PARSE_RUN;
    int i = 1;
    while ((res == PARSE_RUN) && (i < argc)) {
        const char *opt = argv[i];
        const char *val = ((i + 1) < argc) ? argv[i + 1] : NULL;
        uint32_t v = 0U;
        bool ok = true;
        bool takes_value = true;
        if (strcmp(opt, "--help") == 0) {
            usage(stdout);
            res = PARSE_EXIT_OK;
            takes_value = false;
        } else if (strcmp(opt, "--version") == 0) {
            (void)printf("tinystreamd protocol %u (pool %lu bytes)\n",
                         (unsigned)TS_PROTOCOL_VERSION, (unsigned long)ts_pool_size());
            res = PARSE_EXIT_OK;
            takes_value = false;
        } else if (strcmp(opt, "--no-loss-adapt") == 0) {
            cfg->loss_adapt = false;
            takes_value = false;
        } else if (val == NULL) {
            ok = false;
        } else if (strcmp(opt, "--source") == 0) {
            ok = parse_source(val, &cfg->capture);
        } else if (strcmp(opt, "--fps") == 0) {
            ok = parse_u32(val, 1U, TSD_ARG_MAX_FPS, &v);
            cfg->fps = v;
            cfg->capture.fps = v;
        } else if (strcmp(opt, "--kbps") == 0) {
            ok = parse_u32(val, 1U, TSD_ARG_MAX_KBPS, &v);
            cfg->kbps_client = v;
        } else if (strcmp(opt, "--kbps-global") == 0) {
            ok = parse_u32(val, 1U, TSD_ARG_MAX_KBPS, &v);
            cfg->kbps_global = v;
        } else if (strcmp(opt, "--udp-port") == 0) {
            ok = parse_u32(val, 0U, 65535U, &v);
            cfg->udp_port = (uint16_t)v;
        } else if (strcmp(opt, "--tcp-port") == 0) {
            ok = parse_u32(val, 1U, 65535U, &v);
            cfg->tcp_port = (uint16_t)v;
        } else if (strcmp(opt, "--mtu") == 0) {
            /* fragment size is fixed by PROTOCOL.md v2: only check that the link can carry it */
            ok = parse_u32(val, TS_MTU_PAYLOAD, 65535U, &v);
        } else if (strcmp(opt, "--seq-start") == 0) {
            ok = parse_u32(val, 0U, 65535U, &v);
            cfg->seq_start = (uint16_t)v;
        } else if (strcmp(opt, "--keyframe-interval") == 0) {
            ok = parse_u32(val, TSD_ARG_MIN_KEY_MS, TSD_ARG_MAX_KEY_MS, &v);
            cfg->keyframe_period_ms = v;
        } else if (strcmp(opt, "--seed") == 0) {
            ok = parse_u32(val, 0U, 0xFFFFFFFFU, &v);
            cfg->capture.seed = v;
        } else if (strcmp(opt, "--noise-after-s") == 0) {
            ok = parse_u32(val, 0U, TSD_ARG_MAX_NOISE_S, &v);
            cfg->capture.noise_after_ms = v * 1000U;
        } else if (strcmp(opt, "--stream-id") == 0) {
            ok = parse_u32(val, 1U, 0xFFFFFFFFU, &v);
            cfg->stream_id = v;
        } else {
            ok = false;
        }
        if (!ok) {
            (void)fprintf(stderr, "tinystreamd: bad or missing value for '%s'\n", opt);
            usage(stderr);
            res = PARSE_EXIT_USAGE;
        }
        i += takes_value ? 2 : 1;
    }
    return res;
}

static bool install_signals(void)
{
    struct sigaction sa;
    struct sigaction ign;
    bool ok;
    (void)memset(&sa, 0, sizeof(sa));
    (void)memset(&ign, 0, sizeof(ign));
    sa.sa_handler = on_signal;          /* no SA_RESTART: poll() returns EINTR at once */
    (void)sigemptyset(&sa.sa_mask);
    ign.sa_handler = SIG_IGN;
    (void)sigemptyset(&ign.sa_mask);
    ok = (sigaction(SIGTERM, &sa, NULL) == 0) && (sigaction(SIGINT, &sa, NULL) == 0) &&
         (sigaction(SIGPIPE, &ign, NULL) == 0);
    return ok;
}

/* Per-start stream id: wall clock nanoseconds mixed with the pid. */
static uint32_t make_stream_id(void)
{
    struct timespec ts;
    uint32_t id = (uint32_t)getpid();
    if (clock_gettime(CLOCK_REALTIME, &ts) == 0) {
        id ^= (uint32_t)ts.tv_nsec ^ ((uint32_t)ts.tv_sec << 7);
    }
    return (id != 0U) ? id : 1U;
}

static int run(tsd_t *d, ts_pool_t *pool)
{
    int rc = TSD_EXIT_OK;
    while ((g_stop == 0) && (rc == TSD_EXIT_OK)) {
        uint32_t now = ts_io_now_ms(&pool->io.io);
        ts_err_t r = tsd_tick(d, now);
        if (r == TS_E_IO) {
            (void)fprintf(stderr, "tinystreamd: capture source failed (%s), exiting\n", ts_err_str(r));
            rc = TSD_EXIT_RUNTIME;
        } else {
            uint32_t wait = tsd_next_due_ms(d) - now;
            if (wait >= 0x80000000U) {
                wait = 0U;
            }
            if (wait > TSD_MAX_WAIT_MS) {
                wait = TSD_MAX_WAIT_MS;
            }
            if (ts_io_posix_wait(&pool->io, wait) == TS_IO_ERR) {
                (void)fprintf(stderr, "tinystreamd: poll failed, exiting\n");
                rc = TSD_EXIT_RUNTIME;
            }
        }
    }
    return rc;
}

int main(int argc, char **argv)
{
    int rc = TSD_EXIT_OK;
    tsd_cfg_t cfg;
    parse_res_t pr;
    tsd_cfg_default(&cfg);
    pr = parse_args(argc, argv, &cfg);
    if (pr == PARSE_EXIT_USAGE) {
        rc = TSD_EXIT_USAGE;
    } else if (pr == PARSE_EXIT_OK) {
        rc = TSD_EXIT_OK;
    } else if (!install_signals()) {
        (void)fprintf(stderr, "tinystreamd: cannot install signal handlers\n");
        rc = TSD_EXIT_INIT;
    } else {
        ts_pool_t *pool = ts_pool_get();
        tsd_t *d = NULL;
        (void)setvbuf(stdout, NULL, _IONBF, 0U);   /* no lazily malloc'ed stdio buffer after the seal */
        if (cfg.stream_id == 0U) {
            cfg.stream_id = make_stream_id();
        }
        ts_pool_init(pool);
        if (ts_io_posix_open(&pool->io, cfg.udp_port, cfg.tcp_port) != TS_OK) {
            (void)fprintf(stderr, "tinystreamd: cannot bind udp %u / tcp %u\n",
                          (unsigned)cfg.udp_port, (unsigned)cfg.tcp_port);
            rc = TSD_EXIT_INIT;
        } else {
            d = tsd_create(&cfg, &pool->io.io, pool);
            if (d == NULL) {
                if (cfg.capture.kind == TS_CAP_FILE) {
                    /* the HIL clip is recorded once on the Pi; say how to get it */
                    (void)fprintf(stderr, "tinystreamd: cannot open capture file '%s' (raw 320x240 gray8 frames): "
                                  "record it on the Pi or generate a stand-in with "
                                  "'python3 scripts/make_clip.py' (README, \"Running the daemon\")\n",
                                  cfg.capture.path);
                } else {
                    (void)fprintf(stderr, "tinystreamd: cannot open capture source\n");
                }
                rc = TSD_EXIT_INIT;
            }
        }
        if (d != NULL) {
            ts_init_done();
            (void)fprintf(stderr, "tinystreamd: streaming on udp %u, control tcp %u, stream id %08x\n",
                          (unsigned)ts_io_posix_udp_port(&pool->io), (unsigned)cfg.tcp_port,
                          (unsigned)d->cfg.stream_id);
            rc = run(d, pool);
            tsd_stop(d);
            if (rc == TSD_EXIT_OK) {
                (void)fprintf(stderr, "tinystreamd: stopped\n");
            }
        }
        ts_io_posix_close(&pool->io);
    }
    return rc;
}
