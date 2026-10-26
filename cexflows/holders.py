"""Holder positions: every wallet's cost basis and sale history, rebuilt from the token's transfers.

Tokens are followed as cost lots from the moment they are minted:

    mint                               lots enter at zero cost (origin "mint")
    wallet -> wallet                   lots move with their cost, oldest first
    exchange wallet or pool -> wallet  a purchase: the lot is repriced at that day's price
    wallet -> exchange wallet or pool  a sale: recorded for the sender with the multiple of cost
    wallet -> exchange deposit address a sale (the address only forwards to a hot wallet)

So vesting and treasury contracts hand zero-cost lots to insiders without being labelled, and
buyers carry what they paid. Exchange-side purchases are priced at the day's price: the DEX pool
price where a pool was deep enough is not yet used (ALT, the first token, has no deep pool), so
every price is CoinGecko's daily close for now.

Not modelled as holders, though their transfers still move lots: exchange wallets (many users
each), pools, contracts other than Safe multisigs, and unlabelled services (wallets with
thousands of counterparties).
"""
from __future__ import annotations

import json
import urllib.request
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Callable, Deque, Dict, Iterable, List, Optional, Set, Tuple

from . import etherscan
from .cache import Cache
from .flows import token_decimals
from .labels import hot_wallets
from .prices import daily_prices

ZERO = "0x" + "0" * 40
GECKOTERMINAL = {"ethereum": "eth", "arbitrum": "arbitrum"}
SAFE_GET_OWNERS = "0xa0e67e2b"     # getOwners(): answers only on a Safe multisig
SERVICE_COUNTERPARTIES = 2000      # more distinct counterparties than this: a service, not a holder
TRADER_SALES = 100                 # this many sales or more: a market maker or bot, not a holder
HISTORY_CHUNK = 250_000            # blocks per cached chunk of a token's history


