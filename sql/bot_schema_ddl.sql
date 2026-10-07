-- Per-bot objects (PRV-1..4). Rendered with {catalog}/{schema}; idempotent.

CREATE SCHEMA IF NOT EXISTS {catalog}.{schema}
  COMMENT 'Chatbot: {display_name}. Managed by the Chatbot Factory app (LCY-5, REL-10).';

CREATE VOLUME IF NOT EXISTS {catalog}.{schema}.docs
  COMMENT 'Source documents, page images and config.yml';

-- Document manifest (PRV-3, DOC-4, DOC-5, DCL-1, IDN-4)
CREATE TABLE IF NOT EXISTS {catalog}.{schema}.manifest (
  doc_id STRING NOT NULL,        -- stable across versions
  doc_version INT,
  doc_name STRING,
  file_path STRING,              -- volume path (downloadable from the app, DCL-5)
  file_ext STRING,
  content_hash STRING,           -- sha256, dedup (DCL-7) and incremental re-index (CAS-7)
  size_bytes BIGINT,
  page_count INT,
  source STRING,                 -- upload | git | sharepoint
  source_ref STRING,             -- commit SHA or SharePoint item id
  uploaded_by STRING,
  uploaded_at TIMESTAMP,
  parser STRING,                 -- ai_parse_document | text_reader
  parser_version STRING,
  status STRING,                 -- pending_review | approved | flagged | archived | superseded
  is_active BOOLEAN,
  readability STRING,            -- green | yellow | red | n/a (QA-7)
  qa_json STRING,                -- check results, per-page errors (QA-5, QA-13)
  flag_reason STRING,
  page_range STRING,             -- QA-11 re-parse with pages excluded, e.g. '1-3,5-40'
  doc_owner STRING,              -- metadata, editable without re-chunking (DCL-3)
  effective_date DATE,           -- DCL-1
  expires_at TIMESTAMP,          -- NULL with no_expiry = true: valid until replaced
  no_expiry BOOLEAN,
  security_scope STRING,         -- IDN-4 (day 2 filtering)
  geo_scope STRING,              -- e.g. US, CA, US-TX, ALL
  chunk_count INT,
  updated_at TIMESTAMP
) TBLPROPERTIES (delta.enableChangeDataFeed = true);

-- Stage 1: raw parse output, one row per doc version (PRV-8)
CREATE TABLE IF NOT EXISTS {catalog}.{schema}.parsed_elements (
  doc_id STRING, doc_version INT, parser STRING,
  parsed VARIANT,                -- ai_parse_document output (NULL for text path)
  text_content STRING,           -- text path content, or manual override (QA-11)
  is_override BOOLEAN,
  parsed_at TIMESTAMP
);

-- Stage 2: chunks (PRV-4). Old versions are kept for rollback (REL-3).
CREATE TABLE IF NOT EXISTS {catalog}.{schema}.chunked (
  chunk_id STRING NOT NULL,
  doc_id STRING, doc_version INT, doc_name STRING, source_uri STRING,
  chunk_position INT, page_ids ARRAY<INT>, section STRING,
  chunk_to_retrieve STRING, chunk_to_embed STRING,
  doc_type STRING, effective_date STRING, department STRING,
  flagged BOOLEAN,               -- QA-9 single-chunk flag
  injection_flag BOOLEAN,        -- DCL-8 prompt-injection text detected at ingestion
  updated_at TIMESTAMP
) TBLPROPERTIES (delta.enableChangeDataFeed = true);

-- Dedicated index source, confidential bots only (ARC-4); same shape as shared_chunks
CREATE TABLE IF NOT EXISTS {catalog}.{schema}.index_source (
  chunk_id STRING NOT NULL, bot_id STRING, doc_id STRING, doc_version INT,
  doc_name STRING, source_uri STRING, page_ids ARRAY<INT>, section STRING,
  chunk_to_retrieve STRING, chunk_to_embed STRING,
  doc_type STRING, effective_date STRING, department STRING,
  in_live BOOLEAN, in_candidate BOOLEAN, effective_ts BIGINT, expires_ts BIGINT,
  security_scope STRING, geo_scope STRING,
  updated_at TIMESTAMP
) TBLPROPERTIES (delta.enableChangeDataFeed = true);

CREATE TABLE IF NOT EXISTS {catalog}.{schema}.golden_set (
  eval_id STRING, question STRING, expected_answer STRING,
  source_doc_id STRING, source_chunk_id STRING,
  expected_pages ARRAY<INT>,     -- 1-based, EVG-2
  kind STRING,                   -- in_scope | out_of_scope (EVL-4) | feedback (EVL-7) | canary
  difficulty STRING,             -- easy | hard (EVG-4)
  question_type STRING,          -- fact | table | figure | footnote | multi_page | number | ...
  origin STRING,                 -- generated | manual | feedback
  approved BOOLEAN, active BOOLEAN, updated_by STRING, updated_at TIMESTAMP
);

-- UC access probes (GOV-3/GOV-4). The agent checks them with the caller's own
-- credentials: access_probe = may use the live bot; tester_probe = may use the
-- candidate release (owner, owner group, Reviewer, up to 10 testers; REL-1).
CREATE TABLE IF NOT EXISTS {catalog}.{schema}.access_probe (ok BOOLEAN);
CREATE TABLE IF NOT EXISTS {catalog}.{schema}.tester_probe (ok BOOLEAN);
