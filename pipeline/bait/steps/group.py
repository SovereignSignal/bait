"""Step 4 (group): hooks -> projects. Rule 1 hooklist name (generic names excluded; a same-chain verified-name project
is adopted only with evidence), rule 2 code_hash or deployer, rule 3 Claude for the long tail. Flagged hooks are
regrouped first; moved and renamed projects leave aliases. is_factory when a project has > 20 hooks."""
from __future__ import annotations

import os
import time

import sqlalchemy as sa

from ..db import backfill_address_flags, hooks, project_aliases, projects, utcnow
from ..rules import SHARED_DEPLOYERS, code_evidence, merge_evidence, normalize_name, slugify, useful_name
from . import RunContext, log
from .names import add_alias, backfill_unlisted_aliases, hook_rows, retire_project, unique_slug

FACTORY_MIN_HOOKS = 21


def run(conn: sa.Connection, ctx: RunContext) -> dict:
    out = {"by_name": 0, "by_code": 0, "by_deployer": 0, "by_claude": 0, "created": 0, "unassigned": 0,
           "code_fetched": 0, "factories": 0, "split": 0, "promoted": 0, "moved": 0, "retired": 0, "adopted": 0,
           "code_refreshed": 0, "code_missing": 0}
    out["flags_backfilled"] = backfill_address_flags(conn)
    _refresh_code_facts(conn, ctx, out)
    out["aliases_backfilled"] = backfill_unlisted_aliases(conn, ctx.now)
    generic = _data_generic_norms(conn)
    out["split"] = _split_generic(conn, ctx, generic) + _split_proxy_families(conn, ctx)
    _regroup(conn, ctx, out, generic)
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
        unassigned = still

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
    from . import names
    names.run(conn, ctx, out)

    # Factories: > 20 hooks.
    counts = conn.execute(sa.select(hooks.c.project_id, sa.func.count()).where(hooks.c.project_id.is_not(None)).group_by(hooks.c.project_id)).all()
    factory_ids = [pid for pid, n in counts if n >= FACTORY_MIN_HOOKS]
    conn.execute(sa.update(projects).values(is_factory=False).where(projects.c.is_factory.is_(True)))
    if factory_ids:
        conn.execute(sa.update(projects).values(is_factory=True).where(projects.c.id.in_(factory_ids)))
    out["factories"] = len(factory_ids)
    return out


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
    """Hooklist names that unrelated teams share: at least two entries with a known deployer, all of those deployers
    different, and no non-proxy code_hash shared between entries. Unknown deployers prove nothing either way, and a name
    on FACTORY_MIN_HOOKS or more hooks is a factory family by design, so neither makes a name generic."""
    by: dict[str, list] = {}
    for r in conn.execute(sa.select(hooks.c.name, hooks.c.deployer, hooks.c.code_hash, hooks.c.is_proxy)
                          .where(hooks.c.name.is_not(None))):
        if norm := normalize_name(useful_name(r.name, max_len=None)):
            by.setdefault(norm, []).append(r)
    out = set()
    for norm, rows in by.items():
        if not 2 <= len(rows) < FACTORY_MIN_HOOKS:
            continue
        deps = [r.deployer for r in rows if r.deployer and r.deployer not in SHARED_DEPLOYERS]
        codes = [c for r in rows if (c := code_evidence(r))]
        if len(deps) >= 2 and len(set(deps)) == len(deps) and len(set(codes)) == len(codes):
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
    need = sa.or_(sa.func.length(hooks.c.code_hash) == 40, sa.and_(hooks.c.code_hash.is_not(None), hooks.c.is_proxy.is_(None)))
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
            vals = {"code_hash": None, "is_proxy": None}
            out["code_missing"] += 1
        else:
            h, proxy = got[key]
            vals = {"code_hash": h, "is_proxy": proxy}
            if r.project_id is not None and (h != r.code_hash or not proxy):
                vals["regroup"] = True
            if r.contract_name is not None and useful_name(r.contract_name) is None:
                vals.update(contract_name=None, name_checked_at=None)
            out["code_refreshed"] += 1
        conn.execute(sa.update(hooks).values(**vals).where(hooks.c.address == r.address, hooks.c.chain_id == r.chain_id))
    log.info("group: code facts for %d of %d hooks, %d without code", out["code_refreshed"], len(todo), out["code_missing"])


def _split_proxy_families(conn, ctx) -> int:
    """Auto-named projects (bytecode families, verified-name merges) that joined proxies by shared bytecode before proxy
    status was known. If a proxy hook has no evidence (non-proxy code, a real deployer) tying it to the rest of its
    project, the whole project goes back through the rules. No alias: its hooks may land in several projects."""
    rows = conn.execute(
        sa.select(hooks.c.project_id, hooks.c.code_hash, hooks.c.is_proxy, hooks.c.deployer, hooks.c.name,
                  hooks.c.contract_name, projects.c.name.label("pname"), projects.c.norm_name.label("pnorm"))
        .join(projects, projects.c.id == hooks.c.project_id)
        .where(hooks.c.project_id.in_(sa.select(hooks.c.project_id).where(hooks.c.is_proxy.is_(True))))).all()
    by: dict[int, list] = {}
    for r in rows:
        if _provisional(r.pname, r.pnorm):
            by.setdefault(r.project_id, []).append(r)
    bad = [pid for pid, hs in sorted(by.items())
           if len(hs) > 1 and any(h.is_proxy and not merge_evidence([h], [o for o in hs if o is not h]) for h in hs)]
    for pid in bad:
        conn.execute(sa.update(hooks).values(project_id=None).where(hooks.c.project_id == pid))
        retire_project(conn, pid, into=None, reason="split:proxy", now=ctx.now)
    if bad:
        log.info("group: split %d auto-named projects that joined proxies by bytecode", len(bad))
    return len(bad)


def _split_generic(conn, ctx, generic: set[str]) -> int:
    """Projects keyed on a template, proxy or data-generic name ("ERC1967Proxy", "LimitOrderHook") merged unrelated hooks
    by name alone. Detach their hooks so the rules regroup them on evidence; the project goes with no alias (its hooks
    now live in several)."""
    bad = [p.id for p in conn.execute(sa.select(projects.c.id, projects.c.name, projects.c.norm_name)
                                      .where(projects.c.norm_name.is_not(None)))
           if not p.norm_name.startswith(("contract:", "code:"))
           and (not useful_name(p.name, max_len=None) or p.norm_name in generic)]
    for pid in bad:
        conn.execute(sa.update(hooks).values(project_id=None).where(hooks.c.project_id == pid))
        retire_project(conn, pid, into=None, reason="split:generic", now=ctx.now)
    if bad:
        log.info("group: split %d projects keyed on a template or proxy name", len(bad))
    return len(bad)


def _join_targets(conn) -> tuple[dict, dict]:
    """code_hash -> project and (chain, deployer) -> project from assigned hooks. Proxy bytecode is shared by unrelated
    deployments and CREATE2 factories deploy for everyone, so neither is evidence."""
    by_hash: dict[str, int] = {}
    by_dep: dict[tuple[int, str], int] = {}
    for r in conn.execute(sa.select(hooks.c.code_hash, hooks.c.is_proxy, hooks.c.deployer, hooks.c.chain_id,
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
        sa.select(hooks.c.address, hooks.c.chain_id, hooks.c.code_hash, hooks.c.is_proxy, hooks.c.project_id,
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
        elif c := _code(h):
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
            vals = {"code_hash": info.code_hash, "is_proxy": info.is_proxy, "regroup": h.project_id is not None}
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
