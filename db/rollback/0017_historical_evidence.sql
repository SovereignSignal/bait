-- First roll back the scorer/importer that reads this table. Preserve/export needed evidence before dropping it.
-- This does not restore project_daily classifications: any rescore or score rollback is a separate reviewed action.
BEGIN;
DROP TABLE IF EXISTS historical_swap_evidence;
-- Reset only this migration so the normal runner can reapply it. Direct/manual migrations may have no tracker.
DO $$
BEGIN
    IF to_regclass('schema_migrations') IS NOT NULL THEN
        EXECUTE 'DELETE FROM schema_migrations WHERE filename = ''0017_historical_evidence.sql''';
    END IF;
END
$$;
COMMIT;
