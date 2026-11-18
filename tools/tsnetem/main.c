/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tools/tsnetem/main.c
 * Userspace bidirectional UDP chaos relay. The datagram decisions come from
 * the same ts_netem_filter_len() that the loopback tests use in-process
 * (tools/tsnetem/lib/netem.c), so a scenario means the same thing on the
 * HIL link and in tests.
 *
 *   upstream peer (daemon) --UDP--> [listen socket] --filter--> [forward socket] --UDP--> forward (receiver)
 *   upstream peer (daemon) <--UDP-- [listen socket] <--filter-- [forward socket] <--UDP-- forward (NACKs)
 *
 * The upstream peer is learned from the first datagram on the listen socket
 * (or fixed with --peer). tinystream wiring on one laptop: tsrecv binds the
 * --forward port and announces the --listen port in HELLO, so the daemon
 * sends DATA to the relay and the receiver NACKs back through it.
 *
 * Live control (--slider, one command per line on stdin):
 *   5 | 5%        loss = 5 %          + / - / ++ / --   loss +-1 / +-5 %
 *   burst 50      drop the next 50    blackout 2s       cable pulled for 2 s
 *   pull | plug   cable on / off      <spec>            e.g. "dup=1,reorder=5,hold=15ms"
 *   s | stats     print counters      q | quit          exit
 * Scenario file (--scenario): one step per line, cumulative, e.g.
 *   t=5s loss=5%
 *   t=20s blackout 2s
 *   t=30s clean
 * Single-threaded poll() loop, no dynamic memory (tables are static).
 */
#include <stdio.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <errno.h>
#include <signal.h>
#include <poll.h>
#include <time.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include "ts_config.h"
#include "ts_netem.h"
#include "relay_peer.h"

#define RELAY_HOLD_MAX     4096U      /**< datagrams held for delay / reorder */
#define RELAY_DGRAM_MAX    TS_IO_DGRAM_MAX
#define RELAY_RX_MAX       65536U
#define RELAY_LINE_MAX     512U
#define RELAY_DRAIN_MAX    256U       /**< datagrams per socket per loop iteration */
#define RELAY_POLL_MAX_MS  50U

typedef enum relay_dir {
    DIR_UP = 0,     /* listen -> forward (daemon -> receiver) */
    DIR_DOWN = 1    /* forward -> peer   (receiver -> daemon) */
} relay_dir_t;

typedef union relay_sa {
    struct sockaddr    sa;
    struct sockaddr_in in;
} relay_sa_t;

typedef struct relay_held {
    uint32_t    release_ms;
    relay_dir_t dir;
    uint16_t    len;
    uint8_t     data[RELAY_DGRAM_MAX];
} relay_held_t;

typedef struct relay_dir_stats {
    uint32_t rx;
    uint32_t tx;
    uint64_t rx_bytes;
    uint64_t tx_bytes;
    uint32_t send_err;
} relay_dir_stats_t;

typedef struct relay {
    int              fd_listen;
    int              fd_fwd;
    relay_sa_t       fwd;
    relay_sa_t       peer;
    bool             have_peer;
    bool             peer_fixed;
    bool             filter_up;
    bool             filter_down;
    bool             slider;
    bool             quiet;
    uint32_t         stats_ms;
    uint32_t         start_ms;
    uint32_t         next_stats_ms;
    bool             blackout;
    uint32_t         blackout_end_ms;
    ts_netem_cfg_t   cfg;
    ts_netem_t       nm;
    relay_dir_stats_t ds[2];
    uint32_t         no_peer;
    uint32_t         foreign;
    uint32_t         oversize;
    uint32_t         hold_overflow;
    uint16_t         held_count;
    char             line[RELAY_LINE_MAX];
    size_t           line_len;
} relay_t;

static relay_t           g_relay;
static relay_held_t      g_held[RELAY_HOLD_MAX];
static uint8_t           g_rx[RELAY_RX_MAX];
static ts_netem_script_t g_script;
static volatile sig_atomic_t g_stop = 0;

static void on_signal(int sig)
{
    (void)sig;
    g_stop = 1;
}

