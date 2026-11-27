#!/usr/bin/env python3
"""Orchestrate lab tests and merges for Renovate runtime PRs in inference snaps.

On every run this script:

1. Fetches the list of onboarded inference snaps.
2. Lists the open pull requests of each snap repository.
3. For each PR opened by Renovate that changes a runtime dependency affecting
   inference (llama.cpp, OpenVINO, Intel compute runtime, drivers, ...):
   - reconciles delivery of the trigger label per head SHA using a PR comment,
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
from datetime import datetime, timedelta, timezone

CATALOG_URL = "https://canonical.github.io/inference-snaps-admin/onboarded-snaps.json"
GITHUB_API_URL = "https://api.github.com"
TRIGGER_LABEL = "trigger-tests"
RENOVATE_AUTHORS = {"renovate[bot]"}
LAB_WORKFLOW = ".github/workflows/pr-label.yaml"
LAB_JOB_RE = re.compile(r"^trigger-tests / test(?: \(| /|$)")
MAX_LABEL_ATTEMPTS = 3
LABEL_RETRY_GRACE = timedelta(minutes=5)
RUN_DISCOVERY_TIMEOUT = timedelta(minutes=30)

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
ORCHESTRATOR_LOGS_RE = re.compile(r"\nOrchestrator logs: \[View run\]\([^\n]+\)\.$", re.MULTILINE)
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

    def graphql(self, query, variables):
        response = self.post("/graphql", {"query": query, "variables": variables})
        if response.get("errors"):
            messages = "; ".join(error["message"] for error in response["errors"])
            raise ValueError(f"GitHub GraphQL: {messages}")
        return response["data"]

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


def uncertain_write(error):
    if isinstance(error, urllib.error.HTTPError):
        return error.code == 408 or 500 <= error.code < 600
    return True  # A transport failure may happen after GitHub applied the write.


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
    state: str = "ready-to-label"
    body: str = ""
    label_attempts: int = 0
    label_requested_at: datetime | None = None


def trusted_comment(comment):
    return (
        comment.get("author_association") in TRUSTED_ASSOCIATIONS
        or (comment.get("user") or {}).get("type") == "Bot"
    )


def find_trigger(comments, sha):
    """Find the recorded attempt for this SHA."""
    for comment in reversed(comments):
        match = TRIGGER_MARKER_RE.search(comment.get("body") or "")
        if trusted_comment(comment) and match and match.group(1) == sha:
            metadata = TRIGGER_STATE_RE.search(comment["body"])
            if metadata is None:
                raise ValueError("Missing state in trigger comment")
            state = json.loads(metadata.group(1))
            if not isinstance(state, dict) or "run_id" not in state or not isinstance(state.get("state"), str):
                raise ValueError("Invalid state in trigger comment")
            run_id = state["run_id"]
            if run_id is not None and (type(run_id) is not int or run_id <= 0):
                raise ValueError("Invalid workflow run ID in trigger comment")
            created_at = parse_time(comment["created_at"])
            phase = state["state"]
            attempts = state.get("label_attempts", 0)
            requested_at = state.get("label_requested_at")
            if (
                type(attempts) is not int or attempts < 0
                or (attempts == 0 and requested_at is not None)
                or (attempts > 0 and not isinstance(requested_at, str))
            ):
                raise ValueError("Invalid label attempt in trigger comment")
            if requested_at is not None:
                requested_at = parse_time(requested_at)
                if requested_at.tzinfo is None:
                    raise ValueError("Missing timezone for label attempt in trigger comment")
            elif "label_attempts" not in state and phase == "waiting-for-run" and run_id is None:
                # Legacy comments did not distinguish recording intent from sending the label.
                attempts, requested_at, phase = 1, created_at, "label-requested"
            return Trigger(sha, comment["id"], created_at, run_id, phase, comment["body"], attempts, requested_at)
    return None


def with_orchestrator_logs(body, url):
    body = ORCHESTRATOR_LOGS_RE.sub("", body)
    return body + f"\nOrchestrator logs: [View run]({url})." if url else body


def trigger_body(repo, sha, run_id=None, state="ready-to-label", workflow_state=None,
                 *, label_attempts=0, label_requested_at=None, orchestrator_run_url=None):
    metadata = json.dumps({
        "run_id": run_id,
        "state": state,
        "label_attempts": label_attempts,
        "label_requested_at": label_requested_at.isoformat() if label_requested_at is not None else None,
    }, sort_keys=True)
    run_link = f"[{run_id}](https://github.com/{repo}/actions/runs/{run_id})" if run_id is not None else "not linked yet"
    body = (
        TRIGGER_MARKER.format(sha=sha)
        + f"\n<!-- inference-snaps-ci-orchestrator: state {metadata} -->"
        + f"\nWorkflow run: {run_link}."
        + (f"\nWorkflow state: {workflow_state}." if workflow_state is not None else "")
        + f"\nOrchestrator state: {state}."
    )
    return with_orchestrator_logs(body, orchestrator_run_url)


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
    def __init__(self, github, dry_run=True, merge_method="squash", *, orchestrator_run_url=None):
        self.github = github
        self.dry_run = dry_run
        self.merge_method = merge_method
        self.orchestrator_run_url = orchestrator_run_url

    def run(self, repos):
        errors = 0
        for repo in repos:
            try:
                pulls = self.github.get_all(f"/repos/{repo}/pulls?state=open")
            except (urllib.error.URLError, TimeoutError, ValueError) as e:
                log.error("%s: failed to list PRs: %s", repo, e)
                errors += 1
                continue
            for pr in pulls:
                try:
                    self.process(repo, pr)
                except (urllib.error.URLError, TimeoutError, ValueError) as e:
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
        comments_path = f"/repos/{repo}/issues/{pr['number']}/comments"
        comments = self.github.get_all(comments_path)
        trigger = find_trigger(comments, sha)

        if trigger is None:
            if self.dry_run:
                log.info("%s: would record a trigger and reconcile the label for %s", ref, sha)
                return "label"
            trigger = self.record_trigger(repo, comments_path, sha, ref)
            self.minimize_outdated_comments(comments, sha, ref)
        else:
            # Refresh the log link even if polling doesn't change the comment's test status.
            self.save_trigger_body(repo, trigger, with_orchestrator_logs(trigger.body, self.orchestrator_run_url))

        runs = self.github.get_all(
            f"/repos/{repo}/actions/workflows/pr-label.yaml/runs?head_sha={sha}&event=pull_request",
            key="workflow_runs",
        )
        if trigger.run_id is None:
            candidates = []
            jobs_pending = False
            for run in runs:
                if self.matches_trigger(run, pr, trigger):
                    jobs = self.run_jobs(repo, run)
                    if any(job["name"].startswith("trigger-tests / ") for job in jobs):
                        candidates.append(run)
                    elif not jobs and run["status"] != "completed":
                        jobs_pending = True
            if len(candidates) > 1:
                log.warning("%s: multiple matching test runs for %s; needs triage", ref, sha)
                self.update_trigger(repo, trigger, "ambiguous-runs")
                return "wait"
            if trigger.state == "ambiguous-runs":
                return "wait"  # A later listing with fewer runs does not resolve an earlier ambiguity.
            if jobs_pending or not candidates:
                return self.reconcile_label(repo, pr, ref, trigger, jobs_pending)
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
        body = trigger_body(
            repo, trigger.sha, trigger.run_id, state, workflow_state,
            label_attempts=trigger.label_attempts, label_requested_at=trigger.label_requested_at,
            orchestrator_run_url=self.orchestrator_run_url,
        )
        self.save_trigger_body(repo, trigger, body)
        trigger.state = state

    def save_trigger_body(self, repo, trigger, body):
        if body != trigger.body and not self.dry_run:
            self.github.patch(
                f"/repos/{repo}/issues/comments/{trigger.comment_id}", {"body": body},
            )
        trigger.body = body

    def record_trigger(self, repo, comments_path, sha, ref):
        log.info("%s: recording lab-test trigger for %s", ref, sha)
        try:
            comment = self.github.post(comments_path, {
                "body": trigger_body(repo, sha, orchestrator_run_url=self.orchestrator_run_url),
            })
        except (urllib.error.URLError, TimeoutError) as error:
            if not uncertain_write(error):
                raise
            log.warning("%s: trigger comment POST uncertain (%s); checking for the saved comment", ref, error)
            trigger = find_trigger(self.github.get_all(comments_path), sha)
            if trigger is None:
                raise
            return trigger
        return Trigger(sha, comment["id"], parse_time(comment["created_at"]), body=comment["body"])

    def minimize_outdated_comments(self, comments, sha, ref):
        node_ids = []
        for comment in comments:
            body = comment.get("body") or ""
            match = TRIGGER_MARKER_RE.match(body)
            if not (trusted_comment(comment) and match and match.group(1) != sha):
                continue
            if not body.startswith(TRIGGER_MARKER.format(sha=match.group(1))) or not TRIGGER_STATE_RE.search(body):
                continue
            if not comment.get("node_id"):
                log.warning("%s: cannot minimize comment %s: missing GraphQL node ID", ref, comment["id"])
                continue
            node_ids.append(comment["node_id"])

        for offset in range(0, len(node_ids), 100):
            batch = node_ids[offset:offset + 100]
            try:
                data = self.github.graphql(
                    """query($ids: [ID!]!) {
                        nodes(ids: $ids) { ... on IssueComment { isMinimized viewerCanMinimize } }
                    }""",
                    {"ids": batch},
                )
            except (urllib.error.URLError, TimeoutError, ValueError) as error:
                log.warning("%s: could not read older comment visibility: %s", ref, error)
                continue
            for node_id, node in zip(batch, data["nodes"]):
                if node and node["isMinimized"]:
                    continue
                if not node or not node["viewerCanMinimize"]:
                    log.warning("%s: cannot minimize comment %s with this token", ref, node_id)
                    continue
                try:
                    data = self.github.graphql(
                        """mutation($id: ID!) {
                            minimizeComment(input: {subjectId: $id, classifier: OUTDATED}) {
                                minimizedComment { isMinimized }
                            }
                        }""",
                        {"id": node_id},
                    )
                    if not data["minimizeComment"]["minimizedComment"]["isMinimized"]:
                        raise ValueError("GitHub did not minimize the comment")
                except (urllib.error.URLError, TimeoutError, ValueError) as error:
                    log.warning("%s: could not minimize comment %s: %s", ref, node_id, error)

    def reconcile_label(self, repo, pr, ref, trigger, jobs_pending):
        if trigger.state == "needs-triage":
            return "wait"  # Still look for a matching run on later polls, but never write another label.

        now = datetime.now(timezone.utc)
        age = now - (trigger.label_requested_at or trigger.triggered_at)
        if trigger.state == "waiting-for-run":
            # Delivery was already confirmed; even add-then-remove is not a reason to retry.
            if age >= RUN_DISCOVERY_TIMEOUT:
                return self.label_triage(repo, trigger, ref, "label delivered but no unique test run found")
            log.info("%s: label delivered; waiting for test run", ref)
            return "wait"

        issue_path = f"/repos/{repo}/issues/{pr['number']}"
        events = self.github.get_all(f"{issue_path}/timeline")
        # Use the first intent as the lower bound so a late event from any attempt blocks retries.
        label_events = {
            event["event"] for event in events
            if event["event"] in {"labeled", "unlabeled"}
            and event["label"]["name"] == TRIGGER_LABEL
            and parse_time(event["created_at"]) >= trigger.triggered_at
        }
        if "labeled" in label_events:
            if age >= RUN_DISCOVERY_TIMEOUT:
                return self.label_triage(repo, trigger, ref, "label delivered but no unique test run found")
            self.update_trigger(repo, trigger, "waiting-for-run")
            log.info("%s: observed label addition; waiting for test run", ref)
            return "wait"

        current = self.github.get(f"/repos/{repo}/pulls/{pr['number']}")
        if current["head"]["sha"] != trigger.sha or current["state"] != "open" or current.get("draft"):
            log.info("%s: PR changed since scan; not labeling %s", ref, trigger.sha)
            return "wait"
        label_present = any(label["name"] == TRIGGER_LABEL for label in current["labels"])
        if jobs_pending or label_present or (trigger.label_attempts and "unlabeled" in label_events):
            reason = "possible test run or label delivery cannot be confirmed"
            if age >= RUN_DISCOVERY_TIMEOUT:
                return self.label_triage(repo, trigger, ref, reason)
            log.info("%s: %s; not relabeling yet", ref, reason)
            return "wait"

        if trigger.label_attempts:
            grace = LABEL_RETRY_GRACE * 2 ** (min(trigger.label_attempts, MAX_LABEL_ATTEMPTS) - 1)
            if age < grace:
                log.info("%s: waiting to reconcile label attempt %s", ref, trigger.label_attempts)
                return "wait"
            if trigger.label_attempts >= MAX_LABEL_ATTEMPTS:
                return self.label_triage(repo, trigger, ref, "label attempt limit reached")

        log.info("%s: %s %r (attempt %s)", ref, "would add" if self.dry_run else "adding",
                 TRIGGER_LABEL, trigger.label_attempts + 1)
        if self.dry_run:
            return "label"
        trigger.label_attempts += 1
        trigger.label_requested_at = datetime.now(timezone.utc)
        self.update_trigger(repo, trigger, "label-requested")
        try:
            self.github.post(f"{issue_path}/labels", {"labels": [TRIGGER_LABEL]})
        except (urllib.error.URLError, TimeoutError) as error:
            if not uncertain_write(error):
                self.label_triage(repo, trigger, ref, f"label request rejected: {error}")
            raise
        self.update_trigger(repo, trigger, "waiting-for-run")
        return "label"

    def label_triage(self, repo, trigger, ref, reason):
        log.warning("%s: %s; needs triage", ref, reason)
        self.update_trigger(repo, trigger, "needs-triage")
        return "wait"


def actions_run_url(env):
    if env.get("GITHUB_ACTIONS") != "true":
        return None
    server, repo, run_id = (env.get(name) for name in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    if not server or not repo or not run_id:
        log.warning("Missing GitHub Actions run metadata; omitting the orchestrator log link")
        return None
    return f"{server.rstrip('/')}/{repo}/actions/runs/{run_id}"


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
    errors = Orchestrator(
        GitHub(token), args.dry_run, args.merge_method, orchestrator_run_url=actions_run_url(os.environ),
    ).run(repos)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
