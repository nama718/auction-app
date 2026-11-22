#!/usr/bin/env python3
"""
server.py - Multi-user online auction server (TCP, newline-delimited JSON).

WHAT IT DOES
    Many clients connect over TCP, pick a nickname, and bid on items that are
    auctioned one at a time. The server is the single referee: it decides which
    bid is valid, tells everybody about new high bids in real time, runs the
    countdown timer, and announces the winner.

WHY THIS DESIGN
    * TCP (not UDP): a bid must never be silently lost or reordered, and TCP
      gives us reliable, in-order delivery for free.
    * One thread per client for reading, one for writing: a slow or dead client
      can never block the other clients (see ClientConn).
    * ONE global lock around auction state: two clients can bid at the same
      instant; the lock turns "simultaneous" into "one after the other", so
      exactly one of them wins and the order is the same for everybody.

APPLICATION-LAYER PROTOCOL
    TCP is a byte STREAM, not a message stream: one send() on the client may
    arrive as two recv()s, or two sends may arrive glued together. So every
    message is ONE line of JSON ending in "\\n" (newline-delimited JSON). The
    receiver buffers bytes and splits on "\\n" to recover message boundaries.

    Client -> Server
        {"type":"join",   "nick":"alice"}
        {"type":"bid",    "amount":120, "id":7, "client_ts":1712345678.1}
        {"type":"status"}
        {"type":"leave"}
    Server -> Client
        {"type":"welcome",      "nick":"alice", "users":["alice","bob"]}
        {"type":"item_start",   "item":"Watch", "start_price":50, "ends_in":30.0}
        {"type":"state",        "item":..., "high_bid":..., "high_bidder":..., "ends_in":...}
        {"type":"bid_accepted", "id":7, "amount":120, "seq":3}
        {"type":"bid_rejected", "id":7, "reason":"too_low", "minimum":121}
        {"type":"new_high_bid", "nick":"bob", "amount":125, "seq":4, "ends_in":12.3,
                                "client_ts":...}
        {"type":"auction_end",  "item":"Watch", "winner":"bob", "amount":125,
                                "accepted_bids":4}
        {"type":"user_joined" | "user_left", "nick":"..."}
        {"type":"all_done",     "results":[...]}
        {"type":"error",        "code":"bad_json" | "join_first" | ...}

USAGE
    python3 server.py --port 5000 --duration 30
"""

import argparse
import json
import queue
import re
import socket
import threading
import time

# A single message line may not exceed this many bytes. Without a limit, a
# misbehaving client could send endless bytes with no newline and make the
# server buffer them until it runs out of memory.
MAX_LINE_BYTES = 4096

# Nicknames: short and boring on purpose, so they are safe to print and log.
NICK_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")

# Each client has a bounded outgoing queue. If a client stops reading and the
# queue fills up, we disconnect it rather than let it eat memory or stall us.
OUTQ_MAX = 500

# Anti-sniping: a bid placed in the last few seconds extends the auction a
# little, so nobody can win by bidding 0.1 s before the end. The number of
# extensions is capped so the auction is guaranteed to finish.
MAX_EXTENSIONS = 3


def log(text):
    """Timestamped, flushed print so logs appear immediately when piped."""
    print(time.strftime("%H:%M:%S"), text, flush=True)


