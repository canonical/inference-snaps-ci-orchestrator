import unittest
from datetime import datetime, timezone

import orchestrator
from orchestrator import FAILED, PASSED, PENDING, Orchestrator

LABELLED_AT = "2026-11-20T10:00:00Z"
BEFORE = "2026-11-20T09:00:00Z"
AFTER = "2026-11-20T11:00:00Z"

LLAMA_PATCH = """@@ -166,8 +166,8 @@ parts:
   llamacpp:
     plugin: dump
-      - on amd64: https://github.com/canonical/llama.cpp-builds/releases/download/b11408/llamacpp-amd64.tar.gz
+      - on amd64: https://github.com/canonical/llama.cpp-builds/releases/download/b11643/llamacpp-amd64.tar.gz
"""

OTHER_PATCH = """@@ -1,3 +1,3 @@ parts:
   llamacpp:
-    source-tag: v1.0.0
+    source-tag: v1.1.0
"""


def make_pr(number=1, author="renovate[bot]", title="Update dependency foo", labels=(), draft=False):
    return {
        "number": number,
        "title": title,
        "user": {"login": author},
        "draft": draft,
        "labels": [{"name": name} for name in labels],
        "head": {"sha": SHA},
    }


SHA = "a" * 40
OTHER_SHA = "b" * 40


def marker(sha=SHA, at=LABELLED_AT, association="MEMBER", user_type="User"):
    return {
        "body": orchestrator.TRIGGER_MARKER.format(sha=sha),
        "created_at": at,
        "author_association": association,
        "user": {"type": user_type},
    }


def check_run(status="completed", conclusion="success", started_at=AFTER, name="build"):
    return {"name": name, "status": status, "conclusion": conclusion, "started_at": started_at}


class FakeGitHub:
    def __init__(self, files=(), comments=(), check_runs=(), status=None):
        self.files = list(files)
        self.comments = list(comments)
        self.check_runs = list(check_runs)
        self.status = status or {"state": "pending", "statuses": []}
        self.calls = []

    def get_all(self, path, key=None):
        if path.endswith("/files"):
            return self.files
        if path.endswith("/comments"):
            return self.comments
        if path.endswith("/check-runs"):
            return self.check_runs
        raise AssertionError(path)

    def get(self, path):
        if path.endswith("/status"):
            return self.status
        raise AssertionError(path)

    def post(self, path, body):
        self.calls.append(("POST", path, body))

    def put(self, path, body):
        self.calls.append(("PUT", path, body))

    def delete(self, path):
        self.calls.append(("DELETE", path, None))


class TestCatalog(unittest.TestCase):
    def test_repos_from_catalog(self):
        catalog = [
            {"snap": "b", "repo_url": "https://github.com/canonical/b-snap"},
            {"snap": "a", "repo_url": "https://github.com/canonical/a-snap.git"},
            {"snap": "a2", "repo_url": "https://github.com/canonical/a-snap/"},
            {"snap": "c", "repo_url": "https://gitlab.com/canonical/c-snap"},
            {"snap": "d"},
        ]
        self.assertEqual(
            orchestrator.repos_from_catalog(catalog),
            ["canonical/a-snap", "canonical/b-snap"],
        )


