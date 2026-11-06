"""Finite direct completion checks using disposable stores and fake transports."""
from contextlib import closing, contextmanager
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import test_canonical_reason as reason_fixture
import test_native_dispatch as ipc_fixture
import test_native_peers as peer_fixture
import test_hosted as hosted_fixture
import test_bridge as bridge_fixture
from bridge import BridgeError, BusError, BusStore
from harmony import HarmonyStore
from native_dispatch import GRANTS, TABLE, TRANSPORTS, NativeDispatcher, complete
from adapters.canonical_reason import CanonicalReasonTransport
from test_codex_app_tools import FakePipe, FakeSystem, SEND_TOOL, inventory, rpc_result


def http_result(fixture, claim, text):
    request = Request(fixture.bus.url + "/api/native/result", data=json.dumps({
        "native_session_id": fixture.actor["native_session_id"], "job_id": claim["job_id"],
        "event_id": claim["event_id"], "last_assistant_message": text}).encode(), headers={
            "Content-Type": "application/json", "X-Sharebus-Request": "1", "Authorization": "Bearer " + fixture.token})
    try:
        with urlopen(request, timeout=3) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


def mcp_result(fixture, claim, text):
    path = Path(__file__).resolve().parents[2] / "sharebus" / "mcp_server.py"
    name = "_sharebus_direct_completion_mcp_r4"
    module = sys.modules.get(name)
    if module is None:
        specification = importlib.util.spec_from_file_location(name, path)
        assert specification is not None and specification.loader is not None
        module = importlib.util.module_from_spec(specification)
        sys.modules[name] = module
        specification.loader.exec_module(module)
    assert Path(module.__file__).resolve() == path
    server = module.build_server("codex", fixture.workspace)
    tool = server._tool_manager.get_tool("sharebus_send")
    return tool.fn(SimpleNamespace(request_context=SimpleNamespace(meta={})), to="user", body=text, kind="result",
        workspace=str(fixture.workspace), session_token=fixture.token, native_session_id=fixture.actor["native_session_id"],
        job_id=claim["job_id"], event_id=claim["event_id"])


def snapshot(fixture, claim):
    with fixture.bus.store.connection() as db:
        job = dict(db.execute("SELECT * FROM jobs WHERE id=?", (claim["job_id"],)).fetchone())
        count = db.execute("SELECT COUNT(*) FROM events WHERE kind='result' AND sender=?", (fixture.actor["id"],)).fetchone()[0]
        actor = db.execute("SELECT channel_verified,active_root,state FROM agents WHERE id=?", (fixture.actor["id"],)).fetchone()[:]
    return job, count, actor


def deny_all(fixture, claim):
    before = snapshot(fixture, claim)
    with fixture.assertRaises((BridgeError, BusError)):
        complete(fixture.workspace, actor=fixture.actor["id"], native_session_id=fixture.actor["native_session_id"],
            job_id=claim["job_id"], event_id=claim["event_id"], text="Unproved synthetic result")
    with fixture.assertRaises(BusError):
        fixture.bus.store.native_result(fixture.actor["id"], fixture.actor["native_session_id"], "Unproved synthetic result",
            claim["job_id"], claim["event_id"])
    fixture.assertIn(http_result(fixture, claim, "Unproved synthetic result")[0], {403, 409})
    fixture.assertTrue(mcp_result(fixture, claim, "Unproved synthetic result").isError)
    fixture.assertEqual(snapshot(fixture, claim), before)