class ClientConn:
    """One connected client: its socket, nickname, and outgoing message queue.

    Why a queue + writer thread? The auction thread and other clients' handler
    threads need to send to this client while holding the global lock. If they
    called sock.sendall() directly, one slow client (full TCP buffer) would
    block everyone. Instead they only enqueue (instant), and this client's own
    writer thread does the potentially slow sendall(). The queue is FIFO, so
    every client also receives events in the order they were produced.
    """

    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.nick = None  # None until the client has sent a valid "join"
        self.outq = queue.Queue(maxsize=OUTQ_MAX)
        self.closed = False
        self.closing = False  # True once a graceful close was requested
        self._close_lock = threading.Lock()

    def send(self, msg):
        """Queue a message (never blocks). Returns False if client is gone/slow."""
        if self.closed:
            return False
        try:
            self.outq.put_nowait(msg)
            return True
        except queue.Full:
            self.close()  # client is not reading: drop it
            return False

    def request_close(self):
        """Close AFTER everything already queued has been sent.

        Used for protocol errors so the client actually receives our error
        message before the connection goes away.
        """
        self.closing = True
        try:
            self.outq.put_nowait(None)  # None is the "stop writing" sentinel
        except queue.Full:
            self.close()

    def writer_loop(self):
        """Runs in its own thread: pull queued messages and write them out."""
        try:
            while True:
                msg = self.outq.get()
                if msg is None:
                    break
                line = json.dumps(msg, separators=(",", ":")) + "\n"
                self.sock.sendall(line.encode("utf-8"))
        except OSError:
            pass  # peer vanished mid-write; the finally block cleans up
        finally:
            self.close()

    def close(self):
        """Hard close, safe to call many times from any thread."""
        with self._close_lock:
            if self.closed:
                return
            self.closed = True
        try:
            self.outq.put_nowait(None)  # wake the writer so it can exit
        except queue.Full:
            pass
        try:
            # shutdown() (unlike close()) also wakes a thread blocked in recv()
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class AuctionServer:
    """Accepts clients and runs the auction. All shared state is guarded by
    self.lock (a reentrant lock, so helpers may be called with it held)."""

    def __init__(self, host, port, items, duration, pause,
                 min_increment, min_clients, anti_snipe):
        self.host = host
        self.port = port
        self.items = items              # list of (name, starting_price)
        self.duration = duration        # seconds per item
        self.pause = pause              # seconds between items
        self.min_increment = min_increment
        self.min_clients = min_clients  # wait for this many bidders before starting an item
        self.anti_snipe = anti_snipe    # seconds; 0 disables
        self.lock = threading.RLock()
        self.clients = {}               # nick -> ClientConn (only joined clients)
        self.current = None             # dict describing the running auction, or None
        self.results = []               # finished auctions
        self.stop_event = threading.Event()
        self.listener = None

    # ---------------------------------------------------------------- startup
    def start(self):
        """Bind the listening socket and start the accept + auction threads."""
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # SO_REUSEADDR lets us restart the server immediately; otherwise the
        # OS keeps the port in TIME_WAIT for a minute after the last run.
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind((self.host, self.port))
        self.listener.listen(128)
        self.port = self.listener.getsockname()[1]  # real port if 0 was requested
        threading.Thread(target=self._accept_loop, daemon=True).start()
        threading.Thread(target=self._auction_loop, daemon=True).start()
        log(f"Auction server listening on {self.host}:{self.port}")

    def stop(self):
        self.stop_event.set()
        try:
            self.listener.close()
        except OSError:
            pass

    def _accept_loop(self):
        while not self.stop_event.is_set():
            try:
                sock, addr = self.listener.accept()
            except OSError:
                break  # listener closed by stop()
            # Disable Nagle's algorithm: it delays small packets to batch
            # them, which would add latency to every tiny bid message.
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn = ClientConn(sock, addr)
            threading.Thread(target=conn.writer_loop, daemon=True).start()
            threading.Thread(target=self._handle_client, args=(conn,), daemon=True).start()

    # ------------------------------------------------------- per-client reader
    def _handle_client(self, conn):
        """Read bytes from one client, split them into lines, process each."""
        buf = b""
        try:
            while True:
                chunk = conn.sock.recv(4096)
                if not chunk:
                    break  # orderly close by the peer
                buf += chunk
                # TCP may deliver half a message or several at once, so loop
                # until no complete line ("\n"-terminated) remains in the buffer.
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if len(line) > MAX_LINE_BYTES:
                        self._protocol_error(conn, "line_too_long")
                        return
                    if line.strip():
                        self._process(conn, line)
                if len(buf) > MAX_LINE_BYTES:  # no newline yet and already too big
                    self._protocol_error(conn, "line_too_long")
                    return
        except OSError:
            pass  # connection reset / aborted: treat like a disconnect
        finally:
            self._drop(conn)  # ALWAYS clean up, however the loop ended

    def _protocol_error(self, conn, code):
        conn.send({"type": "error", "code": code})
        conn.request_close()

    def _drop(self, conn):
        """Remove a client and tell the others. Idempotent."""
        # If a graceful close is pending, the writer thread will close the socket
        # itself once it has flushed the queue (so the client gets our last
        # message). Closing here would throw that message away.
        if not conn.closing:
            conn.close()
        with self.lock:
            if conn.nick and self.clients.get(conn.nick) is conn:
                del self.clients[conn.nick]
                log(f"{conn.nick} disconnected")
                self._broadcast_locked({"type": "user_left", "nick": conn.nick})
            # Note: if conn was the high bidder, their bid STANDS. Bids are
            # commitments; disconnecting is not a way to take a bid back.

    # -------------------------------------------------------- message handling
    def _process(self, conn, line):
        try:
            msg = json.loads(line)
        except ValueError:  # JSONDecodeError and UnicodeDecodeError both land here
            conn.send({"type": "error", "code": "bad_json"})
            return
        if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
            conn.send({"type": "error", "code": "bad_message"})
            return

        mtype = msg["type"]
        if conn.nick is None:  # not joined yet: only "join" is allowed
            if mtype == "join":
                self._do_join(conn, msg)
            else:
                conn.send({"type": "error", "code": "join_first"})
        elif mtype == "bid":
            self._do_bid(conn, msg)
        elif mtype == "status":
            with self.lock:
                self._send_state_locked(conn)
        elif mtype == "leave":
            conn.request_close()
        elif mtype == "join":
            conn.send({"type": "error", "code": "already_joined"})
        else:
            conn.send({"type": "error", "code": "unknown_type"})

    def _do_join(self, conn, msg):
        nick = msg.get("nick")
        if not isinstance(nick, str) or not NICK_RE.match(nick):
            conn.send({"type": "error", "code": "invalid_nick"})
            return
        with self.lock:
            if nick in self.clients:  # check + insert must be atomic
                conn.send({"type": "error", "code": "nick_taken"})
                return
            self.clients[nick] = conn
            conn.nick = nick
            log(f"{nick} joined from {conn.addr[0]}")
            conn.send({"type": "welcome", "nick": nick, "users": sorted(self.clients)})
            self._send_state_locked(conn)  # late joiners see the running auction
            self._broadcast_locked({"type": "user_joined", "nick": nick}, exclude=conn)

    def _do_bid(self, conn, msg):
        amount = msg.get("amount")
        bid_id = msg.get("id")  # echoed back so the client can match replies
        # bool is a subclass of int in Python, so exclude it explicitly.
        if isinstance(amount, bool) or not isinstance(amount, int) or not (0 < amount <= 10**9):
            conn.send({"type": "bid_rejected", "id": bid_id, "reason": "invalid_amount"})
            return

        # ---- critical section --------------------------------------------
        # Everything from "is this bid valid?" to "record it and tell
        # everyone" happens under ONE lock acquisition. Two simultaneous bids
        # are therefore processed strictly one after another; the second is
        # judged against the state the first one created. This is what
        # guarantees we never accept two bids for the same price.
        with self.lock:
            cur = self.current
            now = time.monotonic()
            if cur is None or now >= cur["ends_at"]:
                conn.send({"type": "bid_rejected", "id": bid_id, "reason": "no_active_auction"})
                return
            if cur["high_bidder"] == conn.nick:
                conn.send({"type": "bid_rejected", "id": bid_id, "reason": "already_highest",
                           "minimum": cur["high_bid"] + self.min_increment})
                return
            minimum = (cur["high_bid"] + self.min_increment) if cur["high_bidder"] else cur["start_price"]
            if amount < minimum:
                conn.send({"type": "bid_rejected", "id": bid_id, "reason": "too_low",
                           "minimum": minimum})
                return

            cur["high_bid"] = amount
            cur["high_bidder"] = conn.nick
            cur["accepted"] += 1
            remaining = cur["ends_at"] - now
            if (self.anti_snipe > 0 and remaining < self.anti_snipe
                    and cur["extensions"] < MAX_EXTENSIONS):
                cur["ends_at"] += self.anti_snipe
                cur["extensions"] += 1
                remaining = cur["ends_at"] - now

            conn.send({"type": "bid_accepted", "id": bid_id, "amount": amount,
                       "seq": cur["accepted"]})
            # client_ts is copied through unchanged so receivers on the same
            # machine can measure end-to-end broadcast latency (used by tests).
            self._broadcast_locked({"type": "new_high_bid", "item": cur["item"],
                                    "nick": conn.nick, "amount": amount,
                                    "seq": cur["accepted"], "ends_in": round(remaining, 2),
                                    "client_ts": msg.get("client_ts")}, exclude=conn)
        # ------------------------------------------------------------------

    # ------------------------------------------------------------ broadcasting
    def _broadcast_locked(self, msg, exclude=None):
        """Queue msg for every joined client. Caller must hold self.lock.

        Because we hold the lock while enqueueing, two events produced one
        after the other are enqueued in that order for EVERY client, so all
        clients see the same history.
        """
        for c in list(self.clients.values()):
            if c is not exclude:
                c.send(msg)

    def _send_state_locked(self, conn):
        cur = self.current
        if cur is None:
            conn.send({"type": "state", "item": None})
        else:
            conn.send({"type": "state", "item": cur["item"], "start_price": cur["start_price"],
                       "high_bid": cur["high_bid"], "high_bidder": cur["high_bidder"],
                       "ends_in": round(max(0.0, cur["ends_at"] - time.monotonic()), 2)})

    # ------------------------------------------------------------ auction timer
    def _auction_loop(self):
        """Runs the items one after another, in its own thread."""
        for name, start_price in self.items:
            # Don't auction to an empty room: wait for enough bidders first.
            while not self.stop_event.is_set():
                with self.lock:
                    if len(self.clients) >= self.min_clients:
                        break
                time.sleep(0.1)
            if self.stop_event.is_set():
                return

            with self.lock:
                self.current = {"item": name, "start_price": start_price,
                                "high_bid": start_price, "high_bidder": None,
                                "accepted": 0, "extensions": 0,
                                "ends_at": time.monotonic() + self.duration}
                log(f"Auction started: {name} (start {start_price})")
                self._broadcast_locked({"type": "item_start", "item": name,
                                        "start_price": start_price, "ends_in": self.duration})

            while not self.stop_event.is_set():
                time.sleep(0.02)  # polling granularity = timer accuracy (~20 ms)
                with self.lock:
                    # The deadline check and the "close" below happen under the
                    # same lock as bid validation, so no bid can sneak in after
                    # the auction is declared over.
                    if time.monotonic() >= self.current["ends_at"]:
                        cur = self.current
                        result = {"type": "auction_end", "item": cur["item"],
                                  "winner": cur["high_bidder"],
                                  "amount": cur["high_bid"] if cur["high_bidder"] else None,
                                  "accepted_bids": cur["accepted"]}
                        self.results.append(result)
                        self.current = None
                        log(f"Auction ended: {result}")
                        self._broadcast_locked(result)
                        break
            time.sleep(self.pause)

        with self.lock:
            self._broadcast_locked({"type": "all_done", "results": self.results})
        log("All items sold.")


