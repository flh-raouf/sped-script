#!/usr/bin/env python3
"""Reconstruct each found document from its local page ranges within each Lot."""

from reconstruction_core import Strategy, cli_main

if __name__ == "__main__":
    raise SystemExit(cli_main(Strategy.RANGES))