class IPCCompletionR4Tests(unittest.TestCase):
    setUp = ipc_fixture.NativeDispatchTests.setUp
    tearDown = ipc_fixture.NativeDispatchTests.tearDown
    reserve = ipc_fixture.NativeDispatchTests.reserve

    def send(self):
        claim = self.reserve()
        self.dispatcher.deliver(claim["dispatch_id"])
        return claim

    def test_reservation_and_untrusted_attempt_flag_cannot_complete_on_any_surface(self):
        class NoLedger:
            def __init__(self, **kwargs):
                self.gate = kwargs["authorize_send"]
            def send_authorized(self, target, prompt, **kwargs):
                assert self.gate(kwargs["authorization_record"])
                return {"state": "native_accepted", "native_accepted": True, "native_input_attempted": True}
        self.dispatcher.adapter_factory = NoLedger
        claim = self.send()
        self.assertIsNotNone(snapshot(self, claim)[0]["native_input_reserved_at"])
        self.assertFalse((self.runtime.bridge.receipts.parent / "codex-ipc.sqlite3").exists())
        deny_all(self, claim)
        self.assertEqual(self.pipe.writes, [])

    def test_prepared_or_not_sent_ledger_cannot_complete_on_any_surface(self):
        self.pipe.response = bytearray(inventory([]))
        claim = self.send()
        ledger = self.runtime.bridge.receipts.parent / "codex-ipc.sqlite3"
        for state in ("not_sent", "prepared"):
            with self.subTest(state=state), closing(sqlite3.connect(ledger)) as db:
                receipt = json.loads(db.execute("SELECT receipt FROM codex_dispatch").fetchone()[0])
                receipt.update(state=state, native_input_attempted=False)
                with db:
                    db.execute("UPDATE codex_dispatch SET receipt=?", (json.dumps(receipt),))
                deny_all(self, claim)
        self.assertEqual(len(self.pipe.writes), 1)

    def test_exact_row_and_every_receipt_identity_field_are_required(self):
        claim = self.send()
        ledger = self.runtime.bridge.receipts.parent / "codex-ipc.sqlite3"
        with closing(sqlite3.connect(ledger)) as db:
            original = db.execute("SELECT * FROM codex_dispatch").fetchone()
        variants = [("target_id", "wrong-native"), ("root_id", "wrong-root"), ("prompt_sha256", "0" * 64)]
        variants += [(key, value) for key, value in (("dispatch_id", "wrong-dispatch"), ("root_id", "wrong-root"),
            ("target_thread_sha256", "0" * 64), ("prompt_sha256", "0" * 64), ("native_input_attempted", 1),
            ("state", "prepared"), ("state", "not_sent"))]
        for index, (field, value) in enumerate(variants):
            with self.subTest(index=index, field=field), closing(sqlite3.connect(ledger)) as db, db:
                db.execute("DELETE FROM codex_dispatch")
                row = list(original)
                if index < 3:
                    row[{"target_id": 1, "root_id": 2, "prompt_sha256": 3}[field]] = value
                else:
                    receipt = json.loads(row[4])
                    receipt[field] = value
                    row[4] = json.dumps(receipt)
                db.execute("INSERT INTO codex_dispatch VALUES(?,?,?,?,?)", row)
            deny_all(self, claim)

    def test_attempted_unknown_callback_completes_once_across_all_surfaces(self):
        self.pipe.response = bytearray(inventory([ipc_fixture.SEND_TOOL]))
        claim = self.send()
        self.assertEqual(self.dispatcher._ipc_record(self.runtime.bridge, claim["dispatch_id"])[0], self.actor["native_session_id"])
        text = "Exact selected result after synthetic lost acknowledgement"
        self.assertEqual(http_result(self, claim, text)[0], 200)
        final = snapshot(self, claim)
        self.assertEqual(final[0]["state"], "completed")
        self.assertEqual(final[1], 1)
        self.assertTrue(self.bus.store.native_result(self.actor["id"], self.actor["native_session_id"], text,
            claim["job_id"], claim["event_id"])["duplicate_suppressed"])
        self.assertFalse(mcp_result(self, claim, text).isError)
        self.assertTrue(complete(self.workspace, actor=self.actor["id"], native_session_id=self.actor["native_session_id"],
            job_id=claim["job_id"], event_id=claim["event_id"], text=text)["duplicate_suppressed"])
        self.assertEqual(snapshot(self, claim), final)
        deny_all(self, claim)
        self.assertEqual(len(self.system.opens), 1)
        self.assertEqual(len(self.pipe.writes), 2)

    def test_generic_finish_denies_even_with_exact_attempt_proof(self):
        claim = self.send()
        before = snapshot(self, claim)
        with self.assertRaises(BusError):
            self.bus.store.finish(claim["job_id"], "completed", text="Generic bypass", native_session_id=self.actor["native_session_id"],
                expected_execution_owner="native_dispatch", expected_native=(self.actor["id"], claim["event_id"], self.actor["native_session_id"]))
        self.assertEqual(snapshot(self, claim), before)

    def test_standalone_without_handler_denies_but_authenticated_helper_remains(self):
        claim = self.send()
        self.unregister_completion()
        self.assertIsNone(self.bus.store._native_completion_registration)
        with self.assertRaises(BusError):
            self.bus.store.native_result(self.actor["id"], self.actor["native_session_id"], "Selected helper final", claim["job_id"], claim["event_id"])
        self.assertEqual(http_result(self, claim, "Selected helper final")[0], 409)
        self.assertTrue(mcp_result(self, claim, "Selected helper final").isError)
        self.assertTrue(complete(self.workspace, actor=self.actor["id"], native_session_id=self.actor["native_session_id"],
            job_id=claim["job_id"], event_id=claim["event_id"], text="Selected helper final")["completed"])

    def test_missing_or_malformed_selection_denies_delivery_and_every_completion_surface(self):
        claim = self.reserve()
        with self.bus.store.connection() as db:
            db.execute(f"DROP TRIGGER {TRANSPORTS}_update")
            db.execute(f"DROP TRIGGER {TRANSPORTS}_delete")
        for selection in (None, "not-json", "[]", '{"transport":"unknown"}', '{"transport":"reason"}'):
            with self.subTest(selection=selection), self.bus.store.connection() as db:
                db.execute(f"DELETE FROM {TRANSPORTS}")
                if selection is not None:
                    db.execute(f"INSERT INTO {TRANSPORTS} VALUES(?,?)", (claim["dispatch_id"], selection))
            with self.assertRaises(BridgeError):
                self.dispatcher.deliver(claim["dispatch_id"])
            deny_all(self, claim)
            self.assertIsNone(snapshot(self, claim)[0]["native_input_reserved_at"])
        self.assertEqual(self.system.opens, [])

    def test_stale_generation_binding_root_or_prompt_cannot_complete(self):
        claim = self.send()
        mutations = [
            ("UPDATE agents SET generation=generation+1 WHERE id=?", "UPDATE agents SET generation=generation-1 WHERE id=?", (self.actor["id"],)),
            ("UPDATE agents SET native_session_id='rebound' WHERE id=?", "UPDATE agents SET native_session_id='fixture-original-codex' WHERE id=?", (self.actor["id"],)),
            ("UPDATE jobs SET root_id='wrong-root' WHERE id=?", "UPDATE jobs SET root_id=? WHERE id=?", (claim["job_id"],)),
            ("UPDATE events SET body=body||' changed' WHERE id=?", "UPDATE events SET body=substr(body,1,length(body)-8) WHERE id=?", (claim["event_id"],))]
        for index, (change, restore, arguments) in enumerate(mutations):
            with self.subTest(index=index), self.bus.store.connection() as db:
                db.execute(change, arguments)
            deny_all(self, claim)
            with self.bus.store.connection() as db:
                db.execute(restore, (claim["root_id"], claim["job_id"]) if index == 2 else arguments)

    def test_completion_reader_never_creates_or_writes_transport_ledger(self):
        claim = self.send()
        calls = []
        original = sqlite3.connect
        def checked(path, *args, **kwargs):
            if "codex-ipc.sqlite3" in str(path):
                calls.append((str(path), kwargs.get("uri")))
                self.assertTrue(str(path).endswith("?mode=ro"))
                self.assertIs(kwargs.get("uri"), True)
            return original(path, *args, **kwargs)
        with patch("native_dispatch.sqlite3.connect", checked), patch.object(ipc_fixture.CodexAppToolsAdapter, "_ledger", side_effect=AssertionError("Completion must be read only")):
            self.assertTrue(complete(self.workspace, actor=self.actor["id"], native_session_id=self.actor["native_session_id"],
                job_id=claim["job_id"], event_id=claim["event_id"], text="Read only exact final")["completed"])
        self.assertEqual(len(calls), 1)

    def test_ipc_native_identifier_129_is_rejected_before_transport_inspection(self):
        native_id = "i" * 129
        self.bus.store.update_agent(self.actor["id"], native_session_id=native_id)
        self.actor = self.bus.store.agent(self.actor["id"])
        goal = self.runtime.store.new_goal("Synthetic IPC identifier boundary", idempotency_key="ipc-id-129")
        self.runtime.bridge.dispatch(goal, goal["prompt"], native_ipc_ids=[self.actor["id"]])
        claim = self.dispatcher.reserve(goal["id"], self.actor["id"])
        with patch.object(NativeDispatcher, "_ipc_input_attempt", side_effect=AssertionError("Unsupported identifier reached transport")) as proof:
            with self.assertRaises(BridgeError):
                NativeDispatcher.complete_native(self.runtime.bridge, self.token, actor=self.actor["id"],
                    native_session_id=native_id, job_id=claim["job_id"], event_id=claim["event_id"], text="Rejected oversized IPC ID")
        proof.assert_not_called()
        self.assertEqual(self.system.opens, [])
        self.assertIsNone(snapshot(self, claim)[0]["native_input_reserved_at"])
        self.assertEqual(snapshot(self, claim)[1], 0)


