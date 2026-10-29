# Demo — dated requirement set vs one scenario

About one minute once `uv sync --all-extras --dev` and `npm ci --prefix tools/conformance` have been run. No Docker. No third-party server.

```bash
uv run mcp-failure-lab conformance \
  -t targets/http/fixture-2026.yaml \
  --artifacts artifacts/conformance-requirements
echo "requirements exit: $?"

uv run mcp-failure-lab conformance \
  -t targets/http/fixture-2026.yaml \
  --scenario tools-list \
  --artifacts artifacts/conformance-scenario
echo "scenario exit: $?"
```

Observed on 2026-10-29 after the adapter fix:

- The first command exits 1. Selector `requirements`, revision `2026-07-28`, outcome `OFFICIAL_CONFORMANCE_FAILURE`. The fixture does not implement the frozen server set.
- The second command exits 0. Selector `scenario`, 3 checks passed. The process output says this is not a frozen requirement-set result.

A requirements exit of 0 on `targets/http/fixture-2026.yaml` would be the false green this branch closes.
