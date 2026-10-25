"""Daily USD prices for a token by contract address, from CoinGecko, cached per day.

Key: COINGECKO_PRO_API_KEY or ~/.config/cexflows/coingecko_pro_key (pro host), else
COINGECKO_API_KEY or ~/.config/cexflows/coingecko_key (demo key, public host), else the public
host without a key. Pro keys only work on pro-api.coingecko.com and demo keys only on the public
host; mixing them is an error, not a downgrade. The free and demo tiers serve 365 days of history.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from .cache import Cache

PLATFORMS = {"ethereum": "ethereum", "arbitrum": "arbitrum-one"}
_CFG = os.path.join(os.path.expanduser("~"), ".config", "cexflows")


def _read(path: str) -> str:
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    return ""


def endpoint() -> Tuple[str, Dict[str, str]]:
    pro = os.environ.get("COINGECKO_PRO_API_KEY", "").strip() or _read(os.path.join(_CFG, "coingecko_pro_key"))
    if pro:
        return "https://pro-api.coingecko.com/api/v3", {"x-cg-pro-api-key": pro}
    demo = os.environ.get("COINGECKO_API_KEY", "").strip() or _read(os.path.join(_CFG, "coingecko_key"))
    return "https://api.coingecko.com/api/v3", ({"x-cg-demo-api-key": demo} if demo else {})


def _get(url: str, headers: Dict[str, str], retries: int = 4) -> dict:
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "cexflows", "accept": "application/json", **headers})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"CoinGecko HTTP {e.code}: {e.read()[:200]!r}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == retries - 1:
                raise RuntimeError(f"CoinGecko: {e}") from e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("CoinGecko: retries exhausted")


def daily_prices(chain: str, token: str, start_ts: int, end_ts: int,
                 cache: Optional[Cache] = None) -> Dict[str, float]:
    """UTC day -> USD price for every day in [start_ts, end_ts] that CoinGecko has.

    Each day's value is CoinGecko's daily point for that date (its 00:00 UTC price). Days already
    cached are not fetched again, except today, whose price is still moving.
    """
    chain, token = chain.lower(), token.lower()
    platform = PLATFORMS.get(chain)
    if not platform:
        raise ValueError(f"no CoinGecko platform for chain {chain!r}")
    cache = cache or Cache()
    day = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
    have = cache.prices(chain, token, day(start_ts), day(end_ts))
    today = day(time.time())
    wanted_days = int((end_ts - start_ts) // 86400) + 1
    missing = len([d for d in have if d != today]) < wanted_days - (1 if day(end_ts) == today else 0)
    if missing:
        base, headers = endpoint()
        q = urllib.parse.urlencode({"vs_currency": "usd", "from": int(start_ts) - 86400, "to": int(end_ts) + 86400})
        data = _get(f"{base}/coins/{platform}/contract/{token}/market_chart/range?{q}", headers)
        points: Dict[str, float] = {}
        for ms, price in data.get("prices", []):
            d = day(ms / 1000)
            # Keep the earliest point of each day: CoinGecko's daily series stamps 00:00 UTC.
            if d not in points:
                points[d] = float(price)
        cache.store_prices(chain, token, points)
        have = cache.prices(chain, token, day(start_ts), day(end_ts))
    return have