class ReasonCompletionR4Tests(unittest.TestCase):
    setUp = reason_fixture.CanonicalReasonTests.setUp
    tearDown = reason_fixture.CanonicalReasonTests.tearDown
    reserve = reason_fixture.CanonicalReasonTests.reserve
    process_factory = reason_fixture.CanonicalReasonTests.process_factory

    def no_attempt(self, gates):
        decisions = []
        class NoAttempt:
            def __init__(self, **kwargs):
                self.gate = kwargs["authorize_send"]
            def send_authorized(self, *args, **kwargs):
                decisions.extend(self.gate(kwargs["authorization_record"]) for _ in range(gates))
                return {"state": "delivery_unknown", "native_accepted": False, "native_input_attempted": True}
        claim = self.reserve()
        with patch("native_dispatch.ReasonEngineAdapter", NoAttempt):
            self.dispatcher.deliver(claim["dispatch_id"])
        self.assertEqual(decisions, [True] * gates)
        with self.bus.store.connection() as db:
            self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {GRANTS}").fetchone()[0], int(gates == 2))
        deny_all(self, claim)
        self.assertEqual(self.starts, [])

    def test_zero_gate_rejects_every_surface(self):
        self.no_attempt(0)

    def test_one_gate_rejects_every_surface(self):
        self.no_attempt(1)

    def test_immutable_grant_alone_rejects_every_surface(self):
        self.no_attempt(2)

    def test_exact_attempted_unknown_http_and_mcp_after_pause_expiry_is_observation_only(self):
        self.mode = "unknown"
        claim = self.reserve()
        self.dispatcher.deliver(claim["dispatch_id"])
        self.runtime.control("pause")
        with self.runtime.store._connection(write=True) as db:
            db.execute("UPDATE goals SET deadline=0,used=budget")
            controls = tuple(db.execute("SELECT state,budget,used,deadline FROM goals").fetchone())
        with self.bus.store.connection() as db:
            db.execute("UPDATE roots SET deadline=0")
            before_root = tuple(db.execute("SELECT budget,used,deadline FROM roots").fetchone())
        text = "Selected synthetic late Reason result"
        self.assertEqual(http_result(self, claim, text)[0], 200)
        self.assertFalse(mcp_result(self, claim, text).isError)
        self.assertTrue(self.bus.store.native_result(self.actor["id"], self.binding.native_id, text,
            claim["job_id"], claim["event_id"])["duplicate_suppressed"])
        deny_all(self, claim)
        with self.runtime.store._connection() as db:
            self.assertEqual(tuple(db.execute("SELECT state,budget,used,deadline FROM goals").fetchone()), controls)
        with self.bus.store.connection() as db:
            self.assertEqual(tuple(db.execute("SELECT budget,used,deadline FROM roots").fetchone()), before_root)
            self.assertEqual(db.execute("SELECT value FROM config WHERE key='paused'").fetchone()[0], "true")
        self.assertEqual(len(self.starts), 1)


