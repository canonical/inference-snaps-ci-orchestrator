#!/usr/bin/env python3
"""Orchestrate lab tests and merges for Renovate runtime PRs in inference snaps.

On every run this script:

1. Fetches the list of onboarded inference snaps.
2. Lists the open pull requests of each snap repository.
3. For each PR opened by Renovate that changes a runtime dependency affecting
   inference (llama.cpp, OpenVINO, Intel compute runtime, drivers, ...):
   - adds the trigger label (which starts the lab tests in the snap repo),
   - re-adds the label if new commits were pushed after it was added,
   - merges the PR once all checks that started after labelling have passed.

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


def label_state(timeline, label=TRIGGER_LABEL):
    """Return (labelled_at, pushed_after_label) from the PR timeline.

    `labelled_at` is the time the label was last added, or None.
    `pushed_after_label` tells if commits were pushed after that time.
    """
    labelled_at = None
    for event in timeline:
        if event.get("event") == "labeled" and event["label"]["name"] == label:
            labelled_at = max(filter(None, [labelled_at, parse_time(event["created_at"])]))
    if labelled_at is None:
        return None, False

    for event in timeline:
        if event.get("event") == "head_ref_force_pushed":
            pushed_at = event.get("created_at")
        elif event.get("event") == "committed":
            pushed_at = (event.get("committer") or {}).get("date")
        else:
            continue
        if pushed_at and parse_time(pushed_at) > labelled_at:
            return labelled_at, True
    return labelled_at, False


def evaluate_tests(check_runs, combined_status, labelled_at):
    """Decide whether the tests of a commit are PENDING, FAILED or PASSED.

    Tests have passed when every check run and commit status succeeded, and
    at least one of them started after the label was added (i.e. the tests
    triggered by the label have actually run).
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

    started = [run.get("started_at") for run in check_runs]
    started += [status.get("created_at") for status in statuses]
    if not any(t and parse_time(t) >= labelled_at for t in started):
        return PENDING
    return PASSED


class Orchestrator:
    def __init__(self, github, dry_run=False, merge_method="squash"):
        self.github = github
        self.dry_run = dry_run
        self.merge_method = merge_method

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

        labels = {label["name"] for label in pr.get("labels", [])}
        timeline = self.github.get_all(f"/repos/{repo}/issues/{pr['number']}/timeline")
        labelled_at, pushed_after_label = label_state(timeline)

        if TRIGGER_LABEL not in labels or labelled_at is None:
            self.add_label(repo, pr, ref)
            return "label"
        if pushed_after_label:
            log.info("%s: new commits since tests were triggered, re-triggering", ref)
            self.remove_label(repo, pr, ref)
            self.add_label(repo, pr, ref)
            return "relabel"

        sha = pr["head"]["sha"]
        check_runs = self.github.get_all(f"/repos/{repo}/commits/{sha}/check-runs", key="check_runs")
        combined_status = self.github.get(f"/repos/{repo}/commits/{sha}/status")
        result = evaluate_tests(check_runs, combined_status, labelled_at)
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