static uint32_t mono_ms(void)
{
    struct timespec ts;
    uint64_t ms = 0U;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) == 0) {
        ms = ((uint64_t)ts.tv_sec * 1000U) + ((uint64_t)ts.tv_nsec / 1000000U);
    }
    return (uint32_t)(ms & 0xFFFFFFFFU);
}

/* ---- argument helpers ------------------------------------------------------ */

static bool parse_u32(const char *s, uint32_t max, uint32_t *out)
{
    bool ok = (s != NULL) && (s[0] != '\0');
    uint64_t v = 0U;
    for (size_t i = 0U; ok && (s[i] != '\0'); i++) {
        if ((s[i] < '0') || (s[i] > '9')) {
            ok = false;
        } else {
            v = (v * 10U) + ((uint64_t)(uint8_t)s[i] - 48U);
            if (v > (uint64_t)max) {
                ok = false;
            }
        }
    }
    if (ok) {
        *out = (uint32_t)v;
    }
    return ok;
}

/** "[IP:]PORT" (IP defaults to `def_ip`, e.g. "0.0.0.0" or "127.0.0.1"). */
static bool parse_addr(const char *s, const char *def_ip, relay_sa_t *out)
{
    char ip[64];
    const char *port_s = s;
    const char *colon = strrchr(s, ':');
    uint32_t port = 0U;
    bool ok = true;

    if (colon != NULL) {
        size_t n = strlen(s) - strlen(colon);
        if ((n == 0U) || (n >= sizeof(ip))) {
            ok = false;
        } else {
            (void)memcpy(ip, s, n);
            ip[n] = '\0';
            port_s = &colon[1];
        }
    } else {
        (void)snprintf(ip, sizeof(ip), "%s", def_ip);
    }
    (void)memset(out, 0, sizeof(*out));
    out->in.sin_family = AF_INET;
    if (ok) {
        ok = parse_u32(port_s, 65535U, &port);
    }
    if (ok) {
        out->in.sin_port = htons((uint16_t)port);
        ok = (inet_pton(AF_INET, ip, &out->in.sin_addr) == 1);
    }
    return ok;
}

static void addr_str(const relay_sa_t *a, char *buf, size_t cap)
{
    char ip[INET_ADDRSTRLEN];
    if (inet_ntop(AF_INET, &a->in.sin_addr, ip, (socklen_t)sizeof(ip)) == NULL) {
        (void)snprintf(ip, sizeof(ip), "?");
    }
    (void)snprintf(buf, cap, "%s:%u", ip, (unsigned)ntohs(a->in.sin_port));
}

static bool addr_same(const relay_sa_t *a, const relay_sa_t *b)
{
    return (a->in.sin_addr.s_addr == b->in.sin_addr.s_addr) && (a->in.sin_port == b->in.sin_port);
}

static int open_udp(const relay_sa_t *bind_addr)
{
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd >= 0) {
        int fl = fcntl(fd, F_GETFL, 0);
        if ((fl < 0) || (fcntl(fd, F_SETFL, fl | O_NONBLOCK) < 0) ||
            (bind(fd, &bind_addr->sa, (socklen_t)sizeof(bind_addr->in)) != 0)) {
            (void)close(fd);
            fd = -1;
        }
    }
    return fd;
}

static void print_cfg(FILE *out, const ts_netem_cfg_t *c)
{
    static const char *const scope[] = { "any", "header", "payload" };
    const char *sc = (c->bitflip_scope <= 2U) ? scope[c->bitflip_scope] : "?";
    (void)fprintf(out,
                  "loss=%u%% burst=%u period=%lums reorder=%u%% hold=%lums dup=%u%% flip=%u%%(%s) "
                  "trunc=%u%% throttle=%lukbps delay=%lums cable=%s",
                  (unsigned)c->loss_pct, (unsigned)c->burst_len, (unsigned long)c->burst_period_ms,
                  (unsigned)c->reorder_pct, (unsigned long)c->reorder_delay_ms, (unsigned)c->dup_pct,
                  (unsigned)c->bitflip_pct, sc, (unsigned)c->truncate_pct, (unsigned long)c->throttle_kbps,
                  (unsigned long)c->delay_ms, c->cable_pulled ? "PULLED" : "ok");
}

