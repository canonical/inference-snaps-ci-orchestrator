import copy
import os
import unittest
import urllib.error
from datetime import datetime
from unittest.mock import patch

import orchestrator
from orchestrator import FAILED, PASSED, PENDING, Orchestrator

LABELLED_AT = "2026-11-20T10:00:00Z"
BEFORE = "2026-11-20T09:00:00Z"
AFTER = "2026-11-20T11:00:00Z"
POLL_AT = "2026-11-20T10:10:00Z"
SHA = "a" * 40
OTHER_SHA = "b" * 40
REPO = "canonical/foo-snap"
LAB_NAME = "trigger-tests / test (gpu) / intel-gpu (queue)"

LLAMA_PATCH = """@@ -166,8 +166,8 @@ parts:
   llamacpp:
     plugin: dump
-      - on amd64: https://github.com/canonical/llama.cpp-builds/releases/download/b11408/llamacpp-amd64.tar.gz
+      - on amd64: https://github.com/canonical/llama.cpp-builds/releases/download/b11643/llamacpp-amd64.tar.gz
"""

TAG_PATCH = """@@ -156,4 +156,4 @@ parts:
   cli-intel-opencl-icd:
     source: https://github.com/canonical/intel-compute-runtime-bundle.git
-    source-tag: 26.01.36711.4
+    source-tag: 26.22.38646.4
     plugin: make
"""

OTHER_PATCH = """@@ -1,3 +1,3 @@ parts:
   llamacpp:
-    source-tag: v1.0.0
+    source-tag: v1.1.0
"""


def make_pr(number=1, author="renovate[bot]", title="Update dependency foo", labels=(), draft=False, sha=SHA):
    return {
        "number": number,
        "state": "open",
        "title": title,
        "user": {"login": author},
        "draft": draft,
        "labels": [{"name": name} for name in labels],
        "head": {"sha": sha, "ref": "renovate/runtime"},
    }


def marker(sha=SHA, at=LABELLED_AT, run_id=None, state="waiting-for-run", association="MEMBER"):
    return {
        "id": 10,
        "body": orchestrator.trigger_body(
            REPO, sha, run_id, state,
            label_attempts=1, label_requested_at=orchestrator.parse_time(at),
        ),
        "created_at": at,
        "author_association": association,
        "user": {"type": "User"},
    }


def job(name=LAB_NAME, status="completed", conclusion="success", suite_id=101):
    return {"name": name, "status": status, "conclusion": conclusion, "check_suite": {"id": suite_id}}


def workflow_run(run_id=100, sha=SHA, at=AFTER, status="completed", conclusion="success", **overrides):
    return {
        "id": run_id,
        "head_sha": sha,
        "head_branch": "renovate/runtime",
        "path": orchestrator.LAB_WORKFLOW,
        "event": "pull_request",
        "created_at": at,
        "status": status,
        "conclusion": conclusion,
        "run_attempt": 1,
        "check_suite_id": run_id + 1,
        "pull_requests": [{"number": 1}],
        **overrides,
    }


