-- Swap pages no longer wait for pool lookups (indexer/core.py PoolResolver, 2026-10-14). Additive; also applied at
-- startup by db.create_all.
-- A pool id a committed swap page could not place (not a v4_pools row, not known unhooked). Its swaps in
-- [from_block, seen_block) were left out and are owed until the resolver settles it: a hooked pool past its hook's cap
-- gets them re-read and counted in the same transaction as the delete; an unhooked pool is just deleted.
-- from_time: block time of the first swap left out (a day before it is final; nightly step onchain).
CREATE TABLE IF NOT EXISTS pool_lookup_queue (
  chain_id   integer NOT NULL,
  pool_id    text    NOT NULL,
  from_block bigint  NOT NULL,
  seen_block bigint  NOT NULL,
  from_time  timestamptz,
  PRIMARY KEY (chain_id, pool_id)
);
