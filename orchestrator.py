#!/usr/bin/env python3
"""Orchestrate lab tests and merges for Renovate runtime PRs in inference snaps.

On every run this script:

1. Fetches the list of onboarded inference snaps.
2. Lists the open pull requests of each snap repository.
3. For each PR opened by Renovate that changes a runtime dependency affecting
   inference (llama.cpp, OpenVINO, Intel compute runtime, drivers, ...):
   - attempts the trigger label once per head SHA, recording it in a PR comment,
   - updates that comment with the associated workflow run and its outcome,
   - merges the tested SHA only after the lab workflow and other checks pass.

Only the Python standard library is used.
"""

import argparse
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime

CATALOG_URL = "https://canonical.github.io/inference-snaps-admin/onboarded-snaps.json"
GITHUB_API_URL = "https://api.github.com"
TRIGGER_LABEL = "trigger-tests"
RENOVATE_AUTHORS = {"renovate[bot]"}
LAB_WORKFLOW = ".github/workflows/pr-label.yaml"
LAB_JOB_RE = re.compile(r"^trigger-tests / test(?: \(| /|$)")

# Matched only against changes in snapcraft.yaml.
RUNTIME_PATTERNS = {
    "llama.cpp": re.compile(r"llama\.?cpp", re.IGNORECASE),
    "openvino": re.compile(r"openvino|model_server|\bovms", re.IGNORECASE),
    "intel-compute-runtime": re.compile(r"compute-runtime", re.IGNORECASE),
    "intel-npu-driver": re.compile(r"npu-driver", re.IGNORECASE),
    "level-zero": re.compile(r"level-zero", re.IGNORECASE),
}

# Comment recording the head SHA for which the tests were triggered
TRIGGER_MARKER = "<!-- inference-snaps-ci-orchestrator: tests triggered for {sha} -->\nLab-test trigger attempt for {sha}."
TRIGGER_MARKER_RE = re.compile(r"<!-- inference-snaps-ci-orchestrator: tests triggered for ([0-9a-f]{40}) -->")
TRIGGER_STATE_RE = re.compile(r"<!-- inference-snaps-ci-orchestrator: state (.+) -->")
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}

SUCCESSFUL_CONCLUSIONS = {"success", "neutral", "skipped"}

# Possible outcomes of the test evaluation
PENDING = "pending"
FAILED = "failed"
PASSED = "passed"

log = logging.getLogger("orchestrator")


class GitHub:
    """Minimal GitHub REST API client."""

    def __init__(self, token, api_url=GITHUB_API_URL):
        self.token = token
        self.api_url = api_url.rstrip("/")

    def _request(self, method, url, body=None):
        if not url.startswith("http"):
            url = self.api_url + url
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        if self.token:
            request.add_header("Authorization", "Bearer " + self.token)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=60) as response:
            content = response.read()
            payload = json.loads(content) if content else None
            return payload, response.headers.get("Link", "")

    def get(self, path):
        return self._request("GET", path)[0]

    def get_all(self, path, key=None):
        """GET all pages of a list endpoint.

        `key` is the name of the list in the response for endpoints that wrap
        the list in an object (e.g. "check_runs").
        """
        separator = "&" if "?" in path else "?"
        url = f"{path}{separator}per_page=100"
        items = []
        while url:
            payload, link = self._request("GET", url)
            items.extend(payload[key] if key else payload)
            url = _next_link(link)
        return items

    def post(self, path, body):
        return self._request("POST", path, body)[0]

    def put(self, path, body):
        return self._request("PUT", path, body)[0]

    def patch(self, path, body):
        return self._request("PATCH", path, body)[0]

    def delete(self, path):
        return self._request("DELETE", path)[0]


def _next_link(link_header):
    for part in link_header.split(","):
        match = re.match(r'\s*<([^>]+)>;\s*rel="next"', part)
        if match:
            return match.group(1)
    return None


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fetch_catalog(url=CATALOG_URL):
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.loads(response.read())


