# Code walkthrough

Start with a single question: Alice and Bob both see a price of 45. They each offer 50. What keeps the room from announcing two winners?

## Where everything lives

| File | What to look for |
|---|---|
| [server.py](server.py), [client_gui.py](client_gui.py), [client.py](client.py), [eval_tests.py](eval_tests.py) | These are the four commands from the README. Each starts the corresponding implementation below. |
| [backend/server.py](backend/server.py) | The authoritative item, balances, auction clock, bid rules, and TCP connections. Start at AuctionServer._do_bid to read the decision itself. |
| [frontend/client.py](frontend/client.py) | AuctionClient opens the socket, sends JSON messages, and turns incoming bytes back into messages. The command-line interface uses the same client as the GUI. |
| [frontend/gui.py](frontend/gui.py) | AuctionApp collects input and renders the state reported by the server. |
| [protocol.py](protocol.py) | Nickname and listing limits shared by the input form and the server. The server checks them even when someone uses the CLI. |
| [evaluation/suite.py](evaluation/suite.py) | Starts a real local server, drives clients, checks outcomes, and records load measurements. |
| [backend](backend/__init__.py), [frontend](frontend/__init__.py), [evaluation](evaluation/__init__.py) | Package markers with a short description of each directory. |

## 1. Join and catch up

The server accepts a TCP connection. The client sends a join message with a nickname and role. Once the server accepts it, _do_join sends the client's balance, the people in the room, the current auction state, and the queue. That's how someone joining halfway through a round sees the current high bid without replaying every earlier event. Balances are kept in memory by nickname for this server run. This is a trusted classroom demo, so a nickname is not proof of identity.

## 2. Send the bid for the item on screen

AuctionApp.place_bid passes the amount and the displayed auction ID to AuctionClient.bid. For example, Alice might send:

~~~json
{"type":"bid","auction_id":1,"amount":50,"id":7}
~~~

The auction ID identifies the item. The request ID lets Alice match the reply to her click. The sequence number comes later: the server assigns it when a bid succeeds. These numbers answer three different questions.

Each JSON message ends with a newline. TCP preserves byte order but does not preserve application message boundaries. The server's _handle_client keeps an unfinished line for the next read and processes every complete line already in the buffer. A per-line size limit bounds unfinished requests.

## 3. Decide the race once

In _do_bid, the server holds the auction lock while checking the active round, deadline, bidder, minimum price, and balance, then recording an accepted bid. Suppose Alice gets the lock first. Her 50 becomes the high bid. When Bob gets the lock, his 50 is compared against Alice's new price and rejected. The server clock decides whether a bid is on time. Neither laptop's clock settles the race.

A late valid bid can extend the countdown within the configured extension limit. Alice's acceptance and the observers' updates carry the same price, auction ID, sequence, and remaining time from that decision. Each client has a bounded outgoing queue. Its writer thread sends queued messages in order so the auction lock never has to wait for a slow socket write.

## 4. Update the screen and close the sale

The client reader delivers incoming messages to a queue. AuctionApp._pump runs on the Tk thread and hands each message to handle. This keeps widget updates on the UI thread. The countdown displayed on a laptop is an estimate; the server announces the actual end.

The auction loop closes the round under the same lock used for bids. It records one result and settles the winner's charge and any seller payment. Disconnecting after placing a winning bid does not erase that commitment. When the queue drains, the server announces the results.

## 5. Check these claims yourself

Run python eval_tests.py from the repository root. In evaluation/suite.py, test_stream_and_contention exercises split and combined TCP writes, the equal-price race, the extended deadline, and a late joiner's snapshot. test_balances checks what happens to the money. load_test checks whether concurrent bidders agree on the result and writes measurements to results.json. Use the [README evaluation section](README.md#what-the-network-tests-establish) for the measured results and the distinction between local protocol timing and a two-laptop LAN demo.
