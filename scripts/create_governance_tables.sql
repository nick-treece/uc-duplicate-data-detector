-- ============================================================================
-- Create Metadata Snapshot Tables (CTAS)
-- ============================================================================
--
-- Snapshots every system.information_schema view as a table in the
-- metadata schema.  Because the data is materialised at creation time
-- using the owner's privileges, consumers only need SELECT on the
-- metadata schema — not on system.information_schema directly.
--
-- The person running this script becomes the schema and table owner.
-- Their privileges determine what metadata is visible at snapshot time.
--
-- To refresh the data, re-run the entire script (CREATE OR REPLACE).
-- Consider scheduling this script as a Databricks Job on a weekly cadence.
--
-- Usage:
--   1. Open this file in a Databricks SQL editor (Pro or Classic warehouse)
--   2. Run all statements (the executing user becomes the schema owner)
--   3. Grant SELECT on the metadata schema to consumers
--
-- Catalog: catalog_40_copper_uc_metadata
--          (defined in databricks.yml as the app_catalog variable)
-- ============================================================================


-- ── Catalog and schema context ───────────────────────────────────────────────

USE CATALOG catalog_40_copper_uc_metadata;

CREATE SCHEMA IF NOT EXISTS metadata
COMMENT 'Snapshot mirrors of system.information_schema — provides definer-rights access for app consumers.';

USE SCHEMA metadata;


-- ============================================================================
-- Catalog & Schema Metadata
-- ============================================================================

CREATE OR REPLACE TABLE catalogs
COMMENT 'Mirror of system.information_schema.catalogs'
AS SELECT * FROM system.information_schema.catalogs;

CREATE OR REPLACE TABLE schemata
COMMENT 'Mirror of system.information_schema.schemata'
AS SELECT * FROM system.information_schema.schemata;

CREATE OR REPLACE TABLE tables
COMMENT 'Mirror of system.information_schema.tables'
AS SELECT * FROM system.information_schema.tables;

CREATE OR REPLACE TABLE views
COMMENT 'Mirror of system.information_schema.views'
AS SELECT * FROM system.information_schema.views;

CREATE OR REPLACE TABLE columns
COMMENT 'Mirror of system.information_schema.columns'
AS SELECT * FROM system.information_schema.columns;

CREATE OR REPLACE TABLE volumes
COMMENT 'Mirror of system.information_schema.volumes'
AS SELECT * FROM system.information_schema.volumes;

CREATE OR REPLACE TABLE routines
COMMENT 'Mirror of system.information_schema.routines'
AS SELECT * FROM system.information_schema.routines;

CREATE OR REPLACE TABLE routine_columns
COMMENT 'Mirror of system.information_schema.routine_columns'
AS SELECT * FROM system.information_schema.routine_columns;

CREATE OR REPLACE TABLE parameters
COMMENT 'Mirror of system.information_schema.parameters'
AS SELECT * FROM system.information_schema.parameters;

CREATE OR REPLACE TABLE information_schema_catalog_name
COMMENT 'Mirror of system.information_schema.information_schema_catalog_name'
AS SELECT * FROM system.information_schema.information_schema_catalog_name;


-- ============================================================================
-- Privileges
-- ============================================================================

CREATE OR REPLACE TABLE catalog_privileges
COMMENT 'Mirror of system.information_schema.catalog_privileges'
AS SELECT * FROM system.information_schema.catalog_privileges;

CREATE OR REPLACE TABLE schema_privileges
COMMENT 'Mirror of system.information_schema.schema_privileges'
AS SELECT * FROM system.information_schema.schema_privileges;

CREATE OR REPLACE TABLE table_privileges
COMMENT 'Mirror of system.information_schema.table_privileges'
AS SELECT * FROM system.information_schema.table_privileges;

CREATE OR REPLACE TABLE volume_privileges
COMMENT 'Mirror of system.information_schema.volume_privileges'
AS SELECT * FROM system.information_schema.volume_privileges;

CREATE OR REPLACE TABLE routine_privileges
COMMENT 'Mirror of system.information_schema.routine_privileges'
AS SELECT * FROM system.information_schema.routine_privileges;

CREATE OR REPLACE TABLE connection_privileges
COMMENT 'Mirror of system.information_schema.connection_privileges'
AS SELECT * FROM system.information_schema.connection_privileges;

CREATE OR REPLACE TABLE credential_privileges
COMMENT 'Mirror of system.information_schema.credential_privileges'
AS SELECT * FROM system.information_schema.credential_privileges;

CREATE OR REPLACE TABLE external_location_privileges
COMMENT 'Mirror of system.information_schema.external_location_privileges'
AS SELECT * FROM system.information_schema.external_location_privileges;

CREATE OR REPLACE TABLE metastore_privileges
COMMENT 'Mirror of system.information_schema.metastore_privileges'
AS SELECT * FROM system.information_schema.metastore_privileges;

CREATE OR REPLACE TABLE share_recipient_privileges
COMMENT 'Mirror of system.information_schema.share_recipient_privileges'
AS SELECT * FROM system.information_schema.share_recipient_privileges;

-- Deprecated — included for completeness; may be removed in a future release.
CREATE OR REPLACE TABLE storage_credential_privileges
COMMENT 'Mirror of system.information_schema.storage_credential_privileges (DEPRECATED)'
AS SELECT * FROM system.information_schema.storage_credential_privileges;


