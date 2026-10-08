"""Step "radar": what is being built before it has volume. Writes radar_items (one row per URL), read by web /building.

Sources, each capped and each skipped with a log line when its key is missing; a failing source is logged and counted in
source_errors (it never fails the run):
  1. registry_submission: Uniswap/hooklist issues + PRs opened in the last 30 days, newest first, RADAR_REGISTRY_PAGES pages
     of 100 (GITHUB_TOKEN, or the anonymous API when KEYLESS is not 0). A bot PR that "Closes #N" is folded into issue N.
  2. repo: GitHub repository + code search, RADAR_GITHUB_QUERIES search calls per run (code search needs GITHUB_TOKEN).
  3. hackathon / incubator / audit / launch_post: Exa (last 60 days, RADAR_EXA_QUERIES queries), classified by Claude Haiku
     (RADAR_CLASSIFY_MAX per run). URLs already stored only get last_seen bumped.
  4. Retention: rows with last_seen older than RETENTION_DAYS are deleted.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta

import sqlalchemy as sa

from ..clients.github import GitHub, parse_repo
from ..db import as_utc, hooks, projects, radar_items, upsert
from . import RunContext, log

RETENTION_DAYS = 90
REGISTRY_DAYS = 30
REPO_DAYS = 30
REPO_FRESH_DAYS = 14
EXA_DAYS = 60
MIN_SCORE = 0.5
TEMPLATE_WORDS = ("template", "tutorial", "workshop", "course")
TEMPLATE_STAR_PASS = 20

HOOKLIST_OWNER, HOOKLIST_REPO = "Uniswap", "hooklist"

# Every chain the hooklist registry uses (hook.chain -> chainId in hooklist.json, 2026-10), plus a few spellings.
HOOKLIST_CHAIN_IDS = {
    "ethereum": 1, "mainnet": 1, "optimism": 10, "bnb": 56, "bsc": 56, "unichain": 130, "polygon": 137, "monad": 143,
    "xlayer": 196, "worldchain": 480, "world chain": 480, "soneium": 1868, "megaeth": 4326, "tempo": 4217,
    "robinhood": 4663, "arc": 5042, "base": 8453, "celo": 42220, "arbitrum": 42161, "avalanche": 43114, "ink": 57073,
    "linea": 59144, "blast": 81457, "zora": 7777777,
}

# GitHub search queries. {date} = pushed cutoff. Repo searches return full repo objects; code searches return files,
# whose repos are then resolved with one batched repo: search (counts against the same cap).
# Checked against live search 2026-10-08: README matches and the bare uniswap-v4 topic were mostly bots, SDKs and screeners.
REPO_QUERIES = [
    '"uniswap v4" hook in:name,description pushed:>{date} fork:false',
    "topic:uniswap-v4-hooks pushed:>{date}",
    "hook in:name,description topic:uniswap-v4 pushed:>{date}",
    "topic:uniswap-v4-hook pushed:>{date}",
]
MAX_PER_OWNER = 3   # launchpads that mint one repo per token launch would otherwise flood the list
CODE_QUERIES = [
    "BaseHook v4-core language:Solidity",
    "getHookPermissions v4-periphery language:Solidity",
]

# Exa queries with the kind they usually surface (Claude decides the stored kind).
EXA_QUERIES = [
    ("ETHGlobal showcase project built a Uniswap v4 hook", "hackathon"),
    ("Uniswap Hook Incubator UHI cohort demo day hook project", "incubator"),
    ("Uniswap v4 hook security audit report", "audit"),
    ("Uniswap v4 hook audit Cantina Spearbit OpenZeppelin Certora Code4rena Sherlock", "audit"),
    ("new Uniswap v4 hook launch announcement", "launch_post"),
    ("we just launched our Uniswap v4 hook on mainnet", "launch_post"),
]

# GitHub search rate limits: 30/min authenticated, 10/min anonymous; code search 10/min.
SEARCH_SPACING_AUTH = 2.1
SEARCH_SPACING_ANON = 6.5
CODE_SPACING = 6.5

SLEEP = time.sleep   # tests replace this


def _cap(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


def run(conn: sa.Connection, ctx: RunContext) -> dict:
    out = {"registry": 0, "repos": 0, "exa_new": 0, "classified": 0, "dropped": 0, "bumped": 0, "github_calls": 0,
           "exa_calls": 0, "linked": 0, "pruned": 0, "source_errors": 0}
    gh = _github(ctx)
    for name, fn in (("registry", _registry), ("repos", _repos), ("exa", _exa)):
        try:
            fn(conn, ctx, gh, out)
        except Exception as e:  # one source never takes down the others or the run
            out["source_errors"] += 1
            log.warning("radar source=%s failed: %s", name, e)
    out["linked"] = _link_projects(conn)
    cutoff = ctx.now - timedelta(days=RETENTION_DAYS)
    out["pruned"] = conn.execute(sa.delete(radar_items).where(radar_items.c.last_seen < cutoff)).rowcount or 0
    return out


def _github(ctx: RunContext):
    gh = ctx.client("github")
    if gh is not None:
        return gh
    if os.environ.get("KEYLESS", "1") != "0":
        log.info("radar: no GITHUB_TOKEN, using the anonymous GitHub API (registry + repo search, no code search)")
        return GitHub.anonymous()
    log.info("radar: no GITHUB_TOKEN and KEYLESS=0, GitHub sources skipped")
    return None


def _existing(conn, urls: list[str]) -> set[str]:
    if not urls:
        return set()
    return set(conn.execute(sa.select(radar_items.c.url).where(radar_items.c.url.in_(urls))).scalars())


def _write(conn, ctx: RunContext, rows: list[dict]) -> int:
    """Upsert GitHub-sourced rows (deduped by URL). first_seen is set once; everything else refreshes."""
    by_url: dict[str, dict] = {}
    for r in rows:
        by_url[r["url"]] = r
    rows = [{**r, "first_seen": ctx.now, "last_seen": ctx.now} for r in by_url.values()]
    for i in range(0, len(rows), 200):
        upsert(conn, radar_items, rows[i:i + 200], keys=["url"],
               update=[c for c in rows[0] if c not in ("url", "first_seen", "project_slug")])
    return len(rows)


# ---------- 1. registry submissions ----------

_SECTION = re.compile(r"^###\s*(.+?)\s*$", re.M)
_ADDR = re.compile(r"0x[0-9a-fA-F]{40}(?![0-9a-fA-F])")
_CLOSES = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", re.I)
_PR_PATH = re.compile(r"hooks/([a-z0-9_-]+)/(0x[0-9a-fA-F]{40})")
_TITLE_PAREN_CHAIN = re.compile(r"\(([A-Za-z][\w ]*)\)\s*$")
_TITLE_ON_CHAIN = re.compile(r"\bon\s+([A-Za-z][\w]*)\s*$", re.I)
_NO_RESPONSE = {"_no response_", "none", "n/a", "-", ""}


def parse_form(body: str | None) -> dict[str, str]:
    """GitHub issue-form body ("### Chain\\n\\nbase\\n\\n### Hook Address ...") -> {"chain": "base", "hook address": ...}."""
    body = body or ""
    out: dict[str, str] = {}
    heads = list(_SECTION.finditer(body))
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
        val = body[m.end():end].strip()
        if val.lower() not in _NO_RESPONSE:
            out[m.group(1).strip().lower()] = val
    return out


def chain_id_of(name: str | None) -> int | None:
    if not name:
        return None
    n = name.strip().lower()
    if n.isdigit():
        return int(n)
    return HOOKLIST_CHAIN_IDS.get(n) or HOOKLIST_CHAIN_IDS.get(n.replace("-", " ")) or HOOKLIST_CHAIN_IDS.get(n.replace(" ", ""))


def parse_submission(item: dict) -> dict:
    """chain_id, hook_address, name, description from a hooklist issue (form) or PR (title + file path)."""
    title = (item.get("title") or "").strip()
    body = item.get("body") or ""
    form = parse_form(body)
    chain = form.get("chain")
    addr_m = _ADDR.search(form.get("hook address", "")) or None
    if "pull_request" in item:
        pm = _PR_PATH.search(body)
        if pm:
            chain = chain or pm.group(1)
            addr_m = addr_m or _ADDR.search(pm.group(2))
    if not chain:
        m = _TITLE_PAREN_CHAIN.search(title) or _TITLE_ON_CHAIN.search(title)
        chain = m.group(1) if m else None
    addr = addr_m.group(0) if addr_m else None
    if not addr:
        m = _ADDR.search(title) or (_ADDR.search(body) if "pull_request" in item else None)
        addr = m.group(0) if m else None
    name = form.get("hook name")
    if not name:
        name = re.sub(r"^hook:\s*", "", title, flags=re.I)
        name = re.sub(r"^add\s+", "", name, flags=re.I)
        name = _TITLE_PAREN_CHAIN.sub("", name)
        name = re.sub(r"\s+(?:hook\s+)?on\s+[A-Za-z]\w*\s*$", "", name, flags=re.I).strip() or title
    desc = form.get("description")
    if not desc and "pull_request" in item:
        m = re.search(r"##\s*Summary\s*\n+(.+?)(?:\n##|\Z)", body, re.S)
        desc = m.group(1).strip() if m else None
    return {"chain_id": chain_id_of(chain), "hook_address": addr.lower() if addr else None,
            "name": name[:200], "description": (desc or "").strip()}


def _dt(s) -> datetime | None:
    if not s:
        return None
    try:
        return as_utc(datetime.fromisoformat(str(s).replace("Z", "+00:00")))
    except ValueError:
        return None


def _state(item: dict) -> str:
    pr = item.get("pull_request") or {}
    if pr.get("merged_at"):
        return "merged"
    return "open" if item.get("state") == "open" else "closed"


def _registry(conn, ctx: RunContext, gh, out: dict) -> None:
    if gh is None:
        log.info("radar registry skipped: no GitHub client")
        return
    pages = _cap("RADAR_REGISTRY_PAGES", 3)
    cutoff = ctx.now - timedelta(days=REGISTRY_DAYS)
    items: list[dict] = []
    for page in range(1, pages + 1):
        batch = gh.issues(HOOKLIST_OWNER, HOOKLIST_REPO, page=page, per_page=100)
        out["github_calls"] += 1
        fresh = [i for i in batch if (_dt(i.get("created_at")) or ctx.now) >= cutoff]
        items += fresh
        if len(batch) < 100 or len(fresh) < len(batch):
            break
    out["registry"] = _write(conn, ctx, registry_rows(items))


def registry_rows(items: list[dict]) -> list[dict]:
    """One row per submission. A PR that closes an issue in the same batch is folded into that issue's state."""
    issues = {i["number"]: i for i in items if "pull_request" not in i and "number" in i}
    pr_state: dict[int, str] = {}
    standalone = []
    for i in items:
        if "pull_request" not in i:
            continue
        closes = [int(n) for n in _CLOSES.findall(i.get("body") or "")]
        linked = [n for n in closes if n in issues]
        if linked:
            for n in linked:
                st = _state(i)
                if pr_state.get(n) != "merged":
                    pr_state[n] = st
        else:
            standalone.append(i)
    rows = []
    for i in list(issues.values()) + standalone:
        sub = parse_submission(i)
        st = _state(i)
        if "pull_request" not in i:
            linked = pr_state.get(i["number"])
            if linked == "merged":
                st = "merged"
            elif linked == "open" and st == "open":
                st = "open, PR in review"
        label = st[0].upper() + st[1:]
        desc = sub["description"]
        summary = f"{label}. {desc}" if desc else f"{label} registry submission."
        if len(summary) > 280:
            summary = summary[:277].rstrip() + "..."
        rows.append({
            "url": i["html_url"], "kind": "registry_submission", "title": sub["name"], "summary": summary,
            "source": "github.com/Uniswap/hooklist", "repo": None, "stars": None,
            "chain_id": sub["chain_id"], "hook_address": sub["hook_address"],
            "published_at": _dt(i.get("created_at")), "score": None,
        })
    return rows


