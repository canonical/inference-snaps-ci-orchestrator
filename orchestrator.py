#!/usr/bin/env python3
"""Orchestrate lab tests and merges for Renovate runtime PRs in inference snaps.

On every run this script:

1. Fetches the list of onboarded inference snaps.
2. Lists the open pull requests of each snap repository.
3. For each PR opened by Renovate that changes a runtime dependency affecting
   inference (llama.cpp, OpenVINO, Intel compute runtime, drivers, ...):
   - adds the trigger label (which starts the lab tests in the snap repo),
     recording the tested head SHA in a PR comment,
   - re-adds the label if the head SHA changed since the tests were triggered,
   - merges the PR once all checks have passed, including checks that
     started after the tests were triggered.

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
from datetime import datetime

CATALOG_URL = "https://canonical.github.io/inference-snaps-admin/onboarded-snaps.json"
GITHUB_API_URL = "https://api.github.com"
TRIGGER_LABEL = "trigger-tests"
RENOVATE_AUTHORS = {"renovate[bot]"}

# Patterns matched (case-insensitively) against the PR title and the lines
# changed by the PR. A match means the PR touches an inference runtime.
RUNTIME_PATTERNS = {
    "llama.cpp": re.compile(r"llama\.?cpp", re.IGNORECASE),
    "openvino": re.compile(r"openvino|model_server|\bovms", re.IGNORECASE),
    "intel-compute-runtime": re.compile(r"compute-runtime", re.IGNORECASE),
    "intel-npu-driver": re.compile(r"npu-driver", re.IGNORECASE),
    "level-zero": re.compile(r"level-zero", re.IGNORECASE),
}

# Comment recording the head SHA for which the tests were triggered
TRIGGER_MARKER = "<!-- inference-snaps-ci-orchestrator: tests triggered for {sha} -->\nTriggered lab tests for {sha}."
TRIGGER_MARKER_RE = re.compile(r"<!-- inference-snaps-ci-orchestrator: tests triggered for ([0-9a-f]{40}) -->")
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


def changed_lines(files):
    """Yield the lines added or removed by the PR."""
    for f in files:
        for line in (f.get("patch") or "").splitlines():
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
                yield line[1:]


def runtime_dependencies(pr, files):
    """Return the names of the runtime dependencies changed by the PR."""
    texts = [pr.get("title") or ""] + list(changed_lines(files))
    return sorted(
        name
        for name, pattern in RUNTIME_PATTERNS.items()
        if any(pattern.search(text) for text in texts)
    )


def last_trigger(comments):
    """Return (sha, triggered_at) of the last tests trigger, or (None, None).

    Each trigger is recorded by a marker comment with the tested head SHA.
    Only comments from repository members and bots are trusted.
    """
    for comment in reversed(comments):
        trusted = (
            comment.get("author_association") in TRUSTED_ASSOCIATIONS
            or (comment.get("user") or {}).get("type") == "Bot"
        )
        match = TRIGGER_MARKER_RE.search(comment.get("body") or "")
        if trusted and match:
            return match.group(1), parse_time(comment["created_at"])
    return None, None


def evaluate_tests(check_runs, combined_status, triggered_at, required_check=None):
    """Decide whether the tests of a commit are PENDING, FAILED or PASSED.

    Tests have passed when every check run and commit status succeeded, and
    the tests triggered by the label have actually run: at least one check
    started after the trigger. If `required_check` (regex) is set, a check
    run with a matching name must have started after the trigger.
    """
    statuses = combined_status.get("statuses") or []
    if any(run["status"] != "completed" for run in check_runs):
        return PENDING
    if statuses and combined_status.get("state") == "pending":
        return PENDING
    if any(run["conclusion"] not in SUCCESSFUL_CONCLUSIONS for run in check_runs):
        return FAILED
    if statuses and combined_status.get("state") != "success":
        return FAILED

    def after_trigger(t):
        return t and parse_time(t) >= triggered_at

    if required_check:
        pattern = re.compile(required_check)
        if not any(pattern.search(run.get("name", "")) and after_trigger(run.get("started_at")) for run in check_runs):
            return PENDING
        return PASSED

    started = [run.get("started_at") for run in check_runs]
    started += [status.get("created_at") for status in statuses]
    if not any(after_trigger(t) for t in started):
        return PENDING
    return PASSED


class Orchestrator:
    def __init__(self, github, dry_run=False, merge_method="squash", required_check=None):
        self.github = github
        self.dry_run = dry_run
        self.merge_method = merge_method
        self.required_check = required_check

    def run(self, repos):
        errors = 0
        for repo in repos:
            try:
                pulls = self.github.get_all(f"/repos/{repo}/pulls?state=open")
            except urllib.error.URLError as e:
                log.error("%s: failed to list PRs: %s", repo, e)
                errors += 1
                continue
            for pr in pulls:
                try:
                    self.process(repo, pr)
                except urllib.error.URLError as e:
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
        dependencies = runtime_dependencies(pr, files)
        if not dependencies:
            log.info("%s: no runtime dependency changes", ref)
            return "skip-not-runtime"
        log.info("%s: %r changes runtime dependencies: %s", ref, pr["title"], ", ".join(dependencies))

        sha = pr["head"]["sha"]
        labels = {label["name"] for label in pr.get("labels", [])}
        comments = self.github.get_all(f"/repos/{repo}/issues/{pr['number']}/comments")
        triggered_sha, triggered_at = last_trigger(comments)

        if TRIGGER_LABEL not in labels:
            self.trigger_tests(repo, pr, ref)
            return "label"
        if triggered_sha != sha:
            log.info("%s: tests were not triggered for %s, re-triggering", ref, sha)
            self.remove_label(repo, pr, ref)
            self.trigger_tests(repo, pr, ref)
            return "relabel"

        check_runs = self.github.get_all(f"/repos/{repo}/commits/{sha}/check-runs", key="check_runs")
        combined_status = self.github.get(f"/repos/{repo}/commits/{sha}/status")
        result = evaluate_tests(check_runs, combined_status, triggered_at, self.required_check)
        if result == PENDING:
            log.info("%s: waiting for tests on %s", ref, sha)
            return "wait"
        if result == FAILED:
            log.warning("%s: tests failed on %s, needs manual attention", ref, sha)
            return "failed"

        log.info("%s: tests passed on %s, merging", ref, sha)
        if not self.dry_run:
            self.github.put(
                f"/repos/{repo}/pulls/{pr['number']}/merge",
                {"sha": sha, "merge_method": self.merge_method},
            )
        return "merge"

    def trigger_tests(self, repo, pr, ref):
        # The marker is posted before the label so that every check started
        # by the label starts after the marker.
        sha = pr["head"]["sha"]
        log.info("%s: triggering tests for %s", ref, sha)
        if not self.dry_run:
            self.github.post(
                f"/repos/{repo}/issues/{pr['number']}/comments",
                {"body": TRIGGER_MARKER.format(sha=sha)},
            )
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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=os.environ.get("DRY_RUN", "").lower() == "true",
        help="only log the actions (default: $DRY_RUN == 'true')",
    )
    parser.add_argument("--merge-method", default="squash", choices=["merge", "squash", "rebase"])
    parser.add_argument(
        "--required-check",
        default=os.environ.get("REQUIRED_CHECK") or None,
        help="regex of the lab test check run name required to merge (default: $REQUIRED_CHECK)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        log.error("GITHUB_TOKEN is not set")
        return 1

    repos = args.repo or repos_from_catalog(fetch_catalog(args.catalog_url))
    log.info("Scanning %d repos%s", len(repos), " (dry run)" if args.dry_run else "")
    errors = Orchestrator(GitHub(token), args.dry_run, args.merge_method, args.required_check).run(repos)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