class ReasonIdentifierBoundaryR4Tests(unittest.TestCase):
    setUp = reason_fixture.CanonicalReasonTests.setUp
    tearDown = reason_fixture.CanonicalReasonTests.tearDown
    process_factory = reason_fixture.CanonicalReasonTests.process_factory

    def prepare(self, length):
        native_id = "n" * length
        self.bus.store.update_agent(self.actor["id"], native_session_id=native_id)
        self.actor = self.bus.store.agent(self.actor["id"])
        self.binding = replace(self.binding, native_id=native_id)
        self.rollout.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": native_id, "cwd": str(self.workspace)}}) + "\n", encoding="utf-8")
        self.reason = CanonicalReasonTransport(engine_path=self.engine, engine_sha256=self.reason.engine_sha256,
            bindings={self.actor["id"]: self.binding}, process_factory=self.process_factory)
        self.goal = self.runtime.store.new_goal("Synthetic Reason identifier boundary", idempotency_key="reason-id-" + str(length))
        self.runtime.bridge.dispatch(self.goal, self.goal["prompt"], native_ipc_ids=[self.actor["id"]])
        self.dispatcher = NativeDispatcher(self.runtime.bridge, harmony_store=self.runtime.store, reason_transport=self.reason)
        return self.dispatcher.reserve(self.goal["id"], self.actor["id"])

    def complete_length(self, length):
        claim = self.prepare(length)
        self.assertEqual(self.dispatcher.deliver(claim["dispatch_id"])["state"], "transport_accepted")
        text = "Exact selected synthetic Reason identifier result"
        self.assertEqual(http_result(self, claim, text)[0], 200)
        self.assertFalse(mcp_result(self, claim, text).isError)
        self.assertTrue(complete(self.workspace, actor=self.actor["id"], native_session_id=self.binding.native_id,
            job_id=claim["job_id"], event_id=claim["event_id"], text=text)["duplicate_suppressed"])
        self.assertEqual(snapshot(self, claim)[0]["state"], "completed")
        self.assertEqual(snapshot(self, claim)[1], 1)
        self.assertEqual(len(self.starts), 1)

    def test_reason_native_identifier_129_completes_through_the_same_kernel(self):
        self.complete_length(129)

    def test_reason_native_identifier_200_completes_through_the_same_kernel(self):
        self.complete_length(200)

    def test_reason_native_identifier_201_is_rejected_before_transport_inspection(self):
        claim = self.prepare(201)
        with patch.object(NativeDispatcher, "_reason_input_attempt", side_effect=AssertionError("Unsupported identifier reached transport")) as proof:
            with self.assertRaises(BridgeError):
                NativeDispatcher.complete_native(self.runtime.bridge, self.token, actor=self.actor["id"],
                    native_session_id=self.binding.native_id, job_id=claim["job_id"], event_id=claim["event_id"], text="Rejected oversized Reason ID")
        proof.assert_not_called()
        self.assertEqual(self.starts, [])
        self.assertIsNone(snapshot(self, claim)[0]["native_input_reserved_at"])
        self.assertEqual(snapshot(self, claim)[1], 0)

    def test_native_identifier_257_is_rejected_before_store_construction(self):
        with patch("native_dispatch.HarmonyStore", side_effect=AssertionError("Oversized identifier constructed a store")) as store:
            with self.assertRaises(BridgeError):
                NativeDispatcher.complete_native(self.runtime.bridge, self.token, actor=self.actor["id"],
                    native_session_id="n" * 257, job_id="unread-job", event_id="unread-event", text="Rejected canonical oversized ID")
        store.assert_not_called()
        self.assertEqual(self.starts, [])


