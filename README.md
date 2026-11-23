# Live Auction Over TCP

A multi-user online auction built with Python sockets and threads. A central
server runs timed auctions; any number of clients connect, bid, and see every
new bid live. The server decides which bid is valid and announces the winner.
Users join as a **bidder** (bids on items) or an **auctioneer** (lists items for
sale). If no auctioneer lists anything, the server runs its built-in item list.

The normal way to use the app is the **graphical client** (`client_gui.py`).
A command-line client also exists but is not needed.

## Requirements
- Python 3.8 or newer (3.12+ recommended). **No compile step and no third-party packages.**
- Works on Windows, macOS and Linux.
- The GUI uses Tkinter, which ships with Python on Windows and macOS. On some
  Linux installs run `sudo apt install python3-tk` first.
- If `python` is not recognized, use `py` (Windows) or `python3` (macOS/Linux).

## Where to read the code

Start with the [code walkthrough](code-walkthrough.md) for a complete auction round, including what happens when two bids arrive together.

Follow a bid from the screen to the server and back:

1. [Desktop interface](frontend/gui.py): `AuctionApp.place_bid` captures the displayed item and amount.
2. [TCP client](frontend/client.py): `AuctionClient.bid` sends the request. The reader delivers replies and broadcasts to the interface.
3. [Auction server](backend/server.py): `AuctionServer._do_bid` validates and records the bid under the auction lock. The module header describes the wire messages.
4. [Evaluation suite](evaluation/suite.py): start with `test_functionality` for the two-bidder flow, then `load_test` for concurrency and latency measurements.

[Protocol limits](protocol.py) are shared by the GUI and server. The root scripts are the launch commands shown below.

## Quick start (everything on one computer)

Open a separate terminal window for each step. Replace `path\to\auction-app` with
the folder where you unzipped the project (e.g. `cd C:\Users\you\Downloads\auction-app`).

**1. Server**
```
cd path\to\auction-app
python server.py --port 5000 --duration 30
```

**2. Bidder window (alice)**
```
cd path\to\auction-app
python client_gui.py --nick alice
```

**3. Bidder window (bob)**
```
cd path\to\auction-app
python client_gui.py --nick bob
```

**4. Auctioneer window (carol)**
```
cd path\to\auction-app
python client_gui.py --nick carol --role auctioneer
```

Each GUI opens on a connect screen (server, port, nickname, role). It is
pre-filled with `127.0.0.1` and port `5000`, so just press Connect.

Server options: `--duration` (seconds per item), `--pause` (seconds between
items), `--min-clients` (wait for this many bidders before each item),
`--balance` (play money each bidder starts with, default 500),
`--items "Watch:50,Book:5"` (name:starting price), `--anti-snipe 3`,
`--host` (interface to listen on, default `0.0.0.0` = all interfaces).

## Running across different computers / networks

The server listens on all network interfaces by default, so clients on other
machines only need the server's address and port.

**Same Wi-Fi / same local network (LAN)**
1. On the server computer, find its local IP address: run `ipconfig` and read
   the *IPv4 Address* (for example `192.168.1.25`). On macOS/Linux use
   `ifconfig` or `ip addr`.
2. Start the server there: `python server.py --port 5000 --duration 30`
3. The first time, Windows Firewall asks whether to allow Python. Click
   **Allow access** (private networks). If it was blocked, open an
   Administrator PowerShell and run:
   ```
   netsh advfirewall firewall add rule name="Auction" dir=in action=allow protocol=TCP localport=5000
   ```
4. On each other computer, start the GUI pointing at that IP:
   ```
   python client_gui.py --host 192.168.1.25 --port 5000 --nick alice
   ```
   (or type the IP into the "Server" box on the connect screen).

**Different networks:** Use a private team VPN and connect to the server's VPN address. This classroom application uses plaintext TCP and nickname-based accounts. Keep it on a trusted network. For a single-computer demo, pass `--host 127.0.0.1` to the server.

**If a client cannot connect**: check that the server is running, the IP and
port are right, both machines are on the same network (or the VPN is up), and
the firewall allows TCP on the port. Test with
`Test-NetConnection 192.168.1.25 -Port 5000` in PowerShell.

## Using the GUI
- **Bidder:** type an amount (the box pre-fills the minimum next bid) and click
  **Place bid**, or click **Bid minimum**. The screen shows the current item,
  high bid, highest bidder, countdown, the queue ("Up next"), who is online,
  your balance, and a live feed of everything that happens.