class TestRuntimeDependencies(unittest.TestCase):
    def test_detects_changed_lines(self):
        files = [{"filename": "snap/snapcraft.yaml", "patch": LLAMA_PATCH}]
        self.assertEqual(orchestrator.runtime_dependencies(make_pr(), files), ["llama.cpp"])

    def test_ignores_context_lines(self):
        files = [{"filename": "snap/snapcraft.yaml", "patch": OTHER_PATCH}]
        self.assertEqual(orchestrator.runtime_dependencies(make_pr(), files), [])

    def test_detects_title(self):
        for title, expected in [
            ("Update dependency canonical/llama.cpp-builds to v1", "llama.cpp"),
            ("Update dependency openvinotoolkit/model_server to v2026.4.1", "openvino"),
            ("Update dependency canonical/intel-compute-runtime-bundle to v26", "intel-compute-runtime"),
            ("Update dependency intel/linux-npu-driver to v1.30", "intel-npu-driver"),
        ]:
            with self.subTest(title=title):
                self.assertIn(expected, orchestrator.runtime_dependencies(make_pr(title=title), []))

    def test_unrelated(self):
        pr = make_pr(title="Update actions/checkout action to v5")
        files = [{"filename": ".github/workflows/ci.yaml", "patch": "-uses: actions/checkout@v4\n+uses: actions/checkout@v5"}]
        self.assertEqual(orchestrator.runtime_dependencies(pr, files), [])


class TestLastTrigger(unittest.TestCase):
    def test_no_trigger(self):
        self.assertEqual(orchestrator.last_trigger([{"body": "hello", "author_association": "MEMBER"}]), (None, None))

    def test_uses_latest_marker(self):
        comments = [marker(OTHER_SHA, BEFORE), marker(SHA, LABELLED_AT), {"body": "hi", "created_at": AFTER}]
        self.assertEqual(
            orchestrator.last_trigger(comments),
            (SHA, datetime(2026, 11, 20, 10, tzinfo=timezone.utc)),
        )

    def test_trusts_bots(self):
        self.assertEqual(orchestrator.last_trigger([marker(association="NONE", user_type="Bot")])[0], SHA)

    def test_ignores_untrusted_authors(self):
        comments = [marker(OTHER_SHA, BEFORE), marker(SHA, AFTER, association="NONE")]
        self.assertEqual(orchestrator.last_trigger(comments)[0], OTHER_SHA)


class TestEvaluateTests(unittest.TestCase):
    labelled_at = orchestrator.parse_time(LABELLED_AT)
    no_status = {"state": "pending", "statuses": []}

    def evaluate(self, check_runs, status=None, required_check=None):
        return orchestrator.evaluate_tests(check_runs, status or self.no_status, self.labelled_at, required_check)

    def test_passed(self):
        self.assertEqual(self.evaluate([check_run(), check_run(conclusion="skipped")]), PASSED)

    def test_in_progress(self):
        self.assertEqual(self.evaluate([check_run(), check_run(status="in_progress", conclusion=None)]), PENDING)

    def test_failed(self):
        self.assertEqual(self.evaluate([check_run(), check_run(conclusion="failure")]), FAILED)

    def test_no_checks_after_label(self):
        self.assertEqual(self.evaluate([check_run(started_at=BEFORE)]), PENDING)

    def test_no_checks(self):
        self.assertEqual(self.evaluate([]), PENDING)

    def test_required_check(self):
        runs = [check_run(name="build"), check_run(name="lab-tests / gpu", started_at=BEFORE)]
        self.assertEqual(self.evaluate(runs, required_check=r"^lab-tests"), PENDING)
        runs = [check_run(name="build"), check_run(name="lab-tests / gpu")]
        self.assertEqual(self.evaluate(runs, required_check=r"^lab-tests"), PASSED)
        self.assertEqual(self.evaluate([check_run(name="build")], required_check=r"^lab-tests"), PENDING)

    def test_statuses(self):
        status = {"state": "success", "statuses": [{"state": "success", "created_at": AFTER}]}
        self.assertEqual(self.evaluate([], status), PASSED)
        status = {"state": "pending", "statuses": [{"state": "pending", "created_at": AFTER}]}
        self.assertEqual(self.evaluate([check_run()], status), PENDING)
        status = {"state": "failure", "statuses": [{"state": "failure", "created_at": AFTER}]}
        self.assertEqual(self.evaluate([check_run()], status), FAILED)


