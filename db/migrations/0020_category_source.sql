-- Where a project's category came from: enrichment, or the deterministic fallback for projects enrichment has not
-- reached (rules.fallback_category). NULL on rows written before this.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS category_source text;
