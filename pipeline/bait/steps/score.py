"""Step 6 (score): project_daily(day) from closed UTC days.

One window convention (DESIGN.md "Windows"). The run on `day` reports the UTC days that are over: window_end = day - 1;
"24h" = the day window_end; "7d" = window_end-6 .. window_end; "prev 7d" = window_end-13 .. window_end-7. Windows sum the
per-day columns of pool_snapshots (volume_usd_day, tx_count_day); the row for `day` itself is still running: its volume is
never read, but it carries the pool's TVL "now", so a pool created this morning counts.
- Volume: a day without a row adds $0 (the pool was not listed, or did not trade). A pool whose cumulative volume at
  window_end is NULL (the onchain step could not measure it) is left out of every volume window, not read as $0; when
  that is every pool of a project, its vol_24h / vol_7d / vol_7d_prev are NULL.
- Swaps: a pool's count is known when every day of the window is (days before the pool was created count as 0; a pool
  with no volume in the window adds 0). Otherwise swaps_7d / swaps_24h are NULL, never a 7-day number built from one day.
- A capped hook's aggregate pool (pool id 'hook:<address>', swaps of pools the indexer does not store) adds to the project's
  volume and swaps and to whether its volume is known, never to pools_n or score_trending: its per-pool concentration is
  lost in the aggregate, so no wash rule can judge it.
- pools_n: per hook, its pools with a snapshot in the window, or hooks.pools_total (every pool initialized through it, from
  the indexer) when larger.
Wash (rules.WASH_REASONS) is its own flag: listed in All with a badge, never ranked; is_spam is only rules.is_spam.
Reads stored rows only, so `python -m bait run --steps score --day <d>` writes the same rows however often it runs.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import sqlalchemy as sa

from ..db import (HOOK_POOL_PREFIX, as_utc, hooks, indexer_state, pool_daily_onchain, pool_snapshots, pool_stats, pool_top_wallets, pool_wallet_day, pools,
                  project_daily, projects, upsert)
from ..rules import (WASH_CONCENTRATED_WALLETS, WASH_MAX_WALLETS_7D, WASH_MIN_VOL_7D, WASH_REASONS, WASH_SHARE, corroborated, custom_accounting,
                     is_new, is_spam, is_symmetric, is_wash_7d, is_wash_7d_by_swaps, is_wash_24h, is_wash_concentrated,
                     is_wash_no_liquidity, is_wash_vol_tvl, score_trending, wallets_lower_bound)
from . import RunContext, log
from .onchain import ready_chains

_D = timedelta(days=1)


def window_end(day: date) -> date:
    """The last closed UTC day the run on `day` reports."""
    return day - _D


def run(conn: sa.Connection, ctx: RunContext) -> dict:
    day = ctx.day
    end = window_end(day)
    w24, w7, wp = (end, end), (end - 6 * _D, end), (end - 13 * _D, end - 7 * _D)
    hook_rows = conn.execute(sa.select(hooks.c.chain_id, hooks.c.address, hooks.c.project_id, hooks.c.pools_total)
                             .where(hooks.c.project_id.is_not(None))).all()
    hook_to_project = {(r.chain_id, r.address): r.project_id for r in hook_rows}
    pool_project, custom = {}, set()   # custom: pools of custom-accounting hooks (liquidity lives in the hook)
    pool_hook: dict[tuple, tuple] = {}
    for p in conn.execute(sa.select(pools.c.pool_id, pools.c.chain_id, pools.c.hook_address, pools.c.created_at)):
        pid = hook_to_project.get((p.chain_id, p.hook_address))
        if pid:
            pool_project[(p.pool_id, p.chain_id)] = (pid, as_utc(p.created_at).date() if p.created_at else None)
            pool_hook[(p.pool_id, p.chain_id)] = (p.chain_id, p.hook_address)
            if _custom(p.hook_address):
                custom.add((p.pool_id, p.chain_id))
    by_pool: dict[tuple, dict] = {}
    # rows up to and including the running day: a pool's existence and TVL are "now"; volume windows still end at `end`
    for s in conn.execute(sa.select(pool_snapshots).where(pool_snapshots.c.day >= wp[0], pool_snapshots.c.day <= day)):
        by_pool.setdefault((s.pool_id, s.chain_id), {})[s.day] = s

    agg: dict[int, dict] = {}
    active: dict[tuple, int] = {}     # (chain, hook) -> its pools with a snapshot in the window
    unknown_vol: set[tuple] = set()   # cumulative volume NULL at window_end (onchain held it back): not $0
    pool_tvl: dict[tuple, float | None] = {}
    pool_vol7: dict[tuple, float] = {}
    pool_swaps7: dict[tuple, int] = {}     # swaps on the days that carry the 7-day volume: an upper bound of its wallets
    pool_vol_days: dict[tuple, list[date]] = {}   # the days that carry it
    for key, rows in by_pool.items():
        if key not in pool_project:
            continue
        pid, created = pool_project[key]
        a = agg.setdefault(pid, _empty())
        now = rows[max(rows)]          # latest row up to the running day: existence and TVL (a pool born this morning counts)
        closed = [d for d in rows if d <= end]
        cur = rows[max(closed)] if closed else now   # latest closed row: the volume-unknown marker and lifetime volume
        folded = key[0].startswith(HOOK_POOL_PREFIX)
        a["usage_rows"] += 1
        if not folded:                                 # a hook's aggregate pool adds usage, never a pool
            a["pools"] += 1
            active[pool_hook[key]] = active.get(pool_hook[key], 0) + 1
        a["tvl"] += now.tvl_usd or 0.0
        a["max_tvl"] = max(a["max_tvl"], now.tvl_usd or 0.0)
        a["stats"].append((now.tvl_usd or 0.0, max(now.tx_count_cum or 0, cur.tx_count_cum or 0)))
        if cur.volume_usd_cum is not None:
            a["life"] += cur.volume_usd_cum   # legacy cumulative: a lower bound of lifetime volume, enough for the spam exemption
        pool_tvl[key] = now.tvl_usd
        quiet: dict[str, bool] = {}
        if cur.volume_usd_cum is None:
            unknown_vol.add(key)
        else:
            a["vol_known"] = True
            for name, w in (("vol24", w24), ("vol7", w7), ("vol7p", wp)):
                v = _vol(rows, w)
                a[name] += v
                if folded:
                    a["fold_" + name] += v
                if name == "vol7":
                    pool_vol7[key] = v
                quiet[name] = v == 0
        pool_vol_days[key] = [d for d, r in rows.items() if w7[0] <= d <= w7[1] and (r.volume_usd_day or 0) > 0]
        sb = _swap_bound(rows, w7)
        if sb is not None:
            pool_swaps7[key] = sb
        for name, w, vol_name in (("tx24", w24, "vol24"), ("tx7", w7, "vol7")):
            n, known = _swaps(rows, w, created)
            a[name] += n
            if not known and not quiet.get(vol_name, False):
                a[name + "_known"] = False

    # pools_n: per hook, its pools with a snapshot, or every pool initialized through it (hooks.pools_total) when larger, so a
    # launchpad past the indexer's per-hook cap is not reported as its few traded pools
    extra: dict[int, int] = {}
    for r in hook_rows:
        more = (r.pools_total or 0) - active.get((r.chain_id, r.address), 0)
        if more > 0:
            extra[r.project_id] = extra.get(r.project_id, 0) + more

    ready = _ready(conn, ctx, end)
    reasons, wallet_known = _wash(conn, day, end, pool_project, ready, unknown_vol, pool_vol7, pool_tvl, pool_swaps7, custom,
                                  pool_vol_days)
    wallets = _wallets(conn, day, end, ready, pool_project)

    first_seen = {r.id: as_utc(r.first_seen_at) for r in conn.execute(sa.select(projects.c.id, projects.c.first_seen_at))}
    # A project's age is its earliest pool when known; on a first run every project was "first seen" today.
    for (pid, created) in pool_project.values():
        if created is not None and pid in first_seen:
            c = datetime.combine(created, datetime.min.time(), tzinfo=timezone.utc)
            if c < first_seen[pid]:
                first_seen[pid] = c
    rows = []
    for pid, fs in first_seen.items():
        a = agg.get(pid) or _empty()
        spam = is_spam(a["stats"], max(a["life"], a["vol7"]))
        if not spam and is_spam(a["stats"]) and pid not in reasons and pid not in wallet_known:
            # escaped the spam rule only by moving >= $1M, and nothing can check who moved it: listed, never ranked
            reasons[pid] = ["unverified"]
        wash = pid in reasons
        # every pool's volume unknown (p5 held it back): the project's volume is unknown, never $0
        known = a["vol_known"] or not a["usage_rows"]
        young = fs > datetime.combine(day - 7 * _D, datetime.min.time(), tzinfo=timezone.utc)
        rows.append({
            "project_id": pid, "day": day, "window_end": end, "tvl_usd": a["tvl"], "pools_n": a["pools"] + extra.get(pid, 0),
            "vol_24h": a["vol24"] if known else None, "vol_7d": a["vol7"] if known else None,
            "vol_7d_prev": a["vol7p"] if known else None,
            "swaps_24h": a["tx24"] if a["tx24_known"] else None, "swaps_7d": a["tx7"] if a["tx7_known"] else None,
            "wallets_24h": wallets.get(pid, (None, None))[0], "wallets_exact": wallets.get(pid, (None, None))[1],
            # folded usage (a capped hook's aggregate pool) is displayed, never ranked: no wash rule can judge it
            "score_trending": 0.0 if (wash or young) else score_trending(a["vol7"] - a["fold_vol7"], a["vol7p"] - a["fold_vol7p"]),
            # the known days are a lower bound of the 7-day count, so ">= 10 of them" still proves "swaps_7d >= 10"
            "is_new": not wash and is_new(fs, day, a["max_tvl"], a["tx7"]),
            "is_spam": spam,           # true spam only: no pools, or tiny, idle and < $1M
            "is_wash": wash, "wash_reason": ",".join(reasons.get(pid, [])) or None,
        })
    for i in range(0, len(rows), 500):
        upsert(conn, project_daily, rows[i:i + 500], keys=["project_id", "day"])
    trending = sum(1 for r in rows if r["score_trending"] > 0)
    counts = {f"wash_{r}": sum(r in rs for rs in reasons.values()) for r in WASH_REASONS}
    if reasons:
        log.info("score: %d wash-like projects (%s)", len(reasons), " ".join(f"{k}={v}" for k, v in counts.items()))
    return {"projects": len(rows), "with_pools": len(agg), "new": sum(r["is_new"] for r in rows),
            "trending": trending, "spam": sum(r["is_spam"] for r in rows), "wash": len(reasons), **counts}


def _ready(conn, ctx: RunContext, end: date) -> list[int]:
    """Chains whose on-chain days through `end` are complete. The nightly run (day = today) uses onchain.ready_chains,
    freshness included. A re-score of a past day (package 9) must not depend on the indexer's freshness at re-score time:
    backfilled and the swap cursor's block time past the end of `end` is enough."""
    if ctx.day >= ctx.now.astimezone(timezone.utc).date():
        return ready_chains(conn, ctx)
    boundary = datetime.combine(end + _D, datetime.min.time(), tzinfo=timezone.utc)
    return [st.chain_id for st in conn.execute(sa.select(indexer_state).order_by(indexer_state.c.chain_id))
            if (not ctx.chains or st.chain_id in ctx.chains) and st.backfilled
            and st.swap_block_time is not None and as_utc(st.swap_block_time) >= boundary]


