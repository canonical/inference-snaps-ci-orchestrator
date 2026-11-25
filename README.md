# inference-snaps-ci-orchestrator
A central place to monitor PRs and trigger tests

## How it works

The [Orchestrate](.github/workflows/orchestrate.yaml) workflow runs every 5 minutes
(and on demand). [`orchestrator.py`](orchestrator.py):

1. Reads the [onboarded inference snaps](https://canonical.github.io/inference-snaps-admin/onboarded-snaps.json)
   and lists the open PRs of each snap repository (`repo_url`).
2. Selects non-draft PRs authored by `renovate[bot]` that change a runtime dependency
   affecting inference. Only changed lines in `snap/snapcraft.yaml` or `snapcraft.yaml`
   are matched against `RUNTIME_PATTERNS` (llama.cpp, OpenVINO / OVMS, Intel compute
   runtime, Intel NPU driver, Level Zero). A changed `source-tag` also uses its immediately
   preceding, unchanged `source` URL. PR titles and other files are ignored.
3. Records one trigger attempt per head SHA in a PR comment, then adds `trigger-tests`.
   An already-present label is removed first so that the addition fires an event.
   The shared workflow consumes the label; its absence does **not** cause another attempt.
4. Finds the matching `.github/workflows/pr-label.yaml` run using the SHA, head branch,
   PR association when available, creation time and `trigger-tests / ` job names.
   It updates the same comment with a clickable run ID and subsequent state. Multiple possible
   runs are left for triage rather than guessing. Each SHA keeps its own comment, so
   returning to an older SHA does not cause another attempt.
5. Waits for the linked workflow to complete successfully. It requires a nonempty lab
   matrix (`trigger-tests / test ...` jobs), with **every** lab job completed with
   `success`. Skipped or neutral lab tests do not pass; skipped cached-build jobs are
   allowed. Jobs are read from a specific run attempt, not mixed across reruns.
6. Merges the PR (squash, pinned to the tested SHA) after the remaining head-commit
   checks and statuses pass. Historical `PR Label` runs are excluded from this extra
   gate: the linked run is the authority for lab results. Branch protection still applies.

Comments show GitHub's raw **Workflow state** (for example, `queued`, `in_progress`,
or `completed`) separately from the **Orchestrator state** (for example, `pending`,
`waiting-for-checks`, or `merged`). States are refreshed on each poll.

### Setup

- `ORCHESTRATOR_TOKEN` secret: a token (e.g. fine-grained PAT or GitHub App token) with
  *Pull requests: read & write*, *Issues: read & write*, *Contents: read & write* and
  *Actions / Checks / Commit statuses: read* on the snap repositories.
  The default `GITHUB_TOKEN` can't be used: it has no access to other repositories, and
  labels added with it would not trigger workflows.
- Runs are **dry-run by default**, without comment, label or merge writes. Set the
  `DRY_RUN` repository variable explicitly to `false` to enable scheduled writes.
  Manual runs have an independent `dry-run` input, enabled by default.
- The scheduled workflow must be on the default branch. Polling is intentional;
  no webhook configuration is needed.
- `REQUIRED_CHECK` is no longer used. Lab execution is identified from the existing
  shared workflow's job names, with no fallback to unrelated successful checks.

### Run locally

```shell
GITHUB_TOKEN=... python3 orchestrator.py --repo canonical/deepseek-r1-snap
# Explicitly enable writes:
GITHUB_TOKEN=... python3 orchestrator.py --apply --repo canonical/deepseek-r1-snap
```

### Known limitations

- The normal polling interval is 5 minutes; GitHub may delay or skip scheduled runs.
- There is one automatic label-add attempt per SHA, including when the request fails
  or the workflow never starts. The comment is persisted before the label request.
  Missing runs and failed tests are logged and left for triage; there are no automatic
  retries. A rerun of the linked run can be evaluated, but another run ID is not
  automatically substituted once linked.
- Snap repositories must use the existing `PR Label` workflow and shared lab-job naming
  convention. Renaming them requires updating this orchestrator.
- Runs should not overlap across local processes or other orchestrator installations.
  GitHub Actions concurrency serializes this repository's workflow; PR comments are
  persistent state, not a cross-process lock.
- Merging may be refused by branch protection (e.g. required reviews); the error is
  logged and the run fails.