class DeferredCompletionR4Tests(unittest.TestCase):
    setUp = peer_fixture.NativePeerTests.setUp
    tearDown = peer_fixture.NativePeerTests.tearDown
    peer = peer_fixture.NativePeerTests.peer
    finish = peer_fixture.NativePeerTests.finish
    child_job = peer_fixture.NativePeerTests.child_job

    def retire(self, which, delete=False):
        old = self.peer(self.a, self.b, "revoked-old-head")
        self.finish(self.a, self.original[self.a["id"]])
        self.finish(self.b, self.original[self.b["id"]])
        actor = self.a if which == "sender" else self.b
        if delete:
            with self.bus.store.connection() as db:
                db.execute("DELETE FROM agents WHERE id=?", (actor["id"],))
        else:
            self.bus.store.update_agent(actor["id"], native_session_id="rebound-" + which)
        before = len(self.sends)
        self.assertEqual(self.runtime.drain_native_peers(), [])
        self.assertEqual(self.child_job(old)["state"], "avalokita_binding_revoked")
        self.assertIsNone(self.child_job(old)["native_input_reserved_at"])
        self.assertEqual(len(self.sends), before)
        return old

    def test_rebound_sender_head_retires_and_next_lawful_root_proceeds(self):
        self.retire("sender")
        third, token = self.bus.store.register("codex", "Fresh peer", [], "external_attached", native_session_id="third-original")
        fresh = self.runtime.prompt({"text": "New lawful root", "idempotency_key": "after-revoked-head",
            "recipient_ids": [third["id"], self.b["id"]]})
        child = self.runtime.peer(token, {"operation": "send-keys", "root_id": fresh["goal"]["id"],
            "recipient": self.b["id"], "body": "Fresh lawful child", "kind": "message", "idempotency_key": "fresh-child"})
        with self.bus.store.connection() as db:
            parent = dict(db.execute("SELECT * FROM jobs WHERE event_id=? AND agent_id=?", (fresh["dispatch"]["event_id"], self.b["id"])).fetchone())
        self.finish(self.b, parent)
        before = len(self.sends)
        self.runtime.drain_native_peers()
        self.assertEqual(self.child_job(child)["state"], "awaiting_native_reply")
        self.assertEqual(len(self.sends), before + 1)

    def test_deleted_sender_unattempted_head_retires(self):
        self.retire("sender", delete=True)

    def test_rebound_recipient_unattempted_head_retires(self):
        self.retire("recipient")

    def test_deleted_recipient_unattempted_head_retires(self):
        self.retire("recipient", delete=True)

    def test_reserved_or_unknown_head_is_not_automatically_retired(self):
        old = self.peer(self.a, self.b, "uncertain-head")
        self.finish(self.b, self.original[self.b["id"]])
        self.unknown = True
        self.runtime.drain_native_peers()
        before = self.child_job(old)
        self.assertIsNotNone(before["native_input_reserved_at"])
        self.bus.store.update_agent(self.a["id"], native_session_id="rebound-unknown-sender")
        count = len(self.sends)
        self.assertEqual(self.runtime.drain_native_peers(), [])
        self.assertEqual(self.child_job(old), before)
        self.assertEqual(len(self.sends), count)

    def test_optional_and_injected_harmony_are_cached_before_all_writers(self):
        original = HarmonyStore
        constructions = []
        class CheckedHarmony(original):
            def __init__(self, *args, **kwargs):
                constructions.append(True)
                super().__init__(*args, **kwargs)
        with patch("native_dispatch.HarmonyStore", CheckedHarmony):
            dispatcher = NativeDispatcher(self.runtime.bridge, adapter_factory=self.factory)
            self.assertEqual(len(constructions), 1)
            injected = NativeDispatcher(self.runtime.bridge, adapter_factory=self.factory, harmony_store=self.runtime.store)
            self.assertIs(injected.harmony, self.runtime.store)
            self.assertEqual(len(constructions), 1)
        with patch("native_dispatch.HarmonyStore", side_effect=AssertionError("Constructor under held writer")):
            with dispatcher.admission() as (db, local, harmony):
                self.assertTrue(db.in_transaction)
                self.assertEqual(dispatcher._load_routing(), dispatcher.routing)
            dispatcher.peer_send(self.a_token, self.goal, self.b["id"], "Cached store peer", "message", "cached-peer")


