"""Disposable owned CLI fixtures. No real provider process or model is used."""
import json
import importlib.util
import os
from pathlib import Path
import sys
import shutil
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from bridge import BridgeError, BusError, BusStore, agent_binding
from server import Runtime


class FakeNative:
    instances = []
    gates = {}
    controlled_gates = set()
    unknown = set()
    approvals = set()

    def __init__(self, provider, workspace, agent, token_file, on_event=None, on_approval=None, **options):
        self.provider, self.actor, self.token_file = provider, agent, token_file
        self.on_event, self.on_approval = on_event, on_approval
        self.options, self.session_id = options, None
        self.start_ids, self.prompts, self.replies = [], [], []
        self.pid = os.getpid()
        self.release = threading.Event()
        self.entered = threading.Event()
        self.instances.append(self)

    def start(self, resume_id=None):
        self.start_ids.append(resume_id)
        if self.provider not in self.unknown:
            self.session_id = resume_id or "observed-" + self.actor
            self.on_event({"type": "session", "session_id": self.session_id})
        return self.session_id

    def turn(self, prompt, timeout=240):
        self.prompts.append(prompt)
        self.on_event({"type": "input_attempt", **({"session_id": self.session_id} if self.session_id else {})})
        if self.provider in self.unknown:
            return SimpleNamespace(text="", session_id=None, success=False, needs_human=False,
                                   status="delivery_unknown", input_attempted=True)
        self.on_event({"type": "turn", "status": "accepted", "session_id": self.session_id,
                       "turn_id": "turn-" + str(len(self.prompts))})
        if self.provider in self.approvals:
            self.on_approval({"approval_id": "approval-" + self.actor, "provider": self.provider,
                "method": "item/tool/requestUserInput", "session_id": self.session_id, "turn_id": "turn-1",
                "details": {"summary": "Choose a label", "questions": [{"id": "q1", "question": "Which label?", "options": []}]}})
            if not self.release.wait(6):
                raise TimeoutError
        gate = self.gates.get(self.provider)
        if gate is not None and len(self.prompts) == 1:
            if self.provider in self.controlled_gates:
                self.entered.set()
                gate.wait()
            elif not gate.wait(6):
                raise TimeoutError
        return SimpleNamespace(text=self.provider + " selected public result " + str(len(self.prompts)),
            session_id=self.session_id, success=True, status="completed", input_attempted=True, delivery_only=False)

    def respond_approval(self, approval_id, result):
        if set(result) != {"answers"}:
            raise ValueError("Exact native answer required")
        self.replies.append((approval_id, result))
        self.release.set()

    def interrupt(self):
        self.release.set()
        gate = self.gates.get(self.provider)
        if gate:
            gate.set()

    def close(self):
        self.interrupt()
        self.pid = None


class HostedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name) / "workspace"
        self.workspace.mkdir()
        FakeNative.instances, FakeNative.gates, FakeNative.unknown, FakeNative.approvals = [], {}, set(), set()
        candidates = [{"provider": item, "installed": True, "managed_supported": True} for item in ("codex", "claude", "antigravity")]
        self.runtime = Runtime(self.workspace, discovery=False, native_ipc=False,
                               hosted_factory=FakeNative, hosted_discovery=candidates)

    def tearDown(self):
        for instance in FakeNative.instances:
            instance.interrupt()
        self.runtime.hosted.close()
        self.temp.cleanup()

    def configure(self, *providers):
        value = self.runtime.hosted.configure({"providers": list(providers)})
        self.assertEqual(value["state"], "ready")
        self.bus = self.runtime.hosted.server
        return value

    def prompt(self, key="one", **values):
        return self.runtime.prompt({"text": "Coordinate this selected work", "idempotency_key": key, **values})

    def wait(self, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.025)
        self.fail("Owned fixture did not reach the expected semantic state")

    def completed(self, count=1):
        return self.wait(lambda: len([job for job in self.bus.store.jobs() if job["state"] == "completed"]) >= count)

    def manual_job(self):
        actor = self.configure("codex")["sessions"][0]["agent_id"]
        self.bus.supervisor._stop.set()
        response = self.prompt(recipient_ids=[actor], budget=1, ttl=30)
        job = self.bus.store.claim(actor)
        ledger = self.bus.supervisor.hosted
        ledger.begin(self.bus.store.agent(actor), job, 1)
        self.bus.store.update_agent(actor, generation=1)
        ledger.event(actor, job, 1, {"type": "session", "session_id": "observed-original"})
        return actor, job, ledger, response

    def test_observation_and_lazy_enable_do_not_start_cli_or_claim_existing_gui(self):
        self.assertEqual(self.runtime.hosted.state()["state"], "disabled")
        self.runtime.summary()
        self.assertFalse((self.workspace / ".sharebus").exists())
        store = BusStore(self.workspace)
        existing, _ = store.register("codex", "Original CEO", [], "native_attached", "CEO", native_session_id="original-gui")
        event = store.send("user", existing["id"], "Preserve this original queued input")
        before = store.agent(existing["id"])
        first = self.configure("codex", "claude", "antigravity")
        self.configure("codex", "claude", "antigravity")
        self.runtime.summary()
        self.runtime.hosted.state()
        time.sleep(0.85)
        self.assertEqual(len(FakeNative.instances), 0)
        self.assertEqual(len(self.bus.store.agents()), 4)
        self.assertTrue(all(item["identity_state"] == "pending" and item["native_session_id"] is None for item in first["sessions"]))
        self.assertEqual(store.agent(existing["id"]), before)
        self.assertEqual(next(job for job in store.jobs() if job["id"] == event["jobs"][0]["id"])["state"], "queued")

    def test_first_human_prompt_initializes_installed_owned_providers_once(self):
        with self.runtime.bridge._db() as db:
            self.assertIsNone(db.execute("SELECT value FROM settings WHERE key='hosted_providers'").fetchone())
        first = self.prompt("first-auto")
        self.bus = self.runtime.hosted.server
        try:
            self.completed(3)
        except AssertionError as error:
            self.fail(str(error) + " " + str({
                "jobs": [{key: item.get(key) for key in ("state", "execution_owner", "generation")}
                         for item in self.bus.store.jobs()],
                "agents": [{key: item.get(key) for key in ("provider", "state", "generation")}
                           for item in self.bus.store.agents()],
                "native": [{"provider": item.provider, "starts": len(item.start_ids), "turns": len(item.prompts)}
                           for item in FakeNative.instances]}))
        self.assertEqual(set(self.runtime.hosted.providers()), {"codex", "claude", "antigravity"})
        self.assertEqual(len(self.bus.store.agents()), 3)
        self.assertEqual(len(first["dispatch"]["participants"]), 3)
        with patch.object(self.runtime.hosted, "before_prompt", side_effect=AssertionError("Recovery reinitialized providers")):
            self.prompt("first-auto")
        self.assertEqual(len(self.bus.store.jobs()), 3)
        self.assertEqual(len(FakeNative.instances), 3)

    def test_prompt_public_result_identity_pin_and_resident_second_turn(self):
        pending = self.configure("codex")
        actor = pending["sessions"][0]["agent_id"]
        binding = agent_binding(self.bus.store.agent(actor))
        first = self.prompt(recipient_ids=[actor])
        self.completed()
        public = self.runtime.state()
        self.assertTrue(any("selected public result" in item["body"] for item in public["messages"]))
        receipt = self.runtime.bridge.dispatches()[first["goal"]["id"]]
        self.assertEqual(receipt["bindings"][actor], binding)
        self.assertTrue(receipt["delivery_verified"])
        self.assertEqual(self.bus.store.agent(actor)["native_session_id"], "observed-" + actor)
        self.prompt(recipient_ids=[actor])
        self.assertEqual(len(self.bus.store.jobs()), 1)
        self.prompt("two", recipient_ids=[actor])
        self.completed(2)
        self.assertEqual(len(FakeNative.instances), 1)
        self.assertEqual(FakeNative.instances[0].start_ids, [None])
        self.assertEqual(len(FakeNative.instances[0].prompts), 2)
        self.assertTrue(FakeNative.instances[0].options["subscription_only"])

    def test_postwrite_unknown_without_native_id_blocks_new_prompt_and_restart(self):
        FakeNative.unknown.add("claude")
        actor = self.configure("claude")["sessions"][0]["agent_id"]
        result = self.prompt(recipient_ids=[actor])
        self.wait(lambda: self.runtime.hosted.state()["state"] == "blocked")
        state = self.runtime.hosted.state()["sessions"][0]
        self.assertIsNone(state["native_session_id"])
        self.assertEqual(state["delivery_state"], "delivery_unknown")
        with self.assertRaises(BridgeError):
            self.prompt("fresh", recipient_ids=[actor])
        with self.assertRaises(BusError):
            self.bus.supervisor.restart(actor)
        self.runtime.bridge.reconcile_dispatches()
        self.assertEqual(self.runtime.bridge.dispatches()[result["goal"]["id"]]["status"], "delivery_unknown")
        count = len(FakeNative.instances[0].prompts)
        self.prompt(recipient_ids=[actor])
        self.assertEqual(len(FakeNative.instances[0].prompts), count)
        identities = [(job["id"], job["event_id"], job["root_id"], job["agent_id"]) for job in self.bus.store.jobs()]
        self.runtime.hosted.close()
        with self.assertRaises(BridgeError):
            self.runtime.hosted.before_prompt()
        self.runtime = Runtime(self.workspace, discovery=False, native_ipc=False, hosted_factory=FakeNative,
                               hosted_discovery=[{"provider": "claude", "installed": True, "managed_supported": True}])
        self.runtime.hosted.before_prompt()
        self.bus = self.runtime.hosted.server
        self.assertEqual([(job["id"], job["event_id"], job["root_id"], job["agent_id"]) for job in self.bus.store.jobs()], identities)
        self.assertIsNone(self.bus.store.agent(actor)["native_session_id"])
        with self.assertRaises(BridgeError):
            self.prompt("after-restart", recipient_ids=[actor])
        self.assertEqual(sum(len(item.prompts) for item in FakeNative.instances), 1)

    def test_exact_operator_approval_and_duplicate_response(self):
        FakeNative.approvals.add("codex")
        actor = self.configure("codex")["sessions"][0]["agent_id"]
        self.prompt(recipient_ids=[actor])
        approval = self.wait(lambda: (self.runtime.hosted.state()["approvals"] or [None])[0])
        self.assertTrue(approval["can_approve"])
        payload = {"agent_id": actor, "approval_id": approval["approval_id"], "result": {"answers": {"q1": {"answers": ["Blue"]}}}}
        with self.assertRaises(BridgeError):
            self.runtime.hosted.reply({**payload, "agent_id": "foreign-actor"})
        self.runtime.hosted.reply(payload)
        self.runtime.hosted.reply(payload)
        self.completed()
        self.assertEqual(len(FakeNative.instances[0].replies), 1)

    def test_directed_same_root_busy_peer_defers_then_uses_same_native_session(self):
        FakeNative.gates = {"codex": threading.Event(), "claude": threading.Event()}
        with patch.object(FakeNative, "controlled_gates", {"codex", "claude"}):
            try:
                sessions = self.configure("codex", "claude")["sessions"]
                actors = {item["provider"]: item["agent_id"] for item in sessions}
                response = self.prompt(recipient_ids=list(actors.values()))
                self.wait(lambda: len(FakeNative.instances) == 2 and all(item.entered.is_set() for item in FakeNative.instances))
                adapters = {item.provider: item for item in FakeNative.instances}
                original_sessions = {provider: item.session_id for provider, item in adapters.items()}
                self.assertTrue(all(len(item.prompts) == 1 for item in adapters.values()))
                actor = actors["codex"]
                token = (self.bus.store.credentials / (actor + ".token")).read_text()
                context = self.runtime.bridge.request("/api/avalokita/context", token=token)
                payload = {**context, "operation": "send-keys", "recipient": actors["claude"], "body": "Please review this exact shared task", "kind": "critique", "idempotency_key": "peer-one"}
                result = self.runtime.peer(token, payload)
                self.assertEqual(result["state"], "accepted")
                self.assertEqual(len(self.bus.store.jobs()), 3)
                duplicate = self.runtime.peer(token, payload)
                self.assertEqual(duplicate["event"]["id"], result["event"]["id"])
                self.assertEqual(len(self.bus.store.jobs()), 3)
                with self.assertRaises(BridgeError):
                    self.runtime.peer(token, {**payload, "root_id": "foreign-goal"})
                followup = [job for job in self.bus.store.jobs() if job["event_id"] == result["event"]["id"]]
                self.assertEqual(len(followup), 1)
                self.assertEqual(followup[0]["state"], "queued")
                self.assertEqual(len(adapters["claude"].prompts), 1)
                with self.bus.store.connection() as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM avalokita_hosted_attempts WHERE job_id=?", (followup[0]["id"],)).fetchone()[0], 0)
                FakeNative.gates["claude"].set()
                self.wait(lambda: adapters["claude"].prompts.__len__() == 2)
                FakeNative.gates["codex"].set()
                self.completed(3)
                public = self.runtime.state()
                jobs = [item for item in public["jobs"] if item.get("parent_job_id")]
                self.assertEqual(len(jobs), 1)
                self.assertEqual(jobs[0]["root_id"], response["goal"]["id"])
                self.assertEqual(jobs[0]["parent_job_id"], context["parent_job_id"])
                self.assertTrue(self.runtime.bridge.dispatches()[response["goal"]["id"]]["delivery_verified"])
                self.assertEqual(len(FakeNative.instances), 2)
                self.assertEqual(len(adapters["codex"].prompts), 1)
                self.assertEqual(len(adapters["claude"].prompts), 2)
                for provider, adapter in adapters.items():
                    self.assertEqual(adapter.start_ids, [None])
                    self.assertEqual(adapter.session_id, original_sessions[provider])
                    self.assertEqual(self.bus.store.agent(actors[provider])["native_session_id"], original_sessions[provider])
            finally:
                for gate in FakeNative.gates.values():
                    gate.set()

    def test_observed_id_rebind_and_workspace_replacement_are_rejected(self):
        actor = self.configure("codex")["sessions"][0]["agent_id"]
        self.prompt(recipient_ids=[actor])
        self.completed()
        token = (self.bus.store.credentials / (actor + ".token")).read_text()
        self.bus.store.update_agent(actor, native_session_id="foreign-native-id")
        with self.assertRaises(BridgeError):
            self.runtime.bridge.authenticate_peer(token)
        ledger = self.bus.supervisor.hosted
        self.runtime.hosted.close()
        old = self.workspace.with_name("old-workspace")
        self.workspace.rename(old)
        self.workspace.mkdir()
        sentinel = self.workspace / "untouched.txt"
        sentinel.write_text("new workspace")
        with self.assertRaises(BusError):
            ledger.state()
        self.assertEqual(list(self.workspace.iterdir()), [sentinel])
        sentinel.unlink()
        self.workspace.rmdir()
        old.rename(self.workspace)

    def test_disable_after_claim_blocks_the_native_write_barrier(self):
        actor, job, ledger, _ = self.manual_job()
        ledger.configure([], self.bus.discovery)
        with self.assertRaises(BusError):
            ledger.event(actor, job, 1, {"type": "input_attempt", "session_id": "observed-original"})
        with self.bus.store.connection() as db:
            self.assertEqual(db.execute("SELECT input_attempted FROM avalokita_hosted_attempts WHERE job_id=?", (job["id"],)).fetchone()[0], 0)
        self.assertEqual(FakeNative.instances, [])

    def test_stale_native_event_does_not_restore_a_rebound_actor(self):
        actor, job, ledger, _ = self.manual_job()
        self.bus.store.update_agent(actor, native_session_id="replacement-native")
        with self.assertRaises(BusError):
            ledger.event(actor, job, 1, {"type": "session", "session_id": "observed-original"})
        with self.assertRaises(BusError):
            ledger.event(actor, job, 1, {"type": "input_attempt", "session_id": "observed-original"})
        self.assertEqual(self.bus.store.agent(actor)["native_session_id"], "replacement-native")

    def test_generation_revocation_blocks_old_input_even_with_same_native_id(self):
        actor, job, ledger, _ = self.manual_job()
        self.bus.store.update_agent(actor, generation=2)
        with self.assertRaises(BusError):
            ledger.event(actor, job, 1, {"type": "input_attempt", "session_id": "observed-original"})
        self.assertEqual(self.bus.store.agent(actor)["generation"], 2)

    def test_completion_is_atomic_and_cannot_restore_revoked_native_identity(self):
        actor, job, ledger, _ = self.manual_job()
        ledger.event(actor, job, 1, {"type": "input_attempt", "session_id": "observed-original"})
        ledger.event(actor, job, 1, {"type": "turn", "status": "accepted", "session_id": "observed-original", "turn_id": "observed-turn"})
        self.bus.store.update_agent(actor, native_session_id="replacement-native")
        with self.assertRaises(BusError):
            ledger.complete(job, 1, SimpleNamespace(text="Stale native result", session_id="observed-original"))
        ledger.attention(job, 1, "Review revoked native result")
        self.assertEqual(self.bus.store.agent(actor)["native_session_id"], "replacement-native")
        with self.bus.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE sender=? AND kind='result'", (actor,)).fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT state FROM avalokita_hosted_attempts WHERE job_id=?", (job["id"],)).fetchone()[0], "delivery_unknown")

    def test_requested_budget_ttl_preserve_last_reserved_turn_and_identity(self):
        actor, job, ledger, response = self.manual_job()
        with self.bus.store.connection() as db:
            root = db.execute("SELECT * FROM roots WHERE id=?", (job["root_id"],)).fetchone()
            self.assertEqual((root["budget"], root["used"]), (1, 1))
            self.assertLessEqual(root["deadline"], response["goal"]["deadline"])
        ledger.event(actor, job, 1, {"type": "input_attempt", "session_id": "observed-original"})
        ledger.complete(job, 1, SimpleNamespace(text="Exact selected result", session_id="observed-original"))
        self.assertEqual(self.bus.store.agent(actor)["native_session_id"], "observed-original")
        self.assertEqual(self.bus.store.jobs()[0]["state"], "completed")
        self.assertEqual(self.runtime.bridge.reconcile_dispatches()[response["goal"]["id"]]["status"], "completed")

    def test_packaged_source_uses_existing_configured_mcp_interpreter_by_source_hash(self):
        source = HERE.parent / "sharebus/hosted_runtime.py"
        target = Path(self.temp.name) / "win-unpacked/resources/sharebus"
        target.mkdir(parents=True)
        for name in ("hosted_runtime.py", "mcp_server.py"):
            shutil.copyfile(source.with_name(name), target / name)
        spec = importlib.util.spec_from_file_location("packaged_hosted_fixture", target / "hosted_runtime.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        interpreter = module.resolve_mcp_python()
        self.assertTrue(Path(interpreter).is_file())
        self.assertEqual(Path(interpreter).name.lower(), "python.exe")

    def test_hosted_sender_can_reach_exact_attached_codex_and_rebind_is_rejected(self):
        from test_native_peers import NativePeerTests
        fixture = NativePeerTests()
        fixture.setUp()
        try:
            for actor in (fixture.a, fixture.b):
                fixture.finish(actor, fixture.original[actor["id"]])
            fixture.bus.native_enabled = True
            fixture.bus.avalokita_peer = fixture.runtime.peer
            fixture.bus.discovery.append({"provider": "claude", "installed": True, "managed_supported": True})
            state = fixture.bus.supervisor.configure_hosted(["claude"])
            actor = state["sessions"][0]["agent_id"]
            first = fixture.runtime.prompt({"text": "Selected hosted-to-attached peer work", "idempotency_key": "hosted-to-attached",
                "recipient_ids": [actor, fixture.b["id"]], "budget": 3, "ttl": 60})
            job = fixture.bus.store.claim(actor)
            ledger = fixture.bus.supervisor.hosted
            ledger.begin(fixture.bus.store.agent(actor), job, 1)
            fixture.bus.store.update_agent(actor, generation=1)
            ledger.event(actor, job, 1, {"type": "session", "session_id": "observed-owned-claude"})
            ledger.event(actor, job, 1, {"type": "input_attempt", "session_id": "observed-owned-claude"})
            token = (fixture.bus.store.credentials / (actor + ".token")).read_text()
            payload = {"operation": "send-keys", "root_id": first["goal"]["id"], "recipient": fixture.b["id"],
                "body": "Please review this same-root result", "kind": "critique", "idempotency_key": "owned-to-attached",
                "parent_job_id": job["id"], "parent_event_id": job["event_id"]}
            value = fixture.runtime.peer(token, payload)
            self.assertEqual(value["state"], "deferred")
            original = next(item for item in fixture.bus.store.jobs() if item["event_id"] == first["dispatch"]["event_id"] and item["agent_id"] == fixture.b["id"])
            fixture.finish(fixture.b, original)
            before = len(fixture.sends)
            fixture.runtime.drain_native_peers()
            self.assertEqual(len(fixture.sends), before + 1)
            self.assertEqual(fixture.sends[-1]["target"], fixture.b["native_session_id"])
            fixture.bus.store.update_agent(actor, native_session_id="replacement-owned-claude")
            with self.assertRaises(BridgeError):
                fixture.runtime.peer(token, {**payload, "idempotency_key": "revoked-owned-sender"})
            self.assertEqual(len(fixture.sends), before + 1)
        finally:
            fixture.tearDown()

    def test_standalone_daemon_without_peer_delegate_stays_blocked(self):
        from daemon import BusHTTPServer
        bus = BusHTTPServer(self.workspace, enable_native=False, discovery=[{"provider": "codex", "installed": True, "managed_supported": True}])
        bus.native_enabled = True
        (bus.store.private / "runtime.json").write_text(json.dumps(bus.runtime()))
        thread = threading.Thread(target=bus.serve_forever, daemon=True)
        thread.start()
        try:
            value = self.runtime.hosted.configure({"providers": ["codex"]})
            self.assertEqual(value["state"], "blocked")
            self.assertEqual(self.runtime.hosted.state()["state"], "blocked")
            self.assertEqual(bus.store.agents(), [])
            self.assertEqual(FakeNative.instances, [])
        finally:
            bus.shutdown()
            bus.close()

    def test_standalone_live_dispatcher_cannot_claim_persisted_hosted_work(self):
        from daemon import BusHTTPServer
        actor = self.configure("codex")["sessions"][0]["agent_id"]
        self.bus.supervisor._stop.set()
        self.prompt(recipient_ids=[actor])
        self.runtime.hosted.close()
        store = BusStore(self.workspace)
        before = store.jobs()
        self.assertEqual(before[0]["state"], "queued")
        # Unlike the HTTP-only fixture, this starts the actual dispatcher in
        # the constructor with a persisted enabled owner and accepted job.
        with patch.object(BusStore, "claim", side_effect=AssertionError("Standalone hosted claim must not run")) as claim:
            bus = BusHTTPServer(self.workspace, enable_native=True, discovery=[], adapter_factory=FakeNative)
            try:
                self.assertTrue(bus.supervisor._dispatcher.is_alive())
                bus.supervisor.wake()
                time.sleep(0.9)
                self.assertFalse(claim.called)
                self.assertEqual(bus.store.jobs(), before)
                self.assertEqual(FakeNative.instances, [])
                self.assertEqual(bus.supervisor._workers, {})
            finally:
                bus.close()

    def test_delegate_revocation_blocks_run_and_exact_input_barrier(self):
        actor, job, ledger, _ = self.manual_job()
        self.bus.avalokita_peer = None
        with self.assertRaises(BusError):
            self.bus.supervisor._run_hosted(self.bus.store.agent(actor), job)
        with self.assertRaises(BusError):
            ledger.event(actor, job, 1, {"type": "input_attempt", "session_id": "observed-original"})
        with self.bus.store.connection() as db:
            self.assertEqual(db.execute("SELECT input_attempted FROM avalokita_hosted_attempts WHERE job_id=?", (job["id"],)).fetchone()[0], 0)
        self.assertEqual(FakeNative.instances, [])


if __name__ == "__main__":
    unittest.main()
