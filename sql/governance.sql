-- Unity Catalog ABAC for raw logs (GOV-10, PRV-1). Rendered by jobs/setup_platform.py.
-- Columns are tagged pii=text|id; one schema-level policy per tag value masks them for everyone
-- except Security and MLOps. Support reads the real tables and sees masked values.
CREATE GOVERNED TAG pii VALUES ('text', 'id');

CREATE OR REPLACE FUNCTION {catalog}.{platform}.mask_pii_text(v STRING) RETURNS STRING
  COMMENT 'ABAC mask: AI masking of names, emails, phones, addresses, plus restricted number patterns'
  RETURN ai_mask(regexp_replace(regexp_replace(v, '\\b\\d{{3}}-\\d{{2}}-\\d{{4}}\\b', '[SSN]'),
                                '\\b(?:\\d[ -]?){{13,19}}\\b', '[NUMBER]'),
                 array('person', 'email', 'phone', 'address'));

CREATE OR REPLACE FUNCTION {catalog}.{platform}.mask_pii_id(v STRING) RETURNS STRING
  COMMENT 'ABAC mask: stable hash so support can still group by user'
  RETURN sha2(v, 256);

ALTER TABLE {catalog}.{platform}.request_log ALTER COLUMN question SET TAGS ('pii' = 'text');
ALTER TABLE {catalog}.{platform}.request_log ALTER COLUMN answer SET TAGS ('pii' = 'text');
ALTER TABLE {catalog}.{platform}.request_log ALTER COLUMN user_id SET TAGS ('pii' = 'id');
ALTER TABLE {catalog}.{platform}.request_log ALTER COLUMN user_enc SET TAGS ('pii' = 'id');
ALTER TABLE {catalog}.{platform}.guardrail_events ALTER COLUMN content SET TAGS ('pii' = 'text');
ALTER TABLE {catalog}.{platform}.guardrail_events ALTER COLUMN user_id SET TAGS ('pii' = 'id');
ALTER TABLE {catalog}.{platform}.conversations ALTER COLUMN content SET TAGS ('pii' = 'text');
ALTER TABLE {catalog}.{platform}.conversations ALTER COLUMN user_id SET TAGS ('pii' = 'id');
ALTER TABLE {catalog}.{platform}.feedback ALTER COLUMN comment SET TAGS ('pii' = 'text');
ALTER TABLE {catalog}.{platform}.feedback ALTER COLUMN user_id SET TAGS ('pii' = 'id');

CREATE OR REPLACE POLICY mask_pii_text ON SCHEMA {catalog}.{platform}
  COLUMN MASK {catalog}.{platform}.mask_pii_text TO `account users` EXCEPT {security}, {admins}{extra}
  FOR TABLES MATCH COLUMNS has_tag_value('pii', 'text') AS c ON COLUMN c;

CREATE OR REPLACE POLICY mask_pii_id ON SCHEMA {catalog}.{platform}
  COLUMN MASK {catalog}.{platform}.mask_pii_id TO `account users` EXCEPT {security}, {admins}{extra}
  FOR TABLES MATCH COLUMNS has_tag_value('pii', 'id') AS c ON COLUMN c;