class OwnedCompletionHandlerR4Tests(unittest.TestCase):
    setUp = hosted_fixture.HostedTests.setUp
    tearDown = hosted_fixture.HostedTests.tearDown
    configure = hosted_fixture.HostedTests.configure

    def test_actual_owned_broker_installs_exact_handler_and_unregisters_on_close(self):
        self.configure("codex")
        owned = self.runtime.hosted.server
        self.assertIs(owned.store._native_completion_registration[1].__self__, self.runtime)
        self.assertEqual(owned.store._native_completion_registration[1].__func__, self.runtime.complete_native_result.__func__)
        unregister = owned._avalokita_completion_unregister
        self.runtime.hosted.close()
        self.assertIsNone(owned.store._native_completion_registration)
        unregister()
        self.assertIsNone(owned.store._native_completion_registration)

    def test_closing_runtime_refuses_new_handler_work(self):
        self.runtime.begin_closing()
        with self.assertRaises(BusError):
            self.runtime.complete_native_result(actor="missing", native_session_id="missing", text="Not admitted",
                job_id="missing", event_id="missing")

    def test_owned_http_and_real_mcp_method_automatically_use_the_installed_kernel(self):
        self.configure("codex")
        self.actor, self.token = self.bus.store.register("codex", "Exact direct peer", [], "external_attached",
            native_session_id="owned-direct-original")
        goal = self.runtime.store.new_goal("Exact owned broker direct input", idempotency_key="owned-direct-r4")
        self.runtime.bridge.dispatch(goal, goal["prompt"], recipient_ids=[self.actor["id"]], native_ipc_ids=[self.actor["id"]])
        pipe = FakePipe(inventory([SEND_TOOL]) + rpc_result(2, {"success": True, "contentItems": [
            {"type": "inputText", "text": json.dumps({"threadId": self.actor["native_session_id"]})}]}))
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "owned-synthetic-source"}):
            dispatcher = NativeDispatcher(self.runtime.bridge, harmony_store=self.runtime.store,
                adapter_factory=lambda **kwargs: ipc_fixture.CodexAppToolsAdapter(system=FakeSystem(pipe), **kwargs))
            claim = dispatcher.reserve(goal["id"], self.actor["id"])
            self.assertIn(http_result(self, claim, "Selected owned final")[0], {403, 409})
            dispatcher.deliver(claim["dispatch_id"])
        self.assertEqual(http_result(self, claim, "Selected owned final")[0], 200)
        self.assertFalse(mcp_result(self, claim, "Selected owned final").isError)
        self.assertEqual(snapshot(self, claim)[1], 1)
        self.assertEqual(len(pipe.writes), 2)