def _custom(hook: str | None) -> bool:
    """rules.custom_accounting, False for an address that is not hex (never a real hook)."""
    try:
        return bool(hook) and custom_accounting(hook)
    except ValueError:
        return False


def _empty() -> dict:
    return {"tvl": 0.0, "max_tvl": 0.0, "pools": 0, "vol24": 0.0, "vol7": 0.0, "vol7p": 0.0, "tx24": 0, "tx7": 0,
            "tx24_known": True, "tx7_known": True, "stats": [], "life": 0.0,
            "vol_known": False, "usage_rows": 0, "fold_vol24": 0.0, "fold_vol7": 0.0, "fold_vol7p": 0.0}


def _vol(rows: dict, w: tuple[date, date]) -> float:
    """Sum of volume_usd_day over the window; a missing day or a NULL value adds $0."""
    return sum((r.volume_usd_day or 0.0) for d, r in rows.items() if w[0] <= d <= w[1])


def _swaps(rows: dict, w: tuple[date, date], created: date | None) -> tuple[int, bool]:
    """Sum of the known tx_count_day over the window, and whether every day is known (a day before the pool was created
    counts as 0, known)."""
    total, known, d = 0, True, w[0]
    while d <= w[1]:
        r = rows.get(d)
        if r is not None and r.tx_count_day is not None:
            total += r.tx_count_day
        elif created is None or d >= created:
            known = False
        d += _D
    return total, known


