-- One schema per former SQLite database. Runs once, on first container start.
-- The app also creates a missing schema when it connects, so this file is a
-- convenience for tools that look at the database before the app has run.
CREATE SCHEMA IF NOT EXISTS main;
CREATE SCHEMA IF NOT EXISTS logs;
CREATE SCHEMA IF NOT EXISTS latency;
CREATE SCHEMA IF NOT EXISTS health;
CREATE SCHEMA IF NOT EXISTS sandbox;
