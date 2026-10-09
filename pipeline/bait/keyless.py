"""Keyless snapshot: real hook usage with no API keys.

GeckoTerminal (free, ~30 calls/min) lists v4 pools per chain with volume, reserve,
unique traders and creation time, but not the hook. The hook is resolved on-chain
with PositionManager.poolKeys(bytes25) over public RPCs. Hooks are joined to the
hooklist registry and grouped with the same rules as the nightly pipeline.

Covers Robinhood chain and Ink, which have no public v4 subgraph.

Usage:
    python -m bait.keyless --out snapshot.json [--pages 10] [--ohlcv-cap 160]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .logsetup import configure
from .rules import flags_from_address, is_wash_24h, normalize_name, routable, score_trending, slugify, wallets_lower_bound

GT = "https://api.geckoterminal.com/api/v2"
HOOKLIST = "https://raw.githubusercontent.com/Uniswap/hooklist/main/hooklist.json"
POOL_KEYS = "0x86b6be7d"  # poolKeys(bytes25)
ZERO = "0x" + "0" * 40

# GeckoTerminal network -> chain id, PositionManager (developers.uniswap.org v4 deployments), public RPC.
CHAINS = {
    "eth":         (1,     "ethereum",  "0xbd216513d74c8cf14cf4747e6aaa6420ff64ee9e", "https://ethereum-rpc.publicnode.com"),
    "base":        (8453,  "base",      "0x7c5f5a4bbd8fd63184577525326123b519429bdc", "https://base-rpc.publicnode.com"),
    "unichain":    (130,   "unichain",  "0x4529a01c7a0410167c5740c487a8de60232617bf", "https://mainnet.unichain.org"),
    "robinhood":   (4663,  "robinhood", "0x58daec3116aae6d93017baaea7749052e8a04fa7", "https://rpc.mainnet.chain.robinhood.com"),
    "bsc":         (56,    "bnb",       "0x7a4a5c919ae2541aed11041a1aeee68f1287f95b", "https://bsc-rpc.publicnode.com"),
    "arbitrum":    (42161, "arbitrum",  "0xd88f38f930b7952f2db2432cb002e7abbf3dd869", "https://arbitrum-one-rpc.publicnode.com"),
    "avax":        (43114, "avalanche", "0xb74b1f14d2754acfcbbe1a221023a5cf50ab8acd", "https://avalanche-c-chain-rpc.publicnode.com"),
    "polygon_pos": (137,   "polygon",   "0x1ec2ebf4f37e7363fdfe3551602425af0b3ceef9", "https://polygon-bor-rpc.publicnode.com"),
    "ink":         (57073, "ink",       "0x1b35d13a2e2528f192637f14b05f0dc0e7deb566", "https://rpc-gel.inkonchain.com"),
    "optimism":    (10,    "optimism",  "0x3c3ea4b57a46241e54610e5f022e5c45859a1017", "https://optimism-rpc.publicnode.com"),
}

# PoolManager per chain (developers.uniswap.org v4 deployments) and a public RPC that serves narrow eth_getLogs.
# Used for pools never touched by PositionManager (hook-managed liquidity such as Bunni or EulerSwap): the hook is read
# from the pool's Initialize event, located by GeckoTerminal's pool_created_at -> block estimate -> narrow window.
LOGS = {
    "eth":         ("0x000000000004444c5dc75cB358380D2e3dE08A90", "https://eth.drpc.org", 4),
    "base":        ("0x498581ff718922c3f8e6a244956af099b2652b2b", "https://mainnet.base.org", 120),
    "unichain":    ("0x1f98400000000000000000000000000000000004", "https://mainnet.unichain.org", 200),
    "robinhood":   ("0x8366a39cc670b4001a1121b8f6a443a643e40951", "https://rpc.mainnet.chain.robinhood.com", 200),
    "bsc":         ("0x28e2ea090877bf75740558f6bfb36a5ffee9e9df", "https://bsc.drpc.org", 4),
    "arbitrum":    ("0x360e68faccca8ca495c1b759fd9eee466db9fb32", "https://arb1.arbitrum.io/rpc", 200),
    "avax":        ("0x06380c0e0912312b5150364b9dc4542ba0dbbc85", "https://api.avax.network/ext/bc/C/rpc", 100),
    "polygon_pos": ("0x67366782805870060151383f4bbff9dab53e5cd6", "https://polygon.drpc.org", 40),
    "ink":         ("0x360e68faccca8ca495c1b759fd9eee466db9fb32", "https://rpc-gel.inkonchain.com", 200),
    "optimism":    ("0x9a13f98cb987694c9f086b1f5eb990eea8264ec3", "https://mainnet.optimism.io", 120),
}
INITIALIZE = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
# Blockscout instances that answer a full-range topic lookup keylessly from a server (others bot-challenge).
BLOCKSCOUT_LOGS = {"eth": "https://eth.blockscout.com", "unichain": "https://unichain.blockscout.com",
                   "ink": "https://explorer.inkonchain.com"}

CATEGORY_KEYWORDS = [
    ("launchpad", ("launch", "memecoin", "bonding", "creator", "fair launch", "token launch")),
    ("orders", ("limit order", "twamm", "dca", "stop loss", "take profit", "order")),
    ("mev-lvr", ("mev", "lvr", "arbitrage", "auction", "sandwich")),
    ("lending", ("lend", "borrow", "vault", "euler", "collateral", "yield")),
    ("compliance", ("kyc", "compliance", "permission", "allowlist", "whitelist", "verified")),
    ("oracle", ("oracle", "twap", "price feed")),
    ("lp-management", ("rebalanc", "liquidity management", "concentrated", "range", "ldf", "liquidity density")),
    ("dynamic-fee", ("dynamic fee", "volatility", "surge fee", "fee adjust")),
]


_log = logging.getLogger("bait")


def log(msg: str) -> None:
    """Progress line. Through the "bait" logger, so Railway gets package 4's JSON line at level info (plain text on stderr
    was tagged as an error)."""
    _log.info("[keyless] %s", msg)


def warn(msg: str) -> None:
    _log.warning("[keyless] %s", msg)


class Http:
    def __init__(self, gt_spacing: float = 6.5, cache_dir: str | None = None):
        self.gt_spacing = gt_spacing
        self._last_gt = 0.0
        self.gt_calls = 0
        self.cache_dir = cache_dir
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

    def _cache_path(self, path: str) -> str | None:
        if not self.cache_dir:
            return None
        return os.path.join(self.cache_dir, hashlib.sha1(path.encode()).hexdigest() + ".json")

    def _open(self, url: str, data: bytes | None = None) -> object:
        req = urllib.request.Request(url, data=data, headers={
            "Accept": "application/json", "User-Agent": "bait/0.1", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.load(r)

    def gt(self, path: str) -> dict | None:
        cp = self._cache_path(path)
        if cp and os.path.exists(cp):
            with open(cp) as f:
                return json.load(f)
        res = self._gt_live(path)
        if cp and res is not None:
            with open(cp, "w") as f:
                json.dump(res, f)
        return res

    def _gt_live(self, path: str) -> dict | None:
        for attempt in range(6):
            wait = self.gt_spacing - (time.time() - self._last_gt)
            if wait > 0:
                time.sleep(wait)
            self._last_gt = time.time()
            self.gt_calls += 1
            try:
                return self._open(GT + path)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(20 * (attempt + 1))
                    continue
                if e.code == 404:
                    return None
                warn(f"GT {e.code} {path}")
                return None
            except Exception as e:  # network blip
                warn(f"GT error {e!r} {path}")
                time.sleep(5)
        return None

    def rpc_batch(self, url: str, calls: list[dict]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(calls), 10):  # some public RPCs (Unichain) reject batches over 10
            chunk = calls[i:i + 10]
            for attempt in range(2):
                try:
                    res = self._open(url, json.dumps(chunk).encode())
                    out.extend(res if isinstance(res, list) else [res])
                    break
                except urllib.error.HTTPError as e:
                    warn(f"RPC {url} HTTP {e.code}")
                    if e.code in (400, 403, 413):
                        break  # request-shape or plan limit: retrying will not help
                    time.sleep(2)
                except Exception as e:
                    warn(f"RPC {url} error {e!r}")
                    time.sleep(2)
        return out


def decode_pool_key(result: str | None) -> dict | None:
    if not result or len(result) < 2 + 64 * 5:
        return None
    w = [result[2 + 64 * k: 2 + 64 * (k + 1)] for k in range(5)]
    c0, c1 = "0x" + w[0][24:], "0x" + w[1][24:]
    if int(c1, 16) == 0:  # pool never touched by PositionManager
        return None
    return {"currency0": c0, "currency1": c1, "fee": int(w[2], 16), "tickSpacing": int(w[3], 16), "hooks": "0x" + w[4][24:]}


def categorize(text: str, flags: dict, props: dict) -> str:
    t = (text or "").lower()
    for cat, words in CATEGORY_KEYWORDS:
        if any(w in t for w in words):
            return cat
    if props.get("dynamicFee"):
        return "dynamic-fee"
    return "other"


def flag_labels(flags: dict) -> list[str]:
    names = {
        "beforeSwap": "beforeSwap", "afterSwap": "afterSwap", "beforeSwapReturnsDelta": "returnsDelta",
        "afterSwapReturnsDelta": "afterSwapDelta", "beforeAddLiquidity": "beforeAddLiquidity",
        "afterAddLiquidity": "afterAddLiquidity", "beforeRemoveLiquidity": "beforeRemoveLiquidity",
        "afterInitialize": "afterInit", "beforeInitialize": "beforeInit", "beforeDonate": "beforeDonate",
    }
    return [v for k, v in names.items() if flags.get(k)][:4]


def collect(http: Http, pages: int, new_pages: int = 2, nets: list[str] | None = None) -> dict:
    pools: dict[tuple[str, str], dict] = {}
    for net, (cid, cname, _, _) in CHAINS.items():
        if nets is not None and net not in nets:
            continue
        dexes = http.gt(f"/networks/{net}/dexes?page=1") or {}
        dex_ids = [d["id"] for d in dexes.get("data", []) if "uniswap" in d["id"] and "v4" in d["id"]]
        if not dex_ids:
            dexes2 = http.gt(f"/networks/{net}/dexes?page=2") or {}
            dex_ids = [d["id"] for d in dexes2.get("data", []) if "uniswap" in d["id"] and "v4" in d["id"]]
        if not dex_ids:
            log(f"{cname}: no uniswap v4 dex on GeckoTerminal, skipped")
            continue
        dex = dex_ids[0]
        before = len(pools)
        for sort in ("h24_volume_usd_desc", "h24_tx_count_desc"):
            for page in range(1, pages + 1):
                res = http.gt(f"/networks/{net}/dexes/{dex}/pools?page={page}&sort={sort}")
                rows = (res or {}).get("data", [])
                for p in rows:
                    a = p["attributes"]
                    pools[(net, a["address"].lower())] = {"net": net, "attrs": a}
                if len(rows) < 20:
                    break
        # newest pools across all dexes, keep v4 ones
        for page in range(1, new_pages + 1):
            res = http.gt(f"/networks/{net}/new_pools?page={page}")
            for p in (res or {}).get("data", []):
                dex_rel = (((p.get("relationships") or {}).get("dex") or {}).get("data") or {}).get("id", "")
                if dex_rel == dex:
                    a = p["attributes"]
                    pools[(net, a["address"].lower())] = {"net": net, "attrs": a}
        log(f"{cname}: {len(pools) - before} v4 pools from GeckoTerminal ({dex})")
    return pools


def resolve_hooks(http: Http, pools: dict) -> None:
    by_net: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for key in pools:
        by_net[key[0]].append(key)
    for net, keys in by_net.items():
        _, cname, pm, rpc = CHAINS[net]
        calls = [{"jsonrpc": "2.0", "id": i, "method": "eth_call",
                  "params": [{"to": pm, "data": POOL_KEYS + k[1][2:52].ljust(64, "0")}, "latest"]}
                 for i, k in enumerate(keys)]
        res = {r.get("id"): r.get("result") for r in http.rpc_batch(rpc, calls)}
        ok = hooked = 0
        for i, k in enumerate(keys):
            pk = decode_pool_key(res.get(i))
            pools[k]["key"] = pk
            if pk:
                ok += 1
                hooked += int(pk["hooks"], 16) != 0
        log(f"{cname}: resolved {ok}/{len(keys)} pool keys, {hooked} hooked")


def _rpc1(http: Http, url: str, method: str, params: list):
    """One call, paced; backs off when a public endpoint says its request limit is reached."""
    for attempt in range(4):
        time.sleep(0.25)
        res = http.rpc_batch(url, [{"jsonrpc": "2.0", "id": 1, "method": method, "params": params}])
        if res and "result" in res[0]:
            return res[0]["result"]
        err = (res[0].get("error") or {}) if res else {}
        if "limit" in str(err.get("message", "")).lower() and "range" not in str(err.get("message", "")).lower():
            time.sleep(8 * (attempt + 1))
            continue
        return None
    return None


def _block_at(http: Http, url: str, ts: int, model: dict) -> int | None:
    """Estimate the block at unix ts with a block-time model, then refine against real timestamps."""
    if "head" not in model:
        head = _rpc1(http, url, "eth_blockNumber", [])
        if head is None:
            return None
        head = int(head, 16)
        back = min(head - 1, 2_000_000)
        bh = _rpc1(http, url, "eth_getBlockByNumber", [hex(head), False])
        bp = _rpc1(http, url, "eth_getBlockByNumber", [hex(head - back), False])
        if not bh or not bp:
            return None
        th, tp = int(bh["timestamp"], 16), int(bp["timestamp"], 16)
        model.update(head=head, th=th, bt=max((th - tp) / back, 0.05))
    est = int(model["head"] - (model["th"] - ts) / model["bt"])
    for _ in range(4):
        est = max(1, min(est, model["head"]))
        b = _rpc1(http, url, "eth_getBlockByNumber", [hex(est), False])
        if not b:
            return est
        delta = int(b["timestamp"], 16) - ts
        if abs(delta) <= model["bt"] * 2:
            return est
        est = int(est - delta / model["bt"])
    return est


def resolve_via_logs(http: Http, pools: dict) -> None:
    """Pools PositionManager never saw: read hooks from the Initialize event near pool_created_at."""
    pending: dict[str, list] = defaultdict(list)
    for key, p in pools.items():
        if not p.get("key") and p["attrs"].get("pool_created_at") and key[0] in LOGS:
            pending[key[0]].append(key)
    budget = float(os.environ.get("KEYLESS_FALLBACK_BUDGET", "150"))  # seconds per chain; public endpoints throttle
    for net, keys in pending.items():
        pm, url, win = LOGS[net]
        bs = BLOCKSCOUT_LOGS.get(net)
        model: dict = {}
        found = 0
        # biggest pools first, so a budget cut drops the long tail, not the flagships
        keys = sorted(keys, key=lambda k: -float(pools[k]["attrs"]["volume_usd"]["h24"] or 0))
        started = time.time()
        for key in keys:
            if time.time() - started > budget:
                log(f"{CHAINS[net][1]}: Initialize-log fallback hit its {budget:.0f}s budget; rest skipped this run")
                break
            logs = None
            if bs:
                q = (f"{bs}/api?module=logs&action=getLogs&fromBlock=0&toBlock=latest&address={pm}"
                     f"&topic0={INITIALIZE}&topic1={key[1]}&topic0_1_opr=and")
                for attempt in range(4):
                    time.sleep(1.0)  # Blockscout free API throttles bursts
                    try:
                        res = http._open(q)
                        logs = res.get("result") if isinstance(res, dict) and isinstance(res.get("result"), list) else None
                        break
                    except urllib.error.HTTPError as e:
                        if e.code == 429:
                            time.sleep(4 * (attempt + 1))
                            continue
                        warn(f"Blockscout {bs} HTTP {e.code}")
                        break
                    except Exception as e:
                        warn(f"Blockscout {bs} error {e!r}")
                        break
            if not logs:
                ts = int(datetime.fromisoformat(pools[key]["attrs"]["pool_created_at"].replace("Z", "+00:00")).timestamp())
                blk = _block_at(http, url, ts, model)
                if blk is None:
                    continue
                for w in (win, win * 3):
                    logs = _rpc1(http, url, "eth_getLogs", [{"address": pm, "topics": [INITIALIZE, key[1]],
                                                              "fromBlock": hex(max(0, blk - w)), "toBlock": hex(blk + w)}])
                    if logs:
                        break
            if not logs:
                continue
            lg = logs[0]
            d = lg["data"][2:]
            words = [d[i * 64:(i + 1) * 64] for i in range(5)]
            pools[key]["key"] = {"currency0": "0x" + lg["topics"][2][26:], "currency1": "0x" + lg["topics"][3][26:],
                                 "fee": int(words[0], 16), "tickSpacing": int(words[1], 16) if int(words[1], 16) < 2**23 else int(words[1], 16) - 2**256,
                                 "hooks": "0x" + words[2][24:], "via": "initialize-log"}
            found += 1
        log(f"{CHAINS[net][1]}: Initialize-log fallback resolved {found}/{len(keys)} remaining pools")


def code_hashes(http: Http, addrs_by_net: dict[str, set[str]]) -> dict[tuple[str, str], str]:
    """sha1 of deployed bytecode per (net, address); clones of one implementation share it."""
    out: dict[tuple[str, str], str] = {}
    for net, addrs in addrs_by_net.items():
        addrs = sorted(addrs)
        rpc = CHAINS[net][3]
        calls = [{"jsonrpc": "2.0", "id": i, "method": "eth_getCode", "params": [a, "latest"]} for i, a in enumerate(addrs)]
        res = {r.get("id"): r.get("result") for r in http.rpc_batch(rpc, calls)}
        for i, a in enumerate(addrs):
            code = res.get(i)
            if code and code != "0x":
                out[(net, a)] = hashlib.sha1(code.encode()).hexdigest()
        log(f"{CHAINS[net][1]}: bytecode for {sum(1 for a in addrs if (net, a) in out)}/{len(addrs)} hooks")
    return out


def fetch_ohlcv(http: Http, hooked: list[dict], cap: int) -> None:
    ranked = sorted(hooked, key=lambda p: -float(p["attrs"]["volume_usd"]["h24"] or 0))
    if len(ranked) > cap:
        log(f"OHLCV capped at {cap} of {len(ranked)} hooked pools; the rest carry 24h volume only")
    for p in ranked[:cap]:
        res = http.gt(f"/networks/{p['net']}/pools/{p['attrs']['address']}/ohlcv/day?aggregate=1&limit=15&currency=usd")
        lst = (((res or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        p["daily"] = sorted([(int(r[0]), float(r[5] or 0)) for r in lst])


def fetch_trades(http: Http, net: str, pool_id: str) -> list[dict] | None:
    """Recent trades for one pool (GeckoTerminal returns up to 300 from the last 24h), as attribute dicts with
    tx_from_address, kind (buy|sell), volume_in_usd, block_timestamp, tx_hash. None when the call failed."""
    res = http.gt(f"/networks/{net}/pools/{pool_id}/trades")
    if res is None:
        return None
    return [t.get("attributes") or {} for t in (res.get("data") or []) if isinstance(t, dict)]


def build(pools: dict, hooklist: list[dict], now: datetime, codes: dict | None = None) -> dict:
    reg = {}
    for h in hooklist:
        hk = h["hook"]
        reg[(hk["address"].lower(), int(hk["chainId"]))] = h

    projects: dict[str, dict] = {}
    unresolved = sum(1 for p in pools.values() if not p.get("key"))
    codes = codes or {}
    # bytecode -> registry project norm, so a clone of a registered hook joins that project
    code_to_norm: dict[str, str] = {}
    for (net, addr), h in codes.items():
        e = reg.get((addr, CHAINS[net][0]))
        if e:
            code_to_norm.setdefault(h, normalize_name(e["hook"]["name"]))
    for p in pools.values():
        pk = p.get("key")
        if not pk or int(pk["hooks"], 16) == 0:
            continue
        cid, cname, _, _ = CHAINS[p["net"]]
        hook_addr = pk["hooks"].lower()
        entry = reg.get((hook_addr, cid))
        name = (entry or {}).get("hook", {}).get("name") or f"Unlisted hook {hook_addr[:6]}…{hook_addr[-4:]}"
        norm = normalize_name(name) if entry else hook_addr
        if not entry:
            ch = codes.get((p["net"], hook_addr))
            if ch and ch in code_to_norm:
                norm = code_to_norm[ch]          # clone of a registered hook
            elif ch:
                norm = "code:" + ch              # unlisted family: same bytecode, one card
        proj = projects.setdefault(norm, {"name": name, "norm": norm, "hooks": {}, "pools": [], "inRegistry": bool(entry)})
        if entry and not proj["inRegistry"]:
            proj.update(name=name, inRegistry=True)
        proj["hooks"][(hook_addr, cid)] = entry or {"hook": {"address": hook_addr, "chainId": cid, "chain": cname,
                                                             "name": name, "description": "", "auditUrl": ""},
                                                    "flags": flags_from_address(hook_addr), "properties": {}}
        a = p["attrs"]
        tx = a.get("transactions", {}).get("h24", {})
        daily = p.get("daily")
        vol7 = prev7 = None
        if daily:
            vols = [v for _, v in daily]
            vol7 = sum(vols[-7:])
            prev7 = sum(vols[-14:-7])
        proj["pools"].append({
            "poolId": a["address"], "chain": cname, "chainId": cid, "name": a.get("name", ""),
            "vol24h": float(a["volume_usd"]["h24"] or 0), "vol7d": vol7, "vol7dPrev": prev7,
            "tvl": float(a.get("reserve_in_usd") or 0),
            "txs24h": int(tx.get("buys", 0)) + int(tx.get("sells", 0)),
            "traders24h": int(tx.get("buyers", 0)) + int(tx.get("sellers", 0)),
            "walletsMin24h": wallets_lower_bound(None, int(tx.get("buyers", 0)), int(tx.get("sellers", 0)), None),
            "createdAt": a.get("pool_created_at"),
            "dynamicFee": bool(pk["fee"] & 0x800000), "feeBps": None if pk["fee"] & 0x800000 else pk["fee"] / 100,
            "token0": pk["currency0"], "token1": pk["currency1"], "hook": hook_addr,
            "daily": [{"t": t, "v": round(v, 2)} for t, v in (daily or [])],
        })

    for norm, pr in projects.items():
        if not pr["inRegistry"] and len(pr["hooks"]) > 1:
            first = sorted(a for a, _ in pr["hooks"])[0]
            pr["name"] = f"Unlisted hook family {first[:6]}…"
    out = []
    for norm, pr in projects.items():
        pools_ = sorted(pr["pools"], key=lambda x: -x["vol24h"])
        hooks = list(pr["hooks"].values())
        primary_addr = pools_[0]["hook"]
        primary = next((h for h in hooks if h["hook"]["address"].lower() == primary_addr), hooks[0])
        desc = primary["hook"].get("description") or ""
        vol24 = sum(x["vol24h"] for x in pools_)
        vol7 = sum((x["vol7d"] if x["vol7d"] is not None else x["vol24h"] * 7) for x in pools_)
        prev7 = sum((x["vol7dPrev"] or 0) for x in pools_ if x["vol7d"] is not None)
        has_series = any(x["vol7d"] is not None for x in pools_)
        tvl = sum(x["tvl"] for x in pools_)
        traders = sum(x["traders24h"] for x in pools_)
        washed = sum(x["vol24h"] for x in pools_ if is_wash_24h(x["vol24h"], x["walletsMin24h"]))
        wash = vol24 > 0 and washed / vol24 >= 0.5
        created = [datetime.fromisoformat(x["createdAt"].replace("Z", "+00:00")) for x in pools_ if x.get("createdAt")]
        first_seen = min(created) if created else None
        young = bool(first_seen and first_seen >= now - timedelta(days=7))
        is_new = young and (tvl > 1_000 or sum(x["txs24h"] for x in pools_) >= 10)
        # Trending is week-over-week growth, so it needs a prior week; launches under 7 days old belong in New.
        score = 0.0 if (wash or not has_series or young) else score_trending(vol7, prev7)
        chains = sorted({x["chain"] for x in pools_})
        days: dict[int, float] = defaultdict(float)
        for x in pools_:
            for d in x["daily"]:
                days[d["t"]] += d["v"]
        out.append({
            "slug": slugify(pr["name"]) if pr["inRegistry"] else ("unlisted-" + norm.replace("code:", "")[:10].removeprefix("0x")),
            "name": pr["name"], "inRegistry": pr["inRegistry"],
            "category": categorize(desc + " " + pr["name"], primary.get("flags", {}), primary.get("properties", {})),
            "whatLine": desc, "whyBlurb": "",
            "chains": chains,
            "flags": flag_labels(primary.get("flags", {})),
            "routable": all(routable(h) for h in hooks) if pr["inRegistry"] else None,
            "audited": any(h["hook"].get("auditUrl") for h in hooks),
            "verified": any(h["hook"].get("verifiedSource") for h in hooks),
            "upgradeable": any((h.get("properties") or {}).get("upgradeable") for h in hooks),
            "hooksN": len(hooks), "poolsN": len(pools_),
            "metrics": {"vol24h": round(vol24, 2), "vol7d": round(vol7, 2), "vol7dPrev": round(prev7, 2),
                        "tvl": round(tvl, 2), "txs24h": sum(x["txs24h"] for x in pools_), "traders24h": traders,
                        "hasSeries": has_series},
            "series": [{"t": t, "v": round(v, 2)} for t, v in sorted(days.items())],
            "wash": wash, "isNew": is_new, "firstSeen": first_seen.isoformat() if first_seen else None,
            "scoreTrending": round(score, 4),
            "hooks": [{"address": h["hook"]["address"], "chain": h["hook"].get("chain") or "", "name": h["hook"].get("name", ""),
                       "auditUrl": h["hook"].get("auditUrl", ""), "flags": flag_labels(h.get("flags", {}))} for h in hooks],
            "pools": [{k: v for k, v in x.items() if k != "daily"} for x in pools_[:12]],
            "receipts": [],
        })
    # unique slugs
    seen: dict[str, int] = {}
    for p in out:
        n = seen.get(p["slug"], 0)
        seen[p["slug"]] = n + 1
        if n:
            p["slug"] = f"{p['slug']}-{n + 1}"
    out.sort(key=lambda p: -p["metrics"]["vol7d"])
    return {"projects": out, "unresolvedPools": unresolved}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--pages", type=int, default=10)
    ap.add_argument("--ohlcv-cap", type=int, default=160)
    ap.add_argument("--new-pages", type=int, default=2)
    ap.add_argument("--hooklist-file")
    ap.add_argument("--cache-dir", help="cache GeckoTerminal responses here; reruns are instant")
    ap.add_argument("--spacing", type=float, default=6.5, help="seconds between GeckoTerminal calls")
    args = ap.parse_args(argv)
    configure()
    now = datetime.now(timezone.utc)
    http = Http(gt_spacing=args.spacing, cache_dir=args.cache_dir)
    if args.hooklist_file:
        hooklist = json.load(open(args.hooklist_file))
    else:
        hooklist = http._open(HOOKLIST)
    log(f"hooklist: {len(hooklist)} hooks")
    pools = collect(http, args.pages, args.new_pages)
    resolve_hooks(http, pools)
    resolve_via_logs(http, pools)
    hooked = [p for p in pools.values() if p.get("key") and int(p["key"]["hooks"], 16) != 0]
    fetch_ohlcv(http, hooked, args.ohlcv_cap)
    by_net: dict[str, set[str]] = defaultdict(set)
    for p in hooked:
        by_net[p["net"]].add(p["key"]["hooks"].lower())
    codes = code_hashes(http, by_net)
    snap = build(pools, hooklist, now, codes)
    snap.update({
        "generatedAt": now.isoformat(),
        "source": "GeckoTerminal pools + PositionManager.poolKeys over public RPC + Uniswap hooklist",
        "registryHooks": len(hooklist),
        "poolsScanned": len(pools),
        "hookedPools": len(hooked),
        "gtCalls": http.gt_calls,
    })
    with open(args.out, "w") as f:
        json.dump(snap, f, separators=(",", ":"))
    log(f"wrote {args.out}: {len(snap['projects'])} projects from {len(hooked)} hooked pools of {len(pools)} scanned; "
        f"{snap['unresolvedPools']} pools unresolved (never touched by PositionManager)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