-- ============================================================================
-- Tags
-- ============================================================================

CREATE OR REPLACE TABLE catalog_tags
COMMENT 'Mirror of system.information_schema.catalog_tags'
AS SELECT * FROM system.information_schema.catalog_tags;

CREATE OR REPLACE TABLE schema_tags
COMMENT 'Mirror of system.information_schema.schema_tags'
AS SELECT * FROM system.information_schema.schema_tags;

CREATE OR REPLACE TABLE table_tags
COMMENT 'Mirror of system.information_schema.table_tags'
AS SELECT * FROM system.information_schema.table_tags;

CREATE OR REPLACE TABLE column_tags
COMMENT 'Mirror of system.information_schema.column_tags'
AS SELECT * FROM system.information_schema.column_tags;

CREATE OR REPLACE TABLE volume_tags
COMMENT 'Mirror of system.information_schema.volume_tags'
AS SELECT * FROM system.information_schema.volume_tags;


-- ============================================================================
-- Constraints
-- ============================================================================

CREATE OR REPLACE TABLE table_constraints
COMMENT 'Mirror of system.information_schema.table_constraints'
AS SELECT * FROM system.information_schema.table_constraints;

CREATE OR REPLACE TABLE check_constraints
COMMENT 'Mirror of system.information_schema.check_constraints (reserved for future use)'
AS SELECT * FROM system.information_schema.check_constraints;

CREATE OR REPLACE TABLE constraint_column_usage
COMMENT 'Mirror of system.information_schema.constraint_column_usage'
AS SELECT * FROM system.information_schema.constraint_column_usage;

CREATE OR REPLACE TABLE constraint_table_usage
COMMENT 'Mirror of system.information_schema.constraint_table_usage'
AS SELECT * FROM system.information_schema.constraint_table_usage;

CREATE OR REPLACE TABLE key_column_usage
COMMENT 'Mirror of system.information_schema.key_column_usage'
AS SELECT * FROM system.information_schema.key_column_usage;

CREATE OR REPLACE TABLE referential_constraints
COMMENT 'Mirror of system.information_schema.referential_constraints'
AS SELECT * FROM system.information_schema.referential_constraints;


-- ============================================================================
-- Security (Row Filters & Column Masks)
-- ============================================================================

CREATE OR REPLACE TABLE row_filters
COMMENT 'Mirror of system.information_schema.row_filters'
AS SELECT * FROM system.information_schema.row_filters;

CREATE OR REPLACE TABLE column_masks
COMMENT 'Mirror of system.information_schema.column_masks'
AS SELECT * FROM system.information_schema.column_masks;


-- ============================================================================
-- Delta Sharing & Connections
-- ============================================================================

CREATE OR REPLACE TABLE connections
COMMENT 'Mirror of system.information_schema.connections'
AS SELECT * FROM system.information_schema.connections;

CREATE OR REPLACE TABLE credentials
COMMENT 'Mirror of system.information_schema.credentials'
AS SELECT * FROM system.information_schema.credentials;

CREATE OR REPLACE TABLE external_locations
COMMENT 'Mirror of system.information_schema.external_locations'
AS SELECT * FROM system.information_schema.external_locations;

CREATE OR REPLACE TABLE metastores
COMMENT 'Mirror of system.information_schema.metastores'
AS SELECT * FROM system.information_schema.metastores;

CREATE OR REPLACE TABLE providers
COMMENT 'Mirror of system.information_schema.providers'
AS SELECT * FROM system.information_schema.providers;

CREATE OR REPLACE TABLE recipients
COMMENT 'Mirror of system.information_schema.recipients'
AS SELECT * FROM system.information_schema.recipients;

CREATE OR REPLACE TABLE recipient_allowed_ip_ranges
COMMENT 'Mirror of system.information_schema.recipient_allowed_ip_ranges'
AS SELECT * FROM system.information_schema.recipient_allowed_ip_ranges;

CREATE OR REPLACE TABLE recipient_tokens
COMMENT 'Mirror of system.information_schema.recipient_tokens'
AS SELECT * FROM system.information_schema.recipient_tokens;

CREATE OR REPLACE TABLE shares
COMMENT 'Mirror of system.information_schema.shares'
AS SELECT * FROM system.information_schema.shares;

CREATE OR REPLACE TABLE catalog_provider_share_usage
COMMENT 'Mirror of system.information_schema.catalog_provider_share_usage'
AS SELECT * FROM system.information_schema.catalog_provider_share_usage;

CREATE OR REPLACE TABLE schema_share_usage
COMMENT 'Mirror of system.information_schema.schema_share_usage'
AS SELECT * FROM system.information_schema.schema_share_usage;

CREATE OR REPLACE TABLE table_share_usage
COMMENT 'Mirror of system.information_schema.table_share_usage'
AS SELECT * FROM system.information_schema.table_share_usage;

-- Deprecated — included for completeness; may be removed in a future release.
CREATE OR REPLACE TABLE storage_credentials
COMMENT 'Mirror of system.information_schema.storage_credentials (DEPRECATED)'
AS SELECT * FROM system.information_schema.storage_credentials;
