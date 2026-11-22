#!/usr/bin/env python3
"""
eval_tests.py - Automated evaluation of the auction server.

Three groups of tests (these map to the "Evaluations" section of the report):
  1. FUNCTIONALITY - normal bidding works end to end.
  2. ROBUSTNESS    - crashes, garbage input and oversized messages do not hurt
                     the server or the other clients.
  3. LOAD          - N simulated bidders hammer one item at once; we check
                     correctness invariants and measure latency.

The server is started as a SEPARATE PROCESS (like in real use) so the numbers
are not distorted by sharing Python's GIL with the test bots.

USAGE
    python3 eval_tests.py              # everything (about 40 s)
    python3 eval_tests.py --quick      # smaller load test
Results are printed and saved to results.json.
"""

import argparse
import json
import random
import socket
import statistics
import subprocess
import sys
import threading
import time

from client import AuctionClient

PASSED, FAILED = [], []


def check(name, condition):
    """Record and print one PASS/FAIL line."""
    (PASSED if condition else FAILED).append(name)
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}", flush=True)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ServerProc:
    """Context manager: run server.py in a subprocess on a free port."""

    def __init__(self, items, duration, pause=0.3, min_clients=1, anti_snipe=0):
        self.port = free_port()
        self.cmd = [sys.executable, "server.py", "--host", "127.0.0.1",
                    "--port", str(self.port), "--items", items,
                    "--duration", str(duration), "--pause", str(pause),
                    "--min-clients", str(min_clients), "--anti-snipe", str(anti_snipe)]

    def __enter__(self):
        self.proc = subprocess.Popen(self.cmd, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        for _ in range(100):  # wait up to ~5 s for the port to accept connections
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return self
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("server did not start")

    def __exit__(self, *exc):
        self.proc.terminate()
        self.proc.wait(timeout=5)

    def alive(self):
        return self.proc.poll() is None


class Probe:
    """A client that records every message so tests can wait for specific ones."""

    def __init__(self, port, nick):
        self.msgs = []
        self.cv = threading.Condition()
        self.client = AuctionClient("127.0.0.1", port, nick, self._on_msg)
        self.client.connect()

    def _on_msg(self, msg, _t):
        with self.cv:
            self.msgs.append(msg)
            self.cv.notify_all()

    def wait(self, predicate, timeout=6.0):
        """Return the first recorded message matching predicate, or None."""
        deadline = time.time() + timeout
        with self.cv:
            while True:
                for m in self.msgs:
                    if predicate(m):
                        return m
                left = deadline - time.time()
                if left <= 0:
                    return None
                self.cv.wait(left)

    def wait_type(self, mtype, timeout=6.0, **fields):
        return self.wait(lambda m: m.get("type") == mtype
                         and all(m.get(k) == v for k, v in fields.items()), timeout)


# ------------------------------------------------------------ 1. FUNCTIONALITY
def test_functionality():
    print("\n== 1. Functionality ==")
    with ServerProc("Test Item:10", duration=3, min_clients=3) as s:
        a, b, c = (Probe(s.port, n) for n in ("alice", "bob", "carol"))
        check("all three clients receive welcome", all(p.wait_type("welcome") for p in (a, b, c)))
        check("auction starts once 3 users joined", all(p.wait_type("item_start") for p in (a, b, c)))

        a.client.bid(12, 1)
        check("valid bid is accepted", a.wait_type("bid_accepted", id=1) is not None)
        check("other users are told about the new high bid",
              b.wait_type("new_high_bid", nick="alice", amount=12) is not None
              and c.wait_type("new_high_bid", nick="alice", amount=12) is not None)

        b.client.bid(11, 2)
        rej = b.wait_type("bid_rejected", id=2)
        check("bid below current high is rejected (too_low)", rej is not None and rej["reason"] == "too_low")

        b.client.bid(15, 3)
        check("higher bid takes the lead",
              b.wait_type("bid_accepted", id=3) is not None
              and a.wait_type("new_high_bid", nick="bob", amount=15) is not None)

        dup = Probe(s.port, "alice")
        check("duplicate nickname is refused", dup.wait_type("error", code="nick_taken") is not None)

        ends = [p.wait_type("auction_end", timeout=8) for p in (a, b, c)]
        check("everyone is told the same winner and price",
              all(e and e["winner"] == "bob" and e["amount"] == 15 for e in ends))
        a.client.bid(99, 4)
        rej = a.wait_type("bid_rejected", id=4)
        check("bid after the auction ends is rejected", rej is not None and rej["reason"] == "no_active_auction")
        for p in (a, b, c):
            p.client.close()


# ---------------------------------------------------------------- 2. ROBUSTNESS
def test_robustness():
    print("\n== 2. Robustness ==")
    with ServerProc("Robust Item:10", duration=4, min_clients=3) as s:
        a, b, c = (Probe(s.port, n) for n in ("alice", "bob", "carol"))
        for p in (a, b, c):
            p.wait_type("item_start")

        c.client.bid(20, 1)
        c.wait_type("bid_accepted")
        c.client.close(abrupt=True)  # carol "crashes" while she is the high bidder
        check("others are told when a client crashes", a.wait_type("user_left", nick="carol") is not None)
        check("server keeps running after a client crash", s.alive())

        b.client.bid(21, 2)
        check("auction continues; the crashed user's bid still stands (21 > 20 needed)",
              b.wait_type("bid_accepted", id=2) is not None)

        # Garbage input over a raw socket (no AuctionClient, so we control the bytes).
        raw = socket.create_connection(("127.0.0.1", s.port), timeout=3)
        raw.sendall(b"this is not json\n")
        raw.sendall(b'{"type":"bid","amount":99}\n')
        time.sleep(0.4)
        data = raw.recv(4096).decode()
        check("malformed JSON gets an error, not a crash", "bad_json" in data)
        check("bidding before joining is refused", "join_first" in data)

        raw.sendall(b"x" * 10000)  # no newline: tries to make the server buffer forever
        got = b""
        try:
            while True:
                chunk = raw.recv(4096)
                if not chunk:
                    break
                got += chunk
        except OSError:
            pass
        check("oversized message gets error and disconnect", b"line_too_long" in got)
        raw.close()

        for i, bad in enumerate(["lots", -5, True, 1.5], start=10):
            b.client.send({"type": "bid", "amount": bad, "id": i})
        results = [b.wait_type("bid_rejected", id=i) for i in range(10, 14)]
        check("invalid amounts (string, negative, bool, float) are rejected",
              all(r and r["reason"] == "invalid_amount" for r in results))

        check("server still alive after all abuse", s.alive())
        a.client.bid(30, 3)
        check("normal bidding still works afterwards", a.wait_type("bid_accepted", id=3) is not None)
        end = b.wait_type("auction_end", timeout=8)
        check("auction still finishes with correct winner", end is not None and end["winner"] == "alice"
              and end["amount"] == 30)
        for p in (a, b):
            p.client.close()


# -------------------------------------------------------------------- 3. LOAD
class Bot:
    """A simulated bidder that bids aggressively as soon as the item starts."""

    def __init__(self, port, idx):
        self.nick = f"bot{idx}"
        self.min_next = 1           # lowest bid we believe is currently legal
        self.pending = {}           # bid id -> time we sent it
        self.rtts = []              # bid -> accepted/rejected round trips (ms)
        self.bcast = []             # bid by another bot -> arrival here (ms)
        self.accepted = []          # amounts the server accepted from us
        self.rejected = 0
        self.end = None
        self.started = threading.Event()
        self.finished = threading.Event()
        self.next_id = 0
        self.client = AuctionClient("127.0.0.1", port, self.nick, self._on_msg)

    def _on_msg(self, m, t):
        kind = m.get("type")
        if kind == "item_start":
            self.min_next = m["start_price"]
            self.started.set()
        elif kind == "new_high_bid":
            self.min_next = m["amount"] + 1
            if m.get("client_ts"):
                self.bcast.append((t - m["client_ts"]) * 1000)
        elif kind == "bid_accepted":
            self.min_next = m["amount"] + 1
            self.accepted.append(m["amount"])
            self._reply(m)
        elif kind == "bid_rejected":
            self.rejected += 1
            self.min_next = max(self.min_next, m.get("minimum", 0))
            self._reply(m)
        elif kind == "auction_end":
            self.end = m
            self.finished.set()

    def _reply(self, m):
        sent = self.pending.pop(m.get("id"), None)
        if sent is not None:
            self.rtts.append((time.perf_counter() - sent) * 1000)

    def run(self):
        """Bid every 20-200 ms at (or just above) the lowest legal price. Many
        bots pick the same price at the same time, so many bids collide: that
        is the point, it stresses the server's locking."""
        self.started.wait(timeout=30)
        while not self.finished.is_set():
            time.sleep(random.uniform(0.02, 0.2))
            self.next_id += 1
            self.pending[self.next_id] = time.perf_counter()
            try:
                self.client.bid(self.min_next + random.randint(0, 2), self.next_id)
            except OSError:
                break


def pct(values, p):
    if not values:
        return float("nan")
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]