class FakeGitHub:
    def __init__(self, files=None, comments=(), runs=(), jobs=None, checks=(), statuses=()):
        self.files = files if files is not None else [{"filename": "snap/snapcraft.yaml", "patch": LLAMA_PATCH}]
        self.comments = copy.deepcopy(list(comments))
        self.runs = copy.deepcopy(list(runs))
        self.jobs = jobs if jobs is not None else {100: [job()]}
        self.checks = list(checks)
        self.statuses = list(statuses)
        self.calls = []
        self.reads = []
        self.fail_label = False
        self.merge_response = {"merged": True}
        self.pr = make_pr()

    def get_all(self, path, key=None):
        self.reads.append(path)
        if path.endswith("/pulls?state=open"):
            return [make_pr()]
        if path.endswith("/files"):
            return self.files
        if path.endswith("/comments"):
            return self.comments
        if path.endswith("/timeline"):
            return []
        if "/actions/workflows/pr-label.yaml/runs?" in path:
            return [run for run in self.runs if run["head_sha"] in path]
        if path.endswith("/jobs"):
            run_id = int(path.split("/runs/")[1].split("/")[0])
            return self.jobs.get(run_id, [])
        if path.endswith("/check-runs"):
            return self.checks
        if path.endswith("/status"):
            return self.statuses
        raise AssertionError(path)

    def get(self, path):
        self.reads.append(path)
        if path == f"/repos/{REPO}/pulls/{self.pr['number']}":
            return self.pr
        if "/actions/runs/" in path:
            return copy.deepcopy(next(run for run in self.runs if path.endswith(f"/{run['id']}")))
        raise AssertionError(path)

    def post(self, path, body):
        self.calls.append(("POST", path, body))
        if path.endswith("/comments"):
            comment = marker()
            comment.update(id=len(self.comments) + 20, body=body["body"])
            self.comments.append(comment)
            return comment
        if path.endswith("/labels") and self.fail_label:
            raise urllib.error.URLError("label request failed")

    def patch(self, path, body):
        self.calls.append(("PATCH", path, body))
        for comment in self.comments:
            if path.endswith(f"/{comment['id']}"):
                comment["body"] = body["body"]

    def put(self, path, body):
        self.calls.append(("PUT", path, body))
        return self.merge_response

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
        self.assertEqual(orchestrator.repos_from_catalog(catalog), ["canonical/a-snap", "canonical/b-snap"])


class TestRuntimeDependencies(unittest.TestCase):
    def test_detects_changed_lines_only_in_snapcraft(self):
        for filename in ("snap/snapcraft.yaml", "snapcraft.yaml", "README.md", ".github/workflows/ci.yaml"):
            with self.subTest(filename=filename):
                expected = ["llama.cpp"] if filename in {"snap/snapcraft.yaml", "snapcraft.yaml"} else []
                self.assertEqual(
                    orchestrator.runtime_dependencies([{"filename": filename, "patch": LLAMA_PATCH}]), expected,
                )

    def test_detects_runtime_urls(self):
        for url, expected in [
            ("openvinotoolkit/model_server", "openvino"),
            ("intel/linux-npu-driver", "intel-npu-driver"),
            ("oneapi-src/level-zero", "level-zero"),
        ]:
            with self.subTest(url=url):
                files = [{"filename": "snap/snapcraft.yaml", "patch": f"+    source: https://github.com/{url}/v2"}]
                self.assertEqual(orchestrator.runtime_dependencies(files), [expected])

    def test_renovate_llamacpp_release_updates(self):
        release = "https://github.com/canonical/llama.cpp-builds/releases/download"
        for field, asset in [
            ("- on amd64", "llamacpp-amd64.tar.gz"),
            ("- on arm64", "llamacpp-arm64.tar.gz"),
            ("- on amd64", "llamacpp-amd64+cuda12.tar.gz"),
            ("- on arm64", "llamacpp-arm64+cuda12.tar.gz"),
            ("- on amd64", "llamacpp-amd64+cuda12.9.tar.gz"),
            ("- on arm64", "llamacpp-arm64+cuda12.9.tar.gz"),
            ("- on amd64", "llamacpp-amd64+rocm.tar.gz"),
            ("source", "llamacpp-amd64+rocm.tar.gz"),
            ("source", "llamacpp-amd64+onemkl.tar.gz"),
        ]:
            with self.subTest(field=field, asset=asset):
                diff = f"-      {field}: {release}/v0.5.0/{asset}\n+      {field}: {release}/v0.6.0/{asset}"
                files = [{"filename": "snap/snapcraft.yaml", "patch": diff}]
                self.assertEqual(orchestrator.runtime_dependencies(files), ["llama.cpp"])

    def test_renovate_openvino_release_update(self):
        release = "https://github.com/openvinotoolkit/model_server/releases/download"
        asset = "ovms_ubuntu24_2026.2.0_python_off.tar.gz"
        diff = f"-    source: {release}/v2026.2/{asset}\n+    source: {release}/v2026.2.1/{asset}"
        files = [{"filename": "snap/snapcraft.yaml", "patch": diff}]
        self.assertEqual(orchestrator.runtime_dependencies(files), ["openvino"])

    def test_source_tag_with_unchanged_runtime_source(self):
        for part in ("cli-intel-opencl-icd", "intel-opencl-icd", "intel-compute-runtime"):
            with self.subTest(part=part):
                diff = TAG_PATCH.replace("cli-intel-opencl-icd", part)
                files = [{"filename": "snap/snapcraft.yaml", "patch": diff}]
                self.assertEqual(orchestrator.runtime_dependencies(files), ["intel-compute-runtime"])

    def test_ignores_renovate_modelctl_and_webui_release_updates(self):
        for package, asset in [
            ("inference-snaps", "modelctl-linux-amd64.tar.xz"),
            ("inference-snaps-webui", "inference-snaps-webui.tar.xz"),
        ]:
            with self.subTest(package=package):
                release = f"https://github.com/canonical/{package}/releases/download"
                diff = f"-    source: {release}/v1.0.0/{asset}\n+    source: {release}/v1.1.0/{asset}"
                files = [{"filename": "snap/snapcraft.yaml", "patch": diff}]
                self.assertEqual(orchestrator.runtime_dependencies(files), [])

    def test_ignores_unrelated_context_and_missing_patch(self):
        files = [{"filename": "snap/snapcraft.yaml", "patch": OTHER_PATCH}]
        self.assertEqual(orchestrator.runtime_dependencies(files), [])
        self.assertEqual(orchestrator.runtime_dependencies([{"filename": "snap/snapcraft.yaml"}]), [])

    def test_source_context_does_not_leak_to_other_keys_or_hunks(self):
        for change in [
            "-    plugin: make\n+    plugin: dump",
            "@@ -90,1 +90,1 @@\n-    source-tag: v1\n+    source-tag: v2",
        ]:
            files = [{
                "filename": "snap/snapcraft.yaml",
                "patch": "     source: https://github.com/canonical/intel-compute-runtime-bundle.git\n" + change,
            }]
            self.assertEqual(orchestrator.runtime_dependencies(files), [])


