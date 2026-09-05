# Trans-Hub Ecosystems Source Executor

This public repository contains the auditable Obsidian source executor used by
Trans-Hub's on-demand public discovery flow. On each manual run it first tries
one Stage A registry-resolution job, then one existing Stage B source-discovery
job. Stage A pins and reads the official Obsidian directory only for the claimed
plugin, verifies the directory repository, plugin repository, latest release,
release commit, and exact `manifest.json`/`main.js` asset metadata, and returns
only identities and digests. Stage B reads those two server-approved release
assets and turns the bounded component closure into a canonical source catalog.
The executor does not pre-crawl the ecosystem, accept release archives, execute
plugin JavaScript, or publish raw plugin files.

## Security boundary

The GitHub-hosted workflow is manual-only and fails closed unless it runs from
the repository's protected default branch. It has only read-only repository
access and OIDC token issuance. Checkout does not persist credentials. The
workflow accepts only two repository variables:

- `TRANSHUB_PUBLIC_DISCOVERY_API_BASE`: the HTTPS control-plane base.
- `TRANSHUB_PUBLIC_DISCOVERY_OIDC_AUDIENCE`: the dedicated OIDC audience.

No GitHub secret, cache, workflow artifact, repository dispatch, or
caller-provided URL is used. A protected default-branch schedule runs one fair
cycle every five minutes, alongside manual dispatch. Stage A selects only the version-controlled
`official-directory` profile. That profile fixes GitHub REST API version
`2026-03-10`, repository ID `262342594`, owner ID `65011256`,
`obsidianmd/obsidian-releases`, branch `master`, and
`community-plugins.json`; the claim must match both its authority and validator
profile digests before any network request. The directory is pinned through a
commit and Git tree blob identity, bounded, read completely into memory, checked
against its Git blob SHA-1 and a computed SHA-256, parsed with duplicate-key and
duplicate-plugin rejection, and discarded before result submission. Only a
complete pinned directory with no exact ID produces `absent`; HTTP and network
failures remain retryable.

Stage B derives both GitHub Release asset endpoints from a frozen source plan,
requires one unique `manifest.json` and one unique `main.js`, requires `main.js`
to be the plan's primary content asset, and verifies each asset's size and
SHA-256. Component bytes stay in process memory and are never written to disk.
The static parser runs in a fixed, digest-pinned Python container with networking
disabled, a read-only filesystem, all Linux capabilities dropped,
no-new-privileges, and bounded CPU, memory, process, time, component, request,
and result limits.

Source bytes, base64 input, credentials, URLs, and object keys are never emitted
in normal diagnostics. Only a canonical JSON source catalog and its
materialization binding may leave the offline parser. Kodo receives only that
canonical JSON result; neither source component is retained or uploaded.

## Layout

- `adapters/obsidian/adapter_worker.py`: static Obsidian bundle parser.
- `adapters/obsidian/component_bridge.py`: exact two-component stdin/stdout protocol bridge.
- `adapters/obsidian/build_public_executor.py`: deterministic PYZ builder.
- `adapters/obsidian/offline_executor_host.py`: in-memory component lifecycle and offline container boundary.
- `adapters/obsidian/public_discovery_executor.py`: fair Stage A/Stage B OIDC control client.
- `adapters/obsidian/public-executor-profile.json`: immutable runtime profile.
- `adapters/obsidian/official-directory-profile.json`: immutable official-directory validator profile.

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

- Any registry/repository/release/profile identity, digest, size,
  missing/duplicate component, manifest, catalog, lease binding, upload
  confirmation, or protected-ref mismatch fails closed with a bounded code. A
  Release missing either `manifest.json` or `main.js`, or missing either
  GitHub-provided SHA-256 asset digest, is rejected before Stage B is created.
- Transient network operations retry at most three times. Grant retries reuse
  one command ID; ambiguous confirmation is accepted only after a status read
  proves the task reached `materialization_pending`.
- Each host invocation has a distinct temporary directory and container name;
  the directory contains only the staged executor artifact, never source
  components. Lease fences and server-side task state remain the concurrency
  authority.
- The workflow does not mutate this repository and stores no cache or artifact.
  A healthy run fairly tries one Stage A claim before one Stage B claim and
  emits `executor_no_job` when both queues are empty. A failed run leaves no
  publication to roll back; job/task failure closure and server-side generation
  changes remain control-plane responsibilities.
- The component input protocol is
  `trans-hub.obsidian-public-executor.v2`. The old ZIP-shaped v1 request is
  intentionally rejected rather than supported through a compatibility path.
  Profile schema `v1`, result revision `1`, and canonical source catalog
  revision `2` remain unchanged.

This repository intentionally excludes Trans-Hub server routes, database code,
client plugins, credentials, production configuration, raw-source storage, and
CDN/publication machinery.
