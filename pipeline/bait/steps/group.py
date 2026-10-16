"""Step 4 (group): hooks -> projects. Rule 1 hooklist name (generic names excluded; a same-chain verified-name project
is adopted only with evidence), rule 2 code_hash or deployer, rule 2b bytecode template (code_shape) on one chain, rule 3
Claude for the long tail, rule 4 one team's projects (shared name stem plus a shared signal). Flagged hooks are regrouped
first; moved and renamed projects leave aliases. is_factory when a project has > 20 hooks."""
from __future__ import annotations

import os
import time

import sqlalchemy as sa

from ..db import backfill_address_flags, hooks, project_aliases, projects, utcnow, pools
from ..rules import (SHARED_DEPLOYERS, audit_key, code_evidence, deployment_words, merge_evidence, name_stem, normalize_name,
                     proxy_flag, shape_evidence, slugify, stem_root, team_links, team_name, useful_name, fallback_category)
from . import RunContext, log
from .names import add_alias, backfill_unlisted_aliases, detach_project, hook_rows, retire_project, unique_slug

FACTORY_MIN_HOOKS = 21


def run(conn: sa.Connection, ctx: RunContext) -> dict:
    out = {"by_name": 0, "by_code": 0, "by_deployer": 0, "by_claude": 0, "created": 0, "unassigned": 0,
           "code_fetched": 0, "factories": 0, "split": 0, "promoted": 0, "moved": 0, "retired": 0, "adopted": 0,
           "code_refreshed": 0, "code_missing": 0, "shapes_filled": 0, "by_shape": 0, "shape_merged": 0, "team_merged": 0}
    out["flags_backfilled"] = backfill_address_flags(conn)
    _refresh_code_facts(conn, ctx, out)
    out["aliases_backfilled"] = backfill_unlisted_aliases(conn, ctx.now)
    generic = _data_generic_norms(conn)
    splits = _split_generic(conn, ctx, generic) + _split_proxy_families(conn, ctx)
    out["split"] = len(splits)
    _regroup(conn, ctx, out, generic)
    _fill_shapes(conn, ctx, out, _team_candidates(conn, generic))
    _merge_shape_families(conn, ctx, out)
    unassigned = _unassigned(conn)

    # Rule 1: hooklist name -> curated project, else a same-chain verified-name project with evidence, else a new one.
    by_norm = _projects_by_norm(conn)
    for h in unassigned:
        name = _gname(h, generic)
        norm = normalize_name(name)
        if not norm:
            continue
        pid = by_norm.get(norm)
        if pid is None and (pid := _adopt_contract(conn, ctx, h, name, norm, by_norm)) is not None:
            out["adopted"] += 1
        if pid is None:
            pid = _create_project(conn, ctx, name, norm, h.description)
            by_norm[norm] = pid
            out["created"] += 1
        _assign(conn, h, pid)
        out["by_name"] += 1

    # Rule 2: code_hash. Fetch missing hashes (capped), then join unassigned hooks to a project sharing the hash.
    unassigned = _unassigned(conn)
    if unassigned:
        out["code_fetched"] = _fetch_code_hashes(conn, ctx, unassigned)
        unassigned = _unassigned(conn)
        hash_to_pid, dep_to_pid = _join_targets(conn)
        still = []
        for h in unassigned:
            pid = hash_to_pid.get(c) if (c := _code(h)) else None
            if pid:
                _assign(conn, h, pid)
                out["by_code"] += 1
            elif h.deployer and (pid := dep_to_pid.get((h.chain_id, h.deployer))):
                _assign(conn, h, pid)
                out["by_deployer"] += 1
            else:
                still.append(h)
        # Unassigned hooks that share non-proxy bytecode with each other (one launchpad, many clones) become one project.
        fam: dict[str, list] = {}
        for h in still:
            if c := _code(h):
                fam.setdefault(c, []).append(h)
        still = [h for h in still if not _code(h) or len(fam[_code(h)]) < 2]
        for members in fam.values():
            if len(members) < 2:
                continue
            first = sorted(m.address for m in members)[0]
            pid = _create_project(conn, ctx, f"Unlisted hook family {first[:6]}…", None, None)
            for m in members:
                _assign(conn, m, pid)
            out["created"] += 1
            out["by_code"] += len(members)
        # Rule 2b: same bytecode template on one chain (only immutables differ): join its project, or form a family.
        targets = _shape_targets(conn)
        fam = {}
        rest = []
        for h in still:
            k = shape_evidence(h)
            if k and (pid := targets.get(k)):
                _assign(conn, h, pid)
                out["by_shape"] += 1
            elif k:
                fam.setdefault(k, []).append(h)
            else:
                rest.append(h)
        for members in fam.values():
            if len(members) < 2:
                rest += members
                continue
            first = sorted(m.address for m in members)[0]
            pid = _create_project(conn, ctx, f"Unlisted hook family {first[:6]}…", None, None)
            for m in members:
                _assign(conn, m, pid)
            out["created"] += 1
            out["by_shape"] += len(members)
        unassigned = sorted(rest, key=lambda h: (h.chain_id, h.address))

    # Rule 3: Claude classification for the long tail (nameless hooks or names that matched nothing).
    if unassigned:
        claude = ctx.client("claude")
        if claude is None:
            # No key: every unnamed hook becomes its own project keyed by address so the feed still renders.
            for h in unassigned:
                pid = _create_project(conn, ctx, _gname(h, generic) or f"Unlisted hook {h.address[:6]}…{h.address[-4:]}", None, h.description, category=None)
                _assign(conn, h, pid)
                out["created"] += 1
            unassigned = []
        else:
            from ..costs import BudgetExceeded
            names = {r.id: r.name for r in conn.execute(sa.select(projects.c.id, projects.c.name))}
            # Only an address (Claude would be guessing), or a template/proxy name (a free contract-name lookup comes
            # first): never paid for. Every unassigned hook here has no useful name, so a non-empty name is a generic one.
            # Those get an address-named project (bytecode families were already merged above for free).
            bare = [h for h in unassigned if (h.name or "").strip() or not (h.description or "").strip()]
            for h in bare:
                pid = _create_project(conn, ctx, f"Unlisted hook {h.address[:6]}…{h.address[-4:]}", None, None, category=None)
                _assign(conn, h, pid)
                out["created"] += 1
                out["unlisted_named"] = out.get("unlisted_named", 0) + 1
            unassigned = [h for h in unassigned if h not in bare]
            cap = claude_cap()
            if len(unassigned) > cap:
                log.info("group: %d hooks need Claude, classifying %d this run (GROUP_CLAUDE_MAX); the rest wait", len(unassigned), cap)
            for h in unassigned[:cap]:
                try:
                    res = claude.classify_hook(h.name or "", h.description or "", h.address,
                                               shortlist(h.name or "", h.description or "", list(set(names.values()))))
                except BudgetExceeded:
                    out["budget_stop"] = 1
                    break
                except Exception as e:
                    log.warning("group claude classify failed for %s: %s", h.address, e)
                    continue
                # A template answer ("Counter") names nothing: an address card, never matched or keyed by that name,
                # so the next run neither splits it nor pays Claude again.
                pname = useful_name(res.get("project"), max_len=None)
                if pname and normalize_name(pname) in generic:
                    pname = None
                match = next((pid for pid, n in names.items() if n.lower() == pname.lower()), None) if pname else None
                if match is None:
                    match = _create_project(conn, ctx, pname or f"Unlisted hook {h.address[:6]}…{h.address[-4:]}",
                                            normalize_name(pname) or None, h.description, category=res.get("category"))
                    names[match] = pname or ""
                    out["created"] += 1
                _assign(conn, h, match)
                out["by_claude"] += 1
            unassigned = _unassigned(conn)
    out["unassigned"] = len(unassigned)

    # Name what is still "Unlisted hook ..." from verified source (free), merging same-named hooks on a chain.
    from . import deployers, names
    names.run(conn, ctx, out)
    deployers.run(conn, ctx, out)
    for snap in splits:                                  # split projects' slugs and paid facts follow their hooks
        names.settle_split(conn, snap, ctx.now)
    # Rule 4 last, so projects the rules above created tonight (a new hooklist name, a verified-source name) merge
    # tonight too, before the score step builds their cards.
    _merge_same_team(conn, ctx, out, generic)

    # Factories: > 20 hooks.
    counts = conn.execute(sa.select(hooks.c.project_id, sa.func.count()).where(hooks.c.project_id.is_not(None)).group_by(hooks.c.project_id)).all()
    factory_ids = [pid for pid, n in counts if n >= FACTORY_MIN_HOOKS]
    conn.execute(sa.update(projects).values(is_factory=False).where(projects.c.is_factory.is_(True)))
    if factory_ids:
        conn.execute(sa.update(projects).values(is_factory=True).where(projects.c.id.in_(factory_ids)))
    out["factories"] = len(factory_ids)
    out["categories_guessed"] = _fallback_categories(conn)
    return out


