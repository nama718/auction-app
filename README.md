# Live Auction Over TCP

A multi-user online auction built with Python sockets and threads. A central
server runs timed auctions; any number of clients connect, bid, and see every
new bid live. The server decides which bid is valid and announces the winner.
Users join as a **bidder** (bids on items) or an **auctioneer** (lists items for
sale). If no auctioneer lists anything, the server runs its built-in item list.

## Requirements
- Python 3.8 or newer. **No compile step and no third-party packages.**
- Works on Linux, macOS and Windows.

## Files
| File | Purpose |
|---|---|
| `server.py` | The auction server (TCP, newline-delimited JSON) |
| `client.py` | Command-line client (its `AuctionClient` class is reused by the GUI and the tests) |
| `client_gui.py` | Tkinter desktop client (bidder view and auctioneer view) |
| `eval_tests.py` | Automated functionality, robustness and load tests |

## Run it (3 terminals)

**Terminal 1 - server**
```
python server.py --port 5000 --duration 30
```
Useful options: `--duration` (seconds per item), `--pause` (seconds between
items), `--min-clients` (wait for this many users before each item),
`--items "Watch:50,Book:5"` (name:starting price), `--anti-snipe 3`.

If `python` is not recognized, use `py` (Windows) or `python3` (macOS/Linux).

**Terminals 2 and 3 - clients**
```
python client.py --host 127.0.0.1 --port 5000 --nick alice
python client.py --host 127.0.0.1 --port 5000 --nick bob
```
(Use the server's IP instead of 127.0.0.1 to connect from another machine.)

**Graphical client (recommended for the demo)**
```
python client_gui.py
python client_gui.py --nick alice                   # pre-fill the connect screen
python client_gui.py --nick carol --role auctioneer
```
Tkinter ships with Python on Windows and macOS. On some Linux installs run
`sudo apt install python3-tk` first.

**Auctioneer (command line)**
```
python client.py --nick carol --role auctioneer
```
then type `list 20 Vintage Lamp` (starting price, then item name).

## Using the client
| Type | Effect |
|---|---|
| a whole number, e.g. `120` | place a bid of 120 |
| `status` | show the current item, high bid and time left |
| `quit` | leave |

Roles: a **bidder** can bid but not list items. An **auctioneer** can list items
but not bid, so nobody can ever bid on their own item. Listed items wait in a
queue (max 20) behind the built-in items and run one after another; each
listing carries its own starting price and duration (5 to 300 seconds). In the
GUI the auctioneer uses the "List an item for sale" form.

Rules: a bid must beat the current high bid by at least the minimum increment
(default 1), you cannot outbid yourself, and a bid placed in the last 3 seconds
extends the auction by 3 seconds (up to 3 times) to stop last-second sniping.

Quick demo: start the server, join `alice` and `bob`, bid back and forth, press
Ctrl+C in one client to simulate a crash, and watch the other keep going.

## Run the evaluation
```
python eval_tests.py          # full run, about 60 seconds
python eval_tests.py --quick  # smaller load test
```
It starts its own server on a free port, prints PASS/FAIL for each check, and
saves the load-test numbers to `results.json`. Expected: `55 passed, 0 failed`.

## Protocol summary
One JSON object per line, ending in `\n` (TCP is a byte stream, so the newline
marks message boundaries). Full message list and the reasoning behind the design
are documented in the header comment of `server.py`.

Client to server: `join` (with `role`), `bid`, `list_item` (auctioneers only),
`status`, `leave`.
Server to client: `welcome`, `item_start`, `state`, `queue`, `bid_accepted`,
`bid_rejected`, `item_listed`, `list_rejected`, `new_high_bid`, `auction_end`,
`user_joined`, `user_left`, `all_done`, `error`.
