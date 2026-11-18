/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- src/io/io_posix.c
 * Real sockets behind ts_io_t (include/ts_io.h): one non-blocking UDP socket
 * for DATA out / NACK in, an optional non-blocking TCP listener, up to
 * TS_IO_MAX_TCP control connections (handle == slot index), poll() for the
 * main loop and CLOCK_MONOTONIC for now_ms. Every call is non-blocking:
 * EAGAIN / EWOULDBLOCK / ENOBUFS map to "0 = would block", EINTR is retried.
 * No heap, no stdio.
 */
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <time.h>
#include <unistd.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include "ts_io.h"

#define POSIX_SNDBUF_BYTES  (1024 * 1024)   /* room for a keyframe burst to every client */
#define POSIX_LISTEN_BACKLOG 8
#define POSIX_MAX_POLL      (2U + TS_IO_MAX_TCP)

#ifdef MSG_NOSIGNAL
#define POSIX_SEND_FLAGS (MSG_DONTWAIT | MSG_NOSIGNAL)
#else
#define POSIX_SEND_FLAGS (MSG_DONTWAIT)
#endif

/* container-of: ts_io_t is the first member of ts_io_posix_t (vtable pattern) */
static ts_io_posix_t *posix_of(ts_io_t *io)
{
    /* cppcheck-suppress misra-c2012-11.3 ; ts_io_t is the first member of ts_io_posix_t */
    return (ts_io_posix_t *)io;
}

static bool posix_would_block(int e)
{
    return (e == EAGAIN) || (e == EWOULDBLOCK) || (e == ENOBUFS);
}

static void addr_to_sin(const ts_addr_t *a, struct sockaddr_in *sin)
{
    (void)memset(sin, 0, sizeof(*sin));
    sin->sin_family = AF_INET;
    sin->sin_port = htons(a->port);
    sin->sin_addr.s_addr = htonl(a->ip);
}

static void sin_to_addr(const struct sockaddr_in *sin, ts_addr_t *a)
{
    a->ip = ntohl(sin->sin_addr.s_addr);
    a->port = ntohs(sin->sin_port);
}

static bool set_nonblock(int fd)
{
    bool ok = false;
    int fl = fcntl(fd, F_GETFL);
    if (fl >= 0) {
        ok = (fcntl(fd, F_SETFL, fl | O_NONBLOCK) == 0);
    }
    return ok;
}

static bool tcp_slot_valid(const ts_io_posix_t *p, ts_io_handle_t h)
{
    return (h >= 0) && ((uint32_t)h < TS_IO_MAX_TCP) && (p->tcp_fd[(uint32_t)h] >= 0);
}

static ts_io_handle_t tcp_slot_put(ts_io_posix_t *p, int fd)
{
    ts_io_handle_t h = TS_IO_INVALID;
    for (uint32_t i = 0U; (i < TS_IO_MAX_TCP) && (h == TS_IO_INVALID); i++) {
        if (p->tcp_fd[i] < 0) {
            p->tcp_fd[i] = (int32_t)fd;
            h = (ts_io_handle_t)i;
        }
    }
    return h;
}

/* ---- UDP --------------------------------------------------------------------- */

static int32_t posix_udp_send(ts_io_t *io, const ts_addr_t *to, const uint8_t *buf, size_t len)
{
    int32_t rc = TS_IO_ERR;
    const ts_io_posix_t *p = posix_of(io);
    if ((p->udp_fd >= 0) && (to != NULL) && (buf != NULL) && (len > 0U) && (len <= (size_t)TS_IO_DGRAM_MAX)) {
        struct sockaddr_in sin;
        ssize_t n;
        addr_to_sin(to, &sin);
        do {
            /* cppcheck-suppress misra-c2012-11.3 ; BSD socket API takes struct sockaddr * */
            n = sendto((int)p->udp_fd, buf, len, POSIX_SEND_FLAGS, (const struct sockaddr *)&sin, (socklen_t)sizeof(sin));
        } while ((n < 0) && (errno == EINTR));
        if (n >= 0) {
            rc = (int32_t)n;
        } else if (posix_would_block(errno)) {
            rc = 0;
        } else {
            rc = TS_IO_ERR;
        }
    }
    return rc;
}