def repos_from_catalog(catalog):
    """Return the sorted, unique "owner/name" GitHub repos in the catalog."""
    repos = set()
    for entry in catalog:
        url = (entry.get("repo_url") or "").strip()
        parsed = urllib.parse.urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        if parsed.netloc != "github.com" or len(parts) < 2:
            log.warning("Skipping %s: unsupported repo_url %r", entry.get("snap"), url)
            continue
        name = parts[1].removesuffix(".git")
        repos.add(f"{parts[0]}/{name}")
    return sorted(repos)


def dependency_changes(files):
    """Yield changed snapcraft lines, including sources of changed source tags."""
    for f in files:
        if f["filename"] not in {"snap/snapcraft.yaml", "snapcraft.yaml"}:
            continue
        if not f.get("patch"):
            log.warning("%s: missing patch; cannot classify runtime changes", f["filename"])
            continue
        source = None
        for line in (f.get("patch") or "").splitlines():
            if line.startswith("@@"):
                source = None
                continue
            # Renovate changes source-tag while leaving the source URL untouched.
            match = re.match(r" ([ \t]+)source:\s*(.+)", line)
            if match:
                source = (match.group(1), match.group(2))
            elif source and not re.match(r"[ +\-]" + re.escape(source[0]) + r"source-tag:", line):
                source = None
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
                yield line[1:]
                if source:
                    yield source[1]


def runtime_dependencies(files):
    """Return the names of the runtime dependencies changed by the PR."""
    texts = list(dependency_changes(files))
    return sorted(
        name
        for name, pattern in RUNTIME_PATTERNS.items()
        if any(pattern.search(text) for text in texts)
    )


@dataclass
class Trigger:
    sha: str
    comment_id: int
    triggered_at: datetime
    run_id: int | None = None
    state: str = "waiting-for-run"
    body: str = ""


def find_trigger(comments, sha):
    """Find the recorded attempt for this SHA."""
    for comment in reversed(comments):
        trusted = (
            comment.get("author_association") in TRUSTED_ASSOCIATIONS
            or (comment.get("user") or {}).get("type") == "Bot"
        )
        match = TRIGGER_MARKER_RE.search(comment.get("body") or "")
        if trusted and match and match.group(1) == sha:
            metadata = TRIGGER_STATE_RE.search(comment["body"])
            if metadata is None:
                raise ValueError("Missing state in trigger comment")
            state = json.loads(metadata.group(1))
            if not isinstance(state, dict) or "run_id" not in state or not isinstance(state.get("state"), str):
                raise ValueError("Invalid state in trigger comment")
            run_id = state["run_id"]
            if run_id is not None and (type(run_id) is not int or run_id <= 0):
                raise ValueError("Invalid workflow run ID in trigger comment")
            return Trigger(
                sha, comment["id"], parse_time(comment["created_at"]),
                run_id, state["state"], comment["body"],
            )
    return None


def trigger_body(repo, sha, run_id=None, state="waiting-for-run", workflow_state=None):
    metadata = json.dumps({"run_id": run_id, "state": state}, sort_keys=True)
    run_link = f"[{run_id}](https://github.com/{repo}/actions/runs/{run_id})" if run_id is not None else "not linked yet"
    return (
        TRIGGER_MARKER.format(sha=sha)
        + f"\n<!-- inference-snaps-ci-orchestrator: state {metadata} -->"
        + f"\nWorkflow run: {run_link}."
        + (f"\nWorkflow state: {workflow_state}." if workflow_state is not None else "")
        + f"\nOrchestrator state: {state}."
    )


def evaluate_lab_run(run, jobs):
    """Require a finished workflow and actual success of its entire lab matrix."""
    if run["status"] != "completed":
        return PENDING
    if run["conclusion"] != "success":
        return FAILED
    lab_jobs = [job for job in jobs if LAB_JOB_RE.match(job["name"])]
    if not lab_jobs:
        return FAILED
    if any(job["status"] != "completed" for job in jobs):
        return PENDING
    if any(job["conclusion"] != "success" for job in lab_jobs):
        return FAILED
    if any(job["conclusion"] not in SUCCESSFUL_CONCLUSIONS for job in jobs):
        return FAILED
    return PASSED