def _fallback_categories(conn) -> int:
    """Projects with no category, or "other" (enrichment's answer when its sources named none), get
    rules.fallback_category from their hooks and pools. A specific category from enrichment is never touched. A guess is
    re-made every night (the inputs change as hooks and pools arrive); when it stops holding, the project goes back to
    what it had: "other" if enrichment ran, else nothing."""
    cands = conn.execute(sa.select(projects.c.id, projects.c.is_factory, projects.c.category, projects.c.category_source,
                                   projects.c.enriched_at)
                         .where(sa.or_(projects.c.category.is_(None), projects.c.category == "other",
                                       projects.c.category_source == "rules"))).all()
    if not cands:
        return 0
    ids = [c.id for c in cands]
    by_project: dict[int, dict] = {c.id: {"texts": [], "props": [], "fees": [], "pools_total": None, "factory": bool(c.is_factory),
                                          "current": (c.category, c.category_source), "enriched": c.enriched_at is not None}
                                     for c in cands}
    for h in conn.execute(sa.select(hooks.c.project_id, hooks.c.name, hooks.c.description, hooks.c.contract_name, hooks.c.properties,
                                    hooks.c.pools_total).where(hooks.c.project_id.in_(ids))):
        b = by_project[h.project_id]
        b["texts"] += [h.name, h.description, h.contract_name]
        b["props"].append(h.properties or {})
        if h.pools_total is not None:
            b["pools_total"] = max(b["pools_total"] or 0, h.pools_total)
    for r in conn.execute(sa.select(hooks.c.project_id, pools.c.fee)
                          .select_from(pools.join(hooks, (hooks.c.address == pools.c.hook_address) & (hooks.c.chain_id == pools.c.chain_id)))
                          .where(hooks.c.project_id.in_(ids), pools.c.fee.is_not(None)).distinct()):
        by_project[r.project_id]["fees"].append(r.fee)
    n = 0
    for pid, b in by_project.items():
        cat = fallback_category(b["texts"], b["props"], b["fees"], b["factory"], b["pools_total"])
        if cat:
            new = (cat, "rules")
        elif b["current"][1] == "rules":
            new = ("other", "enrich") if b["enriched"] else (None, None)   # our earlier guess no longer holds
        else:
            continue
        if new != b["current"]:
            conn.execute(sa.update(projects).values(category=new[0], category_source=new[1]).where(projects.c.id == pid))
            n += 1
    return n


