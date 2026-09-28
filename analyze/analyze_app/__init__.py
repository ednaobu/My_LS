"""LinkSquares Analyze, built as a modular monolith.

One process, one database, one deployment. Features are separate *modules*
(ingestion, extraction, categorization, governing, tracking, reporting,
repository, integrations) that call each other as plain Python functions and
share a single SQL schema - so one ingest runs in a single ACID transaction.
"""
