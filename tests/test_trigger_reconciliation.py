import json
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from unittest.mock import Mock, call, patch
from urllib.error import HTTPError

import orchestrator as orch


class TriggerReconciliationTests(unittest.TestCase):
    repo = "canonical/example"
    sha = "a" * 40
    issue = "/repos/canonical/example/issues/244"
    pull = "/repos/canonical/example/pulls/244"

    def setUp(self):
        self.now = datetime(2026, 11, 25, 16, tzinfo=timezone.utc)
        clock = patch.object(orch, "datetime", wraps=datetime)
        self.addCleanup(clock.stop)
        clock.start().now.side_effect = lambda _: self.now
        self.pr = {
            "number": 244, "head": {"sha": self.sha, "ref": "renovate/runtime"},
            "state": "open", "title": "Update runtime", "user": {"login": "renovate[bot]"},
            "labels": [],
        }
        self.comment = None
        self.older_comments = []
        self.events = []
        self.runs = []
        self.jobs = []
        self.comment_error = None
        self.save_comment_on_error = True
        self.label_error = None
        self.label_requests = 0
        self.github = Mock(spec=orch.GitHub)
        self.github.get_all.side_effect = self.get_all
        self.github.get.side_effect = self.get
        self.github.post.side_effect = self.post
        self.github.patch.side_effect = self.patch_comment
        self.orchestrator = orch.Orchestrator(self.github, dry_run=False)

    def get_all(self, path, key=None):
        if path == self.pull + "/files":
            return [{"filename": "snap/snapcraft.yaml", "patch": "+source: model_server"}]
        if path == self.issue + "/comments":
            return self.older_comments + ([self.comment] if self.comment else [])
        if path == self.issue + "/timeline":
            return self.events
        if "/actions/workflows/pr-label.yaml/runs?" in path and key == "workflow_runs":
            return self.runs
        if path.endswith("/jobs") and key == "jobs":
            return self.jobs
        raise AssertionError(path)

    def get(self, path):
        if path == self.pull:
            return self.pr
        if path.endswith("/actions/runs/42"):
            return self.runs[0]
        raise AssertionError(path)

    def save_comment(self, body):
        self.comment = {
            "id": 100, "author_association": "MEMBER",
            "created_at": self.now.isoformat(), "body": body,
        }
        return self.comment

    def post(self, path, payload):
        if path == self.issue + "/comments":
            if not self.comment_error or self.save_comment_on_error:
                self.save_comment(payload["body"])
            if self.comment_error:
                raise self.comment_error
            return self.comment
        if path == self.issue + "/labels":
            self.label_requests += 1
            self.assertEqual(self.state().state, "label-requested")
            if self.label_error:
                raise self.label_error
            return [{"name": orch.TRIGGER_LABEL}]
        raise AssertionError(path)

    def patch_comment(self, path, payload):
        self.assertEqual(path, "/repos/canonical/example/issues/comments/100")
        self.comment["body"] = payload["body"]

    def state(self):
        return orch.find_trigger([self.comment], self.sha)

    def poll(self):
        return self.orchestrator.process(self.repo, self.pr)

    def event(self, kind):
        self.events.append({
            "event": kind, "label": {"name": orch.TRIGGER_LABEL}, "created_at": self.now.isoformat(),
        })

    def run_record(self, run_id=42):
        return {
            "id": run_id, "head_sha": self.sha, "head_branch": self.pr["head"]["ref"],
            "event": "pull_request", "path": orch.LAB_WORKFLOW, "pull_requests": [{"number": 244}],
            "status": "in_progress", "created_at": self.now.isoformat(), "run_attempt": 1,
        }

    def gateway_error(self):
        error = HTTPError("https://api.github.com/", 502, "Bad Gateway", None, BytesIO())
        self.addCleanup(error.close)
        return error

    def test_recovers_comment_committed_despite_502(self):
        self.comment_error = self.gateway_error()
        self.assertEqual(self.poll(), "label")
        self.assertEqual(self.label_requests, 1)
        self.assertEqual(self.state().state, "waiting-for-run")
        self.assertEqual(self.github.post.call_count, 2)

    def test_orchestrator_log_link_is_created_refreshed_and_omitted_locally(self):
        run_base = "https://github.com/canonical/inference-snaps-ci-orchestrator/actions/runs/"
        self.orchestrator.orchestrator_run_url = run_base + "1001"
        self.assertEqual(self.poll(), "label")
        for body in (self.github.post.call_args_list[0].args[1]["body"], self.comment["body"]):
            self.assertIn(f"\nOrchestrator logs: [View run]({run_base}1001).", body)

        self.orchestrator.orchestrator_run_url = run_base + "1002"
        self.assertEqual(self.poll(), "wait")
        self.assertEqual(self.state().state, "waiting-for-run")
        self.assertIn(f"\nOrchestrator logs: [View run]({run_base}1002).", self.comment["body"])
        self.assertNotIn(run_base + "1001", self.comment["body"])

        self.runs.append(self.run_record())
        self.jobs = [{"name": "trigger-tests / test (cpu)"}]
        self.assertEqual(self.poll(), "wait")
        self.assertIn(f"https://github.com/{self.repo}/actions/runs/42", self.comment["body"])
        self.assertIn(run_base + "1002", self.comment["body"])

        self.orchestrator.orchestrator_run_url = None
        self.assertEqual(self.poll(), "wait")
        self.assertNotIn("Orchestrator logs:", self.comment["body"])
        self.assertIn(f"https://github.com/{self.repo}/actions/runs/42", self.comment["body"])

    def old_comment(self, node_id, **overrides):
        return {
            "id": 99, "node_id": node_id, "author_association": "MEMBER",
            "body": orch.trigger_body(self.repo, "b" * 40),
        } | overrides

    def test_new_comment_collapses_only_visible_older_trigger_comments(self):
        self.older_comments = [
            self.old_comment("old"),
            self.old_comment("hidden"),
            self.old_comment("human", body="Please check the logs."),
            self.old_comment("untrusted", author_association="NONE"),
        ]
        self.github.graphql.side_effect = [
            {"nodes": [
                {"isMinimized": False, "viewerCanMinimize": True},
                {"isMinimized": True, "viewerCanMinimize": False},
            ]},
            {"minimizeComment": {"minimizedComment": {"isMinimized": True}}},
        ]
        self.assertEqual(self.poll(), "label")
        query, mutation = self.github.graphql.call_args_list
        self.assertEqual(query.args[1], {"ids": ["old", "hidden"]})
        self.assertIn("classifier: OUTDATED", mutation.args[0])
        self.assertEqual(mutation.args[1], {"id": "old"})
        calls = self.github.mock_calls
        self.assertLess(calls.index(call.post(self.issue + "/comments", {
            "body": orch.trigger_body(self.repo, self.sha),
        })), calls.index(call.graphql(*query.args)))
        self.assertEqual(self.poll(), "wait")
        self.assertEqual(self.github.graphql.call_count, 2)

    def test_minimization_failure_does_not_prevent_labeling(self):
        self.older_comments = [self.old_comment("old")]
        self.comment_error = self.gateway_error()
        self.github.graphql.side_effect = [
            {"nodes": [{"isMinimized": False, "viewerCanMinimize": True}]},
            ValueError("GitHub GraphQL: permission denied"),
        ]
        with self.assertLogs("orchestrator", level="WARNING") as logs:
            self.assertEqual(self.poll(), "label")
        self.assertTrue(any("could not minimize comment old" in line for line in logs.output))
        self.assertEqual(self.label_requests, 1)

    def test_dry_run_does_not_minimize_old_comments(self):
        self.older_comments = [self.old_comment("old")]
        self.orchestrator.dry_run = True
        self.assertEqual(self.poll(), "label")
        self.github.post.assert_not_called()
        self.github.graphql.assert_not_called()

    def test_does_not_label_without_a_persisted_comment(self):
        self.older_comments = [self.old_comment("old")]
        self.comment_error = self.gateway_error()
        self.save_comment_on_error = False
        with self.assertRaises(HTTPError):
            self.poll()
        self.assertIsNone(self.comment)
        self.assertEqual(self.label_requests, 0)
        self.github.graphql.assert_not_called()

    def test_legacy_waiting_comment_can_be_reused(self):
        old_body = (
            orch.TRIGGER_MARKER.format(sha=self.sha)
            + "\n<!-- inference-snaps-ci-orchestrator: state "
            + json.dumps({"run_id": None, "state": "waiting-for-run"}) + " -->"
        )
        self.save_comment(old_body)
        self.now += timedelta(minutes=5)
        self.assertEqual(self.poll(), "label")
        self.assertEqual(self.state().label_attempts, 2)
        self.assertEqual(self.github.post.call_count, 1)

    def test_uncertain_delivery_retries_with_backoff_and_cap(self):
        self.label_error = self.gateway_error()
        with self.assertRaises(HTTPError):
            self.poll()
        for minutes in (5, 10):
            self.now += timedelta(minutes=minutes, seconds=-1)
            self.assertEqual(self.poll(), "wait")
            self.now += timedelta(seconds=1)
            with self.assertRaises(HTTPError):
                self.poll()
        self.now += timedelta(minutes=20)
        self.assertEqual(self.poll(), "wait")
        self.assertEqual(self.state().state, "needs-triage")
        self.assertEqual(self.label_requests, 3)

    def test_add_then_remove_after_502_is_delivery_not_a_retry(self):
        self.label_error = self.gateway_error()
        with self.assertRaises(HTTPError):
            self.poll()
        self.event("labeled")
        self.event("unlabeled")
        self.now += timedelta(minutes=10)
        self.assertEqual(self.poll(), "wait")
        self.assertEqual(self.state().state, "waiting-for-run")
        self.now += timedelta(minutes=20)
        self.assertEqual(self.poll(), "wait")
        self.assertEqual(self.state().state, "needs-triage")
        self.assertEqual(self.label_requests, 1)

    def test_failed_timeline_read_cannot_authorize_retry(self):
        self.label_error = self.gateway_error()
        with self.assertRaises(HTTPError):
            self.poll()
        self.now += timedelta(minutes=5)

        def get_all(path, key=None):
            if path == self.issue + "/timeline":
                raise TimeoutError()
            return self.get_all(path, key)

        self.github.get_all.side_effect = get_all
        with self.assertRaises(TimeoutError):
            self.poll()
        self.assertEqual(self.label_requests, 1)

    def test_provisional_run_blocks_retry_and_is_linked_when_jobs_appear(self):
        self.label_error = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.poll()
        self.runs.append(self.run_record())
        self.now += timedelta(minutes=10)
        self.assertEqual(self.poll(), "wait")
        self.jobs = [{"name": "trigger-tests / test (cpu)"}]
        self.assertEqual(self.poll(), "wait")
        self.assertEqual(self.state().run_id, 42)
        self.assertIn("/actions/runs/42", self.comment["body"])
        self.assertEqual(self.label_requests, 1)

    def test_confirmed_delivery_without_run_does_not_retry(self):
        self.assertEqual(self.poll(), "label")
        self.now += timedelta(minutes=30)
        self.assertEqual(self.poll(), "wait")
        self.assertEqual(self.state().state, "needs-triage")
        self.assertEqual(self.label_requests, 1)

    def test_changed_head_never_receives_stale_label(self):
        self.github.get.side_effect = None
        self.github.get.return_value = {**self.pr, "head": {"sha": "b" * 40}}
        self.assertEqual(self.poll(), "wait")
        self.assertEqual(self.state().state, "ready-to-label")
        self.assertEqual(self.label_requests, 0)

    def test_existing_label_is_not_removed_or_counted_as_delivery(self):
        self.pr["labels"] = [{"name": orch.TRIGGER_LABEL}]
        self.assertEqual(self.poll(), "wait")
        self.assertEqual(self.state().state, "ready-to-label")
        self.github.delete.assert_not_called()
        self.assertEqual(self.label_requests, 0)

    def test_ambiguous_test_runs_do_not_trigger_or_link(self):
        self.save_comment(orch.trigger_body(self.repo, self.sha))
        self.runs = [self.run_record(42), self.run_record(43)]
        self.jobs = [{"name": "trigger-tests / test (cpu)"}]
        self.assertEqual(self.poll(), "wait")
        self.assertEqual(self.state().state, "ambiguous-runs")
        self.assertIsNone(self.state().run_id)
        self.assertEqual(self.label_requests, 0)

    def test_dry_run_does_not_mutate_existing_intent(self):
        self.save_comment(orch.trigger_body(self.repo, self.sha))
        self.orchestrator.dry_run = True
        self.orchestrator.orchestrator_run_url = "https://github.com/canonical/orchestrator/actions/runs/1001"
        self.assertEqual(self.poll(), "label")
        self.assertEqual(self.state().state, "ready-to-label")
        self.github.post.assert_not_called()
        self.github.patch.assert_not_called()
