#!/usr/bin/env python3
"""Diagnose the server-to-model-machine AI Port link.

Run on the website/server machine:
    python tools/diagnose_local_ai.py

It reports the configured URL, the IPv4-resolved URL, whether /api/modules is
reachable, and where the setting came from. The same command can be run on the
model machine to verify AI Port's loopback side.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import local_gateway


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    parser.add_argument("--timeout", type=float, default=1.5)
    args = parser.parse_args()

    if args.json:
        print(local_gateway.as_json(args.timeout))
        return 0

    print(local_gateway.diagnostic_text(args.timeout))
    info = local_gateway.snapshot(args.timeout)
    if info["ready"]:
        print("\nOK: server can reach AI Port.")
        return 0

    print("\nChecklist:")
    print("1. Model machine AI Port must listen on 0.0.0.0:8801.")
    print("2. Model machine firewall must allow inbound TCP 8801 from the LAN.")
    print("3. AIPORT_BASE_URL must point at the model machine, not the server.")
    print("4. Put that value in config/local_ai.env and restart the portal.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