class TestTrigger(unittest.TestCase):
    def test_run_id_links_to_snap_repository(self):
        body = orchestrator.trigger_body("jpm-canonical/smollm2-snap", SHA, 45407156748, PENDING)
        self.assertIn(
            "Workflow run: [45407156748](https://github.com/jpm-canonical/smollm2-snap/actions/runs/45407156748).",
            body,
        )
        trigger = orchestrator.find_trigger([{**marker(), "body": body}], SHA)
        self.assertEqual(trigger.run_id, 45407156748)

    def test_unlinked_run_has_no_link_or_workflow_state(self):
        body = orchestrator.trigger_body(REPO, SHA)
        self.assertIn("Workflow run: not linked yet.", body)
        self.assertNotIn("/actions/runs/", body)
        self.assertNotIn("Workflow state:", body)

    def test_workflow_state_is_raw_and_separate_from_orchestrator_state(self):
        for status in ("queued", "pending", "requested", "waiting", "in_progress", "completed"):
            with self.subTest(status=status):
                body = orchestrator.trigger_body(REPO, SHA, 100, PENDING, status)
                self.assertIn(f"\nWorkflow state: {status}.", body)
                self.assertIn("\nOrchestrator state: pending.", body)
                trigger = orchestrator.find_trigger([{**marker(), "body": body}], SHA)
                self.assertEqual((trigger.run_id, trigger.state), (100, PENDING))

    def test_finds_sha_not_just_latest_comment(self):
        comments = [marker(SHA, run_id=100), marker(OTHER_SHA, AFTER)]
        trigger = orchestrator.find_trigger(comments, SHA)
        self.assertEqual(trigger.run_id, 100)
        self.assertEqual(trigger.triggered_at, orchestrator.parse_time(LABELLED_AT))

    def test_unlinked_attempt_round_trips(self):
        trigger = orchestrator.find_trigger([marker()], SHA)
        self.assertEqual((trigger.sha, trigger.run_id, trigger.state), (SHA, None, "waiting-for-run"))

    def test_missing_or_invalid_metadata_is_an_error(self):
        for metadata in (None, "{}", '{"run_id": null}', '{"state": "pending"}',
                         '{"run_id": null, "state": 1}', "[]", "null", "{invalid}"):
            with self.subTest(metadata=metadata):
                comment = marker()
                comment["body"] = orchestrator.TRIGGER_MARKER.format(sha=SHA)
                if metadata is not None:
                    comment["body"] += f"\n<!-- inference-snaps-ci-orchestrator: state {metadata} -->"
                with self.assertRaises(ValueError):
                    orchestrator.find_trigger([comment], SHA)

    def test_no_matching_or_trusted_trigger(self):
        self.assertIsNone(orchestrator.find_trigger([marker(OTHER_SHA)], SHA))
        self.assertIsNone(orchestrator.find_trigger([marker(association="NONE")], SHA))

    def test_invalid_run_id_is_an_error(self):
        for run_id in ("100", 0, -1, True, 1.5):
            with self.subTest(run_id=run_id), self.assertRaisesRegex(ValueError, "Invalid workflow run ID"):
                orchestrator.find_trigger([marker(run_id=run_id)], SHA)


