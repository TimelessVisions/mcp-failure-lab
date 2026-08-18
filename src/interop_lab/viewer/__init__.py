"""The static evidence viewer's data contract, exporter and privacy controls.

The frontend lives in `viewer/` at the repository root and holds no source of
truth: everything it renders is exported from here, from benchmark artifacts
whose hashes an execution manifest names.
"""

from __future__ import annotations
