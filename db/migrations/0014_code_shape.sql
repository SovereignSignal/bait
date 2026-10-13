-- Bytecode template families. Additive; also applied at startup by db.add_missing_columns.
-- rules.code_shape: sha256 of the runtime without CBOR metadata and with PUSH32 operands (immutables) zeroed. Hooks that
-- share a shape on one chain are one launchpad's per-token deployments; the group step folds unnamed ones together.
ALTER TABLE hooks ADD COLUMN IF NOT EXISTS code_shape text;
