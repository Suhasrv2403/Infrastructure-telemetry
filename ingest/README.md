# ingest/

The production ingest buffer and its accept-and-spool config (P1-01), sized from the load
test in P1-01 and the capacity plan in P3-01. Writes land in Stage 0 (append-only, partitioned
by arrival hour - see CLAUDE.md).

- `service/` - the ingest service itself (accepts device messages, validates just enough to
  route them, spools on backpressure instead of dropping).
- `buffer/` - buffer/queue configuration (topics, partitions, retention) shared by the ingest
  service and Stage 0 writers.