# ---------- 2. repos ----------

def keep_repo(r: dict, now: datetime) -> bool:
    """Drop forks, archived, Uniswap's own repos, stale pushes, unstarred-and-quiet repos and small templates."""
    if r.get("fork") or r.get("archived"):
        return False
    owner = ((r.get("owner") or {}).get("login") or (r.get("full_name") or "").split("/")[0]).lower()
    if owner == "uniswap":
        return False
    stars = int(r.get("stargazers_count") or 0)
    pushed = _dt(r.get("pushed_at"))
    if pushed is None or pushed < now - timedelta(days=REPO_DAYS):
        return False
    if stars < 1 and pushed < now - timedelta(days=REPO_FRESH_DAYS):
        return False
    name = (r.get("name") or "").lower()
    if any(w in name for w in TEMPLATE_WORDS) and stars <= TEMPLATE_STAR_PASS:
        return False
    return True


def per_owner(repos, n: int = MAX_PER_OWNER) -> list[dict]:
    """At most n repos per owner, most starred then most recently pushed."""
    def key(r):
        pushed = _dt(r.get("pushed_at"))
        return (int(r.get("stargazers_count") or 0), pushed.timestamp() if pushed else 0.0)
    ranked = sorted(repos, key=key, reverse=True)
    seen: dict[str, int] = {}
    out = []
    for r in ranked:
        owner = (r.get("full_name") or "").split("/")[0].lower()
        if seen.get(owner, 0) < n:
            seen[owner] = seen.get(owner, 0) + 1
            out.append(r)
    return out


