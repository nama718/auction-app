#!/usr/bin/env python3
"""
client_gui.py - Tkinter desktop client for the auction (no extra packages).

Speaks exactly the same protocol as client.py, through the same AuctionClient
class, so the server does not know or care which one a user is running.

USAGE
    python3 client_gui.py                       # fill in the connect screen
    python3 client_gui.py --nick alice          # pre-fill the fields
    python3 client_gui.py --nick carol --role auctioneer

Bidders see the live item, price, countdown and a bid box.
Auctioneers see a form to list items for sale instead (they cannot bid).

THREADING NOTE
    Tkinter may only be touched from its own (main) thread, but AuctionClient
    delivers messages from a background reader thread. So the reader thread
    only puts each message into a queue.Queue, and the Tk main loop empties
    that queue every 40 ms (_pump) and does all the drawing.
"""

import argparse
import queue
import re
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, ttk

from client import AuctionClient

# Same limits the server enforces (the server re-checks everything anyway).
NICK_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
MAX_ITEM_NAME = 40
MIN_DURATION, MAX_DURATION = 5, 300

# Palette
BG, CARD, INK, MUTED = "#f3f4f6", "#ffffff", "#111827", "#6b7280"
ACCENT, GOOD, BAD, WARN = "#2563eb", "#15803d", "#b91c1c", "#b45309"

ERRORS = {
    "nick_taken": "That nickname is already taken. Pick another one.",
    "invalid_nick": "Nicknames may use letters, digits, _ and - (1 to 16 characters).",
    "invalid_role": "The server did not accept that role.",
    "line_too_long": "The server closed the connection (message too long).",
}
REJECTIONS = {
    "too_low": "Too low",
    "already_highest": "You are already the highest bidder",
    "no_active_auction": "No auction is running right now",
    "invalid_amount": "Enter a whole number above 0",
    "auctioneer_cannot_bid": "Auctioneers cannot bid",
    "insufficient_funds": "Not enough balance",
}
LIST_REJECTIONS = {
    "not_auctioneer": "Only auctioneers can list items",
    "invalid_item": "The server rejected that item (check name, price and duration)",
    "queue_full": "The queue is full, wait for an item to finish",
}