def _swap_bound(rows: dict, w: tuple[date, date]) -> int | None:
    """Swaps on the window's days that carry volume: at least one wallet per swap is impossible to beat, so this is an
    upper bound of the wallets behind that volume. None unless every such day's count is known."""
    days = [r for d, r in rows.items() if w[0] <= d <= w[1] and (r.volume_usd_day or 0) > 0]
    if not days or any(r.tx_count_day is None for r in days):
        return None
    return sum(r.tx_count_day for r in days)


def _wallets(conn, day: date, end: date, ready: list[int], pool_project: dict) -> dict[int, tuple[int, bool]]:
    """Wallets per project for the 24h figure, and whether the number is a distinct count across the project's pools.
    Ready chains: distinct addresses on `end` from pool_wallet_day, one union over the project's pools. Other pools:
    pool_stats(day).wallets_24h (distinct within the pool, rolling 24 h), else traders_24h (buyers + sellers). Adding
    pools, or buyers to sellers, can count a wallet twice: then the number is an upper bound (exact False)."""
    parts: dict[int, list[tuple[int, bool]]] = {}
    if ready:
        q = (sa.select(hooks.c.project_id, sa.func.count(sa.distinct(pool_wallet_day.c.address)).label("n"))
             .select_from(pool_wallet_day
                          .join(pools, (pools.c.pool_id == pool_wallet_day.c.pool_id) & (pools.c.chain_id == pool_wallet_day.c.chain_id))
                          .join(hooks, (hooks.c.address == pools.c.hook_address) & (hooks.c.chain_id == pools.c.chain_id)))
             .where(pool_wallet_day.c.day == end, pool_wallet_day.c.chain_id.in_(ready), hooks.c.project_id.is_not(None))
             .group_by(hooks.c.project_id))
        for r in conn.execute(q):
            parts[r.project_id] = [(r.n, True)]
    for r in conn.execute(sa.select(pool_stats).where(pool_stats.c.day == day)):
        key = (r.pool_id, r.chain_id)
        if key not in pool_project or r.chain_id in ready:
            continue
        n, distinct = (r.wallets_24h, True) if r.wallets_24h is not None else (r.traders_24h, False)
        if n:
            parts.setdefault(pool_project[key][0], []).append((n, distinct))
    return {pid: (sum(n for n, _ in ps), len(ps) == 1 and ps[0][1]) for pid, ps in parts.items()}


