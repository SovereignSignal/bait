"""Offline import of reviewed historical Swap observations; never recent activity or coverage.

The externally supplied SHA-256 pins the reviewed file, not on-chain authenticity. Version 1 accepts the v4
PoolManager Swap ABI and the input/output Swap ABI below (at any matching hook, never by project name). It retains
physical logs; score conservatively counts distinct positive pool transactions and max known USD per transaction.
No network, automatic schema bootstrap, receipt, scoring, enrichment or indexer writes occur in this command.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, localcontext
from pathlib import Path
from urllib.parse import urlsplit

import sqlalchemy as sa

from . import db
from .indexer.chains import CHAIN_BY_ID
from .indexer.decode import SWAP_TOPIC, decode_swap

# Swap(bytes32,address,bool,bool,uint256,uint256,uint160,int24,uint24,uint256)
# ABI source: https://github.com/Bunniapp/bunni-v2/blob/main/src/interfaces/IBunniHook.sol
# This is a format adapter, not a protocol/address exemption. Unknown ABIs require a reviewed adapter.
INPUT_OUTPUT_TOPIC = "0xcd42809a29fc60d050d9e34a2f48fe30855a6451eca6c8a61ca7f21e1881644d"
MAX_BYTES, MAX_EVENTS = 2_000_000, 1000
SCALE = Decimal("0.000000000000000001")


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _hex(value, size: int, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch("0x[0-9a-f]{" + str(size) + "}", value):
        raise ValueError(f"invalid {field}: expected lowercase hex")
    return value


def _sha(value) -> str:
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value):
        raise ValueError("invalid SHA-256")
    return value


def _int(value, field: str, minimum=0, maximum=2**63-1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"invalid {field}")
    return value


def _time(value) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be an explicit UTC string")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid UTC timestamp") from exc
    if result.tzinfo is None or result.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be explicit UTC")
    return result


def _url(value):
    parsed = urlsplit(value) if isinstance(value, str) else None
    if parsed is None or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("source must be an HTTPS URL without credentials")


def _decimal(value, field: str, *, positive=False) -> Decimal:
    try:
        if isinstance(value, bool):
            raise ValueError(field)
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or (positive and result == 0) or abs(result.adjusted()) > 100:
            raise ValueError(field)
        return result
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}") from exc


def _no_duplicate_keys(items):
    obj = {}
    for key, value in items:
        if key in obj:
            raise ValueError("duplicate JSON key")
        obj[key] = value
    return obj


def _uint(word: int, bits: int) -> bool:
    return 0 <= word < 2**bits


def _signed_word(word: int, bits: int) -> bool:
    return word < 2**(bits-1) or 2**256-2**(bits-1) <= word < 2**256


def load_bundle(path: str | Path, expected_sha256: str, *, now: datetime | None = None) -> dict:
    """Validate a pinned reviewed manifest without a database or network. No implicit acceptance flags."""
    expected_sha256 = _sha(expected_sha256)
    if Path(path).stat().st_size > MAX_BYTES:
        raise ValueError("bundle exceeds size limit")
    raw = Path(path).read_bytes()
    if len(raw) > MAX_BYTES or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("bundle size or reviewed SHA-256 mismatch")
    data = json.loads(raw, object_pairs_hook=_no_duplicate_keys,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON value")))
    if (not isinstance(data, dict) or data.get("schema_version") != 1 or isinstance(data.get("schema_version"), bool)
            or data.get("kind") != "historical_swap_evidence" or data.get("coverage") != "partial_observed_events"
            or "historical_total_usd" not in data or data["historical_total_usd"] is not None):
        raise ValueError("unsupported manifest or full historical total assertion")
    _sha(data.get("source_bundle_sha256"))
    events = data.get("events")
    if not isinstance(events, list) or not 1 <= len(events) <= MAX_EVENTS:
        raise ValueError("event count outside import limit")
    exclusions = data.get("exclusions")
    if not isinstance(exclusions, list):
        raise ValueError("exclusions must be explicit")
    excluded = set()
    for entry in exclusions:
        if (not isinstance(entry, dict) or not isinstance(entry.get("reason"), str) or not entry["reason"]
                or not isinstance(entry.get("event_id"), str)
                or not re.fullmatch(r"[1-9][0-9]*:0x[0-9a-f]{64}:[0-9]+", entry.get("event_id", ""))):
            raise ValueError("invalid exclusion")
        if entry["event_id"] in excluded:
            raise ValueError("duplicate exclusion")
        excluded.add(entry["event_id"])
    now = now or datetime.now(timezone.utc)
    rows, seen, transactions, blocks, hashes, positions = [], set(), {}, {}, {}, {}
    for event in events:
        row = _event(event, now)
        identity = f"{row['chain_id']}:{row['transaction_hash']}:{row['log_index']}"
        if identity in seen or identity in excluded:
            raise ValueError("duplicate or excluded event identity")
        seen.add(identity)
        tx_key = row["chain_id"], row["transaction_hash"]
        block = row["block_number"], row["block_hash"], row["block_timestamp"]
        if transactions.setdefault(tx_key, block) != block:
            raise ValueError("inconsistent transaction block identity")
        block_key = row["chain_id"], row["block_number"]
        if blocks.setdefault(block_key, block[1:]) != block[1:]:
            raise ValueError("inconsistent block identity")
        hash_key = row["chain_id"], row["block_hash"]
        if hashes.setdefault(hash_key, (block[0], block[2])) != (block[0], block[2]):
            raise ValueError("inconsistent block hash identity")
        position = row["chain_id"], row["block_hash"], row["log_index"]
        if positions.setdefault(position, row["transaction_hash"]) != row["transaction_hash"]:
            raise ValueError("block log position has conflicting transaction identity")
        evidence = {"event": event, "source_bundle_sha256": data["source_bundle_sha256"],
                    "coverage": data["coverage"], "historical_total_usd": None, "exclusions": exclusions}
        row.update(evidence=evidence, evidence_sha256=digest(evidence), bundle_sha256=expected_sha256, imported_at=now)
        rows.append(row)
    return {"sha256": expected_sha256, "rows": rows}


def _event(event, now):
    if not isinstance(event, dict):
        raise ValueError("invalid event")
    cid = _int(event.get("chain_id"), "chain", 1)
    chain = CHAIN_BY_ID.get(cid)
    if chain is None:
        raise ValueError("chain has no reviewed PoolManager deployment")
    pid = _hex(event.get("pool_id"), 64, "pool")
    hook = _hex(event.get("hook_address"), 40, "hook")
    tx = _hex(event.get("transaction_hash"), 64, "transaction")
    index = _int(event.get("log_index"), "log index", maximum=2**31-1)
    block = _int(event.get("block_number"), "block number")
    block_hash = _hex(event.get("block_hash"), 64, "block hash")
    timestamp = _time(event.get("block_timestamp"))
    if timestamp >= now - timedelta(days=14):
        raise ValueError("historical import requires events older than fourteen days")
    raw = event.get("raw_log")
    if not isinstance(raw, dict):
        raise ValueError("raw log is required")
    for name, value in (("transaction_hash", tx), ("log_index", index), ("block_number", block), ("block_hash", block_hash)):
        if raw.get(name) != value:
            raise ValueError("raw log identity mismatch")
    _int(raw.get("log_index"), "raw log index", maximum=2**31-1)
    _int(raw.get("block_number"), "raw block number")
    source = event.get("source")
    if not isinstance(source, dict) or source.get("kind") != "canonical_log_json" or source.get("sha256") != digest(raw):
        raise ValueError("raw log provenance/hash mismatch")
    _url(source.get("url"))
    if _time(source.get("observed_at")) < timestamp:
        raise ValueError("source observation precedes event")
    topics, payload = raw.get("topics"), raw.get("data")
    if not isinstance(topics, list) or len(topics) < 2 or topics[1] != pid:
        raise ValueError("event pool topic mismatch")
    topics = list(topics)
    while topics and topics[-1] is None:
        topics.pop()  # Blockscout pads its topic list; preserve the original hashed raw log.
    for topic in topics:
        _hex(topic, 64, "topic")
    if len(topics) != 3 or not topics[2].startswith("0x"+"0"*24):
        raise ValueError("Swap requires pool and canonically encoded sender topics")
    if not isinstance(payload, str) or not re.fullmatch(r"0x(?:[0-9a-f]{64}){1,16}", payload):
        raise ValueError("invalid event data")
    words = [int(payload[i:i+64], 16) for i in range(2, len(payload), 64)]
    kind = event.get("event_kind")
    if kind == "pool_manager_swap":
        if topics[0] != SWAP_TOPIC or raw.get("address") != chain.pool_manager or len(words) != 6:
            raise ValueError("invalid PoolManager Swap schema/emitter")
        if not (all(_signed_word(w, 128) for w in words[:2]) and _uint(words[2], 160) and _uint(words[3], 128)
                and _signed_word(words[4], 24) and _uint(words[5], 24)):
            raise ValueError("noncanonical PoolManager ABI integer")
        swap = decode_swap(raw)
        amount0, amount1 = abs(swap["amount0"]), abs(swap["amount1"])
        # Both zero is an alias observation, not positive material usage. One zero is insufficient for valuation.
        positive = amount0 > 0 or amount1 > 0
    elif kind == "custom_input_output_swap":
        if (topics[0] != INPUT_OUTPUT_TOPIC or raw.get("address") != hook or len(words) != 8
                or words[0] not in (0, 1) or words[1] not in (0, 1)):
            raise ValueError("invalid custom input/output Swap schema/emitter")
        if not (_uint(words[4], 160) and _signed_word(words[5], 24) and _uint(words[6], 24)):
            raise ValueError("noncanonical custom ABI integer")
        amount0, amount1 = (words[2], words[3]) if words[1] else (words[3], words[2])
        positive = amount0 > 0 and amount1 > 0
    else:
        raise ValueError("unsupported Swap ABI adapter")
    valuation = event.get("valuation")
    if "valuation" not in event:
        raise ValueError("unknown valuation must be explicit null")
    lower = None if valuation is None else _value(valuation, timestamp, chain.llama, amount0, amount1)
    if lower is not None and not positive:
        raise ValueError("zero-amount observation cannot carry priced usage")
    return {"chain_id": cid, "transaction_hash": tx, "log_index": index, "pool_id": pid,
            "hook_address": hook, "block_number": block, "block_hash": block_hash, "block_timestamp": timestamp,
            "event_kind": kind, "positive": positive, "usd_lower_bound": lower}


def _value(value, timestamp, chain_name, amount0, amount1):
    if not isinstance(value, dict) or value.get("method") != "min_historical_sides" or min(amount0, amount1) <= 0:
        raise ValueError("unsupported historical valuation")
    _url(value.get("quote_url"))
    if _time(value.get("observed_at")) < timestamp:
        raise ValueError("quote capture precedes event")
    raw = value.get("quote_response_utf8")
    if not isinstance(raw, str) or hashlib.sha256(raw.encode()).hexdigest() != _sha(value.get("quote_response_sha256")):
        raise ValueError("historical quote hash mismatch")
    response = json.loads(raw, object_pairs_hook=_no_duplicate_keys, parse_float=Decimal)
    quotes = response.get("coins") if isinstance(response, dict) else None
    if not isinstance(quotes, dict):
        raise ValueError("historical quote has no token map")
    sides = []
    for side, amount in enumerate((amount0, amount1)):
        currency = _hex(value.get(f"currency{side}"), 40, "currency")
        quote = quotes.get(f"{chain_name}:{currency}")
        if not isinstance(quote, dict):
            raise ValueError("quote has no matching chain/token")
        decimals = _int(quote.get("decimals"), "token decimals", 0, 36)
        price = _decimal(quote.get("price"), "historical price", positive=True)
        confidence = _decimal(quote.get("confidence"), "quote confidence")
        ts = _int(quote.get("timestamp"), "quote timestamp")
        if confidence < Decimal("0.95") or confidence > 1 or not 0 <= timestamp.timestamp()-ts <= 300:
            raise ValueError("historical quote is stale, future or low-confidence")
        for field, expected in (("decimals", decimals), ("price", price), ("timestamp", ts), ("confidence", confidence)):
            if _decimal(value.get(f"{field}{side}"), field) != expected:
                raise ValueError("normalized quote differs from source")
        with localcontext() as context:
            context.prec = 120
            sides.append(Decimal(amount) / Decimal(10)**decimals * price)
    if min(sides) >= Decimal(10)**20:
        raise ValueError("USD contribution exceeds storage")
    with localcontext() as context:
        context.prec = 120
        lower = min(sides).quantize(SCALE, rounding=ROUND_DOWN)
    if _decimal(value.get("usd_lower_bound"), "USD lower bound") != lower:
        raise ValueError("USD lower bound differs from source calculation or exceeds storage")
    return lower


def import_bundle(conn, bundle: dict, *, apply=False) -> dict:
    """Caller owns transaction/lock. Validate all identity/replay conditions before the first insert."""
    table, pending, unchanged = db.historical_swap_evidence, [], 0
    project_ids, transactions = set(), {}
    incoming_ids = {f"{r['chain_id']}:{r['transaction_hash']}:{r['log_index']}" for r in bundle["rows"]}
    incoming_excluded = {e["event_id"] for r in bundle["rows"] for e in r["evidence"]["exclusions"]}
    stored_ids, stored_excluded = set(), set()
    for prior in conn.execute(sa.select(table.c.chain_id, table.c.transaction_hash, table.c.log_index, table.c.evidence)):
        stored_ids.add(f"{prior.chain_id}:{prior.transaction_hash}:{prior.log_index}")
        stored_excluded.update(e["event_id"] for e in (prior.evidence or {}).get("exclusions", []))
    if incoming_ids & stored_excluded or incoming_excluded & stored_ids:
        raise ValueError("accepted/excluded identities conflict with retained provenance")
    for row in bundle["rows"]:
        pool = conn.execute(sa.select(db.pools).where(db.pools.c.chain_id == row["chain_id"],
                            db.pools.c.pool_id == row["pool_id"])).first()
        if pool is None or pool.hook_address != row["hook_address"]:
            raise ValueError("evidence does not match an existing chain/pool/hook")
        hook = conn.execute(sa.select(db.hooks.c.project_id).where(db.hooks.c.chain_id == row["chain_id"],
                            db.hooks.c.address == row["hook_address"])).first()
        if hook is None or hook.project_id is None:
            raise ValueError("evidence pool has no current project mapping")
        project_ids.add(hook.project_id)
        if row["positive"]:
            key = row["chain_id"], row["pool_id"], row["transaction_hash"]
            transactions[key] = max(transactions.get(key, Decimal(0)), row["usd_lower_bound"] or Decimal(0))
        value = row["evidence"]["event"]["valuation"]
        if value is not None:
            for side in (0, 1):
                token = getattr(pool, f"token{side}")
                if not isinstance(token, dict) or token.get("id", "").lower() != value[f"currency{side}"]:
                    raise ValueError("valuation currency order does not match stored pool")
        # A conflicting block identity anywhere in an already imported transaction is never silently merged.
        existing_tx = conn.execute(sa.select(table.c.block_number, table.c.block_hash, table.c.block_timestamp)
            .where(table.c.chain_id == row["chain_id"], table.c.transaction_hash == row["transaction_hash"])).all()
        if any((r.block_number, r.block_hash, db.as_utc(r.block_timestamp)) !=
               (row["block_number"], row["block_hash"], row["block_timestamp"]) for r in existing_tx):
            raise ValueError("stored transaction block identity conflicts")
        existing_block = conn.execute(sa.select(table.c.block_number, table.c.block_hash, table.c.block_timestamp)
            .where(table.c.chain_id == row["chain_id"], (table.c.block_number == row["block_number"])
                   | (table.c.block_hash == row["block_hash"]))).all()
        if any((r.block_number, r.block_hash, db.as_utc(r.block_timestamp)) !=
               (row["block_number"], row["block_hash"], row["block_timestamp"])
               for r in existing_block):
            raise ValueError("stored block identity conflicts")
        position_tx = conn.execute(sa.select(table.c.transaction_hash).where(table.c.chain_id == row["chain_id"],
            table.c.block_hash == row["block_hash"], table.c.log_index == row["log_index"])).scalar_one_or_none()
        if position_tx is not None and position_tx != row["transaction_hash"]:
            raise ValueError("stored block log position has conflicting transaction identity")
        prior = conn.execute(sa.select(table.c.evidence_sha256).where(table.c.chain_id == row["chain_id"],
                table.c.transaction_hash == row["transaction_hash"], table.c.log_index == row["log_index"])).scalar_one_or_none()
        if prior is not None:
            if prior != row["evidence_sha256"]:
                raise ValueError("changed replay of immutable event evidence")
            unchanged += 1
        else:
            pending.append(row)
    if apply and pending:
        conn.execute(sa.insert(table), pending)
    pool_summary = []
    for cid, pid in sorted({key[:2] for key in transactions}):
        contributions = [v for key, v in transactions.items() if key[:2] == (cid, pid)]
        with localcontext() as context:
            context.prec = 120
            known = sum(contributions, Decimal(0))
        pool_summary.append({"chain_id": cid, "pool_id": pid, "positive_transactions": len(contributions),
                             "known_usd_contribution": str(known), "historical_total_usd": None})
    return {"bundle_sha256": bundle["sha256"], "inserted" if apply else "would_insert": len(pending),
            "unchanged": unchanged, "positive_observations": sum(r["positive"] for r in bundle["rows"]),
            "project_ids": sorted(project_ids), "pools": pool_summary, "historical_total_usd": None, "scored": False}


def rollback_bundle(conn, sha256: str, *, apply=False, expected_rows=None) -> dict:
    """Delete only rows originally inserted by this digest. Replays never transfer ownership. No rescore."""
    table = db.historical_swap_evidence
    where = table.c.bundle_sha256 == _sha(sha256)
    count = conn.execute(sa.select(sa.func.count()).select_from(table).where(where)).scalar_one()
    if apply:
        if expected_rows is None or count != expected_rows:
            raise ValueError("rollback row count differs from explicitly reviewed count")
        conn.execute(sa.delete(table).where(where))
    return {"bundle_sha256": sha256, "deleted" if apply else "would_delete": count, "scored": False}


def add_parser(sub):
    parser = sub.add_parser("historical-evidence", help="offline reviewed evidence; no schema bootstrap or scoring")
    commands = parser.add_subparsers(dest="evidence_cmd", required=True)
    imp = commands.add_parser("import", help="validate/preview a reviewed manifest; --apply writes evidence only")
    imp.add_argument("file")
    imp.add_argument("--sha256", required=True, help="externally reviewed file SHA-256")
    undo = commands.add_parser("rollback", help="preview removal of one import's owned rows; no rescore")
    undo.add_argument("--sha256", required=True)
    undo.add_argument("--expected-rows", type=int)
    for command in (imp, undo):
        command.add_argument("--database-url", default=None)
        command.add_argument("--stop-at", default="05:30",
                             help="UTC HH:MM before 06:00: --apply refuses from here until 06:30 so the nightly is never raced")
        flags = command.add_mutually_exclusive_group()
        flags.add_argument("--apply", action="store_true")
        flags.add_argument("--dry-run", action="store_true", help="default; SELECT only, no schema/lock/receipt writes")


EXIT_BLACKOUT = 5


def main(args, engine, now: datetime | None = None) -> int:
    from . import repair   # the same nightly blackout rule as one-off repairs
    lock = None
    now = now or datetime.now(timezone.utc)
    try:
        bundle = load_bundle(args.file, args.sha256) if args.evidence_cmd == "import" else None
        if args.apply and repair.in_blackout(getattr(args, "stop_at", "05:30"), now):
            # A write that overlaps the 06:00 UTC nightly either races its lock or, run from a start-command override
            # left in place, replaces the nightly entirely. Preview is harmless at any hour.
            print(f"historical evidence --apply refused: inside the nightly blackout ({args.stop_at} to 06:30 UTC)")
            return EXIT_BLACKOUT
        if args.apply:
            lock = db.try_run_lock(engine)
            if lock is None:
                print("another pipeline/evidence writer holds the run lock")
                return 3
        with engine.begin() as conn:
            if not args.apply and conn.dialect.name == "postgresql":
                conn.execute(sa.text("SET TRANSACTION READ ONLY"))
            if not sa.inspect(conn).has_table("historical_swap_evidence"):
                print("migration 0017 required; no schema changes performed")
                return 4
            result = (import_bundle(conn, bundle, apply=args.apply) if bundle else
                      rollback_bundle(conn, args.sha256, apply=args.apply, expected_rows=args.expected_rows))
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, OSError) as exc:
        # Validation messages name the failed check; they never contain the payload or a connection string.
        print(f"historical evidence rejected: {exc}; no batch changes committed")
        return 2
    except sa.exc.SQLAlchemyError as exc:
        # A database error can echo statement parameters: report the type only.
        print(f"historical evidence rejected ({type(exc).__name__}); no batch changes committed")
        return 2
    finally:
        if lock is not None:
            lock.close()
