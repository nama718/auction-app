#!/usr/bin/env python3
"""
client.py - Auction client: a small reusable class plus an interactive CLI.

The AuctionClient class is deliberately separate from the command-line
interface so the evaluation script (eval_tests.py) can reuse it to drive many
simulated bidders without any terminal I/O.

USAGE
    python3 client.py --host 127.0.0.1 --port 5000 --nick alice
    then type a number to bid (e.g. 120), "status", or "quit".

    python3 client.py --nick carol --role auctioneer
    auctioneers cannot bid; they type  list 20 Vintage Lamp  to sell an item
    with starting price 20.
    For a graphical version run client_gui.py instead.
"""

import argparse
import json
import socket
import struct
import threading
import time


class AuctionClient:
    """Thin wrapper over a TCP socket that speaks newline-delimited JSON."""

    def __init__(self, host, port, nick, on_message=None, role="bidder"):
        """Remember where to connect and who we are; nothing is opened until connect()."""
        self.host, self.port, self.nick, self.role = host, port, nick, role
        self.on_message = on_message or (lambda msg, t: None)
        self.sock = None
        self._send_lock = threading.Lock()  # bots may send from several threads

    def connect(self):
        """Open the TCP connection, start the reader thread, and send 'join'."""
        self.sock = socket.create_connection((self.host, self.port), timeout=5)
        self.sock.settimeout(None)  # blocking reads from here on
        # Same reason as on the server: don't let Nagle delay tiny messages.
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        threading.Thread(target=self._reader, daemon=True).start()
        self.send({"type": "join", "nick": self.nick, "role": self.role})

    def _reader(self):
        """Receive bytes, rebuild '\\n'-terminated lines, deliver each as a dict."""
        buf = b""
        try:
            while True:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:  # TCP is a stream: split messages ourselves
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        try:
                            msg = json.loads(line)
                        except ValueError:
                            continue  # ignore garbage rather than crash
                        self.on_message(msg, time.time())
        except OSError:
            pass
        # Tell the application the connection is gone (server crash, etc.).
        self.on_message({"type": "_disconnected"}, time.time())

    def send(self, msg):
        """Serialize msg as one line of JSON and send it (safe to call from many threads)."""
        data = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
        with self._send_lock:  # keep each message's bytes contiguous
            self.sock.sendall(data)

    def bid(self, amount, bid_id=None):
        # client_ts lets the server echo our send time to other clients, which
        # is how the evaluation measures end-to-end broadcast latency.
        """Place a bid; 'id' lets the caller match the reply, 'client_ts' lets tests time it."""
        self.send({"type": "bid", "amount": amount, "id": bid_id, "client_ts": time.time()})

    def list_item(self, name, start_price, duration=None, req_id=None):
        """Auctioneers only: put an item in the queue. duration=None uses the
        server's default."""
        msg = {"type": "list_item", "name": name, "start_price": start_price, "id": req_id}
        if duration is not None:
            msg["duration"] = duration
        self.send(msg)

    def close(self, abrupt=False):
        """abrupt=True simulates a crash: no 'leave' message, and the OS sends a
        TCP RST (via SO_LINGER with timeout 0) instead of a polite FIN."""
        try:
            if abrupt:
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                     struct.pack("ii", 1, 0))
                # The reader thread is blocked in recv() on this same socket, and
                # the OS will not really close a socket that another thread is
                # still using. SHUT_RD wakes that recv() (it sends nothing), so
                # the close() below can happen and emit the RST immediately.
                self.sock.shutdown(socket.SHUT_RD)
            else:
                self.send({"type": "leave"})
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def describe(msg):
    """Turn a server message into one human-readable line for the CLI."""
    t = msg.get("type")
    if t == "welcome":
        return (f"Connected as {msg['nick']} ({msg.get('role', 'bidder')}), "
                f"balance {msg.get('balance', 0)}. Users online: {', '.join(msg['users'])}")
    if t == "item_start":
        by = f" (sold by {msg['seller']})" if msg.get("seller") else ""
        return f"NEW ITEM: {msg['item']}{by} - starting at {msg['start_price']} ({msg['ends_in']}s)"
    if t == "state":
        if msg.get("item") is None:
            return "No auction running right now."
        who = msg["high_bidder"] or "nobody"
        return (f"{msg['item']}: high bid {msg['high_bid']} by {who}, "
                f"{msg['ends_in']}s left")
    if t == "bid_accepted":
        return f"Your bid of {msg['amount']} is the highest!"
    if t == "bid_rejected":
        extra = f" (minimum {msg['minimum']})" if "minimum" in msg else ""
        if "balance" in msg:
            extra = f" (your balance is {msg['balance']})"
        return f"Bid rejected: {msg['reason']}{extra}"
    if t == "balance":
        return f"Your balance is now {msg['balance']}."
    if t == "new_high_bid":
        return f"{msg['nick']} bid {msg['amount']} ({msg['ends_in']}s left)"
    if t == "auction_end":
        if msg["winner"]:
            return f"SOLD: {msg['item']} to {msg['winner']} for {msg['amount']}"
        return f"{msg['item']} got no bids."
    if t == "item_listed":
        return f"Listed {msg['item']}: position {msg['position']} in the queue."
    if t == "list_rejected":
        return f"Listing rejected: {msg['reason']}"
    if t == "queue":
        if not msg["items"]:
            return "Queue is empty."
        return "Up next: " + ", ".join(f"{i['item']} ({i['start_price']})" for i in msg["items"])
    if t in ("user_joined", "user_left"):
        return f"{msg['nick']} {t.split('_')[1]}"
    if t == "all_done":
        return "All items sold. Thanks for playing!"
    if t == "error":
        return f"Error: {msg['code']}"
    if t == "_disconnected":
        return "Disconnected from server."
    return str(msg)


def main():
    """Command-line entry point: connect, then read commands from the keyboard."""
    ap = argparse.ArgumentParser(description="Auction client")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--nick", required=True)
    ap.add_argument("--role", choices=["bidder", "auctioneer"], default="bidder")
    args = ap.parse_args()

    done = threading.Event()

    def show(msg, _t):
        """Reader-thread callback: print every server message as one readable line."""
        print(describe(msg), flush=True)
        if msg.get("type") == "_disconnected":
            done.set()

    client = AuctionClient(args.host, args.port, args.nick, show, args.role)
    try:
        client.connect()
    except OSError as e:
        print(f"Could not connect: {e}")
        return

    if args.role == "auctioneer":
        print("Type 'list <start price> <item name>' to sell an item, 'status', or 'quit'.")
    else:
        print("Type a number to bid, 'status', or 'quit'.")
    bid_id = 0
    try:
        while not done.is_set():
            line = input().strip()
            if line == "quit":
                break
            if line == "status":
                client.send({"type": "status"})
            elif line.startswith("list "):
                parts = line.split(None, 2)
                if len(parts) == 3 and parts[1].isdigit():
                    client.list_item(parts[2], int(parts[1]))
                else:
                    print("Usage: list <start price> <item name>")
            elif line.isdigit():
                bid_id += 1
                client.bid(int(line), bid_id)
            elif line:
                print("Enter a whole number to bid, 'status', or 'quit'.")
    except (EOFError, KeyboardInterrupt):
        pass
    client.close()


if __name__ == "__main__":
    main()