static int32_t posix_udp_recv(ts_io_t *io, ts_addr_t *from, uint8_t *buf, size_t cap)
{
    int32_t rc = TS_IO_ERR;
    const ts_io_posix_t *p = posix_of(io);
    if ((p->udp_fd >= 0) && (buf != NULL) && (cap > 0U)) {
        bool again = true;
        rc = 0;
        /* zero-length datagrams are consumed and skipped (bounded), so that a
         * return of 0 means "nothing queued" and never ends a drain loop early */
        for (uint32_t k = 0U; again && (k < TS_IO_UDP_EMPTY_SKIP); k++) {
            struct sockaddr_in sin;
            socklen_t sl = (socklen_t)sizeof(sin);
            ssize_t n;
            (void)memset(&sin, 0, sizeof(sin));
            do {
                /* cppcheck-suppress misra-c2012-11.3 ; BSD socket API takes struct sockaddr * */
                n = recvfrom((int)p->udp_fd, buf, cap, MSG_DONTWAIT, (struct sockaddr *)&sin, &sl);
            } while ((n < 0) && (errno == EINTR));
            if (n > 0) {
                if (from != NULL) {
                    sin_to_addr(&sin, from);
                }
                rc = (n > (ssize_t)INT32_MAX) ? INT32_MAX : (int32_t)n;
                again = false;
            } else if (n == 0) {
                rc = 0;   /* empty datagram: skip it and look at the next one */
            } else if (posix_would_block(errno) || (errno == ECONNREFUSED)) {
                rc = 0;   /* nothing queued; ICMP port unreachable from an earlier send is not fatal */
                again = false;
            } else {
                rc = TS_IO_ERR;
                again = false;
            }
        }
    }
    return rc;
}

/* ---- TCP --------------------------------------------------------------------- */

static ts_io_handle_t posix_tcp_accept(ts_io_t *io, ts_addr_t *peer)
{
    ts_io_handle_t h = TS_IO_INVALID;
    ts_io_posix_t *p = posix_of(io);
    if (p->listen_fd >= 0) {
        struct sockaddr_in sin;
        socklen_t sl = (socklen_t)sizeof(sin);
        int fd;
        (void)memset(&sin, 0, sizeof(sin));
        do {
            /* cppcheck-suppress misra-c2012-11.3 ; BSD socket API takes struct sockaddr * */
            fd = accept((int)p->listen_fd, (struct sockaddr *)&sin, &sl);
        } while ((fd < 0) && (errno == EINTR));
        if (fd >= 0) {
            int one = 1;
            (void)setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, (socklen_t)sizeof(one));
            if (set_nonblock(fd)) {
                h = tcp_slot_put(p, fd);
            }
            if (h == TS_IO_INVALID) {
                (void)close(fd);   /* no slot: the peer sees the connection closed */
            } else if (peer != NULL) {
                sin_to_addr(&sin, peer);
            } else {
                /* caller does not want the address */
            }
        } else {
            p->pending_accept = 0U;   /* EAGAIN: queue drained */
        }
    }
    return h;
}

static ts_io_handle_t posix_tcp_connect(ts_io_t *io, const ts_addr_t *to)
{
    ts_io_handle_t h = TS_IO_INVALID;
    ts_io_posix_t *p = posix_of(io);
    if (to != NULL) {
        int fd = socket(AF_INET, SOCK_STREAM, 0);
        if (fd >= 0) {
            struct sockaddr_in sin;
            int one = 1;
            addr_to_sin(to, &sin);
            (void)setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, (socklen_t)sizeof(one));
            if (set_nonblock(fd)) {
                int r;
                do {
                    /* cppcheck-suppress misra-c2012-11.3 ; BSD socket API takes struct sockaddr * */
                    r = connect(fd, (const struct sockaddr *)&sin, (socklen_t)sizeof(sin));
                } while ((r < 0) && (errno == EINTR));
                if ((r == 0) || (errno == EINPROGRESS)) {
                    h = tcp_slot_put(p, fd);
                }
            }
            if (h == TS_IO_INVALID) {
                (void)close(fd);
            }
        }
    }
    return h;
}