static void print_stats(relay_t *r, uint32_t now_ms)
{
    const ts_netem_stats_t *st = ts_netem_stats(&r->nm);
    uint32_t t = now_ms - r->start_ms;
    uint32_t secs = t / 1000U;
    uint32_t tenths = (t % 1000U) / 100U;
    (void)fprintf(stderr, "[%5lu.%01lus] ", (unsigned long)secs, (unsigned long)tenths);
    print_cfg(stderr, &r->nm.cfg);
    (void)fprintf(stderr,
                  "\n          up %lu->%lu  down %lu->%lu  seen %lu drop %lu (burst %lu thr %lu cable %lu) "
                  "dup %lu reord %lu flip %lu trunc %lu held %u ovf %lu nopeer %lu\n",
                  (unsigned long)r->ds[DIR_UP].rx, (unsigned long)r->ds[DIR_UP].tx,
                  (unsigned long)r->ds[DIR_DOWN].rx, (unsigned long)r->ds[DIR_DOWN].tx,
                  (unsigned long)st->seen, (unsigned long)st->dropped, (unsigned long)st->dropped_burst,
                  (unsigned long)st->dropped_throttle, (unsigned long)st->dropped_cable,
                  (unsigned long)st->duplicated, (unsigned long)st->reordered, (unsigned long)st->flipped,
                  (unsigned long)st->truncated, (unsigned)r->held_count, (unsigned long)r->hold_overflow,
                  (unsigned long)r->no_peer);
}

/* ---- forwarding ------------------------------------------------------------- */

static void relay_send(relay_t *r, relay_dir_t dir, const uint8_t *data, uint16_t len)
{
    int fd = (dir == DIR_UP) ? r->fd_fwd : r->fd_listen;
    const relay_sa_t *to = (dir == DIR_UP) ? &r->fwd : &r->peer;
    if ((dir == DIR_DOWN) && !r->have_peer) {
        r->no_peer++;
    } else {
        ssize_t n = sendto(fd, data, (size_t)len, 0, &to->sa, (socklen_t)sizeof(to->in));
        if (n < 0) {
            r->ds[dir].send_err++;
        } else {
            r->ds[dir].tx++;
            r->ds[dir].tx_bytes += (uint64_t)len;
        }
    }
}

static void relay_hold(relay_t *r, relay_dir_t dir, const uint8_t *data, uint16_t len, uint32_t release_ms)
{
    if ((r->held_count >= RELAY_HOLD_MAX) || (len > RELAY_DGRAM_MAX)) {
        r->hold_overflow++;
    } else {
        relay_held_t *h = &g_held[r->held_count];
        h->release_ms = release_ms;
        h->dir = dir;
        h->len = len;
        (void)memcpy(h->data, data, (size_t)len);
        r->held_count++;
    }
}

static bool due(uint32_t now_ms, uint32_t at_ms)
{
    return (uint32_t)(now_ms - at_ms) < 0x80000000U;
}

/** Release due datagrams in arrival order; returns ms until the next release (or RELAY_POLL_MAX_MS). */
static uint32_t relay_release(relay_t *r, uint32_t now_ms)
{
    uint16_t w = 0U;
    uint32_t wait = RELAY_POLL_MAX_MS;
    for (uint16_t i = 0U; i < r->held_count; i++) {
        if (due(now_ms, g_held[i].release_ms)) {
            relay_send(r, g_held[i].dir, g_held[i].data, g_held[i].len);
        } else {
            uint32_t left = g_held[i].release_ms - now_ms;
            if (left < wait) {
                wait = left;
            }
            if (w != i) {
                g_held[w] = g_held[i];
            }
            w++;
        }
    }
    r->held_count = w;
    return wait;
}