def _unassigned(conn):
    return conn.execute(sa.select(hooks).where(hooks.c.project_id.is_(None)).order_by(hooks.c.chain_id, hooks.c.address)).all()


def _projects_by_norm(conn) -> dict[str, int]:
    return {r.norm_name: r.id for r in conn.execute(sa.select(projects.c.norm_name, projects.c.id).where(projects.c.norm_name.is_not(None)))}


def _assign(conn, h, pid: int) -> None:
    conn.execute(sa.update(hooks).values(project_id=pid).where(hooks.c.address == h.address, hooks.c.chain_id == h.chain_id))


def _create_project(conn, ctx, name: str, norm: str | None, description: str | None, category: str | None = None) -> int:
    base = slugify(name)
    slug, i = base, 2
    taken = {r[0] for r in conn.execute(sa.select(projects.c.slug).where(projects.c.slug.like(base + "%")))}
    taken |= set(conn.execute(sa.select(project_aliases.c.old_slug).where(project_aliases.c.old_slug.like(base + "%"))).scalars())
    while slug in taken:
        slug, i = f"{base}-{i}", i + 1
    what = (description or "").strip().split(". ")[0][:160] or None  # placeholder until step 4 runs Claude
    if category is None:
        from ..keyless import categorize
        category = categorize(f"{description or ''} {name}", {}, {})
    r = conn.execute(sa.insert(projects).values(
        slug=slug, name=name, norm_name=norm, category=category, what_line=what, first_seen_at=ctx.now))
    return r.inserted_primary_key[0]


CODE_FACTS_BATCH = 2000


def _gname(h, generic: set[str] = frozenset()) -> str | None:
    """The hooklist name if it identifies a project; template, proxy and data-generic names (`generic`) count as none."""
    name = useful_name(h.name, max_len=None)
    return name if name and normalize_name(name) not in generic else None


def _data_generic_norms(conn) -> set[str]:
    """Hooklist names that unrelated teams share. Deployers only conflict on the same chain (one team often deploys
    per chain from a different address, and per-chain bytecode always differs because the PoolManager is an
    immutable), so a name is generic when, counting per chain the distinct known deployers beyond the first, there are
    at least two such conflicts and no non-proxy code_hash is shared. Unknown deployers prove nothing, and a name on
    FACTORY_MIN_HOOKS or more hooks is a factory family by design."""
    by: dict[str, list] = {}
    for r in conn.execute(sa.select(hooks.c.name, hooks.c.chain_id, hooks.c.deployer, hooks.c.code_hash, hooks.c.is_proxy,
                                    hooks.c.proxy_kind)
                          .where(hooks.c.name.is_not(None))):
        if norm := normalize_name(useful_name(r.name, max_len=None)):
            by.setdefault(norm, []).append(r)
    out = set()
    for norm, rows in by.items():
        if not 2 <= len(rows) < FACTORY_MIN_HOOKS:
            continue
        codes = [c for r in rows if (c := code_evidence(r))]
        if len(set(codes)) < len(codes):
            continue                                     # shared bytecode: one team
        per_chain: dict[int, set[str]] = {}
        for r in rows:
            if r.deployer and r.deployer not in SHARED_DEPLOYERS:
                per_chain.setdefault(r.chain_id, set()).add(r.deployer)
        if sum(len(d) - 1 for d in per_chain.values()) >= 2:
            out.add(norm)
    return out


def _code(h) -> str | None:
    """The hook's code_hash if it can join hooks: known non-proxy bytecode only (rules.code_evidence)."""
    return code_evidence(h)


def _provisional(name: str, norm: str | None) -> bool:
    """Auto-named projects a hook may leave: unlisted, bytecode families, verified-source names. Curated ones never."""
    n = norm or ""
    return not n or n.startswith(("contract:", "code:")) or name.startswith("Unlisted hook")


def _rpc_code_facts(ctx, rows, deadline: float) -> dict:
    """{(chain_id, address): (code_hash, is_proxy) | None} from free public-RPC eth_getCode for keyless chains. None means
    the chain answered with no code; a missing key means no answer. {} when KEYLESS=0 (tests) or on failure."""
    if os.environ.get("KEYLESS", "1") == "0":
        return {}
    from .. import keyless as K
    from .snapshot import keyless_http
    net_by_chain = {cid: net for net, (cid, *_rest) in K.CHAINS.items()}
    by_net: dict[str, set[str]] = {}
    for r in rows:
        if net := net_by_chain.get(r.chain_id):
            by_net.setdefault(net, set()).add(r.address)
    if not by_net:
        return {}
    try:
        got = K.code_facts(keyless_http(ctx), by_net, deadline=deadline)
    except Exception as e:
        log.warning("group: code facts lookup failed: %s", e)
        return {}
    return {(K.CHAINS[net][0], addr): v for (net, addr), v in got.items()}


def _refresh_budget_s() -> float:
    try:
        return float(os.environ.get("REHASH_BUDGET_S", "") or 120)
    except ValueError:
        return 120.0


