-- Package 7: regrouping and project aliases (pipeline group step). The pipeline also creates these itself
-- (db.create_all + db.add_missing_columns), so applying this file is optional on a database the pipeline has run on.
ALTER TABLE hooks ADD COLUMN IF NOT EXISTS regroup boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS project_aliases (
  old_slug    TEXT PRIMARY KEY,
  project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  reason      TEXT NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS project_aliases_project_idx ON project_aliases (project_id);