def dex_pools(chain: str, token: str) -> Dict[str, str]:
    """Pool address -> dex id for the token's pools, from GeckoTerminal's free API."""
    url = f"https://api.geckoterminal.com/api/v2/networks/{GECKOTERMINAL[chain]}/tokens/{token}/pools?page=1"
    req = urllib.request.Request(url, headers={"accept": "application/json", "User-Agent": "cexflows"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    out = {}
    for p in data.get("data", []):
        addr = p["attributes"]["address"].lower()
        if addr.startswith("0x") and len(addr) == 42:   # v4 pools are ids, not addresses
            out[addr] = p["relationships"]["dex"]["data"]["id"]
    return out


def is_safe(chain: str, address: str) -> bool:
    try:
        r = etherscan.call(chain, module="proxy", action="eth_call", to=address, data=SAFE_GET_OWNERS, tag="latest")
    except etherscan.EtherscanError:
        return False
    return isinstance(r, str) and len(r) > 130


def services(transfers: Iterable[dict], limit: int = SERVICE_COUNTERPARTIES) -> Set[str]:
    """Wallets with more than `limit` distinct counterparties: exchanges and other services that
    the labels miss. They act for many users, so they are treated like exchange wallets."""
    peers: Dict[str, Set[str]] = defaultdict(set)
    for t in transfers:
        for a, b in ((t["from"], t["to"]), (t["to"], t["from"])):
            if a != ZERO and len(peers[a]) <= limit:
                peers[a].add(b)
    return {a for a, p in peers.items() if len(p) > limit}


def deposit_addresses(transfers: Iterable[dict], market: Set[str], min_share: float = 0.9) -> Set[str]:
    """Exchange deposit addresses: they pass on what they receive, almost all of it into market
    wallets. Both conditions matter: a holder who once sold to an exchange sends only there too,
    but keeps most of what it received."""
    in_total: Dict[str, int] = defaultdict(int)
    out_total: Dict[str, int] = defaultdict(int)
    out_market: Dict[str, int] = defaultdict(int)
    for t in transfers:
        in_total[t["to"]] += t["value"]
        if t["from"] in market or t["from"] == ZERO:
            continue
        out_total[t["from"]] += t["value"]
        if t["to"] in market:
            out_market[t["from"]] += t["value"]
    return {a for a, v in out_market.items()
            if v >= min_share * out_total[a] and v >= min_share * in_total[a]}


def project_wallets(transfers: List[dict], market: Set[str], is_contract: Callable[[str], bool],
                    min_share: float = 0.9, min_recipients: int = 3) -> Set[str]:
    """The project's own wallets, found from the token's flow: whoever received the mint, plus
    any contract (Safes included) that got nearly all its tokens from a project wallet and pays
    them on to several addresses itself: treasuries, distributors, vesting vaults. They release
    on the unlock schedule rather than at a price."""
    inflow: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    recipients: Dict[str, Set[str]] = defaultdict(set)
    for t in transfers:
        inflow[t["to"]][t["from"]] += t["value"]
        if t["to"] not in market and len(recipients[t["from"]]) < min_recipients:
            recipients[t["from"]].add(t["to"])
    project = {t["to"] for t in transfers if t["from"] == ZERO and t["to"] not in market}
    grew = True
    while grew:
        grew = False
        for w, by in inflow.items():
            if w in project or w in market or w == ZERO or len(recipients[w]) < min_recipients:
                continue
            if sum(v for f, v in by.items() if f in project) >= min_share * sum(by.values()) and is_contract(w):
                project.add(w)
                grew = True
    return project


class Book:
    """Cost lots per wallet, oldest first: [amount, unit cost in USD, acquired ts, origin]."""

    def __init__(self):
        self.lots: Dict[str, Deque[list]] = defaultdict(deque)

    def add(self, wallet: str, amount: int, cost: float, ts: int, origin: str) -> None:
        if amount > 0:
            self.lots[wallet].append([amount, cost, ts, origin])

    def take(self, wallet: str, amount: int) -> List[list]:
        """Remove `amount` oldest first; returns the pieces taken. A shortfall (a sender with
        tokens from before the history, which shouldn't happen from mint) comes back as an
        "unknown" piece at no cost information."""
        taken, q = [], self.lots[wallet]
        while amount > 0 and q:
            lot = q[0]
            part = min(lot[0], amount)
            taken.append([part, lot[1], lot[2], lot[3]])
            lot[0] -= part
            amount -= part
            if lot[0] == 0:
                q.popleft()
        if amount > 0:
            taken.append([amount, None, None, "unknown"])
        return taken

    def balance(self, wallet: str) -> int:
        return sum(l[0] for l in self.lots[wallet])


def build_positions(transfers: List[dict], hot: Set[str], pools: Set[str], deposits: Set[str],
                    price_on: Callable[[int], Optional[float]]) -> Tuple[Book, Dict[str, List[dict]]]:
    """Replay transfers (oldest first). Returns the lot book and each wallet's sales."""
    book = Book()
    sales: Dict[str, List[dict]] = defaultdict(list)
    market = hot | pools
    for t in transfers:
        src, dst, v, ts = t["from"], t["to"], t["value"], t["ts"]
        if v == 0 or src == dst:
            continue
        price = price_on(ts)
        if src == ZERO:
            book.add(dst, v, 0.0, ts, "mint")
            continue
        pieces = book.take(src, v)
        if src in market and dst not in market:
            # A purchase: whatever the market wallet held is replaced by what the buyer paid.
            book.add(dst, v, price if price is not None else float("nan"), ts, "pool" if src in pools else "exchange")
            continue
        if dst in market or dst in deposits:
            if src not in market:
                known = [p for p in pieces if p[1] is not None]
                qty = sum(p[0] for p in known)
                cost = sum(p[0] * p[1] for p in known) / qty if qty else None
                sales[src].append({"ts": ts, "tokens": v, "price": price, "cost": cost,
                                   "venue": "pool" if dst in pools else "exchange"})
            if dst in deposits:
                book.add(dst, v, price if price is not None else float("nan"), ts, "deposit")
            continue
        for amount, cost, acq, origin in pieces:
            book.add(dst, amount, cost if cost is not None else (price or float("nan")), acq or ts, origin)
    return book, sales


def ensure_history(chain: str, token: str, cache: Cache, progress: Callable[[str], None] = lambda m: None) -> None:
    """Cache every transfer of the token since its contract was created, in resumable chunks.
    The first pull of a small cap takes about an hour (ALT: 1.5M transfers, 1,500 calls)."""
    created = etherscan.call(chain, module="contract", action="getcontractcreation", contractaddresses=token)
    start = int(created[0]["blockNumber"])
    end = etherscan.latest_block(chain)
    covered = cache.token_log_ranges(chain, token)
    b = start
    while b <= end:
        z = min(b + HISTORY_CHUNK - 1, end)
        gaps, lo = [], b
        for c_lo, c_hi in covered:      # fetch only the parts of [b, z] not already cached
            if c_hi < lo or c_lo > z:
                continue
            if c_lo > lo:
                gaps.append((lo, c_lo - 1))
            lo = max(lo, c_hi + 1)
        if lo <= z:
            gaps.append((lo, z))
        for g_lo, g_hi in gaps:
            n = cache.store_token_logs(chain, token, etherscan.transfers(chain, token, g_lo, g_hi), g_lo, g_hi)
            progress(f"history blocks {g_lo}-{g_hi}: {n} transfers")
        b = z + 1


def holder_positions(chain: str, token: str, top_n: int = 100, as_of: Optional[str] = None,
                     cache: Optional[Cache] = None, labels_path: Optional[str] = None,
                     progress: Callable[[str], None] = lambda m: None) -> dict:
    """The top N modelled holders with balance, cost basis and sale history, from the token's
    full transfer history (fetched into the cache first if missing)."""
    chain, token = chain.lower(), token.lower()
    cache = cache or Cache()
    end_ts = (int(datetime.strptime(as_of, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()) + 86400
              if as_of else int(datetime.now(timezone.utc).timestamp()) + 1)
    ensure_history(chain, token, cache, progress)
    transfers = cache.token_logs(chain, token, 0, end_ts)
    if not transfers or transfers[0]["from"] != ZERO:
        raise ValueError("the cached history doesn't start at the token's first mint; pull it first")
    progress(f"{len(transfers)} transfers")
    scale = 10 ** token_decimals(chain, token)
    hot = set(hot_wallets(chain, labels_path))
    pools = set(dex_pools(chain, token))
    service = services(transfers) - hot - pools
    hot |= service
    deposits = deposit_addresses(transfers, hot | pools) - pools
    prices = daily_prices(chain, token, transfers[0]["ts"], end_ts - 1, cache)
    day = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
    price_on = lambda ts: prices.get(day(ts))
    progress(f"{len(hot) - len(service)} hot wallets, {len(service)} services, {len(pools)} pools, {len(deposits)} deposit addresses, {len(prices)} price days")
    contract_memo: Dict[str, bool] = {}

    def contract(a: str) -> bool:
        if a not in contract_memo:
            contract_memo[a] = etherscan.is_contract(chain, a)
        return contract_memo[a]

    project = project_wallets(transfers, hot | pools, contract)
    progress(f"{len(project)} project wallets")
    book, sales = build_positions(transfers, hot, pools, deposits, price_on)

    raw: Dict[str, int] = defaultdict(int)
    for t in transfers:
        raw[t["from"]] -= t["value"]
        raw[t["to"]] += t["value"]
    supply = sum(v for a, v in raw.items() if a != ZERO and v > 0)
    ranked = sorted(((v, a) for a, v in raw.items() if a != ZERO and v > 0), reverse=True)
    mismatched = 0
    holders, excluded = [], []
    for bal, w in ranked:
        if len(holders) >= top_n or bal <= 0:
            break
        why = None
        if w in service:
            why = "service"
        elif w in hot:
            why = "exchange"
        elif w in pools:
            why = "pool"
        elif w in deposits:
            why = "deposit address"
        elif w in project:
            why = "project"
        elif contract(w) and not is_safe(chain, w):
            why = "contract"
        if why:
            excluded.append({"address": w, "balance": bal / scale, "share_of_supply": bal / supply, "reason": why})
            continue
        lots = list(book.lots[w])
        if book.balance(w) != bal:
            mismatched += 1
        priced = [l for l in lots if l[1] == l[1]]          # drop NaN costs
        qty = sum(l[0] for l in priced)
        zero = sum(l[0] for l in lots if l[1] == 0)
        s = sales[w]
        multiples = sorted(x["price"] / x["cost"] for x in s if x["price"] and x["cost"])
        zero_share = zero / bal
        holders.append({
            "address": w,
            "kind": ("trader" if len(s) >= TRADER_SALES else "time" if zero_share >= 0.5
                     else "price" if qty else "unknown"),
            "balance": bal / scale,
            "share_of_supply": bal / supply,
            "cost_basis_usd": (sum(l[0] * l[1] for l in priced) / qty) if qty else None,
            "zero_cost_share": zero_share,
            "origins": json.dumps({o: round(sum(l[0] for l in lots if l[3] == o) / bal, 4)
                                   for o in sorted({l[3] for l in lots})}),
            "oldest_lot": day(min(l[2] for l in lots)) if lots else None,
            "n_sales": len(s),
            "sold_tokens": sum(x["tokens"] for x in s) / scale,
            "median_sale_multiple": multiples[len(multiples) // 2] if multiples else None,
            "last_sale": day(s[-1]["ts"]) if s else None,
        })
    progress(f"{len(holders)} holders modelled, {len(excluded)} large wallets set aside, "
             f"{mismatched} with lots not matching their balance")
    return {"holders": holders, "excluded": excluded, "supply": supply / scale, "lot_mismatches": mismatched,
            "price_now": prices.get(day(end_ts - 1)),
            "params": {"chain": chain, "token": token, "top_n": top_n, "as_of": as_of}}