def _refresh_code_facts(conn, ctx, out: dict) -> None:
    """Proxy status is a bytecode fact. Rows with a legacy sha1 code_hash (keyless, 40 chars) or a hash whose proxy status
    is unknown (written before is_proxy existed, or by the on-chain/snapshot steps) get both from eth_getCode: canonical
    hash and is_proxy. New unassigned hooks first, then a window over the rest that moves on every day, so rows an RPC
    never answers do not block the queue. REHASH_BUDGET_S (default 120) bounds the wall clock; a definitive miss (the
    chain has no code there) nulls code_hash. A hash that changed, or one now known not to be a proxy, marks the hook
    for regroup. One-off on the same transition: a generic stored contract name ("ERC1967Proxy") is cleared so the
    lookup, which now resolves implementations, runs again."""
    need = sa.or_(sa.func.length(hooks.c.code_hash) == 40, sa.and_(
        hooks.c.code_hash.is_not(None), hooks.c.is_proxy.is_(None), hooks.c.proxy_kind.is_(None)))   # "unknown" is settled
    cols = (hooks.c.address, hooks.c.chain_id, hooks.c.project_id, hooks.c.code_hash, hooks.c.contract_name)
    order = (hooks.c.chain_id, hooks.c.address)
    todo = conn.execute(sa.select(*cols).where(need, hooks.c.project_id.is_(None)).order_by(*order)
                        .limit(CODE_FACTS_BATCH)).all()
    room = CODE_FACTS_BATCH - len(todo)
    rest = sa.select(*cols).where(need, hooks.c.project_id.is_not(None)).order_by(*order)
    total = conn.execute(sa.select(sa.func.count()).select_from(rest.subquery())).scalar_one() if room > 0 else 0
    if total:
        start = (ctx.day.toordinal() * room) % total
        window = conn.execute(rest.offset(start).limit(room)).all()
        if len(window) < room and start:
            window += conn.execute(rest.limit(min(room - len(window), start))).all()
        todo += window
    if not todo:
        return
    got = _rpc_code_facts(ctx, todo, time.monotonic() + _refresh_budget_s())
    for r in todo:
        key = (r.chain_id, r.address)
        if key not in got:
            continue                                     # not answered: kept, asked again on a later run
        if got[key] is None:
            vals = {"code_hash": None, "is_proxy": None, "proxy_kind": None}
            out["code_missing"] += 1
        else:
            h, kind = got[key]
            vals = {"code_hash": h, "is_proxy": proxy_flag(kind), "proxy_kind": kind}
            if r.project_id is not None and (h != r.code_hash or kind in (None, "eip1167")):
                vals["regroup"] = True
            if r.contract_name is not None and useful_name(r.contract_name) is None:
                vals.update(contract_name=None, name_checked_at=None)
            out["code_refreshed"] += 1
        conn.execute(sa.update(hooks).values(**vals).where(hooks.c.address == r.address, hooks.c.chain_id == r.chain_id))
    log.info("group: code facts for %d of %d hooks, %d without code", out["code_refreshed"], len(todo), out["code_missing"])


def _split_proxy_families(conn, ctx) -> list[dict]:
    """Auto-named projects (bytecode families, verified-name merges) that joined proxies by shared bytecode before proxy
    status was known. If a proxy hook has no evidence (non-proxy code, a real deployer) tying it to the rest of its
    project, the whole project goes back through the rules; settle_split hands its slug to where most hooks land."""
    rows = conn.execute(
        sa.select(hooks.c.project_id, hooks.c.code_hash, hooks.c.is_proxy, hooks.c.proxy_kind, hooks.c.deployer, hooks.c.name,
                  hooks.c.contract_name, projects.c.name.label("pname"), projects.c.norm_name.label("pnorm"))
        .join(projects, projects.c.id == hooks.c.project_id)
        .where(hooks.c.project_id.in_(sa.select(hooks.c.project_id).where(hooks.c.is_proxy.is_(True))))).all()
    by: dict[int, list] = {}
    for r in rows:
        if _provisional(r.pname, r.pnorm):
            by.setdefault(r.project_id, []).append(r)
    bad = [pid for pid, hs in sorted(by.items())
           if len(hs) > 1 and any(h.is_proxy and not merge_evidence([h], [o for o in hs if o is not h]) for h in hs)]
    snaps = [detach_project(conn, pid, "split:proxy", ctx.now) for pid in bad]
    if bad:
        log.info("group: split %d auto-named projects that joined proxies by bytecode", len(bad))
    return snaps


def _split_generic(conn, ctx, generic: set[str]) -> list[dict]:
    """Projects keyed on a template, proxy or data-generic name ("ERC1967Proxy", "LimitOrderHook") merged unrelated hooks
    by name alone. Detach their hooks so the rules regroup them on evidence; settle_split later hands the slug and paid
    facts to wherever most hooks land. A template or proxy name is always split: it identifies nothing worth keeping.
    A real-looking name that is generic only by the deployer data is the judgement call, so one with enrichment or 3+
    hooks is never split automatically: it is logged for a human instead."""
    cand = [p for p in conn.execute(sa.select(projects).where(projects.c.norm_name.is_not(None)))
            if not p.norm_name.startswith(("contract:", "code:"))
            and (not useful_name(p.name, max_len=None) or p.norm_name in generic)]
    snaps = []
    for p in cand:
        n = conn.execute(sa.select(sa.func.count()).select_from(hooks).where(hooks.c.project_id == p.id)).scalar_one()
        by_data = useful_name(p.name, max_len=None) is not None          # not a template name: generic by data only
        if by_data and (p.enriched_at is not None or p.website or p.github or n >= 3):
            log.info("group: would split %s (generic name, %d hooks)", p.slug, n)
            continue
        snaps.append(detach_project(conn, p.id, "split:generic", ctx.now))
    if snaps:
        log.info("group: split %d projects keyed on a template or proxy name", len(snaps))
    return snaps


