--
-- Provision the schemas the DOT Oracle CI job runs in.
--
-- Run once, as the RDS master user, connected to the *pluggable* database (the
-- instance is an oracle-ee-cdb, so the PDB service name is what reaches the data;
-- the CDB root does not hold application schemas):
--
--   sqlplus <master user>@'<endpoint>:1521/<PDB service name>' \
--       @bootstrap_ci_schemas.sql
--
-- One schema per Django row of the CI matrix. They exist so the three matrix cells
-- can run at the same time without migrating and truncating each other's tables;
-- the workflow additionally serializes runs that share a schema. The names are
-- derived in .github/workflows/test.yml (the `oracle-schema` matrix field) and must
-- be kept in step with it.
--
-- These accounts hold the smallest privilege set a Django test run needs. In
-- particular they cannot CREATE USER, DROP USER or CREATE TABLESPACE, which is why
-- tests/oracle_settings.py sets TEST['CREATE_USER'] and TEST['CREATE_DB'] to False:
-- Django would otherwise try to build a throwaway user and tablespace per run.
--
-- The same password is used for all three. They are equally privileged and serve
-- one purpose, so splitting them buys isolation that the shared instance does not
-- actually provide. Store it as the ORACLE_PASSWORD secret of the `oracle-ci`
-- GitHub environment.
--

SET VERIFY OFF
SET FEEDBACK ON
WHENEVER SQLERROR EXIT SQL.SQLCODE

ACCEPT ci_password CHAR PROMPT 'Password for the DOT CI schemas: ' HIDE

-- Quota rather than QUOTA UNLIMITED: the suite builds a few dozen small tables, and
-- a cap keeps a runaway test from filling the tablespace Jenkins also lives in.
DEFINE ci_quota = 2G
DEFINE ci_tablespace = USERS

CREATE USER dot_ci_dj42 IDENTIFIED BY "&ci_password"
    DEFAULT TABLESPACE &ci_tablespace
    TEMPORARY TABLESPACE TEMP
    QUOTA &ci_quota ON &ci_tablespace;

CREATE USER dot_ci_dj52 IDENTIFIED BY "&ci_password"
    DEFAULT TABLESPACE &ci_tablespace
    TEMPORARY TABLESPACE TEMP
    QUOTA &ci_quota ON &ci_tablespace;

CREATE USER dot_ci_dj60 IDENTIFIED BY "&ci_password"
    DEFAULT TABLESPACE &ci_tablespace
    TEMPORARY TABLESPACE TEMP
    QUOTA &ci_quota ON &ci_tablespace;

--
-- CREATE SESSION  - connect at all.
-- ALTER SESSION   - Django's Oracle backend sets NLS_TERRITORY and the date formats
--                   on every new connection (init_connection_state).
-- CREATE TABLE    - the migrations; this also covers indexes and constraints on the
--                   schema's own tables.
-- CREATE SEQUENCE - identity columns and Django's sequence handling.
-- CREATE PROCEDURE / CREATE TRIGGER - required by Django's Oracle test setup.
--
-- Every privilege is schema-local: none of them is an ANY privilege, so these
-- accounts cannot see or touch another schema's objects, including Jenkins'.
--
GRANT CREATE SESSION, ALTER SESSION, CREATE TABLE, CREATE SEQUENCE,
      CREATE PROCEDURE, CREATE TRIGGER
    TO dot_ci_dj42;

GRANT CREATE SESSION, ALTER SESSION, CREATE TABLE, CREATE SEQUENCE,
      CREATE PROCEDURE, CREATE TRIGGER
    TO dot_ci_dj52;

GRANT CREATE SESSION, ALTER SESSION, CREATE TABLE, CREATE SEQUENCE,
      CREATE PROCEDURE, CREATE TRIGGER
    TO dot_ci_dj60;

-- Confirm what was granted, and that nothing extra came along with it.
COLUMN grantee FORMAT A16
COLUMN privilege FORMAT A20
SELECT grantee, privilege
    FROM dba_sys_privs
    WHERE grantee IN ('DOT_CI_DJ42', 'DOT_CI_DJ52', 'DOT_CI_DJ60')
    ORDER BY grantee, privilege;

PROMPT
PROMPT Schemas created. Set the GitHub environment secrets next:
PROMPT   ORACLE_DSN      <endpoint>:1521/<pdb service name>
PROMPT   ORACLE_PASSWORD the password entered above
PROMPT

--
-- Teardown, should you want the instance back the way it was:
--
--   DROP USER dot_ci_dj42 CASCADE;
--   DROP USER dot_ci_dj52 CASCADE;
--   DROP USER dot_ci_dj60 CASCADE;
--
