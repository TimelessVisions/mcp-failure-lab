# Reuse decisions — Track A conformance invocation

## Requirement

Unscoped official conformance for a dated profile must run that revision's frozen requirement set. A pass of one scenario, or of `--spec-version`, must not be stored as that set.

## Gap in this tree

Before this branch, `ConformanceAdapter.run_server` always passed `--spec-version` (`src/interop_lab/conformance/adapter.py` at `d89ae1a`). The artifact field `requirements` was documented as the revision passed via `--requirements`. A live run on 2026-10-29T15:21:08Z against `targets/http/fixture-2026.yaml --scenario tools-list` saved a command containing `--spec-version` and `--scenario`, omitted `--requirements`, and recorded `outcome=OFFICIAL_CONFORMANCE_PASS` with `requirements=2026-07-28`.

The pinned CLI rejects the mix. Measured against the installed `0.2.0-alpha.11` binary:

```text
--requirements cannot be combined with --spec-version
--requirements cannot be combined with --scenario
--requirements cannot be combined with --suite
```

Each exits 1 and writes no checks. `--spec-version` filters the suite as it exists today. `--requirements` is the frozen set and fixes the wire. The conformance README at commit `c321dd32035556e6769d3724a8ee97d87c3faaac` states that difference.

Characterization: `tests/unit/test_conformance_requirements.py`, 6 failed at 2026-10-29T15:26:33Z on `7419791`'s parent, then passed after the adapter change.

## References inspected

### 1. Official conformance package

- Canonical: https://github.com/modelcontextprotocol/conformance
- Pin inspected: tag contents of `@modelcontextprotocol/conformance@0.2.0-alpha.11`, commit `c321dd32035556e6769d3724a8ee97d87c3faaac`, committed 2026-08-07, message `chore: bump version to 0.2.0-alpha.11 (#448)`. License `MIT` in that commit's `package.json`.
- Maintenance signal: on 2026-10-29 npm `dist-tags` were `latest=0.1.16` and `alpha=0.2.0-alpha.12`. Alpha.12 `server --help` still documents `--requirements` as replacing `--suite` and `--spec-version`, and the same binary rejects combining `--requirements` with `--spec-version` (exit 1). Alpha.12 was invoked with `npx` and was not added to `package.json` or the lockfile.
- `0.1.16` is the stable tag and is not a newer revision of the 0.2 alpha line. It was not used.
- Security: `npm audit` on the locked `tools/conformance` install reported 1 critical, 5 high, 4 moderate. That is not a security review. No advisory was patched here.

### 2. Official Python SDK

- Canonical: https://github.com/modelcontextprotocol/python-sdk
- Pin: `mcp==2.0.0` (lockfile upload time 2026-07-28). License `MIT` from the installed distribution metadata.
- Installed `mcp_types.version`: handshake revisions through `2025-11-25`; modern revisions `2026-07-28` only.
- Index latest on 2026-10-29: `2.3.0`. Changelog not inspected. Not installed.

### 3. Official TypeScript SDK

- Canonical: https://github.com/modelcontextprotocol/typescript-sdk
- Pin: `@modelcontextprotocol/sdk@1.30.0`. License `MIT` in the installed `package.json`.
- `dist/esm/types.d.ts` in that install mentions `2024-10-07`, `2025-03-26`, and `2025-11-25`, not `2026-07-28`.
- npm latest on 2026-10-29: `1.32.2`. Not installed.
- The 2026-07-28 specification post (https://blog.modelcontextprotocol.io/posts/2026-07-28/) says the TypeScript and Python SDKs speak `2026-07-28` as of that release. That sentence is about the SDKs current at publication. It does not make the pinned TypeScript 1.30.0 types a 2026 implementation.

The blog post's lifecycle description (no initialize handshake, per-request `_meta`, `Mcp-Method` / `Mcp-Name`, `resultType`, `server/discover`) matches the facts already encoded in `src/interop_lab/profiles/registry.py` for `mcp-2026-07-28`. This PR does not add a third profile. No newer dated revision was found in the conformance `list` help, which offers `2025-11-25` and `2026-07-28` only.

## Options

| Option | Why it lost or won |
|---|---|
| Retain the `--spec-version` invocation | Rejected. It answers a different question and was labeled as `--requirements`. |
| Bump to conformance `0.2.0-alpha.12`, Python `2.3.0`, or TypeScript `1.32.2` | Rejected for this PR. Alpha.12's flag contract matches the pin. The other two were not measured. |
| Reimplement the requirement list in Python | Rejected. The pinned CLI already exits 0 only when no scored scenario has a FAILURE check (`Bc` in the installed `dist/index.js`). Copying the YAML would drift. |
| Vendor or fork conformance | Rejected. MIT would allow use; the project rule is to invoke the package. |
| **Adapt the official CLI interface** | **Chosen.** Unscoped runs pass `--requirements <profile.spec_version>` and take the process exit code as the scored verdict. A `--scenario` run passes `--scenario` and `--spec-version`, because the CLI forbids combining those with `--requirements`, and the artifact selector is `scenario`. |

## Cost and validation

No new dependency and no lockfile change. Callers of an unscoped `mcp-failure-lab conformance` now execute the frozen server set. The partial HTTP fixtures exit 1. The README example that passes `--scenario tools-list` still exits 0 and is labeled `scenario`.

Validated by the six characterization tests, `uv run pyright` (0 errors), `uv run ruff check`, full pytest (986 passed, 50 skipped, 0 failed), and the live CLI runs in `TEST_EVIDENCE.md`.