class BrokerPeerProvenanceR4Tests(unittest.TestCase):
    setUp = bridge_fixture.BridgeTests.setUp
    tearDown = bridge_fixture.BridgeTests.tearDown
    prompt = bridge_fixture.BridgeTests.prompt

    def peer_input(self):
        result = self.prompt()
        self.bus.store.claim(self.agent["id"])
        target = self.bus.store.claim(self.other["id"])
        self.bus.store.reserve_native_input(self.other["id"], target["id"], target["event_id"], target["root_id"], self.other["native_session_id"])
        self.bus.store.await_native(target["id"], "Synthetic broker receipt")
        self.bus.store.native_result(self.other["id"], self.other["native_session_id"], "Original selected result", target["id"], target["event_id"])
        return self.bridge.peer_send(self.token, self.other["id"], "Fresh broker peer input", "message", result["dispatch"]["root_id"], "fresh-broker-peer")

    def test_fresh_broker_peer_retains_normal_claim_reservation_and_completion(self):
        child = self.peer_input()
        self.assertIn("avalokita_broker_peer_dispatch", child["event"]["metadata"])
        self.assertNotIn("avalokita_peer_dispatch", child["event"]["metadata"])
        job = self.bus.store.claim(self.other["id"])
        self.assertEqual(job["id"], child["jobs"][0]["id"])
        self.assertEqual(job["native_completion_domain"], "broker_native")
        self.bus.store.reserve_native_input(self.other["id"], job["id"], job["event_id"], job["root_id"], self.other["native_session_id"])
        self.bus.store.await_native(job["id"], "Synthetic broker peer receipt")
        self.assertTrue(self.bus.store.native_result(self.other["id"], self.other["native_session_id"], "Exact broker peer final", job["id"], job["event_id"])["completed"])

    def test_old_ambiguous_peer_marker_remains_unadopted_and_unchanged(self):
        child = self.peer_input()
        with self.bus.store.connection() as db:
            metadata = child["event"]["metadata"].copy()
            metadata["avalokita_peer_dispatch"] = metadata.pop("avalokita_broker_peer_dispatch")
            encoded = json.dumps(metadata)
            db.execute("UPDATE events SET metadata=? WHERE id=?", (encoded, child["event"]["id"]))
        with self.assertRaises(BusError):
            self.bus.store.claim(self.other["id"])
        with self.bus.store.connection() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (child["jobs"][0]["id"],)).fetchone()
            self.assertIsNone(job["execution_owner"])
            self.assertIsNone(job["native_completion_domain"])
            self.assertIsNone(job["started_at"])
            self.assertIsNone(job["native_input_reserved_at"])
            self.assertEqual(db.execute("SELECT metadata FROM events WHERE id=?", (child["event"]["id"],)).fetchone()[0], encoded)