class AuctionApp:
    """The whole GUI: a connect screen, then the live auction screen (see module docstring)."""
    def __init__(self, root, host="127.0.0.1", port=5000, nick="", role="bidder"):
        """Build both screens, show the connect screen and start the polling loops."""
        self.root = root
        self.events = queue.Queue()  # reader thread -> Tk main thread
        self.client = None
        self.joined = False
        self.results_win = None      # the "Final results" window, once all items are sold
        self.gen = 0                 # bumped on every connect/leave; stale events are dropped
        self.req_id = 0
        self._reset_state()
        self._setup_style()
        self._build_connect(host, port, nick, role)
        self._build_main()
        self.show_connect()
        root.after(40, self._pump)
        root.after(100, self._tick)

    # ------------------------------------------------------------- state
    def _reset_state(self):
        """Forget everything about the previous connection (item, bids, balance, users)."""
        self.nick, self.role = "", "bidder"
        self.item = None             # name of the item on screen (running or just ended)
        self.seller = None
        self.start_price = 0
        self.high_bid = 0
        self.high_bidder = None
        self.active = False          # True while bids are being accepted
        self.ended = None            # the auction_end message, once the item is over
        self.deadline = None         # time.monotonic() when the item ends
        self.total = 1.0             # length of the item, for the progress bar
        self.auction_id = None
        self.min_increment = 1
        self.min_next = 1
        self.balance = 0             # play money, as last reported by the server
        self.users = {}              # nick -> role
        self.queue_items = []

    def _setup_style(self):
        """Pick the ttk theme and define the fonts and colours used everywhere."""
        r = self.root
        r.configure(bg=BG)
        base = tkfont.nametofont("TkDefaultFont").actual()["family"]
        self.f = lambda size, bold=False: (base, size, "bold" if bold else "normal")
        st = ttk.Style(r)
        try:
            st.theme_use("clam")  # looks the same on Windows, macOS and Linux
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=INK, font=self.f(11))
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=CARD)
        st.configure("TLabel", background=BG, foreground=INK)
        for name, size, bold, color in [
            ("Title", 20, True, INK), ("Muted", 10, False, MUTED), ("Hint", 10, False, MUTED),
            ("Item", 26, True, INK), ("Price", 46, True, ACCENT), ("Time", 28, True, INK),
            ("Banner", 14, True, GOOD), ("Section", 11, True, INK),
        ]:
            st.configure(f"{name}.TLabel", font=self.f(size, bold), foreground=color)
            st.configure(f"Card{name}.TLabel", font=self.f(size, bold), foreground=color,
                         background=CARD)
        st.configure("CardMuted.TLabel", background=CARD, foreground=MUTED, font=self.f(11))
        st.configure("TButton", padding=(14, 8), font=self.f(11))
        st.configure("Accent.TButton", background=ACCENT, foreground="white",
                     font=self.f(12, True), padding=(18, 10))
        st.map("Accent.TButton", background=[("active", "#1d4ed8"), ("disabled", "#9ca3af")],
               foreground=[("disabled", "#f3f4f6")])
        st.configure("TEntry", padding=6)
        st.configure("Horizontal.TProgressbar", troughcolor="#e5e7eb", background=ACCENT,
                     thickness=8)

    # ------------------------------------------------------ connect screen
    def _build_connect(self, host, port, nick, role):
        """Create the connect screen: server, port, nickname and role."""
        f = self.connect_frame = ttk.Frame(self.root, padding=30)
        box = ttk.Frame(f, style="Card.TFrame", padding=28)
        box.place(relx=0.5, rely=0.45, anchor="center")
        ttk.Label(box, text="Live Auction", style="CardItem.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(box, text="Connect to an auction server", style="CardMuted.TLabel").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, 16))

        self.var_host, self.var_port = tk.StringVar(value=host), tk.StringVar(value=str(port))
        self.var_nick, self.var_role = tk.StringVar(value=nick), tk.StringVar(value=role)
        rows = [("Server", self.var_host), ("Port", self.var_port), ("Nickname", self.var_nick)]
        for i, (label, var) in enumerate(rows, start=2):
            ttk.Label(box, text=label, style="CardMuted.TLabel").grid(
                row=i, column=0, sticky="w", pady=4, padx=(0, 14))
            e = ttk.Entry(box, textvariable=var, width=26)
            e.grid(row=i, column=1, sticky="ew", pady=4)
            e.bind("<Return>", lambda _e: self.connect())
            if label == "Nickname":
                self.entry_nick = e
        ttk.Label(box, text="Join as", style="CardMuted.TLabel").grid(
            row=5, column=0, sticky="w", pady=4)
        rb = ttk.Frame(box, style="Card.TFrame")
        rb.grid(row=5, column=1, sticky="w")
        for text, val in (("Bidder", "bidder"), ("Auctioneer", "auctioneer")):
            ttk.Radiobutton(rb, text=text, value=val, variable=self.var_role).pack(
                side="left", padx=(0, 14))
        self.lbl_connect_msg = ttk.Label(box, text="", style="CardMuted.TLabel",
                                         foreground=BAD, wraplength=320)
        self.lbl_connect_msg.grid(row=6, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.btn_connect = ttk.Button(box, text="Connect", style="Accent.TButton",
                                      command=self.connect)
        self.btn_connect.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(14, 0))

    # --------------------------------------------------------- main screen
    def _build_main(self):
        """Create the auction screen: item card, bid or listing box, queue, people, feed."""
        m = self.main = ttk.Frame(self.root, padding=14)
        m.columnconfigure(0, weight=3)
        m.columnconfigure(1, weight=2)

        head = ttk.Frame(m)
        head.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(head, text="Live Auction", style="Title.TLabel").pack(side="left")
        ttk.Button(head, text="Leave", command=self.leave).pack(side="right")
        self.lbl_who = ttk.Label(head, style="Muted.TLabel")
        self.lbl_who.pack(side="right", padx=14)
        self.lbl_balance = ttk.Label(head, style="Section.TLabel", foreground=ACCENT)
        self.lbl_balance.pack(side="right", padx=14)

        # --- current item card
        card = ttk.Frame(m, style="Card.TFrame", padding=18)
        card.grid(row=1, column=0, sticky="nsew", pady=(10, 8), padx=(0, 8))
        card.columnconfigure(0, weight=1)
        self.lbl_item = ttk.Label(card, style="CardItem.TLabel")
        self.lbl_item.grid(row=0, column=0, sticky="w")
        self.lbl_seller = ttk.Label(card, style="CardMuted.TLabel")
        self.lbl_seller.grid(row=1, column=0, sticky="w")
        self.lbl_caption = ttk.Label(card, style="CardMuted.TLabel")
        self.lbl_caption.grid(row=2, column=0, sticky="w", pady=(14, 0))
        self.lbl_price = ttk.Label(card, style="CardPrice.TLabel")
        self.lbl_price.grid(row=3, column=0, sticky="w")
        self.lbl_bidder = ttk.Label(card, style="CardMuted.TLabel")
        self.lbl_bidder.grid(row=4, column=0, sticky="w")
        self.lbl_time = ttk.Label(card, style="CardTime.TLabel")
        self.lbl_time.grid(row=0, column=1, rowspan=2, sticky="ne")
        self.bar = ttk.Progressbar(card, maximum=1000, mode="determinate")
        self.bar.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        self.lbl_banner = ttk.Label(card, style="CardBanner.TLabel", wraplength=480)
        self.lbl_banner.grid(row=6, column=0, columnspan=2, sticky="w", pady=(10, 0))

        # --- action card: bid box for bidders, listing form for auctioneers
        act = self.action = ttk.Frame(m, style="Card.TFrame", padding=18)
        act.grid(row=2, column=0, sticky="ew", padx=(0, 8))
        self.bid_frame = ttk.Frame(act, style="Card.TFrame")
        ttk.Label(self.bid_frame, text="Your bid", style="CardSection.TLabel").grid(
            row=0, column=0, sticky="w")
        self.var_bid = tk.StringVar()
        self.entry_bid = ttk.Entry(self.bid_frame, textvariable=self.var_bid, width=10,
                                   font=self.f(16, True))
        self.entry_bid.grid(row=1, column=0, pady=6, padx=(0, 10))
        self.entry_bid.bind("<Return>", lambda _e: self.place_bid())
        self.btn_bid = ttk.Button(self.bid_frame, text="Place bid", style="Accent.TButton",
                                  command=self.place_bid)
        self.btn_bid.grid(row=1, column=1, padx=(0, 10))
        self.btn_min = ttk.Button(self.bid_frame, command=self.bid_minimum)
        self.btn_min.grid(row=1, column=2)
        self.lbl_hint = ttk.Label(self.bid_frame, style="CardMuted.TLabel")
        self.lbl_hint.grid(row=2, column=0, columnspan=3, sticky="w")

        self.list_frame = ttk.Frame(act, style="Card.TFrame")
        ttk.Label(self.list_frame, text="List an item for sale", style="CardSection.TLabel").grid(
            row=0, column=0, columnspan=4, sticky="w")
        self.var_name, self.var_price = tk.StringVar(), tk.StringVar()
        self.var_dur = tk.StringVar(value="30")
        for col, (label, widget) in enumerate([
                ("Item name", ttk.Entry(self.list_frame, textvariable=self.var_name, width=24)),
                ("Start price", ttk.Entry(self.list_frame, textvariable=self.var_price, width=9)),
                ("Seconds", ttk.Spinbox(self.list_frame, from_=MIN_DURATION, to=MAX_DURATION,
                                        textvariable=self.var_dur, width=5))]):
            ttk.Label(self.list_frame, text=label, style="CardMuted.TLabel").grid(
                row=1, column=col, sticky="w", padx=(0, 10), pady=(8, 0))
            widget.grid(row=2, column=col, sticky="w", padx=(0, 10), pady=4)
            widget.bind("<Return>", lambda _e: self.list_item())
        ttk.Button(self.list_frame, text="Add to queue", style="Accent.TButton",
                   command=self.list_item).grid(row=2, column=3)
        self.lbl_list_status = ttk.Label(self.list_frame, style="CardMuted.TLabel")
        self.lbl_list_status.grid(row=3, column=0, columnspan=4, sticky="w")

        # --- right column: queue and people
        side = ttk.Frame(m)
        side.grid(row=1, column=1, rowspan=2, sticky="nsew", pady=(10, 0))
        side.rowconfigure(1, weight=1)
        side.rowconfigure(3, weight=1)
        side.columnconfigure(0, weight=1)
        ttk.Label(side, text="Up next", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        self.lst_queue = self._listbox(side, 1)
        ttk.Label(side, text="People online", style="Section.TLabel").grid(
            row=2, column=0, sticky="w", pady=(10, 0))
        self.lst_users = self._listbox(side, 3)

        # --- live feed
        ttk.Label(m, text="Live feed", style="Section.TLabel").grid(
            row=4, column=0, sticky="w", pady=(10, 2))
        feed = ttk.Frame(m)
        feed.grid(row=5, column=0, columnspan=2, sticky="nsew")
        m.rowconfigure(5, weight=1)
        feed.columnconfigure(0, weight=1)
        feed.rowconfigure(0, weight=1)
        self.txt = tk.Text(feed, height=9, state="disabled", wrap="word", relief="flat",
                           bg=CARD, fg=INK, font=self.f(10), padx=8, pady=6)
        sb = ttk.Scrollbar(feed, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        for tag, color in (("good", GOOD), ("bad", BAD), ("warn", WARN), ("muted", MUTED)):
            self.txt.tag_configure(tag, foreground=color)
        self.txt.tag_configure("bold", font=self.f(10, True))

    def _listbox(self, parent, row):
        """Create one small read-only list (used for the queue and the people list)."""
        lb = tk.Listbox(parent, height=4, relief="flat", bg=CARD, fg=INK, font=self.f(10),
                        highlightthickness=0, activestyle="none", selectbackground=CARD,
                        selectforeground=INK)
        lb.grid(row=row, column=0, sticky="nsew", pady=(2, 0))
        return lb

    # ---------------------------------------------------------- screens
    def show_connect(self):
        """Switch the window to the connect screen."""
        self.main.pack_forget()
        self.connect_frame.pack(fill="both", expand=True)
        self.root.title("Live Auction: connect")
        self.root.minsize(520, 460)
        self.btn_connect.state(["!disabled"])
        self.entry_nick.focus_set()

    def show_main(self):
        """Switch to the auction screen and show the bid box or the listing form by role."""
        self.connect_frame.pack_forget()
        self.main.pack(fill="both", expand=True)
        self.root.title(f"Live Auction: {self.nick} ({self.role})")
        self.root.minsize(880, 700)
        self.lbl_who.configure(text=f"Signed in as {self.nick} ({self.role})")
        self.bid_frame.pack_forget()
        self.list_frame.pack_forget()
        if self.role == "auctioneer":
            self.list_frame.pack(fill="x")
        else:
            self.bid_frame.pack(fill="x")
            self.entry_bid.focus_set()
        self.refresh()

    # --------------------------------------------------------- connecting
    def connect(self):
        """Validate the form, open the connection and send 'join' (the welcome comes back later)."""
        if self.client is not None:
            return
        host, nick = self.var_host.get().strip(), self.var_nick.get().strip()
        if not NICK_RE.match(nick):
            self.lbl_connect_msg.configure(text=ERRORS["invalid_nick"])
            return
        try:
            port = int(self.var_port.get())
        except ValueError:
            self.lbl_connect_msg.configure(text="Port must be a number.")
            return
        self._reset_state()
        self.nick, self.role = nick, self.var_role.get()
        self.joined = False
        self.gen += 1
        gen = self.gen
        self.lbl_connect_msg.configure(text="Connecting...", foreground=MUTED)
        self.btn_connect.state(["disabled"])
        self.root.update_idletasks()
        client = AuctionClient(host, port, nick,
                               lambda m, _t: self.events.put((gen, m)), self.role)
        try:
            client.connect()
        except OSError as e:
            self.lbl_connect_msg.configure(text=f"Could not connect: {e}", foreground=BAD)
            self.btn_connect.state(["!disabled"])
            return
        self.client = client

    def leave(self):
        """Disconnect politely and go back to the connect screen."""
        self._end_connection()
        self.show_connect()
        self.lbl_connect_msg.configure(text="", foreground=BAD)

    def _end_connection(self):
        """Close the socket and reset all state; late events from it are ignored."""
        self.gen += 1  # anything the old connection still delivers is now ignored
        if self.results_win is not None:
            self.results_win.destroy()
            self.results_win = None
        if self.client is not None:
            self.client.close()
            self.client = None
        self.joined = False
        self._reset_state()
        self._clear_feed()

    def shutdown(self):
        """Window close button: disconnect, then close the window."""
        self._end_connection()
        self.root.destroy()

    # ------------------------------------------------------------ actions
    def place_bid(self):
        """Send the number in the bid box as a bid (after the same checks the server makes)."""
        text = self.var_bid.get().strip()
        if not self.client or not self.active:
            return
        if not text.isdigit() or int(text) <= 0:
            self._hint("Enter a whole number above 0", BAD)
            return
        if int(text) > self.balance:  # same rule the server applies; saves a round trip
            self._hint(f"Not enough balance (you have {self.balance})", BAD)
            return
        self.req_id += 1
        self.client.bid(int(text), self.req_id, auction_id=self.auction_id)
        self._hint("", MUTED)

    def bid_minimum(self):
        """Fill the bid box with the lowest legal bid and send it."""
        self.var_bid.set(str(self.min_next))
        self.place_bid()

    def list_item(self):
        """Auctioneer: validate the form and ask the server to queue the item."""
        name, price, dur = self.var_name.get().strip(), self.var_price.get().strip(), self.var_dur.get().strip()
        if not name or len(name) > MAX_ITEM_NAME:
            return self._list_status(f"Item name must be 1 to {MAX_ITEM_NAME} characters", BAD)
        if not price.isdigit() or int(price) <= 0:
            return self._list_status("Start price must be a whole number above 0", BAD)
        if not dur.isdigit() or not (MIN_DURATION <= int(dur) <= MAX_DURATION):
            return self._list_status(f"Seconds must be {MIN_DURATION} to {MAX_DURATION}", BAD)
        if not self.client:
            return
        self.req_id += 1
        self.client.list_item(name, int(price), int(dur), self.req_id)
        self._list_status("Sending...", MUTED)

    def _hint(self, text, color):
        """Show a short message under the bid box."""
        self.lbl_hint.configure(text=text, foreground=color)

    def _list_status(self, text, color):
        """Show a short message under the listing form."""
        self.lbl_list_status.configure(text=text, foreground=color)

    # ----------------------------------------------------- message intake
    def _pump(self):
        """Runs on the Tk thread: drain everything the reader thread queued."""
        try:
            while True:
                gen, msg = self.events.get_nowait()
                if gen == self.gen:
                    self.handle(msg)
        except queue.Empty:
            pass
        self.root.after(40, self._pump)

    def handle(self, msg):
        """Update the state and the screen for ONE server message (runs on the Tk thread)."""
        t = msg.get("type")
        if not self.joined and self.client is not None:
            if t == "welcome":
                self.joined = True
                self.role = msg.get("role", self.role)
                self.balance = msg.get("balance", 0)
                auct = set(msg.get("auctioneers", []))
                self.users = {u: ("auctioneer" if u in auct else "bidder") for u in msg["users"]}
                self.show_main()
                self._refresh_users()
                self.log(f"Connected as {self.nick}.", "muted")
            elif t == "error":
                self._connect_failed(ERRORS.get(msg.get("code"), f"Server error: {msg.get('code')}"))
            elif t == "_disconnected":
                self._connect_failed("The server closed the connection.")
            return
        if self.client is None:
            return

        if t == "state":
            self._on_state(msg)
        elif t == "item_start":
            self._on_item_start(msg)
        elif t == "bid_accepted":
            self.high_bid, self.high_bidder = msg["amount"], self.nick
            self.min_next = msg["amount"] + self.min_increment
            self.log(f"Your bid of {msg['amount']} is the highest!", "good")
            self._hint("You are the highest bidder", GOOD)
            self.client.send({"type": "status"})  # resync the clock (anti-snipe may extend it)
        elif t == "bid_rejected":
            if "minimum" in msg:
                self.min_next = max(self.min_next, msg["minimum"])
            why = REJECTIONS.get(msg.get("reason"), msg.get("reason"))
            extra = f" (minimum {msg['minimum']})" if "minimum" in msg else ""
            if "balance" in msg:  # insufficient_funds: the server tells us the truth
                self.balance = msg["balance"]
                extra = f" (you have {msg['balance']})"
            self.log(f"Bid rejected: {why}{extra}", "bad")
            self._hint(f"{why}{extra}", BAD)
        elif t == "new_high_bid":
            self.high_bid, self.high_bidder = msg["amount"], msg["nick"]
            self.min_next = msg["amount"] + self.min_increment
            self.deadline = time.monotonic() + msg["ends_in"]
            self.total = max(self.total, msg["ends_in"])
            self.log(f"{msg['nick']} bid {msg['amount']}", "bold")
            self._hint("", MUTED)
        elif t == "auction_end":
            self._on_end(msg)
        elif t == "balance":  # private: we just paid for, or were paid for, an item
            self.balance = msg["balance"]
            self.log(f"Your balance is now {self.balance}", "muted")
        elif t == "queue":
            self.queue_items = msg["items"]
            self._refresh_queue()
        elif t == "item_listed":
            self._list_status(f"Listed {msg['item']} (position {msg['position']} in the queue)", GOOD)
            self.var_name.set("")
            self.var_price.set("")
            self.log(f"You listed {msg['item']}", "good")
        elif t == "list_rejected":
            why = LIST_REJECTIONS.get(msg.get("reason"), msg.get("reason"))
            self._list_status(why, BAD)
            self.log(f"Listing rejected: {why}", "bad")
        elif t == "user_joined":
            self.users[msg["nick"]] = msg.get("role", "bidder")
            self._refresh_users()
            self.log(f"{msg['nick']} joined", "muted")
        elif t == "user_left":
            self.users.pop(msg["nick"], None)
            self._refresh_users()
            self.log(f"{msg['nick']} left", "muted")
        elif t == "all_done":
            self.log("All items sold. Thanks for playing!", "warn")
            self._show_results(msg.get("results", []))
        elif t == "error":
            self.log(f"Error: {msg.get('code')}", "bad")
        elif t == "_disconnected":
            self._end_connection()
            self.show_connect()
            self.lbl_connect_msg.configure(text="Disconnected from the server.", foreground=BAD)
            return
        self.refresh()

    def _connect_failed(self, text):
        """Join did not work (bad nickname, server closed): stay on the connect screen."""
        self.gen += 1
        if self.client is not None:
            self.client.close()
            self.client = None
        self.lbl_connect_msg.configure(text=text, foreground=BAD)
        self.btn_connect.state(["!disabled"])

    def _on_state(self, msg):
        """Apply a 'state' reply: the running item, its bids and the time left."""
        if msg.get("item") is None:
            if not self.ended:
                self.item, self.active, self.deadline = None, False, None
            return
        self.auction_id = msg["auction_id"]
        self.min_increment = msg["min_increment"]
        self.item, self.seller = msg["item"], msg.get("seller")
        self.start_price, self.high_bid = msg["start_price"], msg["high_bid"]
        self.high_bidder = msg["high_bidder"]
        self.min_next = self.high_bid + self.min_increment if self.high_bidder else self.start_price
        self.deadline = time.monotonic() + msg["ends_in"]
        self.total = max(self.total if self.active else 0, msg["ends_in"], 1)
        self.active, self.ended = True, None

    def _on_item_start(self, msg):
        """A new item has started: reset the card, the bid box and the countdown."""
        self.auction_id = msg["auction_id"]
        self.min_increment = msg["min_increment"]
        self.item, self.seller = msg["item"], msg.get("seller")
        self.start_price = self.high_bid = msg["start_price"]
        self.min_next = msg["start_price"]
        self.high_bidder, self.ended, self.active = None, None, True
        self.total = max(msg["ends_in"], 1)
        self.deadline = time.monotonic() + msg["ends_in"]
        self.var_bid.set(str(self.min_next))
        self._hint("", MUTED)
        by = f" (sold by {self.seller})" if self.seller else ""
        self.log(f"NEW ITEM: {msg['item']}{by}, starting at {msg['start_price']}", "warn")

    def _on_end(self, msg):
        """The item is over: stop the clock and remember the result for the banner."""
        self.active, self.deadline, self.ended = False, None, msg
        if msg["winner"]:
            tag = "good" if msg["winner"] == self.nick else "warn"
            self.log(f"SOLD: {msg['item']} to {msg['winner']} for {msg['amount']}", tag)
        else:
            self.log(f"{msg['item']} got no bids.", "warn")

    # ------------------------------------------------------------ drawing
    def refresh(self):
        """Redraw the item card and the controls from the current state."""
        if self.item is None:
            self.lbl_item.configure(text="No auction running")
            self.lbl_seller.configure(text="")
            self.lbl_caption.configure(text="")
            self.lbl_price.configure(text="-")
            self.lbl_bidder.configure(text="Waiting for the next item...")
            self.lbl_banner.configure(text="")
            self.lbl_time.configure(text="")
            self.bar["value"] = 0
        else:
            self.lbl_item.configure(text=self.item)
            self.lbl_seller.configure(
                text=f"Sold by {self.seller}" if self.seller else "Sold by the house")
            self.lbl_price.configure(text=str(self.high_bid))
            mine = self.high_bidder == self.nick
            who = "you" if mine else self.high_bidder
            if self.ended is not None:
                e = self.ended
                self.lbl_caption.configure(text="Final price" if e["winner"] else "Starting price")
                self.lbl_bidder.configure(
                    text=f"Winner: {e['winner']}" if e["winner"] else "No bids were placed")
                if e["winner"] == self.nick:
                    self.lbl_banner.configure(text=f"You won {e['item']} for {e['amount']}!",
                                              foreground=GOOD)
                elif e["winner"]:
                    self.lbl_banner.configure(text=f"SOLD to {e['winner']} for {e['amount']}",
                                              foreground=WARN)
                else:
                    self.lbl_banner.configure(text="Not sold", foreground=MUTED)
                self.lbl_time.configure(text="Ended", foreground=MUTED)
                self.bar["value"] = 0
            else:
                self.lbl_caption.configure(
                    text="Current bid" if self.high_bidder else "Starting price")
                self.lbl_bidder.configure(
                    text=f"Highest bidder: {who}" if self.high_bidder else "No bids yet")
                self.lbl_banner.configure(
                    text="You are the highest bidder" if mine else "", foreground=GOOD)
        # While you lead, your bid is "held": shown here, charged when the item ends.
        held = self.high_bid if (self.active and self.high_bidder == self.nick) else 0
        self.lbl_balance.configure(
            text=f"Balance: {self.balance}" + (f" ({held} held)" if held else ""))
        bidding = self.active and self.role == "bidder"
        self.btn_bid.state(["!disabled"] if bidding else ["disabled"])
        self.btn_min.state(["!disabled"] if bidding else ["disabled"])
        self.btn_min.configure(text=f"Bid minimum ({self.min_next})")
        self._autofill_bid()

    def _autofill_bid(self):
        """Keep the bid box at the minimum unless the user typed something higher."""
        text = self.var_bid.get().strip()
        if not text.isdigit() or int(text) < self.min_next:
            self.var_bid.set(str(self.min_next) if self.active else "")

    def _tick(self):
        """Runs every 100 ms: redraw the countdown and progress bar from the local clock."""
        if self.deadline is not None and self.active:
            rem = max(0.0, self.deadline - time.monotonic())
            self.lbl_time.configure(text=f"{rem:4.1f}s" if rem < 10 else f"{int(rem)}s",
                                    foreground=BAD if rem <= 5 else INK)
            self.bar["value"] = min(1.0, rem / self.total) * 1000
        self.root.after(100, self._tick)

    def _refresh_queue(self):
        """Redraw the 'Up next' list."""
        self.lst_queue.delete(0, "end")
        if not self.queue_items:
            self.lst_queue.insert("end", "(nothing queued)")
        for i, it in enumerate(self.queue_items, start=1):
            by = it["seller"] or "house"
            self.lst_queue.insert("end", f"{i}. {it['item']}  from {it['start_price']}  ({by})")

    def _refresh_users(self):
        """Redraw the 'People online' list, tagging auctioneers and yourself."""
        self.lst_users.delete(0, "end")
        for n in sorted(self.users):
            tags = []
            if self.users[n] == "auctioneer":
                tags.append("auctioneer")
            if n == self.nick:
                tags.append("you")
            self.lst_users.insert("end", n + (f"  ({', '.join(tags)})" if tags else ""))

    def log(self, text, tag="info"):
        """Append one timestamped line to the live feed."""
        self.txt.configure(state="normal")
        self.txt.insert("end", time.strftime("%H:%M:%S  "), "muted")
        self.txt.insert("end", text + "\n", tag)
        self.txt.configure(state="disabled")
        self.txt.see("end")

    def _show_results(self, results):
        """Pop up a table of every item, its seller, winner and price, plus your own totals."""
        if self.results_win is not None:
            self.results_win.destroy()
        win = self.results_win = tk.Toplevel(self.root)
        win.title("Final results")
        win.configure(bg=BG)
        win.geometry("640x420")
        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Final results", style="Title.TLabel").pack(anchor="w")

        cols = ("item", "seller", "winner", "price")
        tree = ttk.Treeview(frame, columns=cols, show="headings", height=8)
        for col, text, width in (("item", "Item", 220), ("seller", "Sold by", 110),
                                 ("winner", "Winner", 110), ("price", "Price", 80)):
            tree.heading(col, text=text)
            tree.column(col, width=width, anchor="e" if col == "price" else "w")
        for r in results:
            tree.insert("", "end", values=(
                r["item"], r.get("seller") or "the house",
                r["winner"] or "no bids", r["amount"] if r["winner"] else "-"))
        tree.pack(fill="both", expand=True, pady=10)

        mine = [r for r in results if r["winner"] == self.nick]
        sold = [r for r in results if r.get("seller") == self.nick and r["winner"]]
        if self.role == "bidder":
            summary = (f"You won {len(mine)} item(s) and spent {sum(r['amount'] for r in mine)}. "
                       f"Final balance: {self.balance}.")
        else:
            summary = (f"You sold {len(sold)} item(s) for {sum(r['amount'] for r in sold)} in total. "
                       f"Final balance: {self.balance}.")
        ttk.Label(frame, text=summary, style="Section.TLabel").pack(anchor="w")
        ttk.Button(frame, text="Close", command=win.destroy).pack(anchor="e", pady=(10, 0))

    def _clear_feed(self):
        """Empty the feed and the side lists (used when leaving)."""
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")
        self.lst_queue.delete(0, "end")
        self.lst_users.delete(0, "end")
        self.lbl_list_status.configure(text="")


def main():
    """Parse the command-line options and open the window."""
    ap = argparse.ArgumentParser(description="Auction GUI client")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--nick", default="")
    ap.add_argument("--role", choices=["bidder", "auctioneer"], default="bidder")
    args = ap.parse_args()

    root = tk.Tk()
    root.geometry("960x760")
    app = AuctionApp(root, args.host, args.port, args.nick, args.role)
    root.protocol("WM_DELETE_WINDOW", app.shutdown)
    root.mainloop()


if __name__ == "__main__":
    main()