def _shape_max() -> int:
    try:
        return max(0, int(os.environ.get("SHAPE_MAX", "") or 3000))
    except ValueError:
        return 3000


def _rpc_shapes(ctx, rows, deadline: float) -> dict:
    """{(chain_id, address): code_shape | None} from public-RPC eth_getCode (keyless chains). None = no code there."""
    if os.environ.get("KEYLESS", "1") == "0":
        return {}
    from .. import keyless as K
    from .snapshot import keyless_http
    net_by_chain = {cid: net for net, (cid, *_rest) in K.CHAINS.items()}
    by_net: dict[str, set[str]] = {}
    for r in rows:
        if net := net_by_chain.get(r.chain_id):
            by_net.setdefault(net, set()).add(r.address)
    if not by_net:
        return {}
    try:
        got = K.code_shapes(keyless_http(ctx), by_net, deadline=deadline)
    except Exception as e:
        log.warning("group: code shape lookup failed: %s", e)
        return {}
    return {(K.CHAINS[net][0], addr): v for (net, addr), v in got.items()}


def _fill_shapes(conn, ctx, out: dict, first: set[int] = frozenset()) -> None:
    """code_shape for non-proxy hooks without one: unassigned hooks first, then hooks of the `first` projects (rule 4
    candidates, whose merge may wait on a shape), then the newest. SHAPE_MAX (3000) per run within REHASH_BUDGET_S;
    hooks an RPC does not answer are asked again next run, "" marks a hook with no code left."""
    cap = _shape_max()
    if cap == 0:
        return
    # no term at all when `first` is empty: a bare constant in ORDER BY is an error on Postgres 15+
    later = [hooks.c.project_id.not_in(sorted(first))] if first else []
    todo = conn.execute(sa.select(hooks.c.address, hooks.c.chain_id)
                        .where(hooks.c.code_shape.is_(None), hooks.c.code_hash.is_not(None), hooks.c.is_proxy.is_(False))
                        .order_by(hooks.c.project_id.is_not(None), *later, hooks.c.first_seen_at.desc(), hooks.c.chain_id,
                                  hooks.c.address)
                        .limit(cap)).all()
    if not todo:
        return
    got = _rpc_shapes(ctx, todo, time.monotonic() + _refresh_budget_s())
    for r in todo:
        key = (r.chain_id, r.address)
        if key in got:
            conn.execute(sa.update(hooks).values(code_shape=got[key] or "")
                         .where(hooks.c.address == r.address, hooks.c.chain_id == r.chain_id))
            out["shapes_filled"] += 1
    log.info("group: code shapes for %d of %d hooks", out["shapes_filled"], len(todo))


def _unlisted(name: str) -> bool:
    return name.startswith("Unlisted hook")


def _shape_groups(conn) -> dict[str, dict[int, sa.Row]]:
    """(chain:shape) -> {project_id: a row} over auto-named projects. Curated projects never take part."""
    groups: dict[str, dict[int, sa.Row]] = {}
    for r in conn.execute(
            sa.select(hooks.c.address, hooks.c.chain_id, hooks.c.code_shape, hooks.c.code_hash, hooks.c.is_proxy,
                      hooks.c.proxy_kind, hooks.c.project_id, projects.c.name.label("pname"), projects.c.norm_name.label("pnorm"))
            .join(projects, projects.c.id == hooks.c.project_id)
            .where(hooks.c.code_shape.is_not(None), hooks.c.code_shape != "")
            .order_by(hooks.c.project_id)):
        if (k := shape_evidence(r)) and _provisional(r.pname, r.pnorm):
            groups.setdefault(k, {}).setdefault(r.project_id, r)
    return groups


def _shape_target(projs: dict[int, sa.Row]) -> int | None:
    """Where a template's unnamed hooks belong: its one verified-name project, else its oldest unlisted family, else its
    oldest unlisted project. Two verified names on one template are two teams: their unnamed siblings stay apart from
    both and only fold among themselves."""
    named = [pid for pid, r in projs.items() if not _unlisted(r.pname)]
    if len(named) == 1:
        return named[0]
    unl = [(not r.pname.startswith("Unlisted hook family"), pid) for pid, r in projs.items() if _unlisted(r.pname)]
    return min(unl)[1] if unl else None


def _shape_targets(conn) -> dict[str, int]:
    return {k: pid for k, projs in _shape_groups(conn).items() if (pid := _shape_target(projs)) is not None}