def repo_row(r: dict) -> dict:
    return {
        "url": r["html_url"], "kind": "repo", "title": r.get("full_name") or r.get("name"),
        "summary": (r.get("description") or "").strip()[:300] or None, "source": "github",
        "repo": r.get("full_name"), "stars": int(r.get("stargazers_count") or 0), "chain_id": None,
        "hook_address": None, "published_at": _dt(r.get("pushed_at")), "score": None,
    }


def _repos(conn, ctx: RunContext, gh, out: dict) -> None:
    if gh is None:
        log.info("radar repos skipped: no GitHub client")
        return
    budget = _cap("RADAR_GITHUB_QUERIES", 8)
    authed = bool(getattr(gh, "authenticated", True))
    spacing = SEARCH_SPACING_AUTH if authed else SEARCH_SPACING_ANON
    date = (ctx.now - timedelta(days=REPO_DAYS)).strftime("%Y-%m-%d")
    found: dict[str, dict] = {}
    used = 0

    def call(fn, q, wait):
        nonlocal used
        if used:
            SLEEP(wait)
        used += 1
        out["github_calls"] += 1
        return fn(q)

    for q in REPO_QUERIES:
        if used >= budget:
            break
        try:
            for r in call(gh.search_repos, q.format(date=date), spacing):
                if r.get("html_url"):
                    found.setdefault(r["html_url"], r)
        except Exception as e:
            out["source_errors"] += 1
            log.warning("radar repo search failed q=%r: %s", q, e)
    code_repos: list[str] = []
    if authed:
        for q in CODE_QUERIES:
            if used >= budget:
                break
            try:
                for hit in call(gh.search_code, q, CODE_SPACING):
                    full = (hit.get("repository") or {}).get("full_name")
                    if full and f"https://github.com/{full}" not in found and full not in code_repos:
                        code_repos.append(full)
            except Exception as e:
                out["source_errors"] += 1
                log.warning("radar code search failed q=%r: %s", q, e)
                break
    else:
        log.info("radar code search skipped: no GITHUB_TOKEN")
    # Resolve code-search repos (stars, pushed_at) with batched repo: qualifiers; GitHub caps a query at 256 chars.
    for chunk in _repo_chunks(code_repos):
        if used >= budget:
            break
        try:
            for r in call(gh.search_repos, chunk, spacing):
                if r.get("html_url"):
                    found.setdefault(r["html_url"], r)
        except Exception as e:
            out["source_errors"] += 1
            log.warning("radar repo resolve failed: %s", e)
    rows = [repo_row(r) for r in per_owner(r for r in found.values() if keep_repo(r, ctx.now))]
    out["repos"] = _write(conn, ctx, rows)
    log.info("radar repos: %d found, %d kept, %d search calls", len(found), len(rows), used)


