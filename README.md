# Trans-Hub Ecosystems Source Executor

This public repository contains the auditable Obsidian source executor used by
Trans-Hub's on-demand public discovery flow. It turns one exact, server-approved
Obsidian release ZIP into a bounded canonical source catalog. It does not
pre-crawl the ecosystem, execute plugin JavaScript, or publish raw plugin files.

## Security boundary

The GitHub-hosted workflow is manual-only and fails closed unless it runs from
the repository's protected default branch. It has only read-only repository
access and OIDC token issuance. Checkout does not persist credentials. The
workflow accepts only two repository variables:

- `TRANSHUB_PUBLIC_DISCOVERY_API_BASE`: the HTTPS control-plane base.
- `TRANSHUB_PUBLIC_DISCOVERY_OIDC_AUDIENCE`: the dedicated OIDC audience.

No GitHub secret, cache, workflow artifact, schedule, repository dispatch, or
caller-provided URL is used. The control client derives the GitHub release asset
endpoint from a frozen source plan, verifies size and SHA-256, and writes the raw
ZIP only to a mode-0700 temporary directory. The parser runs in a fixed,
digest-pinned Python container with networking disabled, a read-only filesystem,
all Linux capabilities dropped, no-new-privileges, and bounded CPU, memory,
process, time, ZIP-entry, expansion, and result limits.

The raw ZIP is deleted before any structured result handoff. Source bytes,
base64 input, credentials, URLs, and object keys are never emitted in normal
diagnostics. Only a canonical JSON source catalog and its materialization
binding may leave the offline parser.

## Layout

- `adapters/obsidian/adapter_worker.py`: static Obsidian bundle parser.
- `adapters/obsidian/public_zip_closure.py`: bounded, non-executing ZIP closure.
- `adapters/obsidian/zip_bridge.py`: stdin/stdout public protocol bridge.
- `adapters/obsidian/build_public_executor.py`: deterministic PYZ builder.
- `adapters/obsidian/offline_executor_host.py`: raw-byte lifecycle and offline container boundary.
- `adapters/obsidian/public_discovery_executor.py`: one-claim OIDC control client.
- `adapters/obsidian/public-executor-profile.json`: immutable runtime profile.

There are no runtime package dependencies; see `requirements.txt`.

## Local verification

From the repository root:

```bash
python3 -m unittest discover -s tests -v
python3 -m adapters.obsidian.build_public_executor --output build/obsidian-public-executor.pyz
git diff --check
```

If Ruff and pytest are already available, the same candidate can also be checked
with:

```bash
ruff check adapters tests
pytest -q
```

Local tests execute the built PYZ directly and use injected runners for host
cleanup/concurrency paths. They do not claim live tasks, request OIDC tokens,
download releases, upload results, or require Docker.

## Failure, concurrency, rollback, and compatibility

- Any profile, digest, size, ZIP, manifest, catalog, lease binding, upload
  confirmation, or protected-ref mismatch fails closed with a bounded code.
- Transient network operations retry at most three times. Grant retries reuse
  one command ID; ambiguous confirmation is accepted only after a status read
  proves the task reached `materialization_pending`.
- Each host invocation has a distinct temporary directory and container name.
  Lease fences and server-side task state remain the concurrency authority.
- The workflow does not mutate this repository and stores no cache or artifact.
  A failed run leaves no publication to roll back; task failure closure and
  server-side generation changes remain control-plane responsibilities.
- Public protocol names, profile schema `v1`, result revision `1`, and
  canonical source catalog revision `2` remain compatible with the originating
  Trans-Hub contract. A protocol change requires an explicit new revision.

This repository intentionally excludes Trans-Hub server routes, database code,
client plugins, credentials, production configuration, raw-source storage, and
CDN/publication machinery.
