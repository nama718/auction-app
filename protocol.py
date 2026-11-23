"""Shared input limits. The GUI provides feedback; the server enforces the rules."""

import re

NICK_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
MAX_ITEM_NAME = 40
MIN_DURATION, MAX_DURATION = 5, 300
