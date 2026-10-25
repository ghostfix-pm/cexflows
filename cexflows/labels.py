"""Exchange hot wallets per chain, from labels/hot_wallets.csv (chain,address,exchange,source)."""
from __future__ import annotations

import csv
import os
from typing import Dict, Optional

DEFAULT_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "labels", "hot_wallets.csv")


def hot_wallets(chain: str, path: Optional[str] = None) -> Dict[str, str]:
    """address (lowercase) -> exchange name, for one chain."""
    out: Dict[str, str] = {}
    with open(path or os.environ.get("CEXFLOWS_LABELS") or DEFAULT_FILE, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["chain"].strip().lower() == chain.lower():
                out[row["address"].strip().lower()] = row["exchange"].strip()
    return out