def load_test(n_bots, duration):
    print(f"\n== 3. Load test: {n_bots} concurrent bidders, {duration}s ==")
    with ServerProc("Load Item:1", duration=duration, min_clients=n_bots) as s:
        bots = [Bot(s.port, i) for i in range(n_bots)]
        for b in bots:
            b.client.connect()
        threads = [threading.Thread(target=b.run, daemon=True) for b in bots]
        for t in threads:
            t.start()
        done = all(b.finished.wait(timeout=duration + 15) for b in bots)
        check(f"{n_bots} bots all saw the auction end", done)

        ends = [b.end for b in bots if b.end]
        all_acc = [a for b in bots for a in b.accepted]
        check("every bot got the identical auction_end message", len({json.dumps(e, sort_keys=True) for e in ends}) == 1)
        end = ends[0] if ends else {}
        check("no lost or double-counted bids (server count == sum of bots' accepted)",
              end.get("accepted_bids") == len(all_acc))
        check("no two accepted bids share the same price", len(set(all_acc)) == len(all_acc))
        check("winning price equals the highest accepted bid", bool(all_acc) and end.get("amount") == max(all_acc))
        winner = next((b for b in bots if b.nick == end.get("winner")), None)
        check("winner is the bot that placed the winning bid", winner is not None and max(winner.accepted) == end.get("amount"))
        check("server still alive", s.alive())

        rtts = [x for b in bots for x in b.rtts]
        bc = [x for b in bots for x in b.bcast]
        total_bids = len(rtts)
        stats = {
            "bots": n_bots, "duration_s": duration,
            "bids_sent": total_bids,
            "bids_accepted": len(all_acc),
            "bids_rejected": sum(b.rejected for b in bots),
            "bids_per_sec": round(total_bids / duration, 1),
            "rtt_ms": {"median": round(statistics.median(rtts), 2), "p95": round(pct(rtts, 95), 2),
                       "p99": round(pct(rtts, 99), 2), "max": round(max(rtts), 2)},
            "broadcast_ms": {"median": round(statistics.median(bc), 2), "p95": round(pct(bc, 95), 2),
                             "p99": round(pct(bc, 99), 2), "max": round(max(bc), 2)},
        }
        print("  ", json.dumps(stats))
        for b in bots:
            b.client.close()
        return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="smaller load test")
    args = ap.parse_args()

    test_functionality()
    test_robustness()
    sizes = [(10, 5)] if args.quick else [(10, 8), (25, 8), (50, 8)]
    load = [load_test(n, d) for n, d in sizes]

    print(f"\nSUMMARY: {len(PASSED)} passed, {len(FAILED)} failed")
    for name in FAILED:
        print("  FAILED:", name)
    with open("results.json", "w") as f:
        json.dump({"passed": len(PASSED), "failed": FAILED, "load": load}, f, indent=2)
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
