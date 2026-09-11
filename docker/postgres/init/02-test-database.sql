-- Creates the database used by the integration test suite.
--
-- Separate from the development database so a test run can truncate or drop
-- tables without destroying local data, and so tests can be run while the app is
-- up. Extensions are installed here too: they live per-database, so the test
-- database needs its own copies or any migration touching vector/trigram indexes
-- fails only under test.
--
-- Like 01-extensions.sql, this runs once on first initialization of an empty data
-- directory. An existing volume needs `make reset`, or the equivalent statements
-- applied by hand.

CREATE DATABASE supportflow_test OWNER supportflow;

\connect supportflow_test

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