static int32_t posix_tcp_recv(ts_io_t *io, ts_io_handle_t h, uint8_t *buf, size_t cap)
{
    int32_t rc = TS_IO_ERR;
    const ts_io_posix_t *p = posix_of(io);
    if (tcp_slot_valid(p, h) && (buf != NULL) && (cap > 0U)) {
        ssize_t n;
        do {
            n = recv((int)p->tcp_fd[(uint32_t)h], buf, cap, MSG_DONTWAIT);
        } while ((n < 0) && (errno == EINTR));
        if (n > 0) {
            rc = (n > (ssize_t)INT32_MAX) ? INT32_MAX : (int32_t)n;
        } else if (n == 0) {
            rc = TS_IO_CLOSED;
        } else if (posix_would_block(errno) || (errno == ENOTCONN)) {
            rc = 0;   /* nothing yet (or a non-blocking connect still in progress) */
        } else if ((errno == ECONNRESET) || (errno == EPIPE)) {
            rc = TS_IO_CLOSED;
        } else {
            rc = TS_IO_ERR;
        }
    }
    return rc;
}

static int32_t posix_tcp_send(ts_io_t *io, ts_io_handle_t h, const uint8_t *buf, size_t len)
{
    int32_t rc = TS_IO_ERR;
    const ts_io_posix_t *p = posix_of(io);
    if (tcp_slot_valid(p, h) && (buf != NULL)) {
        ssize_t n;
        do {
            n = send((int)p->tcp_fd[(uint32_t)h], buf, len, POSIX_SEND_FLAGS);
        } while ((n < 0) && (errno == EINTR));
        if (n >= 0) {
            rc = (n > (ssize_t)INT32_MAX) ? INT32_MAX : (int32_t)n;
        } else if (posix_would_block(errno) || (errno == ENOTCONN)) {
            rc = 0;
        } else {
            rc = TS_IO_ERR;   /* EPIPE / ECONNRESET: the caller closes */
        }
    }
    return rc;
}

static void posix_tcp_close(ts_io_t *io, ts_io_handle_t h)
{
    ts_io_posix_t *p = posix_of(io);
    if (tcp_slot_valid(p, h)) {
        (void)close((int)p->tcp_fd[(uint32_t)h]);
        p->tcp_fd[(uint32_t)h] = -1;
    }
}

static uint32_t posix_now_ms(ts_io_t *io)
{
    struct timespec ts;
    uint32_t ms = 0U;
    (void)io;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) == 0) {
        uint64_t sec = (uint64_t)ts.tv_sec;
        uint64_t nsec = (uint64_t)ts.tv_nsec;
        ms = (uint32_t)(((sec * 1000U) + (nsec / 1000000U)) & 0xFFFFFFFFU);   /* wraps at 2^32 */
    }
    return ms;
}

static const ts_io_vt_t posix_vt = {
    posix_udp_send, posix_udp_recv, posix_tcp_accept, posix_tcp_connect,
    posix_tcp_recv, posix_tcp_send, posix_tcp_close, posix_now_ms
};

/* ---- setup ------------------------------------------------------------------- */

static int open_udp(uint16_t port)
{
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd >= 0) {
        struct sockaddr_in sin;
        ts_addr_t any = { 0U, port };
        int one = 1;
        int sndbuf = POSIX_SNDBUF_BYTES;
        addr_to_sin(&any, &sin);
        (void)setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, (socklen_t)sizeof(one));
        (void)setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &sndbuf, (socklen_t)sizeof(sndbuf));
        /* cppcheck-suppress misra-c2012-11.3 ; BSD socket API takes struct sockaddr * */
        if ((bind(fd, (const struct sockaddr *)&sin, (socklen_t)sizeof(sin)) != 0) || !set_nonblock(fd)) {
            (void)close(fd);
            fd = -1;
        }
    }
    return fd;
}

static int open_listener(uint16_t port)
{
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd >= 0) {
        struct sockaddr_in sin;
        ts_addr_t any = { 0U, port };
        int one = 1;
        addr_to_sin(&any, &sin);
        (void)setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, (socklen_t)sizeof(one));
        /* cppcheck-suppress misra-c2012-11.3 ; BSD socket API takes struct sockaddr * */
        if ((bind(fd, (const struct sockaddr *)&sin, (socklen_t)sizeof(sin)) != 0) ||
            (listen(fd, POSIX_LISTEN_BACKLOG) != 0) || !set_nonblock(fd)) {
            (void)close(fd);
            fd = -1;
        }
    }
    return fd;
}

