---
name: SQLAlchemy schema drift
description: Non-obvious deployment and startup constraints around this app's SQLAlchemy schema.
---

The app's SQLAlchemy models can advance ahead of the existing PostgreSQL schema. `db.create_all()` creates missing tables but does not add columns to tables that already exist, so a model query can fail at runtime with `UndefinedColumn`.

**Why:** The development database contains legacy tables from earlier app versions, and the publish health check runs the app against the database before the deployment can become healthy.

**How to apply:** Before adding or renaming model fields, compare model metadata with `information_schema.columns`, apply additive development schema changes through the database workflow, and republish so Replit can carry the development schema diff to production. Avoid destructive changes to legacy tables; isolate incompatible new models on a separate table when appropriate.