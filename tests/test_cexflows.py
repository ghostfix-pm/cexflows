import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cexflows import etherscan, server
from cexflows.cache import Cache
from cexflows.flows import aggregate_daily

DAY = 86400
T0 = 1_767_225_600  # 2026-01-01 00:00 UTC
HOT = {"0x" + "a" * 40: "Alpha", "0x" + "b" * 40: "Beta"}


def tr(ts, sender, wallet, value):
    return {"ts": ts, "from": sender, "wallet": wallet, "value": value}


def test_aggregate_skips_internal_transfers_and_fills_quiet_days():
    a, b = list(HOT)
    rows = aggregate_daily(
        [tr(T0 + 10, "0x" + "1" * 40, a, 5 * 10**18),
         tr(T0 + 20, "0x" + "2" * 40, b, 2 * 10**18),
         tr(T0 + 30, a, b, 100 * 10**18),          # hot wallet to hot wallet: internal
         tr(T0 + 2 * DAY + 5, "0x" + "1" * 40, a, 10**18)],
        HOT, 18, "2026-01-01", "2026-01-03")
    assert [r["day"] for r in rows] == ["2026-01-01", "2026-01-02", "2026-01-03"]
    assert rows[0]["tokens_to_cex"] == 7 and rows[0]["n_transfers"] == 2
    assert rows[0]["by_exchange"] == {"Alpha": 5, "Beta": 2}
    assert rows[1]["tokens_to_cex"] == 0 and rows[1]["n_transfers"] == 0
    assert rows[2]["tokens_to_cex"] == 1


def test_cache_fetches_only_missing_blocks():
    cache = Cache(":memory:")
    calls = []

    def fetch(chain, token, wallet, a, b):
        calls.append((a, b))
        return [{"block": n, "ts": T0 + n, "tx": f"0x{n:064x}", "log_index": 0, "from": "0x" + "1" * 40, "value": n}
                for n in range(a, b + 1, 10)]

    w = "0x" + "a" * 40
    cache.ensure("arbitrum", "0xt", w, 100, 200, fetch)
    cache.ensure("arbitrum", "0xt", w, 150, 180, fetch)   # inside coverage: no call
    cache.ensure("arbitrum", "0xt", w, 50, 250, fetch)    # both sides
    assert calls == [(100, 200), (50, 99), (201, 250)]
    assert cache.coverage("arbitrum", "0xt", w) == (50, 250)
    got = cache.transfers("arbitrum", "0xt", w, T0, T0 + 1000)
    expected = list(range(50, 100, 10)) + list(range(100, 201, 10)) + list(range(201, 251, 10))
    assert [t["block"] for t in got] == expected and len({t["tx"] for t in got}) == len(got)


def test_inbound_transfers_rereads_last_block_without_duplicates(monkeypatch):
    def log(block, idx):
        return {"blockNumber": hex(block), "timeStamp": hex(T0 + block), "transactionHash": f"0x{block:062x}{idx:02x}",
                "logIndex": hex(idx), "topics": ["0x", "0x" + "0" * 24 + "1" * 40, "0x"], "data": hex(1)}

    monkeypatch.setattr(etherscan, "PAGE", 3)
    # Pages keyed by fromBlock: each full page ends mid-block, so the next one re-reads it.
    pages = {1: [log(1, 0), log(2, 0), log(2, 1)],
             2: [log(2, 0), log(2, 1), log(3, 0)],
             3: [log(3, 0), log(4, 0)]}
    monkeypatch.setattr(etherscan, "call", lambda chain, **p: pages.get(p["fromBlock"], []))
    got = list(etherscan.inbound_transfers("arbitrum", "0xt", "0x" + "a" * 40, 1, 10))
    assert [(t["block"], t["log_index"]) for t in got] == [(1, 0), (2, 0), (2, 1), (3, 0), (4, 0)]


def _serve(runner):
    key = "test-key"
    qmap = {1: "cex_inflows", 4242424: "cex_inflows"}
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(
        key, server.allowed_hosts(0) | {"127.0.0.1:%d" % 0}, server.Executions(runner), qmap))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = server.make_handler(key, server.allowed_hosts(port), server.Executions(runner), qmap)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port, key


def _req(port, method, path, key, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-Dune-API-Key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_server_speaks_dunes_execute_status_results():
    seen = []

    def runner(name, params):
        seen.append((name, params))
        return [{"day": "2026-01-01", "tokens_to_cex": 7.0, "usd_to_cex": None, "n_transfers": 2, "by_exchange": "{}"}]

    httpd, port, key = _serve(runner)
    try:
        assert _req(port, "POST", "/api/v1/query/1/execute", "wrong", {})[0] == 401
        code, body = _req(port, "POST", "/api/v1/query/99/execute", key, {"query_parameters": {}})
        assert code == 404 and "not implemented" in body["error"]
        code, body = _req(port, "POST", "/api/v1/query/4242424/execute", key,
                          {"query_parameters": {"chain": "arbitrum", "days": 30}})
        assert code == 200 and body["execution_id"]
        eid = body["execution_id"]
        for _ in range(50):
            st = _req(port, "GET", f"/api/v1/execution/{eid}/status", key)[1]
            if st["is_execution_finished"]:
                break
            time.sleep(0.05)
        assert st["state"] == "QUERY_STATE_COMPLETED"
        res = _req(port, "GET", f"/api/v1/execution/{eid}/results", key)[1]
        assert res["result"]["rows"][0]["tokens_to_cex"] == 7.0
        assert res["result"]["metadata"]["column_names"][0] == "day"
        assert _req(port, "GET", "/api/v1/query/4242424/results", key)[1]["execution_id"] == eid
        assert seen == [("cex_inflows", {"chain": "arbitrum", "days": 30})]
    finally:
        httpd.shutdown()


def test_server_rejects_foreign_host():
    httpd, port, key = _serve(lambda n, p: [])
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1/query/1/results",
                                     headers={"X-Dune-API-Key": key, "Host": "evil.example"})
        try:
            urllib.request.urlopen(req, timeout=10)
            assert False
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        httpd.shutdown()


def test_inbound_transfers_refuses_a_block_with_more_than_a_page(monkeypatch):
    def log(idx):
        return {"blockNumber": hex(5), "timeStamp": hex(T0), "transactionHash": f"0x{idx:064x}",
                "logIndex": hex(idx), "topics": ["0x", "0x" + "0" * 24 + "1" * 40, "0x"], "data": hex(1)}

    monkeypatch.setattr(etherscan, "PAGE", 2)
    monkeypatch.setattr(etherscan, "call", lambda chain, **p: [log(0), log(1)])
    try:
        list(etherscan.inbound_transfers("arbitrum", "0xt", "0x" + "a" * 40, 5, 10))
        assert False, "should refuse rather than loop"
    except etherscan.EtherscanError as e:
        assert "block 5" in str(e)