def _merge_shape_families(conn, ctx, out: dict) -> None:
    """Rule 2b across time: unlisted projects whose hooks share a bytecode template on one chain are one launchpad's
    per-token deployments. They fold into _shape_target; a single unlisted target becomes "Unlisted hook family 0x…".
    Named projects never move. Old slugs redirect."""
    into: dict[int, int] = {}

    def live(pid: int) -> int:
        while pid in into:
            pid = into[pid]
        return pid

    renamed = set()
    for k, projs in _shape_groups(conn).items():
        target = _shape_target(projs)
        if target is None:
            continue
        target = live(target)
        movers = sorted({live(pid) for pid, r in projs.items() if _unlisted(r.pname)} - {target})
        for pid in movers:
            retire_project(conn, pid, into=target, reason="regroup:code_shape", now=ctx.now)
            into[pid] = target
            out["shape_merged"] += 1
        if movers and target not in renamed:
            renamed.add(target)
            p = conn.execute(sa.select(projects).where(projects.c.id == target)).one()
            if _unlisted(p.name) and not p.name.startswith("Unlisted hook family"):
                first = conn.execute(sa.select(sa.func.min(hooks.c.address)).where(hooks.c.project_id == target)).scalar_one()
                name = f"Unlisted hook family {first[:6]}…"
                slug = unique_slug(conn, name, target)
                conn.execute(sa.update(projects).values(name=name, slug=slug).where(projects.c.id == target))
                if slug != p.slug:
                    add_alias(conn, p.slug, target, "renamed", ctx.now)
    if out["shape_merged"]:
        log.info("group: folded %d unlisted projects into bytecode-template families", out["shape_merged"])


def _team_tokens(r) -> set[str]:
    """One hook's team signals: a deployer that is not a CREATE2 factory, non-proxy bytecode (code_hash) and its
    template (code_shape, on any chain), and the hooklist audit URL."""
    out = set()
    if r.deployer and r.deployer not in SHARED_DEPLOYERS:
        out.add(f"deployer:{r.deployer}")
    if c := code_evidence(r):
        out.add(f"code_hash:{c}")
        if r.code_shape:
            out.add(f"code_shape:{r.code_shape}")
    if a := audit_key(r.audit_url):
        out.add(f"audit:{a}")
    return out


def _team_root(p, generic: set[str]) -> str | None:
    """Whose signals a project's hooks and links are: its stem root; "*" (nobody's) for a generic or template name; None
    for an unlisted project, which says nothing either way (rule 2b folds those)."""
    if _unlisted(p.name):
        return None
    if p.norm_name in generic:
        return "*"
    return stem_root(p.name) or "*"


def _team_rank(p, hooks_n: int):
    """The project a team merge keeps: a curated (hooklist-named) one, then the richest enrichment, the name with the
    fewest deployment words ("Aegis" over "Aegis v2 Hook (Unichain)"), the most hooks, the oldest, the lowest id."""
    rich = (p.enriched_at is not None) + bool(p.website) + bool(p.github) + bool(p.x_handle)
    words = sum(len(w) for w in deployment_words(p.name).values())
    return (_provisional(p.name, p.norm_name), -rich, words, -hooks_n, p.first_seen_at, p.id)


def _team_participants(conn, generic: set[str], prows: dict | None = None) -> dict[str, list[int]]:
    """stem -> project ids that may take part in rule 4: a stem, hooks, and no proxy hook in an auto-named project (the
    proxy split would undo the merge)."""
    prows = prows if prows is not None else {p.id: p for p in conn.execute(sa.select(projects))}
    held = dict(conn.execute(sa.select(hooks.c.project_id, sa.func.max(sa.case((hooks.c.is_proxy.is_(True), 1), else_=0)))
                             .where(hooks.c.project_id.is_not(None)).group_by(hooks.c.project_id)).all())
    by_stem: dict[str, list[int]] = {}
    for pid, p in prows.items():
        stem = name_stem(p.name) if p.norm_name not in generic else None
        if stem and pid in held and not (held[pid] and _provisional(p.name, p.norm_name)):
            by_stem.setdefault(stem, []).append(pid)
    return by_stem


def _team_candidates(conn, generic: set[str]) -> set[int]:
    return {pid for pids in _team_participants(conn, generic).values() if len(pids) > 1 for pid in pids}


def _merge_same_team(conn, ctx, out: dict, generic: set[str] = frozenset()) -> None:
    """Rule 4, one team's projects (DESIGN.md "Same-team merge"). Projects whose names share a stem (rules.name_stem:
    versions, chains, "L2", fee modes and "hook" stripped) merge when they also share a signal that only that name root
    carries: a non-CREATE2 deployer on any chain, non-proxy bytecode or its template, a hooklist audit URL, or two of
    website / GitHub owner / X handle with none of the three disagreeing (enrichment is found by searching the name, so
    one link alone mostly repeats the name). A stem alone never merges. The kept project takes the oldest first_seen_at
    and the others' missing enrichment and receipts; old slugs redirect. Curated projects take part: this is the one
    rule that moves them."""
    prows = {p.id: p for p in conn.execute(sa.select(projects))}
    hrows = conn.execute(sa.select(hooks.c.project_id, hooks.c.deployer, hooks.c.code_hash, hooks.c.is_proxy,
                                   hooks.c.proxy_kind, hooks.c.code_shape, hooks.c.audit_url)
                         .where(hooks.c.project_id.is_not(None))).all()
    tokens: dict[int, set[str]] = {}
    hooks_n: dict[int, int] = {}
    for r in hrows:
        tokens.setdefault(r.project_id, set()).update(_team_tokens(r))
        hooks_n[r.project_id] = hooks_n.get(r.project_id, 0) + 1
    links = {pid: team_links(p.website, p.github, p.x_handle) for pid, p in prows.items()}
    for pid, ls in links.items():
        tokens.setdefault(pid, set()).update(f"link:{k}:{v}" for k, v in ls.items())

    # A signal is a team's only when every named project carrying it has one root: an OpenZeppelin template or an
    # auditor's portfolio URL shows up under unrelated names, and a generic name ("*") marks it as a template.
    roots: dict[str, set[str]] = {}
    for pid, ts in tokens.items():
        if (root := _team_root(prows[pid], generic)) is not None:
            for t in ts:
                roots.setdefault(t, set()).add(root)
    owned = {t for t, rs in roots.items() if len(rs) == 1 and "*" not in rs}

    for stem, pids in sorted(_team_participants(conn, generic, prows).items()):
        if len(pids) < 2:
            continue
        up = {pid: pid for pid in pids}

        def find(x: int) -> int:
            while up[x] != x:
                up[x] = up[up[x]]
                x = up[x]
            return x
        why: dict[int, str] = {}
        carriers: dict[str, list[int]] = {}
        for pid in pids:
            for t in tokens.get(pid, ()):
                if t in owned and not t.startswith("link:"):
                    carriers.setdefault(t, []).append(pid)
        for t, ps in sorted(carriers.items()):
            for o in ps[1:]:
                if find(o) != find(ps[0]):
                    up[find(o)] = find(ps[0])
                    why[o] = t.split(":")[0]
        for i, a in enumerate(pids):
            for b in pids[i + 1:]:
                la, lb = links[a], links[b]
                same = [k for k in la if lb.get(k) == la[k] and f"link:{k}:{la[k]}" in owned]
                clash = any(k in lb and lb[k] != la[k] for k in la)
                if len(same) >= 2 and not clash and find(a) != find(b):
                    up[find(b)] = find(a)
                    why[b] = "links"
        comps: dict[int, list[int]] = {}
        for pid in pids:
            comps.setdefault(find(pid), []).append(pid)
        for members in comps.values():
            if len(members) > 1:
                kinds = ",".join(sorted({why[m] for m in members if m in why}))
                _fold_team(conn, ctx, [prows[m] for m in members], hooks_n, out, f"{stem} by {kinds}")
    if out["team_merged"]:
        log.info("group: merged %d projects into their team's project (same stem and a shared signal)", out["team_merged"])