def evaluate_checks(check_runs, statuses):
    """Evaluate the remaining commit checks, not evidence of lab execution."""
    if any(run["status"] != "completed" for run in check_runs):
        return PENDING
    if any(status["state"] == "pending" for status in statuses):
        return PENDING
    if any(run["conclusion"] not in SUCCESSFUL_CONCLUSIONS for run in check_runs):
        return FAILED
    if any(status["state"] != "success" for status in statuses):
        return FAILED
    return PASSED


class Orchestrator:
    def __init__(self, github, dry_run=True, merge_method="squash"):
        self.github = github
        self.dry_run = dry_run
        self.merge_method = merge_method

    def run(self, repos):
        errors = 0
        for repo in repos:
            try:
                pulls = self.github.get_all(f"/repos/{repo}/pulls?state=open")
            except (urllib.error.URLError, ValueError) as e:
                log.error("%s: failed to list PRs: %s", repo, e)
                errors += 1
                continue
            for pr in pulls:
                try:
                    self.process(repo, pr)
                except (urllib.error.URLError, ValueError) as e:
                    log.error("%s#%s: %s", repo, pr["number"], e)
                    errors += 1
        return errors

    def process(self, repo, pr):
        """Process a PR and return the action taken (for logging and tests)."""
        ref = f"{repo}#{pr['number']}"
        if pr["user"]["login"] not in RENOVATE_AUTHORS:
            return "skip-author"
        if pr.get("draft"):
            log.info("%s: skipping draft PR", ref)
            return "skip-draft"

        files = self.github.get_all(f"/repos/{repo}/pulls/{pr['number']}/files")
        dependencies = runtime_dependencies(files)
        if not dependencies:
            log.info("%s: no runtime dependency changes", ref)
            return "skip-not-runtime"
        log.info("%s: %r changes runtime dependencies: %s", ref, pr["title"], ", ".join(dependencies))

        sha = pr["head"]["sha"]
        comments = self.github.get_all(f"/repos/{repo}/issues/{pr['number']}/comments")
        trigger = find_trigger(comments, sha)

        if trigger is None:
            self.trigger_tests(repo, pr, ref)
            return "label"

        runs = self.github.get_all(
            f"/repos/{repo}/actions/workflows/pr-label.yaml/runs?head_sha={sha}&event=pull_request",
            key="workflow_runs",
        )
        if trigger.run_id is None:
            candidates = []
            for run in runs:
                if self.matches_trigger(run, pr, trigger):
                    jobs = self.run_jobs(repo, run)
                    if any(job["name"].startswith("trigger-tests / ") for job in jobs):
                        candidates.append(run)
            if len(candidates) != 1:
                state = "waiting-for-run" if not candidates else "ambiguous-runs"
                log.warning("%s: %s for %s; no automatic retry, needs triage", ref, state, sha)
                self.update_trigger(repo, trigger, state)
                return "wait"
            trigger.run_id = candidates[0]["id"]
            self.update_trigger(repo, trigger, "linked")

        run = self.github.get(f"/repos/{repo}/actions/runs/{trigger.run_id}")
        if not self.matches_trigger(run, pr, trigger):
            log.error("%s: linked run %s does not match the trigger", ref, trigger.run_id)
            self.update_trigger(repo, trigger, "invalid-run")
            return "failed"
        result = evaluate_lab_run(run, self.run_jobs(repo, run))
        state = result
        if result == PASSED:
            # Old label events have different job names and may be cancelled.
            # The bound run above is the sole authority for this workflow.
            label_suites = {item["check_suite_id"] for item in runs}
            label_suites.add(run["check_suite_id"])
            check_runs = self.github.get_all(f"/repos/{repo}/commits/{sha}/check-runs", key="check_runs")
            checks = [check for check in check_runs if check["check_suite"]["id"] not in label_suites]
            statuses = self.github.get_all(f"/repos/{repo}/commits/{sha}/status", key="statuses")
            result = evaluate_checks(checks, statuses)
            state = "waiting-for-checks" if result == PENDING else result
            if result == PASSED:
                latest = self.github.get(f"/repos/{repo}/actions/runs/{trigger.run_id}")
                if (
                    latest["run_attempt"] != run["run_attempt"]
                    or latest["status"] != "completed"
                    or latest["conclusion"] != "success"
                ):
                    log.info("%s: linked run changed during evaluation; waiting for the next poll", ref)
                    result = PENDING
                    state = result
                    run = latest
        self.update_trigger(repo, trigger, state, run["status"])
        if result != PASSED:
            if result == FAILED:
                log.warning("%s: run %s or commit checks failed; needs triage", ref, trigger.run_id)
                return "failed"
            log.info("%s: %s (run %s)", ref, state, trigger.run_id)
            return "wait"

        log.info("%s: tests passed on %s, merging", ref, sha)
        if not self.dry_run:
            response = self.github.put(
                f"/repos/{repo}/pulls/{pr['number']}/merge",
                {"sha": sha, "merge_method": self.merge_method},
            )
            if not response.get("merged"):
                raise ValueError(f"Merge refused: {response.get('message')}")
            self.update_trigger(repo, trigger, "merged", run["status"])
        return "merge"

    @staticmethod
    def matches_trigger(run, pr, trigger):
        return (
            run["head_sha"] == trigger.sha
            and run["head_branch"] == pr["head"]["ref"]
            and run["path"] == LAB_WORKFLOW
            and run["event"] == "pull_request"
            and parse_time(run["created_at"]) >= trigger.triggered_at
            and (
                not run["pull_requests"]
                or any(pull["number"] == pr["number"] for pull in run["pull_requests"])
            )
        )

    def run_jobs(self, repo, run):
        return self.github.get_all(
            f"/repos/{repo}/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs",
            key="jobs",
        )

    def update_trigger(self, repo, trigger, state, workflow_state=None):
        body = trigger_body(repo, trigger.sha, trigger.run_id, state, workflow_state)
        if body != trigger.body and not self.dry_run:
            self.github.patch(
                f"/repos/{repo}/issues/comments/{trigger.comment_id}", {"body": body},
            )
        trigger.state = state
        trigger.body = body

    def trigger_tests(self, repo, pr, ref):
        sha = pr["head"]["sha"]
        log.info("%s: triggering tests for %s", ref, sha)
        if not self.dry_run:
            # Persist before attempting the label: even a failed/uncertain request
            # must not cause another automatic attempt on the next poll.
            self.github.post(
                f"/repos/{repo}/issues/{pr['number']}/comments",
                {"body": trigger_body(repo, sha)},
            )
        labels = {label["name"] for label in pr.get("labels", [])}
        if TRIGGER_LABEL in labels:
            self.remove_label(repo, pr, ref)
        self.add_label(repo, pr, ref)

    def add_label(self, repo, pr, ref):
        log.info("%s: adding %r label", ref, TRIGGER_LABEL)
        if not self.dry_run:
            self.github.post(f"/repos/{repo}/issues/{pr['number']}/labels", {"labels": [TRIGGER_LABEL]})

    def remove_label(self, repo, pr, ref):
        log.info("%s: removing %r label", ref, TRIGGER_LABEL)
        if not self.dry_run:
            label = urllib.parse.quote(TRIGGER_LABEL, safe="")
            self.github.delete(f"/repos/{repo}/issues/{pr['number']}/labels/{label}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--catalog-url", default=CATALOG_URL)
    parser.add_argument("--repo", action="append", help="process only this owner/name repo (repeatable)")
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument(
        "--dry-run",
        action="store_true",
        default=os.environ.get("DRY_RUN", "").lower() != "false",
        help="only log actions (default unless DRY_RUN=false)",
    )
    execution.add_argument("--apply", action="store_false", dest="dry_run", help="enable comments, labels and merges")
    parser.add_argument("--merge-method", default="squash", choices=["merge", "squash", "rebase"])
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        log.error("GITHUB_TOKEN is not set")
        return 1

    repos = args.repo or repos_from_catalog(fetch_catalog(args.catalog_url))
    log.info("Scanning %d repos%s", len(repos), " (dry run)" if args.dry_run else "")
    errors = Orchestrator(GitHub(token), args.dry_run, args.merge_method).run(repos)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
