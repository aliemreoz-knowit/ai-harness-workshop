/* SPDX-License-Identifier: MIT */
/*
 * tinystream v2 -- tools/tsnetem/relay_peer.h
 * Upstream-peer policy of the tsnetem relay, kept apart from the socket code
 * so that tests/unit/test_netem.c can check it without sockets.
 *
 * The upstream peer (the daemon) is learned from the FIRST datagram on the
 * listen socket, or fixed with --peer. A datagram from any other sender never
 * replaces it: it is counted as foreign and still relayed upstream -> down as
 * before (the filter decides its fate), so a stray sender cannot steal the
 * NACK / keep-alive direction.
 */
#ifndef TSNETEM_RELAY_PEER_H
#define TSNETEM_RELAY_PEER_H

#include <stdbool.h>

typedef enum relay_peer_action {
    RELAY_PEER_KEEP = 0,     /**< from the known peer: nothing changes */
    RELAY_PEER_LEARN,        /**< first datagram, no --peer: it becomes the peer */
    RELAY_PEER_FOREIGN       /**< another sender: counted, the peer is kept */
} relay_peer_action_t;

/**
 * Decide what a datagram on the listen socket does to the upstream peer.
 * `peer_fixed`: --peer was given; `have_peer`: a peer is known;
 * `from_is_peer`: the sender equals the known peer.
 */
static inline relay_peer_action_t relay_peer_action(bool peer_fixed, bool have_peer, bool from_is_peer)
{
    relay_peer_action_t a = RELAY_PEER_KEEP;
    if ((!peer_fixed) && (!have_peer)) {
        a = RELAY_PEER_LEARN;
    } else if (!from_is_peer) {
        a = RELAY_PEER_FOREIGN;
    } else {
        a = RELAY_PEER_KEEP;
    }
    return a;
}

#endif /* TSNETEM_RELAY_PEER_H */