def _fold_team(conn, ctx, members: list, hooks_n: dict[int, int], out: dict, why: str) -> None:
    keep = min(members, key=lambda p: _team_rank(p, hooks_n.get(p.id, 0)))
    for p in members:
        if p.id != keep.id:
            retire_project(conn, p.id, into=keep.id, reason="merge:team", now=ctx.now)
            out["team_merged"] += 1
            log.info("group: team merge %s <- %s (stem %s)", keep.slug, p.slug, why)
    vals = {"first_seen_at": min(p.first_seen_at for p in members)}
    # The kept name loses the deployment words its members disagree on: chains when they span chains, fee modes,
    # versions ("Clanker Static Fee Hook v2 (Base)" -> "Clanker Hook"; two "Aegis v3 Hook (...)" -> "Aegis v3 Hook").
    # Unless another project already goes by it: AngstromL2 stays AngstromL2 next to Angstrom.
    words = [deployment_words(p.name) for p in members]
    drop = {k for k in words[0] if len({w[k] for w in words}) > 1}
    name = team_name(keep.name, drop)
    # a template-looking name would be split again by _split_generic next run
    if any(deployment_words(keep.name)[k] for k in drop) and useful_name(name, max_len=None) and not conn.execute(
            sa.select(projects.c.id).where(sa.func.lower(projects.c.name) == name.lower(), projects.c.id != keep.id)).first():
        slug = unique_slug(conn, name, keep.id)
        vals.update(name=name, slug=slug)
        if slug != keep.slug:
            add_alias(conn, keep.slug, keep.id, "renamed", ctx.now)
    conn.execute(sa.update(projects).values(**vals).where(projects.c.id == keep.id))


def _join_targets(conn) -> tuple[dict, dict]:
    """code_hash -> project and (chain, deployer) -> project from assigned hooks. Proxy bytecode is shared by unrelated
    deployments and CREATE2 factories deploy for everyone, so neither is evidence."""
    by_hash: dict[str, int] = {}
    by_dep: dict[tuple[int, str], int] = {}
    for r in conn.execute(sa.select(hooks.c.code_hash, hooks.c.is_proxy, hooks.c.proxy_kind, hooks.c.deployer, hooks.c.chain_id,
                                    hooks.c.project_id)
                          .where(hooks.c.project_id.is_not(None)).order_by(hooks.c.project_id)):
        if c := _code(r):
            by_hash.setdefault(c, r.project_id)
        if r.deployer and r.deployer not in SHARED_DEPLOYERS:
            by_dep.setdefault((r.chain_id, r.deployer), r.project_id)
    return by_hash, by_dep


def _move(conn, ctx, h, src: int, target: int, reason: str, out: dict) -> None:
    _assign(conn, h, target)
    out["moved"] += 1
    if not conn.execute(sa.select(hooks.c.address).where(hooks.c.project_id == src).limit(1)).first():
        retire_project(conn, src, into=target, reason=reason, now=ctx.now)
        out["retired"] += 1


def _promote(conn, ctx, p, name: str, norm: str) -> None:
    """An auto-named project becomes the curated project for `name`; its old slug keeps working."""
    slug = unique_slug(conn, name, p.id)
    conn.execute(sa.update(projects).values(name=name, norm_name=norm, slug=slug).where(projects.c.id == p.id))
    if slug != p.slug:
        add_alias(conn, p.slug, p.id, "renamed", ctx.now)


def _adopt_contract(conn, ctx, h, name: str, norm: str, by_norm: dict) -> int | None:
    pid = by_norm.get(f"contract:{h.chain_id}:{norm}")
    if pid is None or not merge_evidence([h], hook_rows(conn, pid)):
        return None                                      # two teams can both write "OrderHook"
    _promote(conn, ctx, conn.execute(sa.select(projects).where(projects.c.id == pid)).one(), name, norm)
    by_norm[norm] = pid
    return pid