static void relay_process(relay_t *r, relay_dir_t dir, uint8_t *data, uint16_t len, uint32_t now_ms)
{
    bool filtered = (dir == DIR_UP) ? r->filter_up : r->filter_down;
    ts_netem_verdict_t v = TS_NETEM_PASS;
    uint32_t delay = 0U;
    uint16_t l = len;
    if (filtered) {
        v = ts_netem_filter_len(&r->nm, data, &l, now_ms, &delay);
    }
    if (v != TS_NETEM_DROP) {
        uint8_t copies = (v == TS_NETEM_DUP) ? 2U : 1U;
        for (uint8_t c = 0U; c < copies; c++) {
            if (delay == 0U) {
                relay_send(r, dir, data, l);
            } else {
                relay_hold(r, dir, data, l, now_ms + delay);
            }
        }
    }
}

static void relay_drain(relay_t *r, relay_dir_t dir, uint32_t now_ms)
{
    int fd = (dir == DIR_UP) ? r->fd_listen : r->fd_fwd;
    bool more = true;
    for (uint32_t k = 0U; more && (k < RELAY_DRAIN_MAX); k++) {
        relay_sa_t from;
        socklen_t flen = (socklen_t)sizeof(from.in);
        ssize_t n;
        (void)memset(&from, 0, sizeof(from));
        n = recvfrom(fd, g_rx, sizeof(g_rx), 0, &from.sa, &flen);
        if (n < 0) {
            more = false;   /* EAGAIN or error: try again next poll */
        } else if ((size_t)n > RELAY_DGRAM_MAX) {
            r->oversize++;
        } else {
            r->ds[dir].rx++;
            r->ds[dir].rx_bytes += (uint64_t)n;
            if (dir == DIR_UP) {
                /* the peer is learned from the first datagram only (or fixed with --peer) */
                relay_peer_action_t act = relay_peer_action(r->peer_fixed, r->have_peer,
                                                            r->have_peer && addr_same(&from, &r->peer));
                if (act == RELAY_PEER_LEARN) {
                    char s[64];
                    r->peer = from;
                    r->have_peer = true;
                    addr_str(&from, s, sizeof(s));
                    if (!r->quiet) {
                        (void)fprintf(stderr, "tsnetem: upstream peer is %s\n", s);
                    }
                } else if (act == RELAY_PEER_FOREIGN) {
                    r->foreign++;   /* still relayed downstream; the peer stays */
                } else {
                    /* the known peer */
                }
                relay_process(r, dir, g_rx, (uint16_t)n, now_ms);
            } else if (addr_same(&from, &r->fwd)) {
                relay_process(r, dir, g_rx, (uint16_t)n, now_ms);
            } else {
                r->foreign++;
            }
        }
    }
}

/* ---- live control ------------------------------------------------------------ */

static void relay_apply(relay_t *r, const ts_netem_cfg_t *cfg, uint32_t now_ms)
{
    r->cfg = *cfg;
    ts_netem_set(&r->nm, &r->cfg, now_ms);
    if ((r->cfg.burst_len > 0U) && (r->cfg.burst_period_ms == 0U)) {
        ts_netem_burst_now(&r->nm, r->cfg.burst_len);
        r->cfg.burst_len = 0U;   /* one-shot */
    }
}

static void relay_set_loss(relay_t *r, int32_t loss, uint32_t now_ms)
{
    ts_netem_cfg_t c = r->nm.cfg;
    int32_t l = loss;
    if (l < 0) {
        l = 0;
    }
    if (l > 100) {
        l = 100;
    }
    c.loss_pct = (uint8_t)l;
    relay_apply(r, &c, now_ms);
}

