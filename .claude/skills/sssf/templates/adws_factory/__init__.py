"""adws_factory — the unattended orchestration layer above SSSF workflows.

Design: docs/orchestration.md and docs/adr/0002-0004 in
super-simple-software-factory. Read those before changing behaviour here; the
rules in this package are mostly paying for specific failures that already
happened on the predecessor project (grey-factory).

Three invariants this package must never break:

  * Code here is read at launch and never written at runtime, so it can live in
    the monitored tree. All mutable state goes to the runtime directory the
    permission guard already excludes (ADR-0004).
  * Progress is read from artifacts, never from run status (ADR-0003). Run
    status is consulted only to explain why a stage is *not* done.
  * Nothing below the decider may discard work on a severity judgement
    (ADR-0002). Gates hold; the decider rules.
"""

__version__ = "0.1.0"