class TestEvaluateLabRun(unittest.TestCase):
    def test_requires_finished_workflow_even_if_visible_jobs_pass(self):
        self.assertEqual(orchestrator.evaluate_lab_run(workflow_run(status="in_progress"), [job()]), PENDING)

    def test_entire_matrix_must_succeed(self):
        self.assertEqual(
            orchestrator.evaluate_lab_run(workflow_run(), [job(), job(name="trigger-tests / test (cpu) / cpu")]),
            PASSED,
        )
        for conclusion in ("failure", "skipped", "neutral", "cancelled", "timed_out", "action_required", None):
            with self.subTest(conclusion=conclusion):
                jobs = [job(), job(name="trigger-tests / test (cpu) / cpu", conclusion=conclusion)]
                self.assertEqual(orchestrator.evaluate_lab_run(workflow_run(), jobs), FAILED)

    def test_missing_tests_cannot_be_replaced_by_unrelated_jobs(self):
        for jobs in ([], [job(name="lint")], [job(name="trigger-tests / Check for existing build")]):
            self.assertEqual(orchestrator.evaluate_lab_run(workflow_run(), jobs), FAILED)

    def test_skipped_unexpanded_test_is_not_a_pass(self):
        self.assertEqual(
            orchestrator.evaluate_lab_run(workflow_run(), [job(name="trigger-tests / test", conclusion="skipped")]),
            FAILED,
        )

    def test_cached_build_may_be_skipped(self):
        jobs = [job(), job(name="trigger-tests / build-and-publish", conclusion="skipped")]
        self.assertEqual(orchestrator.evaluate_lab_run(workflow_run(), jobs), PASSED)

    def test_failure_and_pending_are_not_success(self):
        self.assertEqual(orchestrator.evaluate_lab_run(workflow_run(conclusion="failure"), [job()]), FAILED)
        self.assertEqual(orchestrator.evaluate_lab_run(workflow_run(), [job(status="queued")]), PENDING)


class TestEvaluateChecks(unittest.TestCase):
    def test_checks(self):
        self.assertEqual(orchestrator.evaluate_checks([], []), PASSED)
        self.assertEqual(orchestrator.evaluate_checks([job(name="lint", conclusion="neutral")], []), PASSED)
        self.assertEqual(orchestrator.evaluate_checks([job(name="lint", conclusion="failure")], []), FAILED)
        self.assertEqual(orchestrator.evaluate_checks([job(name="lint", status="queued")], []), PENDING)

    def test_statuses(self):
        for state, expected in [("success", PASSED), ("pending", PENDING), ("failure", FAILED), ("error", FAILED)]:
            self.assertEqual(orchestrator.evaluate_checks([], [{"state": state}]), expected)