static void relay_command(relay_t *r, char *line, uint32_t now_ms)
{
    char *p = line;
    size_t n;
    int32_t loss = (int32_t)r->nm.cfg.loss_pct;
    uint32_t v = 0U;
    ts_netem_cfg_t c = r->nm.cfg;

    while ((*p == ' ') || (*p == '\t')) {
        p++;
    }
    n = strlen(p);
    while ((n > 0U) && ((p[n - 1U] == ' ') || (p[n - 1U] == '\t') || (p[n - 1U] == '\r') || (p[n - 1U] == '\n'))) {
        p[n - 1U] = '\0';
        n--;
    }
    if (n == 0U) {
        print_stats(r, now_ms);
    } else if ((strcmp(p, "q") == 0) || (strcmp(p, "quit") == 0)) {
        g_stop = 1;
    } else if ((strcmp(p, "s") == 0) || (strcmp(p, "stats") == 0)) {
        print_stats(r, now_ms);
    } else if ((strcmp(p, "?") == 0) || (strcmp(p, "help") == 0)) {
        (void)fprintf(stderr, "commands: N | N%% | + | - | ++ | -- | burst N | blackout 2s | pull | plug | <spec> | s | q\n");
    } else if (strcmp(p, "+") == 0) {
        relay_set_loss(r, loss + 1, now_ms);
    } else if (strcmp(p, "-") == 0) {
        relay_set_loss(r, loss - 1, now_ms);
    } else if (strcmp(p, "++") == 0) {
        relay_set_loss(r, loss + 5, now_ms);
    } else if (strcmp(p, "--") == 0) {
        relay_set_loss(r, loss - 5, now_ms);
    } else if (strncmp(p, "burst ", 6U) == 0) {
        if (parse_u32(&p[6], 0xFFFFU, &v)) {
            ts_netem_burst_now(&r->nm, (uint16_t)v);
        } else {
            (void)fprintf(stderr, "tsnetem: bad burst count\n");
        }
    } else if (strncmp(p, "blackout", 8U) == 0) {
        char spec[64];
        ts_netem_cfg_t tmp;
        const char *d = &p[8];
        while ((*d == ' ') || (*d == '=')) {
            d++;
        }
        (void)memset(&tmp, 0, sizeof(tmp));
        /* reuse the spec time syntax ("2s", "1500ms") through the delay key */
        (void)snprintf(spec, sizeof(spec), "delay=%s", d);
        if ((ts_netem_cfg_apply(spec, &tmp) == TS_OK) && (tmp.delay_ms > 0U)) {
            r->blackout = true;
            r->blackout_end_ms = now_ms + tmp.delay_ms;
            c.cable_pulled = true;
            relay_apply(r, &c, now_ms);
        } else {
            (void)fprintf(stderr, "tsnetem: usage: blackout <dur>, e.g. blackout 2s\n");
        }
    } else {
        /* a bare number is the loss slider */
        char spec[RELAY_LINE_MAX + 8U];
        if (((p[0] >= '0') && (p[0] <= '9')) && (strchr(p, '=') == NULL)) {
            (void)snprintf(spec, sizeof(spec), "loss=%s", p);
        } else {
            (void)snprintf(spec, sizeof(spec), "%s", p);
        }
        if (ts_netem_cfg_apply(spec, &c) == TS_OK) {
            relay_apply(r, &c, now_ms);
        } else {
            (void)fprintf(stderr, "tsnetem: cannot parse '%s'\n", p);
        }
    }
    if (!r->quiet && (g_stop == 0)) {
        (void)fprintf(stderr, "tsnetem: ");
        print_cfg(stderr, &r->nm.cfg);
        (void)fprintf(stderr, "\n");
    }
}

static void relay_stdin(relay_t *r, uint32_t now_ms)
{
    char buf[256];
    ssize_t n = read(STDIN_FILENO, buf, sizeof(buf));
    if (n == 0) {
        r->slider = false;   /* EOF: stop polling stdin */
    } else if (n > 0) {
        for (ssize_t i = 0; i < n; i++) {
            char ch = buf[i];
            if (ch == '\n') {
                r->line[r->line_len] = '\0';
                relay_command(r, r->line, now_ms);
                r->line_len = 0U;
            } else if (r->line_len < (RELAY_LINE_MAX - 1U)) {
                r->line[r->line_len] = ch;
                r->line_len++;
            } else {
                /* overlong line: ignore the tail */
            }
        }
    } else {
        /* EINTR / EAGAIN: retry on the next poll */
    }
}

/* ---- setup -------------------------------------------------------------------- */

