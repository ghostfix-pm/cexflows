# cexflows

Daily token inflows into exchange hot wallets, measured from Etherscan data and served through a
local, Dune-compatible API. It answers the questions a Dune query about exchange deposits would,
for EVM tokens on Ethereum and Arbitrum, with no Dune plan.

```
python -m cexflows.server        # serves http://127.0.0.1:8766/api/v1
```

Any client that runs saved Dune queries can point at it: same paths, same JSON, same
`X-Dune-API-Key` header, holding this server's key instead of a Dune one.

```
export DUNE_API_BASE=http://127.0.0.1:8766/api/v1
export DUNE_API_KEY=$(cat ~/.cexflows-key)
```

Stdlib only, Python 3.8+.

## What it measures

Users deposit into per-user deposit addresses, which no public label set covers. Exchanges then
sweep those into a handful of hot wallets. cexflows counts every transfer of the token into a
labelled hot wallet from an address outside the labelled set: a sweep of user deposits, usually
hours after the deposit, or a direct deposit. Transfers between two labelled wallets are internal
and skipped. Only inbound transfers are downloaded (the logs endpoint filtered on the Transfer
event's recipient), so a hot wallet's withdrawals never cost a call.

Each wallet's history is cached in SQLite (`~/.cexflows/cache.sqlite`, or `CEXFLOWS_CACHE`) with
the block range it covers, so a rerun only fetches new blocks.

## Queries

The server runs no SQL. Each supported query is implemented natively under a local id:

| id | name | parameters | columns |
| - | - | - | - |
| 1 | `cex_inflows` | `token_address`, `chain` (`ethereum`, `arbitrum`), `days` (default 365) | `day`, `tokens_to_cex`, `usd_to_cex` (null for now), `n_transfers`, `by_exchange` (JSON) |

To make a real Dune query id work against both, map it: `CEXFLOWS_QUERY_MAP=4242424=cex_inflows`.
A query that isn't implemented gets a 404 that lists what is, never empty rows.

Endpoints: `POST /api/v1/query/{id}/execute`, `GET /api/v1/execution/{id}/status`,
`GET /api/v1/execution/{id}/results`, `GET /api/v1/query/{id}/results`. Executions live in memory.

## Configuration

| variable | meaning |
| - | - |
| `ETHERSCAN_API_KEY` | Etherscan V2 key; else `~/.config/cexflows/etherscan_key` |
| `CEXFLOWS_API_KEY` | this server's key; else `~/.cexflows-key`, generated on first run |
| `CEXFLOWS_PORT` | port, default 8766 |
| `CEXFLOWS_HOSTS` | extra allowed `Host` values, comma-separated (loopback is always allowed) |
| `CEXFLOWS_QUERY_MAP` | `dune_id=query_name` pairs |
| `CEXFLOWS_LABELS` | a different hot-wallet CSV |

A free Etherscan key covers both chains: 5 calls a second and 1,000 logs a call. A busy hot
wallet's first year of history takes a few hundred calls; the cache makes reruns cheap.

## Labels

`labels/hot_wallets.csv` lists exchange hot wallets per chain (`chain,address,exchange,source`).
The seed set comes from [eth-labels](https://github.com/dawsbot/eth-labels) (MIT). Exchanges
reuse hot-wallet addresses across EVM chains, so a wallet named on one chain and active with the
token on another is included for that chain, with `source` saying where the name came from.

Coverage is the main limit. A missing exchange lowers the totals, so compare inflows against
their own baseline (a cliff week against normal weeks) rather than reading them as an absolute
figure.

## Limits

- EVM chains on Etherscan only, and only Ethereum and Arbitrum so far.
- Deposits are counted when swept, typically hours late; aggregate to weeks for timing work.
- No USD values yet.
- Not affiliated with Dune. It implements the shape of part of Dune's public API so the same
  client code can use either; it doesn't reproduce Dune's data.

## License

MIT