def _wash(conn, day: date, end: date, pool_project: dict, ready: list[int], unknown: set[tuple], vol7: dict,
          tvl: dict, swaps7: dict, custom: set[tuple], vol_days: dict) -> tuple[dict[int, list[str]], set[int]]:
    """Wash reasons per project (DESIGN.md "Wash rules"). Each rule judges pools; a project gets the reason when the
    pools it flags carry >= WASH_SHARE of the project's volume in that rule's window, leaving out a capped hook's aggregate
    pool ('hook:<address>', folded swaps no rule can judge) on both sides of the share. Pools with unknown volume are never
    judged. Wallet thresholds compare a lower bound (rules.wallets_lower_bound), never buyers + sellers. Every few-wallet
    rule also needs rules.corroborated (volume far above a known TVL, no liquidity, or symmetric traders).
    vol7 / tvl: the pool's closed 7-day volume and latest TVL from the snapshots. swaps7: the swaps behind that volume, an
    upper bound of its wallets when exact (ready chains only), used when no wallet count is known (8 swaps prove < 50)
    and never against a known wallet lower bound above the rule's threshold.
    Returns the reasons and the projects with any wallet data (for the "unverified" fallback in run)."""
    stats = {(r.pool_id, r.chain_id): r for r in conn.execute(sa.select(pool_stats).where(pool_stats.c.day == day))}
    lb = {k: wallets_lower_bound(r.wallets_24h, r.buyers_24h, r.sellers_24h, r.traders_24h) for k, r in stats.items()}
    lb7 = _wallets_7d_lower_bound(conn, day)
    top: dict[tuple, list] = {}
    for r in conn.execute(sa.select(pool_top_wallets.c.pool_id, pool_top_wallets.c.chain_id, pool_top_wallets.c.volume_usd,
                                    pool_top_wallets.c.buys, pool_top_wallets.c.sells).where(pool_top_wallets.c.day == day)):
        top.setdefault((r.pool_id, r.chain_id), []).append((r.volume_usd or 0.0, r.buys or 0, r.sells or 0))
    sym = {k: is_symmetric(top.get(k, []), r.vol_24h_rolling) for k, r in stats.items()}
    vol_on, wallets_7d = _onchain_7d(conn, end, ready)
    distinct7 = {k: wallets_7d.get(k) for k in vol7}
    distinct7.update(_distinct_week(conn, {k: vol_days.get(k, []) for k, v in vol7.items()
                                           if k not in wallets_7d and v >= WASH_MIN_VOL_7D}))
    # Swap counts are exact only on ready chains (the indexer's UTC days). Keyless counts are GeckoTerminal's rolling
    # 06:00-to-06:00 buys + sells filed under the day before: a launch's first hours fall into a day the pool has no row
    # for, so the next night's count can be 4 for a pool that traded with hundreds. Those never bound wallets.
    exact_swaps = {k: n for k, n in swaps7.items() if k[1] in ready}

    def swap_bound(k, limit):
        """The exact swap count as a wallet bound for an "at most `limit` wallets" rule; None (cannot judge) when it is not
        exact or a known wallet lower bound already exceeds `limit`."""
        n = exact_swaps.get(k)
        return None if n is None or lb7.get(k, 0) > limit else n
    # wallets behind a pool's 7-day volume: on-chain distinct count, else the week's lower bound, else its exact swaps
    w7 = {k: wallets_7d.get(k, lb7.get(k, exact_swaps.get(k))) for k in vol7}
    # "unverified" needs that no rule could judge the project: no wallet count, no non-zero bound, no exact swap count
    wallet_known = {pool_project[k][0] for k in (set(wallets_7d) | set(lb7) | set(exact_swaps) | {k for k, n in lb.items() if n})
                    if k in pool_project}
    judged = {
        "24h": {k: (r.vol_24h or 0.0, is_wash_24h(r.vol_24h, lb[k]) and corroborated(vol7.get(k), tvl.get(k), sym[k], k in custom))
                for k, r in stats.items()},
        # on ready chains from the indexer; anywhere through the swap bound (<= 10 swaps for >= $1M)
        "7d": {k: (v, is_wash_7d(v, wallets_7d.get(k), tvl.get(k), sym.get(k, False), k in custom)
                   or is_wash_7d_by_swaps(vol7.get(k, v), swap_bound(k, WASH_MAX_WALLETS_7D)))
               for k, v in {**vol7, **vol_on}.items()},
        # a real distinct count only: pool_wallet_day on ready chains, else exact counts on every volume day joined through
        # pool_top_wallets; or <= 2 swaps, which prove <= 2 wallets
        "concentrated": {k: (v, is_wash_concentrated(v, distinct7[k])
                             or is_wash_concentrated(v, swap_bound(k, WASH_CONCENTRATED_WALLETS)))
                         for k, v in vol7.items()},
        "vol_tvl": {k: (v, is_wash_vol_tvl(v, tvl.get(k), w7[k])) for k, v in vol7.items()},
        "no_liquidity": {k: (v, is_wash_no_liquidity(v, tvl.get(k), w7[k], k in custom)) for k, v in vol7.items()},
        "symmetric": {k: (r.vol_24h_rolling or 0.0, sym[k]) for k, r in stats.items()},
    }
    reasons: dict[int, list[str]] = {}
    for reason in (r for r in WASH_REASONS if r in judged):
        share: dict[int, list[float]] = {}
        for key, (v, hit) in judged[reason].items():
            # a hook's aggregate pool is never judged, nor counted in the share: shares use the unfolded volume, as ranking does
            if key not in pool_project or key in unknown or not v or key[0].startswith(HOOK_POOL_PREFIX):
                continue
            t = share.setdefault(pool_project[key][0], [0.0, 0.0])
            t[0] += v
            t[1] += v if hit else 0.0
        for pid, (total, washed) in share.items():
            if washed / total >= WASH_SHARE:
                reasons.setdefault(pid, []).append(reason)
    return reasons, wallet_known