def parse_items(text):
    """'Watch:50,Book:5' -> [('Watch', 50), ('Book', 5)]"""
    items = []
    for part in text.split(","):
        name, price = part.rsplit(":", 1)
        items.append((name.strip(), int(price)))
    return items


def main():
    ap = argparse.ArgumentParser(description="Online auction server")
    ap.add_argument("--host", default="0.0.0.0", help="interface to listen on")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--duration", type=float, default=30.0, help="seconds per item")
    ap.add_argument("--pause", type=float, default=5.0, help="seconds between items")
    ap.add_argument("--min-increment", type=int, default=1)
    ap.add_argument("--min-clients", type=int, default=1,
                    help="wait for this many connected users before each item")
    ap.add_argument("--anti-snipe", type=float, default=3.0,
                    help="extend by this many seconds if a bid lands this close to the end (0=off)")
    ap.add_argument("--items", default="Vintage Keyboard:10,Mechanical Watch:50,Signed Textbook:5",
                    help="comma list of name:startingPrice")
    args = ap.parse_args()

    server = AuctionServer(args.host, args.port, parse_items(args.items), args.duration,
                           args.pause, args.min_increment, args.min_clients, args.anti_snipe)
    server.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("Shutting down")
        server.stop()


if __name__ == "__main__":
    main()
