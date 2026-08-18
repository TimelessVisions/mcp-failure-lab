"""The contained-HTTP probe contract.

Split into three pieces that are deliberately kept apart:

* ``plan`` — the typed, versioned document the host compiles and the probe
  executes. It describes *transport behaviour only*. Nothing in it names a
  protocol concept, because the probe must not be able to form an opinion
  about MCP.
* ``compile`` — turns a scenario, a target and a dated profile into a plan.
  Every MCP-specific judgement lives here, on the host, next to the tests.
* ``events`` — lowers what the probe reported back into the canonical
  ``WireEvent`` model the assertion engine already consumes.
"""

from __future__ import annotations
