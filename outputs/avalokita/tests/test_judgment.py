"""Typed fixtures prove planning gates, never actual native dispatch."""
from copy import deepcopy
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from judgment import JudgmentRouter
from preference_learning import PreferenceModel


class JudgmentTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.controls = {"workspace_key": "workspace-a", "epoch": 7, "participants": ["gemini", "codex", "claude", "reviewer"],
                         "human_authorized": True, "paused": False, "stopped": False,
                         "observed_at": self.now - 1, "expires_at": self.now + 60}
        self.candidates = [self.actor("gemini", "antigravity", ["research"]),
                           self.actor("codex", "codex", ["frontend"]),
                           self.actor("claude", "claude", ["backend"]),
                           self.actor("reviewer", "claude", ["review"])]
        self.budget = {"remaining_turns": 8, "deadline": self.now + 60, "max_parallel": 2}
        self.model = PreferenceModel()
        self.router = JudgmentRouter(self.model)

    def actor(self, actor_id, provider, capabilities):
        return {"id": actor_id, "provider": provider, "workspace_key": "workspace-a", "control_epoch": 7,
                "capabilities": capabilities, "verified_capabilities": capabilities,
                "observed_at": self.now - 1, "expires_at": self.now + 60,
                "readiness": {"state": "ready", "can_dispatch": True}}

    def task(self, task_id="idea", **changes):
        result = {"id": task_id, "purpose": "research", "objective": "Compare supported local runtime adapters",
                  "required_capabilities": ["research"], "recipient_id": "gemini"}
        result.update(changes)
        return result

    def plan(self, tasks, **changes):
        arguments = {"controls": self.controls, "budget": self.budget}
        arguments.update(changes)
        return self.router.plan(tasks, self.candidates, **arguments)

    def test_empty_and_single_plan_are_advisory_without_native_authority(self):
        self.assertEqual(self.plan([])["state"], "empty")
        result = self.plan([self.task()])
        self.assertEqual(result["state"], "ready_for_admission")
        self.assertEqual(result["branches"][0]["actor_id"], "gemini")
        self.assertEqual(result["branches"][0]["posterior"]["training_state"], "untrained")
        self.assertFalse(result["dispatch_authorized"])
        self.assertFalse(result["execution_verified"])
        self.assertEqual(result["budget"]["reserved_turns"], 1)

    def test_distinct_purposes_conditional_dependencies_and_verified_join(self):
        tasks = [self.task(),
                 self.task("front", purpose="design", objective="Implement the approved frontend", required_capabilities=["frontend"],
                           recipient_id="codex", edit_scope=["web/app.py"], dependencies=["idea"], review_owner="reviewer"),
                 self.task("back", purpose="implementation", objective="Implement a bounded backend worker", required_capabilities=["backend"],
                           recipient_id="claude", edit_scope=["backend/worker.py"], dependencies=["idea"], review_owner="reviewer"),
                 self.task("review", purpose="review", objective="Review both implementations", required_capabilities=["review"],
                           recipient_id="reviewer", dependencies=["front", "back"],
                           condition={"after": "front", "when": "verified"}, join={"requires": ["front", "back"], "mode": "all_verified"})]
        result = self.plan(tasks)
        self.assertEqual(result["state"], "ready_for_admission", result)
        self.assertEqual([item["actor_id"] for item in result["branches"]], ["gemini", "codex", "claude", "reviewer"])
        self.assertEqual([item["state"] for item in result["branches"]], ["proposed", "waiting", "waiting", "conditional"])
        self.assertEqual(result["branches"][3]["join"]["requires"], ["front", "back"])
        self.assertEqual(result["budget"]["reserved_turns"], 4)

    def test_cycles_unknown_dependencies_and_invalid_conditions_are_rejected(self):
        fixtures = [([self.task(dependencies=["other"]), self.task("other", objective="A different objective", dependencies=["idea"])], "dependency_cycle"),
                    ([self.task(dependencies=["absent"])], "unknown_dependency"),
                    ([self.task(condition={"after": "absent", "when": "succeeded"})], "condition_requires_dependency"),
                    ([self.task(condition={"after": "idea", "when": "truthy"})], "invalid_condition"),
                    ([self.task(join={"requires": ["absent"], "mode": "all_verified"})], "join_requires_dependencies")]
        for tasks, code in fixtures:
            with self.subTest(code=code):
                result = self.plan(tasks)
                self.assertEqual(result["state"], "blocked")
                self.assertEqual(result["errors"][0]["code"], code)

    def test_identical_prompt_fanout_is_not_a_distinct_branch_graph(self):
        result = self.plan([self.task(), self.task("copy", purpose="implementation", recipient_id="codex")])
        self.assertEqual(result["errors"][0]["code"], "duplicated_intent")
        self.assertEqual(result["branches"], [])

    def test_control_pause_stop_staleness_and_unknown_human_prevent_model_calls(self):
        self.model.predict = Mock(side_effect=AssertionError("must not infer"))
        for change in ({"paused": True}, {"stopped": True}, {"expires_at": self.now - 1},
                       {"human_authorized": False}, {"epoch": True}):
            with self.subTest(change=change):
                result = self.plan([self.task()], controls={**self.controls, **change})
                self.assertEqual(result["errors"][0]["code"], "unavailable_controls")
        self.model.predict.assert_not_called()

    def test_unknown_stale_moved_and_unverified_candidates_never_receive_assignments(self):
        for change in ({"id": "outsider"}, {"expires_at": self.now - 1}, {"workspace_key": "workspace-b"},
                       {"control_epoch": 6}, {"verified_capabilities": []},
                       {"readiness": {"state": "busy", "can_dispatch": False}}):
            with self.subTest(change=change):
                candidates = [{**self.candidates[0], **change}]
                result = self.router.plan([self.task()], candidates, controls=self.controls, budget=self.budget)
                self.assertEqual(result["state"], "blocked")
                self.assertIsNone(result["branches"][0]["actor_id"])

    def test_explicit_recipient_is_preserved_when_other_provider_is_ready(self):
        self.candidates[1]["capabilities"] = ["research"]
        self.candidates[1]["verified_capabilities"] = ["research"]
        self.candidates[0]["readiness"] = {"state": "unavailable", "can_dispatch": False}
        result = self.plan([self.task()])
        self.assertEqual(result["errors"][0]["code"], "unavailable_recipient")
        self.assertIsNone(result["branches"][0]["actor_id"])

    def test_parallel_prefix_collision_rejected_but_dependency_orders_shared_edits(self):
        tasks = [self.task(edit_scope=["src"], review_owner="reviewer"),
                 self.task("second", objective="Revise a shared source after research", edit_scope=["src/a.py"], review_owner="reviewer")]
        self.assertEqual(self.plan(tasks)["errors"][0]["code"], "parallel_edit_collision")
        tasks[1]["dependencies"] = ["idea"]
        self.assertEqual(self.plan(tasks)["state"], "ready_for_admission")

    def test_review_owner_must_be_ready_capable_and_independent(self):
        for reviewer, expected in ((None, "unavailable_review_owner"), ("outsider", "unavailable_review_owner"),
                                   ("gemini", "unavailable_review_owner")):
            with self.subTest(reviewer=reviewer):
                result = self.plan([self.task(edit_scope=["src/a.py"], review_owner=reviewer)])
                self.assertEqual(result["errors"][0]["code"], expected)
        self.candidates[0]["capabilities"].append("review")
        self.candidates[0]["verified_capabilities"] = list(self.candidates[0]["capabilities"])
        result = self.plan([self.task(edit_scope=["src/a.py"], review_owner="gemini")])
        self.assertEqual(result["errors"][0]["code"], "review_owner_conflict")

    def test_shared_turn_cost_deadline_and_invalid_limits(self):
        result = self.plan([self.task(turn_cost=3)], budget={**self.budget, "remaining_turns": 2})
        self.assertEqual(result["errors"][0]["code"], "budget_exhausted")
        for changes in ({"deadline": self.now - 1}, {"remaining_turns": True}, {"max_parallel": 0}):
            self.assertEqual(self.plan([self.task()], budget={**self.budget, **changes})["errors"][0]["code"], "unavailable_budget")

    def test_ambiguous_untrained_candidates_require_native_judgment(self):
        self.candidates[1].update(capabilities=["research"], verified_capabilities=["research"])
        result = self.plan([self.task(recipient_id=None)])
        self.assertEqual(result["state"], "needs_native_judgment")
        self.assertIsNone(result["branches"][0]["actor_id"])

    def test_model_cannot_invent_native_identity_or_leak_prediction_fields(self):
        actual = self.model.predict

        def predict(context, candidates):
            result = actual(context, candidates)
            result[0].update(id="invented-actor", native_session_id="private", root_id="unauthorized")
            return result

        self.model.predict = predict
        result = self.plan([self.task()])
        self.assertEqual(result["state"], "needs_native_judgment")
        self.assertNotIn("private", str(result))
        self.assertIsNone(result["branches"][0]["actor_id"])
        result = self.plan([self.task(native_session_id="task-cannot-grant-authority")])
        self.assertEqual(result["errors"][0]["code"], "unknown_task_field")

    def test_scopes_cannot_escape_or_hide_case_insensitive_collision(self):
        for scope in ("../outside", "C:/outside", "/outside", "src/../outside", "src/*.py", "src\\a.py"):
            with self.subTest(scope=scope):
                self.assertEqual(self.plan([self.task(edit_scope=[scope])])["errors"][0]["code"], "invalid_edit_scope")
        result = self.plan([self.task(edit_scope=["SRC/a.py"], review_owner="reviewer"),
                            self.task("other", objective="Review the same file spelling", edit_scope=["src/A.py"], review_owner="reviewer")])
        self.assertEqual(result["errors"][0]["code"], "parallel_edit_collision")

    def test_ambiguous_windows_components_cannot_be_assigned_as_distinct_edits(self):
        for scope in ("src/a.py.", "src/a.py ", "src/CON", "src/con.txt", "src/PRN.json", "src/COM1.py",
                      "src/LPT9", "src/COM\u00b9.txt", "src/CONIN$", "src/PROGRA~1/a.py", "src/a.py:stream"):
            with self.subTest(scope=scope):
                result = self.plan([self.task(edit_scope=["src/a.py"], review_owner="reviewer"),
                                    self.task("other", objective="Edit an independently proposed file", edit_scope=[scope], review_owner="reviewer")])
                self.assertEqual(result["state"], "blocked")
                self.assertEqual(result["errors"][0]["code"], "invalid_edit_scope")

    @unittest.skipUnless(os.name == "nt", "Windows actual filename alias fixture")
    def test_actual_windows_samefile_alias_never_receives_parallel_assignments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            canonical = root / "src/a.py"
            alias = root / "src/a.py."
            canonical.write_text("fixture", encoding="utf-8")
            self.assertTrue(canonical.samefile(alias))
            result = self.plan([self.task(edit_scope=["src/a.py"], review_owner="reviewer"),
                                self.task("other", objective="Apply a second independent edit", edit_scope=["src/a.py."], review_owner="reviewer")])
            self.assertEqual(result["state"], "blocked")
            self.assertEqual(result["branches"], [])

    def test_readiness_callback_cannot_override_stale_control_or_workspace(self):
        readiness = Mock(return_value={"state": "ready", "can_dispatch": True})
        router = JudgmentRouter(self.model, readiness)
        candidates = [deepcopy(self.candidates[0])]
        candidates[0]["workspace_key"] = "other"
        result = router.plan([self.task()], candidates, controls=self.controls, budget=self.budget)
        self.assertEqual(result["state"], "blocked")
        readiness.assert_not_called()


if __name__ == "__main__":
    unittest.main()
