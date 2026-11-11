# Validation scope

## Synthetic verification

The repository's local verification uses Python 3.12, the pinned top-level dependencies,
never-valid fixture credentials and reserved `.invalid` domains. It makes no live
ZeroByte requests and starts no listener on a real network.

Commands:

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python -m compileall -q src tests
```

Historical local result on 2026-11-06: **27 tests passed**, plus successful compilation.
A Pydantic `IncompleteFieldDefinitionWarning` for the SDK `lifespan` annotation was
emitted; it did not fail the synthetic suite and should be reviewed during dependency
validation. The tests validate:

- One combined FastMCP catalog with 9 read and 4 write tools.
- Fixed operations, projected outputs, authentication and request/response bounds.
- In-process MCP initialization, discovery, read/write fixture calls and shutdown.
- Exact namespace-manifest coverage.

`deployment/api-coverage.json` enumerates 97 operations in the referenced ZeroByte
v0.44.0 generated SDK: 13 implemented, 84 omitted. It is not a live API inventory.
The official Python base image is pinned by digest in the Dockerfile.

## Template regression verification

Local result on 2026-11-07 with Python 3.12.14: **28 tests passed** (the original
27 plus one template regression), plus successful compilation. The same nonfatal
Pydantic warning remains. No API implementation code changed.

The new regression uses only the standard library. It requires the template's
single-line `tmpfs` value to be a JSON-compatible YAML flow list containing exactly
one quoted mount: `/tmp:rw,noexec,nosuid,size=16m`. This preserves all required
options and catches the original unquoted value, whose commas create four YAML
list entries. Temporarily restoring that malformed value made the regression fail.

This is a bounded template check, not a general YAML parser or full Compose
validation. Docker/Compose CLI validation was not run because neither CLI was
available. No image build, container creation or deployment was performed.

## Separate deployment gates

The synthetic suite does **not** establish:

- A successful Docker image build or valid Compose runtime on a deployment host.
- A completed dependency vulnerability audit or transitive dependency lock.
- Live ZeroByte API compatibility, credential validity or network reachability.
- Effective MetaMCP namespace invocation filtering or endpoint credential separation.
- Successful real backups, cancellation, refresh, restore, prune or permission repair.

Run and record the README deployment acceptance tests for each actual installation.
Never run a consequential write merely to prove connectivity; obtain the appropriate
operation-specific approval first. Stored healthy status and passing fixtures do not
prove recoverability.