def _repo_chunks(names: list[str], limit: int = 240) -> list[str]:
    chunks, cur = [], ""
    for n in names:
        part = f"repo:{n}"
        if cur and len(cur) + 1 + len(part) > limit:
            chunks.append(cur)
            cur = ""
        cur = f"{cur} {part}".strip()
    if cur:
        chunks.append(cur)
    return chunks


# ---------- 3. Exa + Claude ----------

def _exa(conn, ctx: RunContext, _gh, out: dict) -> None:
    exa = ctx.client("exa")
    if exa is None:
        log.info("radar exa skipped: no EXA_API_KEY")
        return
    claude = ctx.client("claude")
    if claude is None:
        log.info("radar exa skipped: no ANTHROPIC_API_KEY to classify results")
        return
    n_queries = _cap("RADAR_EXA_QUERIES", 6)
    max_classify = _cap("RADAR_CLASSIFY_MAX", 30)
    cands: dict[str, dict] = {}
    for q, hint in EXA_QUERIES[:n_queries]:
        try:
            hits = exa.search_recent(q, days=EXA_DAYS, n=10, now=ctx.now)
        except Exception as e:
            out["source_errors"] += 1
            log.warning("radar exa query failed q=%r: %s", q, e)
            continue
        out["exa_calls"] += 1
        for h in hits:
            url = (h.get("url") or "").split("#")[0]
            if url:
                cands.setdefault(url, {**h, "url": url, "hint": hint})
    have = _existing(conn, list(cands))
    if have:
        conn.execute(sa.update(radar_items).where(radar_items.c.url.in_(list(have))).values(last_seen=ctx.now))
        out["bumped"] = len(have)
    new = [c for u, c in cands.items() if u not in have]
    rows = []
    for c in new[:max_classify]:
        try:
            res = claude.classify_radar({"title": c.get("title"), "url": c["url"], "source": c.get("source"),
                                         "published": c.get("published_at"), "highlights": c.get("highlights") or [],
                                         "query_hint": c["hint"]})
        except Exception as e:
            out["source_errors"] += 1
            log.warning("radar classify failed url=%s: %s", c["url"], e)
            continue
        out["classified"] += 1
        kind = res.get("kind") if res.get("kind") in ("hackathon", "incubator", "audit", "launch_post") else c["hint"]
        score = float(res.get("score") or 0)
        if score < MIN_SCORE:
            out["dropped"] += 1
        rows.append({
            "url": c["url"], "kind": kind, "title": (c.get("title") or "")[:300] or None,
            "summary": (res.get("summary") or "").strip()[:300] or None, "source": c.get("source"),
            "repo": _repo_of(c["url"]), "stars": None, "chain_id": None, "hook_address": _hook_of(c),
            "published_at": c.get("published_at"), "score": score, "first_seen": ctx.now, "last_seen": ctx.now,
        })
    if len(new) > max_classify:
        log.info("radar classify cap %d reached, %d left for the next run", max_classify, len(new) - max_classify)
    if rows:
        upsert(conn, radar_items, rows, keys=["url"], update=[])   # new URLs only; a race just keeps the first row
    out["exa_new"] = len(rows) - out["dropped"]