def _wallets_7d_lower_bound(conn, day: date) -> dict[tuple, int]:
    """Per pool off ready chains: a lower bound of the wallets behind its 7-day volume, the largest daily lower bound over
    the 7 stored pool_stats days (day-6 .. day, each covering the 24 h before it). One quiet day's 0 buyers / 0 sellers
    is not a 7-day count. A pool with no known day is absent (unknown), so the 7-day wallet rules cannot fire on it."""
    out: dict[tuple, int] = {}
    for r in conn.execute(sa.select(pool_stats).where(pool_stats.c.day >= day - 6 * _D, pool_stats.c.day <= day)):
        n = wallets_lower_bound(r.wallets_24h, r.buyers_24h, r.sellers_24h, r.traders_24h)
        if n:                                  # 0 (a quiet day) is no wallet count for a pool that had volume: unknown
            k = (r.pool_id, r.chain_id)
            out[k] = max(out.get(k, 0), n)
    return out


def _distinct_week(conn, pools_days: dict[tuple, list[date]]) -> dict[tuple, int | None]:
    """Distinct wallets behind a pool's 7-day volume off ready chains, or None when it cannot be known. Every day that
    carries volume needs an exact count (pool_stats.wallets_24h on the next day's row, which covers it); a day over 2
    wallets settles it (> 2). Otherwise each day's <= 2 wallets are all in that day's pool_top_wallets (top 20), so the
    union of their addresses is the week's distinct count; a day whose top wallets are missing leaves it unknown."""
    out: dict[tuple, int | None] = {k: None for k in pools_days}
    want = {k: {d + _D for d in days} for k, days in pools_days.items() if days}
    if not want:
        return out
    stat_days = {d for days in want.values() for d in days}
    pids = sorted({k[0] for k in want})
    exact: dict[tuple, dict[date, int]] = {}
    addrs: dict[tuple, dict[date, set]] = {}
    for i in range(0, len(pids), 500):
        part = pids[i:i + 500]
        for r in conn.execute(sa.select(pool_stats.c.pool_id, pool_stats.c.chain_id, pool_stats.c.day, pool_stats.c.wallets_24h)
                              .where(pool_stats.c.pool_id.in_(part), pool_stats.c.day >= min(stat_days),
                                     pool_stats.c.day <= max(stat_days), pool_stats.c.wallets_24h.is_not(None))):
            exact.setdefault((r.pool_id, r.chain_id), {})[r.day] = r.wallets_24h
        for r in conn.execute(sa.select(pool_top_wallets.c.pool_id, pool_top_wallets.c.chain_id, pool_top_wallets.c.day,
                                        pool_top_wallets.c.address)
                              .where(pool_top_wallets.c.pool_id.in_(part), pool_top_wallets.c.day >= min(stat_days),
                                     pool_top_wallets.c.day <= max(stat_days))):
            addrs.setdefault((r.pool_id, r.chain_id), {}).setdefault(r.day, set()).add(r.address)
    for k, days in want.items():
        counts = exact.get(k, {})
        if any(d not in counts for d in days):
            continue                                   # a volume day without an exact count: unknown
        if any(counts[d] > WASH_CONCENTRATED_WALLETS for d in days):
            out[k] = max(counts[d] for d in days)      # more than 2 on one day is more than 2 in the week
            continue
        seen = addrs.get(k, {})
        if any(len(seen.get(d, ())) < counts[d] for d in days):
            continue                                   # top wallets missing for a day: unknown
        out[k] = len(set().union(*(seen.get(d, set()) for d in days)))
    return out