def _regroup_by_code(conn, ctx, code_hash: str, out: dict) -> None:
    """Rule 2 across time: same non-proxy bytecode, one project. Target: a curated holder, else a verified-name one,
    else the oldest. Only auto-named projects give hooks up."""
    rows = [r for r in conn.execute(
        sa.select(hooks.c.address, hooks.c.chain_id, hooks.c.code_hash, hooks.c.is_proxy, hooks.c.proxy_kind, hooks.c.project_id,
                  projects.c.name.label("pname"), projects.c.norm_name.label("pnorm"))
        .join(projects, projects.c.id == hooks.c.project_id).where(hooks.c.code_hash == code_hash)) if _code(r)]
    if len({r.project_id for r in rows}) < 2:
        return
    target = min(rows, key=lambda r: (_provisional(r.pname, r.pnorm), r.pname.startswith("Unlisted hook"), r.project_id)).project_id
    for r in rows:
        if r.project_id != target and _provisional(r.pname, r.pnorm):
            _move(conn, ctx, r, r.project_id, target, "regroup:code_hash", out)


def _regroup(conn, ctx, out: dict, generic: set[str] = frozenset()) -> None:
    """Hooks flagged by ingest (the registry named them) or by a code_hash write. A named hook in an auto-named project
    joins the curated project with that name, or its project is promoted to that name. A hashed one pulls same-bytecode
    hooks together. Unassigned flagged hooks are left to rules 1-3."""
    marked = conn.execute(sa.select(hooks).where(hooks.c.regroup.is_(True)).order_by(hooks.c.chain_id, hooks.c.address)).all()
    by_norm = _projects_by_norm(conn)
    for h in marked:
        cur = conn.execute(sa.select(projects).join(hooks, hooks.c.project_id == projects.c.id)
                           .where(hooks.c.address == h.address, hooks.c.chain_id == h.chain_id)).first()
        if cur is None:
            continue
        name = _gname(h, generic)
        norm = normalize_name(name)
        if norm and _provisional(cur.name, cur.norm_name):
            target = by_norm.get(norm)
            if target is not None and target != cur.id:
                _move(conn, ctx, h, cur.id, target, "regroup:name", out)
            else:
                _promote(conn, ctx, cur, name, norm)
                by_norm[norm] = cur.id
                out["promoted"] += 1
        if c := _code(h):                                # after the name move, so clones follow it
            _regroup_by_code(conn, ctx, c, out)
    if marked:
        conn.execute(sa.update(hooks).values(regroup=False).where(hooks.c.regroup.is_(True)))


def _fetch_code_hashes(conn, ctx, unassigned) -> int:
    """Fill code_hash for hooks missing one, unassigned hooks first, then assigned ones (both sides of a
    code_hash join need a hash). Capped by ctx.max_code_fetch per run; the rest catch up on later nights."""
    bs = ctx.client("blockscout")
    if bs is None:
        return 0
    pending = [h for h in unassigned if not h.code_hash]
    pending += conn.execute(sa.select(hooks).where(hooks.c.project_id.is_not(None), hooks.c.code_hash.is_(None))
                            .order_by(hooks.c.first_seen_at.desc())).all()
    n = 0
    for h in pending[: ctx.max_code_fetch]:
        info = bs.fetch_code(h.chain_id, h.address)
        n += 1
        if info and info.code_hash:
            vals = {"code_hash": info.code_hash, "is_proxy": info.is_proxy, "proxy_kind": info.proxy_kind,
                    "regroup": h.project_id is not None}
            if info.verified:
                vals["verified_source"] = True
            conn.execute(sa.update(hooks).values(**vals).where(hooks.c.address == h.address, hooks.c.chain_id == h.chain_id))
    return n


# --- Claude classification cost controls -------------------------------------------------------------
SHORTLIST_N = 25            # candidate project names sent per call (was every name, up to 400)
PROMPT_CHARS_MAX = 2500     # worst-case prompt size with the shortlist, used by the cost preview


def claude_cap() -> int:
    """Max Claude classifications per run (GROUP_CLAUDE_MAX, default 100); the rest wait for later runs."""
    import os
    try:
        return max(0, int(os.environ.get("GROUP_CLAUDE_MAX", "") or 100))
    except ValueError:
        return 100


def shortlist(name: str, description: str, known: list[str], n: int = SHORTLIST_N) -> list[str]:
    """The n known project names closest to this hook's name/description; empty when nothing is close.
    Unnamed hooks with no description have nothing to match on, so they get an empty list (Claude names them)."""
    import difflib
    probe = f"{name} {description}".strip().lower()
    if not probe or not known:
        return []
    scored = []
    for k in known:
        kl = k.lower()
        r = difflib.SequenceMatcher(None, probe[:120], kl).ratio()
        if kl and (kl in probe or probe.split()[0] in kl):
            r += 1.0
        scored.append((r, k))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [k for r, k in scored[:n] if r >= 0.35]


def claude_candidates_count(conn) -> int:
    """Upper bound on hooks that would reach Claude now: unassigned, unnamed, but with a description to work from.
    (Named hooks are grouped by name for free; hooks with neither name nor description are never sent.)"""
    return conn.execute(sa.select(sa.func.count()).select_from(hooks).where(
        hooks.c.project_id.is_(None), sa.or_(hooks.c.name.is_(None), hooks.c.name == ""),
        hooks.c.description.is_not(None), hooks.c.description != "")).scalar_one()