def _repo_of(url: str) -> str | None:
    if "github.com/" not in url:
        return None
    p = parse_repo(url)
    return f"{p[0]}/{p[1]}" if p and p[1] else None


def _hook_of(c: dict) -> str | None:
    m = _ADDR.search(" ".join([c.get("title") or "", *(c.get("highlights") or [])]))
    return m.group(0).lower() if m else None


# ---------- project links ----------

def _link_projects(conn) -> int:
    """project_slug from hooks.project_id (registry rows, by address + chain; web rows by address on any chain) and from
    projects.github (repo rows). Recomputed every run, so a hook grouped later still links."""
    items = conn.execute(sa.select(radar_items.c.url, radar_items.c.hook_address, radar_items.c.chain_id,
                                   radar_items.c.repo, radar_items.c.project_slug)).all()
    addrs = {r.hook_address for r in items if r.hook_address}
    by_hook: dict[tuple[str, int], str] = {}
    by_addr: dict[str, str] = {}
    if addrs:
        q = (sa.select(hooks.c.address, hooks.c.chain_id, projects.c.slug)
             .join(projects, projects.c.id == hooks.c.project_id).where(hooks.c.address.in_(list(addrs))))
        for a, cid, slug in conn.execute(q):
            by_hook[(a.lower(), cid)] = slug
            by_addr.setdefault(a.lower(), slug)
    by_repo: dict[str, str] = {}
    if any(r.repo for r in items):
        for slug, gh_url in conn.execute(sa.select(projects.c.slug, projects.c.github).where(projects.c.github.is_not(None))):
            p = parse_repo(gh_url)
            if p and p[1]:
                by_repo[f"{p[0]}/{p[1]}".lower()] = slug
    n = 0
    for r in items:
        slug = None
        if r.hook_address:
            slug = by_hook.get((r.hook_address, r.chain_id)) if r.chain_id else None
            slug = slug or (by_addr.get(r.hook_address) if r.chain_id is None else None)
        if not slug and r.repo:
            slug = by_repo.get(r.repo.lower())
        if slug != r.project_slug:
            conn.execute(sa.update(radar_items).where(radar_items.c.url == r.url).values(project_slug=slug))
        n += slug is not None
    return n
