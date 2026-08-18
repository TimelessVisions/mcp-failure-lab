"""MCP Interoperability and Failure-Recovery Benchmark — Private 0.3.

A benchmark that deliberately produces **no overall ranking**. There is no best
SDK here, no safest implementation, no most compliant target and no aggregate
score, because thirty-two scenarios against three implementations cannot support
one, and a number that looks like it can is worse than no number at all.

What it produces instead is category-level observation with explicit
denominators: for each category, how many scenarios *applied*, how many actually
executed, how many produced a stable pass, how many produced a stable non-pass,
how many were inconclusive, how many were not applicable, how many were not
measured at all — and what none of that establishes.

The modules divide the work along the line that matters:

* ``model`` — the pinned definition and the typed shape of every output. Unknown
  fields are rejected, so a stale definition fails loudly rather than half-applying.
* ``validation`` — what makes a sample valid, and what makes one rejected. A
  rejected sample contributes to no median and no summary.
* ``runner`` — executing the definition and collecting samples.
* ``reporting`` — turning valid samples into category observations and artifacts.
"""