static void usage(FILE *out)
{
    (void)fprintf(out,
        "usage: tsnetem --listen [IP:]PORT --forward IP:PORT [options]\n"
        "  --listen [IP:]PORT   where the upstream peer (daemon) sends to\n"
        "  --forward IP:PORT    where datagrams are relayed to (receiver)\n"
        "  --peer IP:PORT       fixed upstream peer (default: learned from the first datagram)\n"
        "  --bind [IP:]PORT     local address of the forward socket (default: ephemeral)\n"
        "  --spec SPEC          initial impairment, e.g. loss=5,burst=50,reorder=2,hold=15ms,dup=1,\n"
        "                       flip=1,scope=payload,trunc=1,throttle=2000,delay=10,cable=0\n"
        "  --seed N             PRNG seed (default 1)\n"
        "  --scenario FILE      timeline, one step per line: 't=5s loss=5%%', 't=20s blackout 2s'\n"
        "  --filter up|down|both  directions that pass the filter (default both)\n"
        "  --slider             live control on stdin (loss %%, + / -, burst N, blackout 2s, pull, plug, spec, s, q)\n"
        "  --stats-ms MS        status line period (default 1000, 0 = off)\n"
        "  --quiet              no status output\n"
        "  --check SPEC         parse SPEC, print it and exit\n"
        "  --check-scenario F   parse a scenario file, print the steps and exit\n"
        "  --version | --help\n");
}

static bool load_scenario(const char *path, const ts_netem_cfg_t *base, const ts_netem_scenario_t **out)
{
    bool ok = true;
    FILE *f = fopen(path, "r");
    ts_netem_script_init(&g_script, base);
    if (f == NULL) {
        (void)fprintf(stderr, "tsnetem: cannot open %s: %s\n", path, strerror(errno));
        ok = false;
    } else {
        char line[RELAY_LINE_MAX];
        while (ok && (fgets(line, (int)sizeof(line), f) != NULL)) {
            ts_err_t rc = ts_netem_script_line(&g_script, line);
            if (rc != TS_OK) {
                (void)fprintf(stderr, "tsnetem: %s:%lu: %s in '%s'\n", path, (unsigned long)g_script.lines,
                              ts_err_str(rc), line);
                ok = false;
            }
        }
        (void)fclose(f);
    }
    if (ok) {
        *out = ts_netem_script_finish(&g_script);
    }
    return ok;
}

static void print_scenario(const ts_netem_scenario_t *s)
{
    for (uint8_t i = 0U; i < s->count; i++) {
        uint32_t secs = s->steps[i].at_ms / 1000U;
        uint32_t ms = s->steps[i].at_ms % 1000U;
        (void)fprintf(stdout, "t=%lu.%03lus ", (unsigned long)secs, (unsigned long)ms);
        print_cfg(stdout, &s->steps[i].cfg);
        (void)fprintf(stdout, "\n");
    }
}

typedef struct relay_args {
    const char *listen;
    const char *forward;
    const char *peer;
    const char *bind_fwd;
    const char *spec;
    const char *scenario;
    const char *check;
    const char *check_scenario;
    const char *filter;
    uint32_t    seed;
    bool        have_seed;
    bool        help;
    bool        version;
} relay_args_t;

static bool parse_args(int argc, char **argv, relay_args_t *a, relay_t *r)
{
    bool ok = true;
    int i = 1;
    (void)memset(a, 0, sizeof(*a));
    while (ok && (i < argc)) {
        const char *o = argv[i];
        const char *val = ((i + 1) < argc) ? argv[i + 1] : NULL;
        bool takes = true;
        if (strcmp(o, "--listen") == 0) {
            a->listen = val;
        } else if (strcmp(o, "--forward") == 0) {
            a->forward = val;
        } else if (strcmp(o, "--peer") == 0) {
            a->peer = val;
        } else if (strcmp(o, "--bind") == 0) {
            a->bind_fwd = val;
        } else if (strcmp(o, "--spec") == 0) {
            a->spec = val;
        } else if (strcmp(o, "--scenario") == 0) {
            a->scenario = val;
        } else if (strcmp(o, "--check") == 0) {
            a->check = val;
        } else if (strcmp(o, "--check-scenario") == 0) {
            a->check_scenario = val;
        } else if (strcmp(o, "--filter") == 0) {
            a->filter = val;
        } else if (strcmp(o, "--seed") == 0) {
            ok = parse_u32(val, 0xFFFFFFFFU, &a->seed);
            a->have_seed = true;
        } else if (strcmp(o, "--stats-ms") == 0) {
            ok = parse_u32(val, 3600000U, &r->stats_ms);
        } else {
            takes = false;
            if (strcmp(o, "--slider") == 0) {
                r->slider = true;
            } else if (strcmp(o, "--quiet") == 0) {
                r->quiet = true;
            } else if ((strcmp(o, "--help") == 0) || (strcmp(o, "-h") == 0)) {
                a->help = true;
            } else if (strcmp(o, "--version") == 0) {
                a->version = true;
            } else {
                (void)fprintf(stderr, "tsnetem: unknown option %s\n", o);
                ok = false;
            }
        }
        if (takes) {
            if (val == NULL) {
                (void)fprintf(stderr, "tsnetem: %s needs a value\n", o);
                ok = false;
            }
            i += 2;
        } else {
            i += 1;
        }
    }
    return ok;
}

