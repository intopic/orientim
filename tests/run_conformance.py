# -*- coding: utf-8 -*-
"""Conformance: the 20 sources of non-determinism, strict or loose key.

The specification lives in `orientim/patterns.py` and its server in
`orientim/_probe.py` — the very one a user runs with `orientim conformance`.
This file once held a second copy of the same twenty patterns; the two had
started to drift apart and reported different numbers for the same source.
There is no copy any more.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from orientim import conformance


def main(argv):
    strict = "--loose" not in argv
    rep = conformance.run(strict=strict)
    print(conformance.format_report(rep))
    return 1 if rep["undeclared_failures"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