- **Auctioneer:** use the **List an item for sale** form (item name, start
  price, seconds) and click **Add to queue**.
- When every item is sold, a **Final results** window lists each item, its
  seller, winner and price, plus your own totals.

## Rules
- Roles: a bidder can bid but not list items. An auctioneer can list items but
  not bid, so nobody can bid on their own item. Listed items wait in a queue
  (max 20) behind the built-in items and run one after another; each listing
  has its own starting price and duration (5 to 300 seconds).
- Money: every bidder starts with the server's `--balance` (default 500). A bid
  above your balance is refused. When an item ends, the winner is charged and
  the auctioneer who listed it is paid (built-in items are sold by "the
  house"). A winner who disconnects is still charged, and balances are kept for
  the whole server run, so reconnecting does not reset your money. Auctioneers
  start at 0 and earn by selling.
- Bids: a bid must beat the current high bid by at least the minimum increment
  (default 1), you cannot outbid yourself, and a bid in the last 3 seconds
  extends the auction by 3 seconds (up to 3 times) to stop last-second sniping.
- Dead connections: the server turns on TCP keepalive for every client, so a
  user whose laptop loses Wi-Fi (no goodbye is ever sent) is detected and
  removed after about a minute instead of staying "online" forever.

Quick demo: start the server, open two bidder GUIs and one auctioneer GUI, bid
back and forth, close one bidder window abruptly and watch the others keep going.

## Run the evaluation
```
cd path\to\auction-app
python eval_tests.py          # full run, about 60 seconds
python eval_tests.py --quick  # smaller load test
```
It starts its own server on a free port (no need to start yours), prints PASS/FAIL for each check, and saves the
load-test numbers to `results.json`. The full run includes 10, 25 and 50 local bidders. RTT measures bid-to-reply time on loopback; broadcast timing measures arrival at the client callback, before GUI rendering.

## What the network tests establish

A bid carries the auction ID the bidder saw. The server checks that ID, the price, the balance and its own monotonic deadline under one lock. Lock acquisition determines the winner of an equal-price race. Client timestamps are for measurements only. Each accepted bid increments the auction's sequence number once. The bidder's reply and the observers' updates carry the same sequence and remaining time, so the bidder can update its clock without requesting another snapshot.

The evaluation sends a join across two writes and a status command in the same write as the end of the join. TCP carries bytes and can choose different packet boundaries. The test checks that the server decodes both complete messages in order. A separate two-bidder race checks that only one equal-price offer wins, both screens' underlying messages agree on the extended deadline, and a late joiner gets the current price and sequence in its first snapshot.

### Local evaluation on Windows, November 23, 2026

The full suite passed 75 checks. Each load round ran for eight seconds using clients and a separate server process on one computer. RTT runs from the client sending its bid to receiving the server's decision. A rejection is a processed decision and counts toward throughput.

| Concurrent bidders | Replies measured | Accepted | Rejected | Median RTT | 95th percentile RTT |
|---:|---:|---:|---:|---:|---:|
| 10 | 714 | 669 | 45 | 0.94 ms | 1.86 ms |
| 25 | 1839 | 1512 | 327 | 1.19 ms | 3.12 ms |
| 50 | 3592 | 2632 | 960 | 1.11 ms | 2.78 ms |

These measurements cover loopback communication. A two-laptop rehearsal should record the network used, confirm both screens agree on the final winner and price, and measure client-side round-trip time if comparing LAN performance. One-way timing across separate machines needs synchronized clocks.

## Optional: command-line client
```
python client.py --host 127.0.0.1 --port 5000 --nick alice
python client.py --nick carol --role auctioneer     # then: list 20 Vintage Lamp
```
Type a whole number to bid, `status` to see the current item, `quit` to leave.

## Protocol summary
One JSON object per line, ending in `\n` (TCP is a byte stream, so the newline
marks message boundaries). Full message list and the reasoning behind the design
are documented in the header comment of `backend/server.py`.

Each active state and item start includes an integer auction_id. Bids must echo the ID of the displayed item; older clients must be updated. Results are announced when the queue drains, even with an auctioneer connected. Nicknames identify play-money balances on a trusted demo network; they do not authenticate returning users.

Client to server: `join` (with `role`), `bid`, `list_item` (auctioneers only),
`status`, `leave`.
Server to client: `welcome`, `item_start`, `state`, `queue`, `balance`, `bid_accepted`,
`bid_rejected`, `item_listed`, `list_rejected`, `new_high_bid`, `auction_end`,
`user_joined`, `user_left`, `all_done`, `error`.
