# inference-snaps-ci-orchestrator
A central place to monitor PRs and trigger tests

## How it works

The [Orchestrate](.github/workflows/orchestrate.yaml) workflow runs every 5 minutes
(and on demand). [`orchestrator.py`](orchestrator.py):

1. Reads the [onboarded inference snaps](https://canonical.github.io/inference-snaps-admin/onboarded-snaps.json)
   and lists the open PRs of each snap repository (`repo_url`).
2. Selects non-draft PRs authored by `renovate[bot]` that change a runtime dependency
   affecting inference. A dependency is detected when the PR title or the lines changed
   by the PR match one of the `RUNTIME_PATTERNS` (llama.cpp, OpenVINO / OVMS,
   Intel compute runtime, Intel NPU driver, Level Zero).
3. Adds the `trigger-tests` label to the PR, which is expected to start the lab tests in
   the snap repository. If commits are pushed after the label was added (e.g. Renovate
   rebased the PR), the label is removed and re-added to re-run the tests.
4. Merges the PR (squash, pinned to the tested head SHA) once all check runs and commit
   statuses of the head commit have passed, and at least one of them started after the
   label was added. If any check fails, the PR is left for a human to look at.

### Setup

- `ORCHESTRATOR_TOKEN` secret: a token (e.g. fine-grained PAT or GitHub App token) with
  *Pull requests: read & write*, *Issues: read & write*, *Contents: read & write* and
  *Checks / Commit statuses: read* on the snap repositories.
  The default `GITHUB_TOKEN` can't be used: it has no access to other repositories, and
  labels added with it would not trigger workflows.
- `DRY_RUN` repository variable (optional): set to `true` to only log what scheduled runs
  would do. Manual runs have a `dry-run` input (enabled by default).

### Run locally

```shell
GITHUB_TOKEN=... python3 orchestrator.py --dry-run [--repo canonical/deepseek-r1-snap]
```

### Tests

```shell
python3 -m unittest discover -s tests -v
```

### Known limitations

- Polling instead of webhooks: actions are delayed by up to 5 minutes (scheduled
  workflows may also be delayed or skipped by GitHub under load).
- "All tests passed" is inferred from the checks of the head commit. If the lab tests
  never start after labelling, the PR waits forever.
- Merging may be refused by branch protection (e.g. required reviews); the error is
  logged and the run fails.