ts_err_t ts_io_posix_open(ts_io_posix_t *p, uint16_t udp_port, uint16_t tcp_listen_port)
{
    ts_err_t rc = TS_E_INVAL;
    if (p != NULL) {
        (void)memset(p, 0, sizeof(*p));
        p->io.vt = &posix_vt;
        p->udp_fd = -1;
        p->listen_fd = -1;
        for (uint32_t i = 0U; i < TS_IO_MAX_TCP; i++) {
            p->tcp_fd[i] = -1;
        }
        rc = TS_OK;
        p->udp_fd = (int32_t)open_udp(udp_port);
        if (p->udp_fd < 0) {
            rc = TS_E_IO;
        }
        if ((rc == TS_OK) && (tcp_listen_port != 0U)) {
            p->listen_fd = (int32_t)open_listener(tcp_listen_port);
            if (p->listen_fd < 0) {
                rc = TS_E_IO;
            }
        }
        if (rc != TS_OK) {
            ts_io_posix_close(p);
        }
    }
    return rc;
}

int32_t ts_io_posix_wait(ts_io_posix_t *p, uint32_t timeout_ms)
{
    int32_t rc = TS_IO_ERR;
    if (p != NULL) {
        struct pollfd pfd[POSIX_MAX_POLL];
        nfds_t n = 0U;
        nfds_t listen_at = POSIX_MAX_POLL;
        int r;
        int tmo = (timeout_ms > (uint32_t)INT32_MAX) ? INT32_MAX : (int)timeout_ms;
        if (p->udp_fd >= 0) {
            pfd[n].fd = (int)p->udp_fd;
            pfd[n].events = POLLIN;
            pfd[n].revents = 0;
            n++;
        }
        if (p->listen_fd >= 0) {
            listen_at = n;
            pfd[n].fd = (int)p->listen_fd;
            pfd[n].events = POLLIN;
            pfd[n].revents = 0;
            n++;
        }
        for (uint32_t i = 0U; i < TS_IO_MAX_TCP; i++) {
            if (p->tcp_fd[i] >= 0) {
                pfd[n].fd = (int)p->tcp_fd[i];
                pfd[n].events = POLLIN;
                pfd[n].revents = 0;
                n++;
            }
        }
        r = poll(pfd, n, tmo);
        if (r >= 0) {
            if ((listen_at < n) && ((pfd[listen_at].revents & POLLIN) != 0)) {
                p->pending_accept = 1U;
            }
            rc = (int32_t)r;
        } else if (errno == EINTR) {
            rc = 0;   /* a signal (SIGTERM) woke us up: let the caller look at its flag */
        } else {
            rc = TS_IO_ERR;
        }
    }
    return rc;
}

uint16_t ts_io_posix_udp_port(const ts_io_posix_t *p)
{
    uint16_t port = 0U;
    if ((p != NULL) && (p->udp_fd >= 0)) {
        struct sockaddr_in sin;
        socklen_t sl = (socklen_t)sizeof(sin);
        (void)memset(&sin, 0, sizeof(sin));
        /* cppcheck-suppress misra-c2012-11.3 ; BSD socket API takes struct sockaddr * */
        if (getsockname((int)p->udp_fd, (struct sockaddr *)&sin, &sl) == 0) {
            port = ntohs(sin.sin_port);
        }
    }
    return port;
}

void ts_io_posix_close(ts_io_posix_t *p)
{
    if (p != NULL) {
        for (uint32_t i = 0U; i < TS_IO_MAX_TCP; i++) {
            if (p->tcp_fd[i] >= 0) {
                (void)close((int)p->tcp_fd[i]);
                p->tcp_fd[i] = -1;
            }
        }
        if (p->listen_fd >= 0) {
            (void)close((int)p->listen_fd);
            p->listen_fd = -1;
        }
        if (p->udp_fd >= 0) {
            (void)close((int)p->udp_fd);
            p->udp_fd = -1;
        }
        p->pending_accept = 0U;
    }
}
