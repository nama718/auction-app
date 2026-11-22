# Live Auction Over TCP

A multi-user online auction built with Python sockets and threads. A central
server runs timed auctions; any number of clients connect, bid, and see every
new bid live. The server decides which bid is valid and announces the winner.

## Requirements
- Python 3.8 or newer. **No compile step and no third-party packages.**
- Works on Linux, macOS and Windows.

## Files
| File | Purpose |
|---|---|
| `server.py` | The auction server (TCP, newline-delimited JSON) |
| `client.py` | Interactive command-line client (also reused by the tests) |
| `eval_tests.py` | Automated functionality, robustness and load tests |

## Run it (3 terminals)

**Terminal 1 - server**
```
python3 server.py --port 5000 --duration 30
```
Useful options: `--duration` (seconds per item), `--pause` (seconds between
items), `--min-clients` (wait for this many users before each item),
`--items "Watch:50,Book:5"` (name:starting price), `--anti-snipe 3`.

**Terminals 2 and 3 - clients**
```
python3 client.py --host 127.0.0.1 --port 5000 --nick alice
python3 client.py --host 127.0.0.1 --port 5000 --nick bob
```
(Use the server's IP instead of 127.0.0.1 to connect from another machine.)

## Using the client
| Type | Effect |
|---|---|
| a whole number, e.g. `120` | place a bid of 120 |
| `status` | show the current item, high bid and time left |
| `quit` | leave |

Rules: a bid must beat the current high bid by at least the minimum increment
(default 1), you cannot outbid yourself, and a bid placed in the last 3 seconds
extends the auction by 3 seconds (up to 3 times) to stop last-second sniping.

Quick demo: start the server, join `alice` and `bob`, bid back and forth, press
Ctrl+C in one client to simulate a crash, and watch the other keep going.

## Run the evaluation
```
python3 eval_tests.py          # full run, about 45 seconds
python3 eval_tests.py --quick  # smaller load test
```
It starts its own server on a free port, prints PASS/FAIL for each check, and
saves the load-test numbers to `results.json`. Expected: `40 passed, 0 failed`.

## Protocol summary
One JSON object per line, ending in `\n` (TCP is a byte stream, so the newline
marks message boundaries). Full message list and the reasoning behind the design
are documented in the header comment of `server.py`.

Client to server: `join`, `bid`, `status`, `leave`.
Server to client: `welcome`, `item_start`, `state`, `bid_accepted`,
`bid_rejected`, `new_high_bid`, `auction_end`, `user_joined`, `user_left`,
`all_done`, `error`.
