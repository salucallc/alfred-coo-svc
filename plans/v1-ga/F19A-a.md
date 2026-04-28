# F19A-a: Shared _upload_bundle helper for mcctl push-* subcommands

**Linear:** SAL-3260

**Parent:** F19A (SAL-3070) -- see `plans/v1-ga/F19A-a.md`
**Wave:** 3
**Auto-dispatchable:** yes (no human-assigned label)

## Context

F19A's CLI side needs a shared bundle-upload helper that both `push-policy` (this F19A series) and `push-persona` (sibling F19B) will reuse. This is the foundational primitive -- author once here, reuse downstream.

The helper:
- Takes `(bundle_type: str, path: str | Path, hub_url: str)`.
- Validates the bundle directory contains `manifest.yaml` with required keys (`name`, `version`).
- Computes SHA256 of canonical bundle bytes (tar+gzip of the directory tree, sorted file order).
- Returns `(sha256: str, payload_b64: str, manifest: dict)` for the caller to POST.

Child of SAL-3070 -- siblings: F19A-b (hub handler), F19A-c (CLI subcommand), F19A-d (e2e test).

## APE/V Acceptance (machine-checkable)

1. File `cmd/mcctl/_upload_bundle.py` exists and exports a function named `upload_bundle` (or `_upload_bundle`) with signature accepting `(bundle_type: str, path, hub_url: str)`.
2. File `tests/cmd/mcctl/test_upload_bundle_helper.py` exists with at minimum the test `test_upload_bundle_returns_sha256_and_payload` that asserts the returned tuple shape and that sha256 is 64 hex chars.
3. `pytest tests/cmd/mcctl/test_upload_bundle_helper.py -v` exits 0.

## Out of scope

- Hub-side handler -- F19A-b.
- CLI subcommand wiring -- F19A-c.
- Network call against a live hub -- F19A-d.