class TestProcess(unittest.TestCase):
    runtime_files = [{"filename": "snap/snapcraft.yaml", "patch": LLAMA_PATCH}]

    def process(self, github, pr, dry_run=False):
        return Orchestrator(github, dry_run=dry_run).process("canonical/foo-snap", pr)

    def test_skips_other_authors(self):
        github = FakeGitHub(files=self.runtime_files)
        self.assertEqual(self.process(github, make_pr(author="someone")), "skip-author")
        self.assertEqual(github.calls, [])

    def test_skips_drafts(self):
        github = FakeGitHub(files=self.runtime_files)
        self.assertEqual(self.process(github, make_pr(draft=True)), "skip-draft")

    def test_skips_non_runtime(self):
        github = FakeGitHub(files=[{"filename": "README.md", "patch": "-a\n+b"}])
        self.assertEqual(self.process(github, make_pr()), "skip-not-runtime")
        self.assertEqual(github.calls, [])

    def test_adds_label(self):
        github = FakeGitHub(files=self.runtime_files)
        self.assertEqual(self.process(github, make_pr()), "label")
        self.assertEqual(
            github.calls,
            [
                ("POST", "/repos/canonical/foo-snap/issues/1/comments", {"body": orchestrator.TRIGGER_MARKER.format(sha=SHA)}),
                ("POST", "/repos/canonical/foo-snap/issues/1/labels", {"labels": ["trigger-tests"]}),
            ],
        )

    def test_dry_run(self):
        github = FakeGitHub(files=self.runtime_files)
        self.assertEqual(self.process(github, make_pr(), dry_run=True), "label")
        self.assertEqual(github.calls, [])

    def test_relabels_after_push(self):
        github = FakeGitHub(files=self.runtime_files, comments=[marker(OTHER_SHA)], check_runs=[check_run()])
        self.assertEqual(self.process(github, make_pr(labels=["trigger-tests"])), "relabel")
        self.assertEqual([call[0] for call in github.calls], ["DELETE", "POST", "POST"])
        self.assertEqual(github.calls[0][1], "/repos/canonical/foo-snap/issues/1/labels/trigger-tests")
        self.assertIn(SHA, github.calls[1][2]["body"])

    def test_relabels_without_marker(self):
        github = FakeGitHub(files=self.runtime_files, check_runs=[check_run()])
        self.assertEqual(self.process(github, make_pr(labels=["trigger-tests"])), "relabel")

    def test_waits(self):
        github = FakeGitHub(
            files=self.runtime_files,
            comments=[marker()],
            check_runs=[check_run(status="queued", conclusion=None)],
        )
        self.assertEqual(self.process(github, make_pr(labels=["trigger-tests"])), "wait")
        self.assertEqual(github.calls, [])

    def test_failed(self):
        github = FakeGitHub(
            files=self.runtime_files,
            comments=[marker()],
            check_runs=[check_run(conclusion="failure")],
        )
        self.assertEqual(self.process(github, make_pr(labels=["trigger-tests"])), "failed")
        self.assertEqual(github.calls, [])

    def test_merges(self):
        github = FakeGitHub(
            files=self.runtime_files,
            comments=[marker()],
            check_runs=[check_run()],
        )
        self.assertEqual(self.process(github, make_pr(labels=["trigger-tests"])), "merge")
        self.assertEqual(
            github.calls,
            [("PUT", "/repos/canonical/foo-snap/pulls/1/merge", {"sha": SHA, "merge_method": "squash"})],
        )


class TestNextLink(unittest.TestCase):
    def test_next_link(self):
        header = (
            '<https://api.github.com/x?page=2>; rel="next", '
            '<https://api.github.com/x?page=5>; rel="last"'
        )
        self.assertEqual(orchestrator._next_link(header), "https://api.github.com/x?page=2")
        self.assertIsNone(orchestrator._next_link('<https://api.github.com/x?page=1>; rel="prev"'))
        self.assertIsNone(orchestrator._next_link(""))


if __name__ == "__main__":
    unittest.main()
