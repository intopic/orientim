# -*- coding: utf-8 -*-
"""An agent that looks like it works — and is not stable."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
B = "http://127.0.0.1:8731"


def agent(h):
    """Plan, search, then decide. Looks perfectly reasonable."""
    c = h.client()
    c.post(f"{B}/chat-stable", content=b'{"plan":"find the status"}')
    r = c.post(f"{B}/chat", content=b'{"q":"status"}').json()   # temperature > 0
    n = int("".join(ch for ch in r["text"] if ch.isdigit()) or 0)

    if n % 3 == 0:                       # branches depend on the model's output
        c.post(f"{B}/search", content=b'{"q":"deep"}')
        c.post(f"{B}/search", content=b'{"q":"again"}')
        return "found via deep search"
    if n % 3 == 1:
        c.post(f"{B}/search", content=b'{"q":"fast"}')
        return "found fast"
    return "not found"