static int run_checks(const relay_args_t *a, const ts_netem_cfg_t *base)
{
    int rc = 0;
    if (a->check != NULL) {
        ts_netem_cfg_t c;
        if (ts_netem_cfg_parse(a->check, &c) == TS_OK) {
            print_cfg(stdout, &c);
            (void)fprintf(stdout, "\n");
        } else {
            (void)fprintf(stderr, "tsnetem: invalid spec '%s'\n", a->check);
            rc = 1;
        }
    }
    if ((rc == 0) && (a->check_scenario != NULL)) {
        const ts_netem_scenario_t *sc = NULL;
        if (load_scenario(a->check_scenario, base, &sc)) {
            print_scenario(sc);
        } else {
            rc = 1;
        }
    }
    return rc;
}

static bool relay_setup(relay_t *r, const relay_args_t *a)
{
    relay_sa_t la;
    relay_sa_t ba;
    bool ok = true;

    r->filter_up = true;
    r->filter_down = true;
    if (a->filter != NULL) {
        if (strcmp(a->filter, "up") == 0) {
            r->filter_down = false;
        } else if (strcmp(a->filter, "down") == 0) {
            r->filter_up = false;
        } else if (strcmp(a->filter, "both") != 0) {
            (void)fprintf(stderr, "tsnetem: --filter up|down|both\n");
            ok = false;
        } else {
            /* both */
        }
    }
    if (ok && ((a->listen == NULL) || (a->forward == NULL))) {
        usage(stderr);
        ok = false;
    }
    if (ok && (!parse_addr(a->listen, "0.0.0.0", &la) || !parse_addr(a->forward, "127.0.0.1", &r->fwd))) {
        (void)fprintf(stderr, "tsnetem: bad --listen / --forward address\n");
        ok = false;
    }
    if (ok && (a->peer != NULL)) {
        ok = parse_addr(a->peer, "127.0.0.1", &r->peer);
        r->have_peer = ok;
        r->peer_fixed = ok;
        if (!ok) {
            (void)fprintf(stderr, "tsnetem: bad --peer address\n");
        }
    }
    if (ok) {
        ok = parse_addr((a->bind_fwd != NULL) ? a->bind_fwd : "0", "0.0.0.0", &ba);
        if (!ok) {
            (void)fprintf(stderr, "tsnetem: bad --bind address\n");
        }
    }
    if (ok) {
        r->fd_listen = open_udp(&la);
        r->fd_fwd = open_udp(&ba);
        if ((r->fd_listen < 0) || (r->fd_fwd < 0)) {
            (void)fprintf(stderr, "tsnetem: cannot bind UDP sockets: %s\n", strerror(errno));
            ok = false;
        }
    }
    if (ok && !r->quiet) {
        char s1[64];
        char s2[64];
        addr_str(&la, s1, sizeof(s1));
        addr_str(&r->fwd, s2, sizeof(s2));
        (void)fprintf(stderr, "tsnetem: relaying %s <-> %s (filter %s%s)\n", s1, s2,
                      r->filter_up ? "up" : "", r->filter_down ? " down" : "");
    }
    return ok;
}