class TestProcess(unittest.TestCase):
    def setUp(self):
        clock = patch.object(orchestrator, "datetime", wraps=datetime)
        self.addCleanup(clock.stop)
        clock.start().now.return_value = orchestrator.parse_time(POLL_AT)

    def process(self, github, pr=None, dry_run=False):
        github.pr = pr or make_pr()
        return Orchestrator(github, dry_run=dry_run).process(REPO, github.pr)

    def test_skips_other_authors_drafts_and_titles_without_changes(self):
        for pr, expected in [
            (make_pr(author="someone"), "skip-author"),
            (make_pr(draft=True), "skip-draft"),
            (make_pr(title="Update llama.cpp"), "skip-not-runtime"),
        ]:
            github = FakeGitHub(files=[])
            self.assertEqual(self.process(github, pr), expected)
            self.assertEqual(github.calls, [])

    def test_records_attempt_before_adding_label(self):
        github = FakeGitHub()
        self.assertEqual(self.process(github), "label")
        requested_at = orchestrator.parse_time(POLL_AT)
        comment_path = f"/repos/{REPO}/issues/comments/20"
        self.assertEqual(
            github.calls,
            [
                ("POST", f"/repos/{REPO}/issues/1/comments", {"body": orchestrator.trigger_body(REPO, SHA)}),
                ("PATCH", comment_path, {"body": orchestrator.trigger_body(
                    REPO, SHA, state="label-requested", label_attempts=1, label_requested_at=requested_at,
                )}),
                ("POST", f"/repos/{REPO}/issues/1/labels", {"labels": ["trigger-tests"]}),
                ("PATCH", comment_path, {"body": orchestrator.trigger_body(
                    REPO, SHA, state="waiting-for-run", label_attempts=1, label_requested_at=requested_at,
                )}),
            ],
        )

    def test_consumed_label_does_not_trigger_again(self):
        github = FakeGitHub()
        self.process(github)
        github.calls.clear()
        self.assertEqual(self.process(github), "wait")
        self.assertEqual(github.calls, [])
        github.runs = [workflow_run()]
        self.assertEqual(self.process(github), "merge")
        self.assertFalse(any(call[0] == "POST" for call in github.calls))
        self.assertEqual(orchestrator.find_trigger(github.comments, SHA).state, "merged")

    def test_uncertain_label_failure_is_not_retried_before_grace_period(self):
        github = FakeGitHub()
        github.fail_label = True
        with self.assertRaises(urllib.error.URLError):
            self.process(github)
        github.calls.clear()
        self.assertEqual(self.process(github), "wait")
        self.assertEqual(github.calls, [])

    def test_new_sha_gets_one_attempt_and_old_sha_is_remembered(self):
        github = FakeGitHub(comments=[marker()])
        self.assertEqual(self.process(github, make_pr(sha=OTHER_SHA)), "label")
        self.assertEqual([call[0] for call in github.calls], ["POST", "PATCH", "POST", "PATCH"])
        github.calls.clear()
        self.assertEqual(self.process(github, make_pr(sha=OTHER_SHA)), "wait")
        self.assertEqual(self.process(github), "wait")
        self.assertEqual(github.calls, [])

    def test_existing_label_is_not_toggled_for_first_attempt(self):
        github = FakeGitHub()
        self.assertEqual(self.process(github, make_pr(labels=["trigger-tests"])), "wait")
        self.assertEqual([call[0] for call in github.calls], ["POST"])
        self.assertEqual(orchestrator.find_trigger(github.comments, SHA).state, "ready-to-label")

    def test_links_and_updates_same_comment_then_merges_pinned_sha(self):
        github = FakeGitHub(comments=[marker()], runs=[workflow_run()])
        self.assertEqual(self.process(github), "merge")
        self.assertEqual(
            [call for call in github.calls if call[0] == "PUT"],
            [("PUT", f"/repos/{REPO}/pulls/1/merge", {"sha": SHA, "merge_method": "squash"})],
        )
        self.assertTrue(all(call[1].endswith("/issues/comments/10") for call in github.calls if call[0] == "PATCH"))
        trigger = orchestrator.find_trigger(github.comments, SHA)
        self.assertEqual((trigger.run_id, trigger.state), (100, "merged"))
        self.assertEqual(trigger.triggered_at, orchestrator.parse_time(LABELLED_AT))
        self.assertIn("\nWorkflow state: completed.", trigger.body)
        self.assertIn("\nOrchestrator state: merged.", trigger.body)

    def test_incomplete_marker_is_reported_without_retriggering(self):
        comment = marker()
        comment["body"] = orchestrator.TRIGGER_MARKER.format(sha=SHA)
        github = FakeGitHub(comments=[comment])
        with self.assertLogs("orchestrator", level="ERROR") as logs:
            self.assertEqual(Orchestrator(github, dry_run=False).run([REPO]), 1)
        self.assertIn("Missing state in trigger comment", logs.output[0])
        self.assertEqual(github.calls, [])

    def test_rejects_unrelated_or_old_runs(self):
        cases = [
            {"sha": OTHER_SHA}, {"at": BEFORE}, {"head_branch": "other"},
            {"path": ".github/workflows/pr-checks.yaml"}, {"event": "push"},
            {"pull_requests": [{"number": 2}]},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                github = FakeGitHub(comments=[marker()], runs=[workflow_run(**overrides)])
                self.assertEqual(self.process(github), "wait")
                self.assertEqual(github.calls, [])

    def test_empty_pr_association_uses_sha_and_branch(self):
        github = FakeGitHub(comments=[marker()], runs=[workflow_run(pull_requests=[])])
        self.assertEqual(self.process(github), "merge")

    def test_build_only_run_cannot_be_linked(self):
        github = FakeGitHub(
            comments=[marker()], runs=[workflow_run()],
            jobs={100: [job(name="trigger-build / build-and-publish"), job(name="trigger-build / test")]},
        )
        self.assertEqual(self.process(github), "wait")
        self.assertEqual(github.calls, [])

    def test_waits_for_jobs_to_appear(self):
        github = FakeGitHub(
            comments=[marker(state="label-requested")], runs=[workflow_run(status="queued")], jobs={},
        )
        self.assertEqual(self.process(github), "wait")
        self.assertEqual(github.calls, [])

    def test_does_not_guess_between_multiple_matching_runs(self):
        github = FakeGitHub(
            comments=[marker()], runs=[workflow_run(), workflow_run(run_id=200)],
            jobs={100: [job()], 200: [job()]},
        )
        self.assertEqual(self.process(github), "wait")
        trigger = orchestrator.find_trigger(github.comments, SHA)
        self.assertEqual(trigger.state, "ambiguous-runs")
        self.assertIsNone(trigger.run_id)

    def test_bound_run_does_not_switch_to_a_later_success(self):
        github = FakeGitHub(
            comments=[marker(run_id=100)], runs=[workflow_run(conclusion="failure"), workflow_run(run_id=200)],
            jobs={100: [job(conclusion="failure")], 200: [job()]},
        )
        self.assertEqual(self.process(github), "failed")
        self.assertEqual(orchestrator.find_trigger(github.comments, SHA).run_id, 100)
        github.calls.clear()
        self.assertEqual(self.process(github), "failed")
        self.assertEqual(github.calls, [])

    def test_validates_bound_run(self):
        github = FakeGitHub(comments=[marker(run_id=100)], runs=[workflow_run(head_branch="other")])
        self.assertEqual(self.process(github), "failed")
        self.assertEqual(orchestrator.find_trigger(github.comments, SHA).state, "invalid-run")

    def test_completed_run_without_lab_success_does_not_merge(self):
        for jobs in ([job(conclusion="neutral")], [job(conclusion="skipped")], [job(name="lint")]):
            github = FakeGitHub(comments=[marker(run_id=100)], runs=[workflow_run()], jobs={100: jobs})
            self.assertEqual(self.process(github), "failed")
            self.assertFalse(any(call[0] == "PUT" for call in github.calls))

    def test_running_workflow_does_not_merge_or_retrigger(self):
        for status in ("queued", "pending", "requested", "waiting", "in_progress"):
            with self.subTest(status=status):
                github = FakeGitHub(comments=[marker(run_id=100)], runs=[workflow_run(status=status)])
                self.assertEqual(self.process(github), "wait")
                self.assertTrue(all(call[0] == "PATCH" for call in github.calls))
                trigger = orchestrator.find_trigger(github.comments, SHA)
                self.assertEqual(trigger.state, PENDING)
                self.assertIn(f"\nWorkflow state: {status}.", trigger.body)
                github.calls.clear()
                self.assertEqual(self.process(github), "wait")
                self.assertEqual(github.calls, [])

    def test_running_then_waiting_for_other_checks(self):
        github = FakeGitHub(
            comments=[marker(run_id=100)], runs=[workflow_run(status="in_progress")],
            checks=[job(name="lint", status="queued", suite_id=999)],
        )
        self.assertEqual(self.process(github), "wait")
        self.assertIn("\nWorkflow state: in_progress.", github.comments[0]["body"])
        github.runs = [workflow_run()]
        self.assertEqual(self.process(github), "wait")
        trigger = orchestrator.find_trigger(github.comments, SHA)
        self.assertEqual(trigger.state, "waiting-for-checks")
        self.assertIn("\nWorkflow state: completed.", trigger.body)

    def test_completed_workflow_waiting_for_job_results(self):
        github = FakeGitHub(
            comments=[marker(run_id=100)], runs=[workflow_run()],
            jobs={100: [job(status="in_progress", conclusion=None)]},
        )
        self.assertEqual(self.process(github), "wait")
        trigger = orchestrator.find_trigger(github.comments, SHA)
        self.assertEqual(trigger.state, PENDING)
        self.assertIn("\nWorkflow state: completed.", trigger.body)

    def test_reads_jobs_for_specific_attempt(self):
        github = FakeGitHub(comments=[marker(run_id=100)], runs=[workflow_run(run_attempt=3)])
        self.assertEqual(self.process(github), "merge")
        self.assertIn(f"/repos/{REPO}/actions/runs/100/attempts/3/jobs", github.reads)

    def test_rerun_starting_during_evaluation_prevents_merge(self):
        github = FakeGitHub(comments=[marker(run_id=100)], runs=[workflow_run()])
        with patch.object(github, "get", side_effect=[
            workflow_run(), workflow_run(run_attempt=2, status="queued", conclusion=None),
        ]):
            self.assertEqual(self.process(github), "wait")
        self.assertFalse(any(call[0] == "PUT" for call in github.calls))
        self.assertIn("\nWorkflow state: queued.", github.comments[0]["body"])

    def test_triage_can_rerun_the_linked_run_without_another_label(self):
        github = FakeGitHub(comments=[marker(run_id=100)], runs=[workflow_run(conclusion="failure")])
        self.assertEqual(self.process(github), "failed")
        github.runs = [workflow_run(run_attempt=2)]
        self.assertEqual(self.process(github), "merge")
        self.assertFalse(any(call[0] == "POST" for call in github.calls))

    def test_ignores_historical_label_checks_but_not_other_workflows(self):
        github = FakeGitHub(
            comments=[marker(run_id=100)],
            runs=[workflow_run(), workflow_run(run_id=200, at=BEFORE, conclusion="cancelled")],
            checks=[job(name="trigger-build / build", suite_id=201, conclusion="cancelled")],
        )
        self.assertEqual(self.process(github), "merge")
        github.checks.append(job(name="lint", suite_id=999, conclusion="failure"))
        self.assertEqual(self.process(github), "failed")

    def test_commit_statuses_still_block_merge(self):
        for state, result in (("pending", "wait"), ("failure", "failed")):
            github = FakeGitHub(
                comments=[marker(run_id=100)], runs=[workflow_run()], statuses=[{"state": state}],
            )
            self.assertEqual(self.process(github), result)
            self.assertFalse(any(call[0] == "PUT" for call in github.calls))
            self.assertEqual(
                orchestrator.find_trigger(github.comments, SHA).state,
                "waiting-for-checks" if state == "pending" else FAILED,
            )

    def test_reports_merge_refusal(self):
        github = FakeGitHub(comments=[marker(run_id=100)], runs=[workflow_run()])
        github.merge_response = {"merged": False, "message": "Blocked by protection"}
        with self.assertRaisesRegex(ValueError, "Merge refused: Blocked by protection"):
            self.process(github)
        self.assertNotEqual(orchestrator.find_trigger(github.comments, SHA).state, "merged")

    def test_run_reports_merge_errors(self):
        github = FakeGitHub(comments=[marker(run_id=100)], runs=[workflow_run()])
        github.merge_response = {"merged": False, "message": "Blocked"}
        with self.assertLogs("orchestrator", level="ERROR") as logs:
            self.assertEqual(Orchestrator(github, dry_run=False).run([REPO]), 1)
        self.assertIn("Merge refused", logs.output[0])

    def test_dry_run_never_writes_at_any_stage(self):
        cases = [
            FakeGitHub(),
            FakeGitHub(comments=[marker()], runs=[workflow_run()]),
            FakeGitHub(comments=[marker(run_id=100)], runs=[workflow_run(conclusion="failure")]),
        ]
        for github in cases:
            with self.subTest(comments=github.comments):
                self.process(github, dry_run=True)
                self.assertEqual(github.calls, [])

    def test_default_instance_is_dry_run(self):
        github = FakeGitHub()
        Orchestrator(github).process(REPO, make_pr())
        self.assertEqual(github.calls, [])


class TestMain(unittest.TestCase):
    def test_safe_defaults_and_explicit_execution(self):
        for value, flags, expected in [
            (None, [], True), ("", [], True), ("true", [], True), ("typo", [], True),
            ("false", [], False), (None, ["--apply"], False), ("false", ["--dry-run"], True),
        ]:
            with self.subTest(value=value, flags=flags):
                env = {"GITHUB_TOKEN": "test-token"}
                if value is not None:
                    env["DRY_RUN"] = value
                with (
                    patch.dict(os.environ, env, clear=True),
                    patch.object(orchestrator, "Orchestrator") as cls,
                    patch.object(orchestrator.logging, "basicConfig"),
                ):
                    cls.return_value.run.return_value = 0
                    self.assertEqual(orchestrator.main(["--repo", REPO, *flags]), 0)
                    self.assertEqual(cls.call_args.args[1], expected)


class TestPagination(unittest.TestCase):
    def test_next_link(self):
        header = '<https://api.github.com/x?page=2>; rel="next", <https://api.github.com/x?page=5>; rel="last"'
        self.assertEqual(orchestrator._next_link(header), "https://api.github.com/x?page=2")
        self.assertIsNone(orchestrator._next_link('<https://api.github.com/x?page=1>; rel="prev"'))
        self.assertIsNone(orchestrator._next_link(""))

    def test_all_pages_of_wrapped_results(self):
        github = orchestrator.GitHub("test-token")
        for key in ("jobs", "workflow_runs", "check_runs", "statuses"):
            with self.subTest(key=key), patch.object(github, "_request") as request:
                request.side_effect = [
                    ({key: [{"id": 1}]}, '<https://api.github.com/x?page=2>; rel="next"'),
                    ({key: [{"id": 2}]}, ""),
                ]
                self.assertEqual(github.get_all("/x?filter=latest", key), [{"id": 1}, {"id": 2}])
                self.assertEqual(request.call_args_list[0].args, ("GET", "/x?filter=latest&per_page=100"))


if __name__ == "__main__":
    unittest.main()
