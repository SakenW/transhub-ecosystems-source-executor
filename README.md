# Trans-Hub Ecosystems Source Executor

This public repository contains the auditable Obsidian source executor used by
Trans-Hub's on-demand public discovery flow. It reads exactly the
server-approved `manifest.json` and `main.js` assets from one GitHub Release and
turns that bounded component closure into a canonical source catalog. It does
not pre-crawl the ecosystem, accept release archives, execute plugin JavaScript,
or publish raw plugin files.

## Security boundary

The GitHub-hosted workflow is manual-only and fails closed unless it runs from
the repository's protected default branch. It has only read-only repository
access and OIDC token issuance. Checkout does not persist credentials. The
workflow accepts only two repository variables:

- `TRANSHUB_PUBLIC_DISCOVERY_API_BASE`: the HTTPS control-plane base.
- `TRANSHUB_PUBLIC_DISCOVERY_OIDC_AUDIENCE`: the dedicated OIDC audience.

No GitHub secret, cache, workflow artifact, schedule, repository dispatch, or
caller-provided URL is used. The control client derives both GitHub Release
asset endpoints from a frozen source plan, requires one unique `manifest.json`
and one unique `main.js`, requires `main.js` to be the plan's primary content
asset, and verifies each asset's size and SHA-256. Component
bytes stay in process memory and are never written to disk. The static parser
runs in a fixed, digest-pinned Python container with networking disabled, a
read-only filesystem, all Linux capabilities dropped, no-new-privileges, and
bounded CPU, memory, process, time, component, request, and result limits.

Source bytes, base64 input, credentials, URLs, and object keys are never emitted
in normal diagnostics. Only a canonical JSON source catalog and its
materialization binding may leave the offline parser. Kodo receives only that
canonical JSON result; neither source component is retained or uploaded.

## Layout

- `adapters/obsidian/adapter_worker.py`: static Obsidian bundle parser.
- `adapters/obsidian/component_bridge.py`: exact two-component stdin/stdout protocol bridge.
- `adapters/obsidian/build_public_executor.py`: deterministic PYZ builder.
- `adapters/obsidian/offline_executor_host.py`: in-memory component lifecycle and offline container boundary.
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

Local tests execute the built PYZ directly and use injected runners for
component validation, non-persistence, cleanup, and concurrency paths. They do
not claim live tasks, request OIDC tokens, download releases, upload results, or
require Docker.

## Failure, concurrency, rollback, and compatibility

- Any profile, digest, size, missing/duplicate component, manifest, catalog,
  lease binding, upload confirmation, or protected-ref mismatch fails closed
  with a bounded code. A Release missing either `manifest.json` or `main.js` is
  rejected before the parser or result grant runs.
- Transient network operations retry at most three times. Grant retries reuse
  one command ID; ambiguous confirmation is accepted only after a status read
  proves the task reached `materialization_pending`.
- Each host invocation has a distinct temporary directory and container name;
  the directory contains only the staged executor artifact, never source
  components. Lease fences and server-side task state remain the concurrency
  authority.
- The workflow does not mutate this repository and stores no cache or artifact.
  A failed run leaves no publication to roll back; task failure closure and
  server-side generation changes remain control-plane responsibilities.
- The component input protocol is
  `trans-hub.obsidian-public-executor.v2`. The old ZIP-shaped v1 request is
  intentionally rejected rather than supported through a compatibility path.
  Profile schema `v1`, result revision `1`, and canonical source catalog
  revision `2` remain unchanged.

This repository intentionally excludes Trans-Hub server routes, database code,
client plugins, credentials, production configuration, raw-source storage, and
CDN/publication machinery.
