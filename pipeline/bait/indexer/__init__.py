"""On-chain Uniswap v4 indexer for Bait: every hooked pool on every chain HyperSync serves, with exact swap usage.

Run: python -m bait.indexer [--once] [--chains base,unichain]  (long-running; Railway service "indexer").
Writes v4_pools, pool_daily_onchain, pool_wallet_day, indexer_state (db.py "# --- indexer ---", 0004_indexer.sql).
The nightly pipeline step "onchain" copies them into the tables the web app reads.
"""
