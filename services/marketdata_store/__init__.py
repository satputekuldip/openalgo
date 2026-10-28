"""Optional ClickHouse store for market data.

Off unless ``CLICKHOUSE_URL`` is set. When on: Historify's downloaded bars are
mirrored into ``ohlcv`` (DuckDB stays Historify's own store), every streamed
tick goes to ``ticks`` with 1-minute bars built from them, and the chain
recorder writes to ``option_chain``. See
docs/plans/2026-10-28-clickhouse-market-data-plan.md.
"""