static void relay_loop(relay_t *r)
{
    while (g_stop == 0) {
        struct pollfd pfd[3];
        nfds_t nfd = 2U;
        uint32_t now = mono_ms();
        uint32_t wait;
        int pr;

        if (r->blackout && due(now, r->blackout_end_ms)) {
            ts_netem_cfg_t c = r->nm.cfg;
            c.cable_pulled = false;
            r->blackout = false;
            relay_apply(r, &c, now);
            if (!r->quiet) {
                (void)fprintf(stderr, "tsnetem: blackout over\n");
            }
        }
        ts_netem_tick(&r->nm, now);
        wait = relay_release(r, now);
        if ((r->stats_ms > 0U) && !r->quiet && due(now, r->next_stats_ms)) {
            print_stats(r, now);
            r->next_stats_ms = now + r->stats_ms;
        }
        pfd[0].fd = r->fd_listen;
        pfd[0].events = POLLIN;
        pfd[0].revents = 0;
        pfd[1].fd = r->fd_fwd;
        pfd[1].events = POLLIN;
        pfd[1].revents = 0;
        if (r->slider) {
            pfd[2].fd = STDIN_FILENO;
            pfd[2].events = POLLIN;
            pfd[2].revents = 0;
            nfd = 3U;
        }
        pr = poll(pfd, nfd, (int)wait);
        if (pr > 0) {
            now = mono_ms();
            if ((pfd[0].revents & POLLIN) != 0) {
                relay_drain(r, DIR_UP, now);
            }
            if ((pfd[1].revents & POLLIN) != 0) {
                relay_drain(r, DIR_DOWN, now);
            }
            if ((nfd == 3U) && ((pfd[2].revents & (POLLIN | POLLHUP)) != 0)) {
                relay_stdin(r, now);
            }
        }
    }
}

int main(int argc, char **argv)
{
    relay_args_t a;
    relay_t *r = &g_relay;
    int rc = 0;
    ts_netem_cfg_t base;
    const ts_netem_scenario_t *sc = NULL;

    (void)memset(r, 0, sizeof(*r));
    r->fd_listen = -1;
    r->fd_fwd = -1;
    r->stats_ms = 1000U;
    (void)memset(&base, 0, sizeof(base));
    base.seed = 1U;

    if (!parse_args(argc, argv, &a, r)) {
        usage(stderr);
        rc = 2;
    } else if (a.help) {
        usage(stdout);
    } else if (a.version) {
        (void)printf("tsnetem 2.0 (protocol %u)\n", (unsigned)TS_PROTOCOL_VERSION);
    } else if ((a.spec != NULL) && (ts_netem_cfg_apply(a.spec, &base) != TS_OK)) {
        (void)fprintf(stderr, "tsnetem: invalid --spec '%s'\n", a.spec);
        rc = 2;
    } else {
        if (a.have_seed) {
            base.seed = a.seed;
        }
        if ((a.check != NULL) || (a.check_scenario != NULL)) {
            rc = run_checks(&a, &base);
        } else if ((a.scenario != NULL) && !load_scenario(a.scenario, &base, &sc)) {
            rc = 2;
        } else if (!relay_setup(r, &a)) {
            rc = 2;
        } else {
            struct sigaction sa;
            uint32_t now = mono_ms();
            (void)memset(&sa, 0, sizeof(sa));
            sa.sa_handler = on_signal;
            (void)sigemptyset(&sa.sa_mask);
            (void)sigaction(SIGINT, &sa, NULL);
            (void)sigaction(SIGTERM, &sa, NULL);

            r->cfg = base;
            r->start_ms = now;
            r->next_stats_ms = now;
            ts_netem_init(&r->nm, &base, now);
            if (sc != NULL) {
                ts_netem_scenario_start(&r->nm, sc, now);
            }
            relay_loop(r);
            if (!r->quiet) {
                print_stats(r, mono_ms());
            }
        }
    }
    if (r->fd_listen >= 0) {
        (void)close(r->fd_listen);
    }
    if (r->fd_fwd >= 0) {
        (void)close(r->fd_fwd);
    }
    return rc;
}