def _onchain_7d(conn, end: date, chains: list[int]) -> tuple[dict, dict]:
    """Per pool on ready chains (onchain.ready_chains: a stuck swap cursor would judge partial volume): on-chain volume and
    distinct wallets over end-6 .. end."""
    if not chains:
        return {}, {}
    start = end - 6 * _D
    vol = {(r.pool_id, r.chain_id): r.v or 0.0 for r in conn.execute(
        sa.select(pool_daily_onchain.c.pool_id, pool_daily_onchain.c.chain_id, sa.func.sum(pool_daily_onchain.c.volume_usd).label("v"))
        .where(pool_daily_onchain.c.day >= start, pool_daily_onchain.c.day <= end, pool_daily_onchain.c.chain_id.in_(chains))
        .group_by(pool_daily_onchain.c.pool_id, pool_daily_onchain.c.chain_id))}
    wallets = {(r.pool_id, r.chain_id): r.n for r in conn.execute(
        sa.select(pool_wallet_day.c.pool_id, pool_wallet_day.c.chain_id, sa.func.count(sa.distinct(pool_wallet_day.c.address)).label("n"))
        .where(pool_wallet_day.c.day >= start, pool_wallet_day.c.day <= end, pool_wallet_day.c.chain_id.in_(chains))
        .group_by(pool_wallet_day.c.pool_id, pool_wallet_day.c.chain_id))}
    return vol, wallets
