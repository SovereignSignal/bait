"""Bytecode template families (rules.code_shape, group rule 2b): a launchpad's per-token hooks differ only in immutables
(PUSH32) and metadata, so they share a shape on one chain and fold into one project; different code never does."""
import sqlalchemy as sa

from bait import db
from bait.rules import code_shape
from bait.steps import group
from tests.conftest import NOW, insert_hook, insert_project

META = "a2646970667358221220" + "11" * 32 + "64736f6c63430008180033"     # CBOR {ipfs, solc} + its length 0x0033


def runtime(immutable: str, selector: str = "a9059cbb", meta_byte: str = "11") -> str:
    """PUSH4 selector, PUSH32 immutable, PUSH20 constant address, STOP, then solc metadata."""
    return ("0x63" + selector + "7f" + immutable.rjust(64, "0") + "73" + "ab" * 20 + "00"
            + META.replace("11" * 32, meta_byte * 32))


def test_code_shape_ignores_immutables_and_metadata_only():
    a, b = runtime("01"), runtime("02", meta_byte="22")
    assert code_shape(a) == code_shape(b) and a != b
    assert code_shape(runtime("01", selector="095ea7b3")) != code_shape(a)          # another function: other code
    assert code_shape(runtime("01").replace("ab" * 20, "cd" * 20)) != code_shape(a)  # another constant address
    assert code_shape("0x") is None and code_shape(None) is None and code_shape("0xzz") is None
    assert code_shape("0x7f01") is not None                                          # a truncated PUSH32 never raises


def _hook(conn, addr, chain, shape, pid=None, proxy=False):
    insert_hook(conn, addr, chain, project_id=pid, code_hash="0xh" + addr)
    conn.execute(sa.update(db.hooks).where(db.hooks.c.address == addr, db.hooks.c.chain_id == chain)
                 .values(code_shape=shape, is_proxy=proxy))


def _unlisted(conn, addr, chain, shape, name=None, proxy=False):
    pid = insert_project(conn, name or f"Unlisted hook {addr[:6]}…{addr[-4:]}", NOW, slug=f"u-{addr}-{chain}")
    conn.execute(sa.update(db.projects).where(db.projects.c.id == pid).values(norm_name=None))
    _hook(conn, addr, chain, shape, pid, proxy)
    return pid


def _pid(conn, addr, chain):
    return conn.execute(sa.select(db.hooks.c.project_id).where(db.hooks.c.address == addr, db.hooks.c.chain_id == chain)).scalar_one()


def test_unlisted_template_siblings_fold_into_one_family(conn, ctx):
    p1 = _unlisted(conn, "0xa1", 4663, "0xS")
    p2 = _unlisted(conn, "0xa2", 4663, "0xS")
    p3 = _unlisted(conn, "0xa3", 4663, "0xS")
    other_chain = _unlisted(conn, "0xa4", 1, "0xS")           # same template elsewhere: its own project
    proxy = _unlisted(conn, "0xa5", 4663, "0xS", proxy=True)  # proxy bytecode is never evidence
    out = group.run(conn, ctx)
    assert out["shape_merged"] == 2
    fam = conn.execute(sa.select(db.projects).where(db.projects.c.id == p1)).one()
    assert fam.name == "Unlisted hook family 0xa1…" and fam.slug == "unlisted-hook-family-0xa1"
    assert {_pid(conn, a, 4663) for a in ("0xa1", "0xa2", "0xa3")} == {p1}
    assert _pid(conn, "0xa4", 1) == other_chain and _pid(conn, "0xa5", 4663) == proxy
    aliases = dict(conn.execute(sa.select(db.project_aliases.c.old_slug, db.project_aliases.c.project_id)).all())
    assert aliases["u-0xa2-4663"] == aliases["u-0xa3-4663"] == aliases["u-0xa1-4663"] == p1     # old links redirect
    assert conn.execute(sa.select(sa.func.count()).select_from(db.projects).where(db.projects.c.id.in_([p2, p3]))).scalar_one() == 0
    assert group.run(conn, ctx)["shape_merged"] == 0                                             # idempotent


def test_one_verified_name_takes_the_family_two_names_stay_apart(conn, ctx):
    named = _unlisted(conn, "0xb1", 1, "0xT", name="MemeLaunchHook")
    conn.execute(sa.update(db.projects).where(db.projects.c.id == named).values(norm_name="contract:1:memelaunch"))
    _unlisted(conn, "0xb2", 1, "0xT")
    _unlisted(conn, "0xb3", 1, "0xT")
    group.run(conn, ctx)
    assert _pid(conn, "0xb2", 1) == _pid(conn, "0xb3", 1) == named

    a = _unlisted(conn, "0xc1", 1, "0xU", name="AlphaHook")
    b = _unlisted(conn, "0xc2", 1, "0xU", name="BetaHook")
    for pid, n in ((a, "alpha"), (b, "beta")):
        conn.execute(sa.update(db.projects).where(db.projects.c.id == pid).values(norm_name=f"contract:1:{n}"))
    _unlisted(conn, "0xc3", 1, "0xU")
    _unlisted(conn, "0xc4", 1, "0xU")
    group.run(conn, ctx)
    assert _pid(conn, "0xc1", 1) == a and _pid(conn, "0xc2", 1) == b
    assert _pid(conn, "0xc3", 1) == _pid(conn, "0xc4", 1) not in (a, b)


def test_curated_projects_never_take_part(conn, ctx):
    curated = insert_project(conn, "Zorb", NOW)
    _hook(conn, "0xd1", 1, "0xV", curated)
    lone = _unlisted(conn, "0xd2", 1, "0xV")
    group.run(conn, ctx)
    assert _pid(conn, "0xd2", 1) == lone


def test_new_hooks_join_their_template_or_form_a_family(conn, ctx):
    fam = _unlisted(conn, "0xe1", 4663, "0xW")
    _hook(conn, "0xe2", 4663, "0xW")                # unassigned, same template: joins
    _hook(conn, "0xf1", 4663, "0xX")                # two unassigned of a new template: a family
    _hook(conn, "0xf2", 4663, "0xX")
    _hook(conn, "0xf3", 4663, "0xY")                # alone: address card
    out = group.run(conn, ctx)
    assert _pid(conn, "0xe2", 4663) == fam and out["by_shape"] == 3
    f = conn.execute(sa.select(db.projects).where(db.projects.c.id == _pid(conn, "0xf1", 4663))).one()
    assert _pid(conn, "0xf2", 4663) == f.id and f.name == "Unlisted hook family 0xf1…"
    assert _pid(conn, "0xf3", 4663) not in (fam, f.id)


def test_fill_shapes_unassigned_first_capped_and_marks_no_code(conn, ctx, monkeypatch):
    _hook(conn, "0x01", 1, None, insert_project(conn, "Old", NOW))
    _hook(conn, "0x02", 1, None)
    _hook(conn, "0x03", 1, None, proxy=True)          # proxies never need a shape
    asked = []

    def fake(ctx, rows, deadline):
        asked.extend(r.address for r in rows)
        return {(r.chain_id, r.address): (None if r.address == "0x02" else "0xZ") for r in rows}
    monkeypatch.setattr(group, "_rpc_shapes", fake)
    monkeypatch.setenv("SHAPE_MAX", "1")
    group.run(conn, ctx)
    assert asked == ["0x02"]
    assert conn.execute(sa.select(db.hooks.c.code_shape).where(db.hooks.c.address == "0x02")).scalar_one() == ""
    group.run(conn, ctx)
    assert asked == ["0x02", "0x01"]
    group.run(conn, ctx)
    assert asked == ["0x02", "0x01"]
