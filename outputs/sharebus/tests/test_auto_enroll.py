"""Isolated native bootstrap, proof replay, private paths, and HTTP authority."""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import auto_enroll
from daemon import BusHTTPServer
from store import BusError, BusStore


def isolated_environment():
    # Windows OpenSSL needs SystemRoot even for urllib's HTTP opener.
    runtime = {}
    if os.name == "nt" and "SYSTEMROOT" in os.environ:
        runtime["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return patch.dict(os.environ, runtime, clear=True)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sharebus-bootstrap-")
        self.root = Path(self.temporary.name).resolve()
        self.environment = isolated_environment()
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def enable(self):
        store = BusStore(self.root)
        store.configure_auto(enabled=True)
        return store

    def hook(self, provider="codex", native="native-original", **extra):
        return auto_enroll.bootstrap_hook(provider, {"session_id": native, "cwd": str(self.root), **extra})

    def test_absent_or_disabled_roots_never_construct_store_or_create_files(self):
        with patch.object(auto_enroll, "BusStore", wraps=BusStore) as constructor:
            self.assertIsNone(self.hook())
            constructor.assert_not_called()
        self.assertFalse((self.root / ".sharebus").exists())
        store = self.enable()
        store.configure_auto(enabled=False)
        before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in self.root.rglob("*") if p.is_file()}
        with patch.object(auto_enroll, "BusStore", wraps=BusStore) as constructor:
            self.assertIsNone(self.hook())
            constructor.assert_not_called()
        self.assertEqual(before, {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in self.root.rglob("*") if p.is_file()})

    def test_actual_hook_cwd_wins_over_global_workspace_and_explicit_mismatch_rejected(self):
        other = self.root / "other"
        other.mkdir()
        BusStore(other).configure_auto(enabled=True)
        with patch.dict(os.environ, {"SHAREBUS_WORKSPACE": str(other)}):
            self.assertIsNone(self.hook())
            self.assertIsNone(auto_enroll.bootstrap_hook("codex", {"session_id": "n", "cwd": str(self.root)}, workspace=other))
        self.assertEqual(BusStore(other).agents(), [])

    def test_managed_and_missing_official_native_fields_never_register(self):
        store = self.enable()
        for name in ("SHAREBUS_MANAGED", "SHAREBUS_AGENT_ID", "SHAREBUS_AGENT_TOKEN_FILE"):
            with patch.dict(os.environ, {name: "owned"}):
                self.assertIsNone(self.hook())
        for payload in ({"threadId": "n", "cwd": str(self.root)}, {"session_id": "n"},
                        {"session_id": "bad\nidentity", "cwd": str(self.root)}):
            self.assertIsNone(auto_enroll.bootstrap_hook("codex", payload))
        self.assertEqual(store.agents(), [])

    def test_antigravity_multiple_enabled_roots_need_explicit_selection(self):
        self.enable()
        other = self.root / "other"
        other.mkdir()
        BusStore(other).configure_auto(enabled=True)
        payload = {"conversationId": "ag-original", "workspacePaths": [str(self.root), str(other)]}
        self.assertIsNone(auto_enroll.bootstrap_hook("antigravity", payload))
        context = auto_enroll.bootstrap_hook("antigravity", payload, workspace=self.root)
        self.assertEqual(json.loads(context.read_text())["workspace"], str(self.root))
        self.assertEqual(BusStore(other).agents(), [])

    def test_duplicate_hooks_preserve_context_code_claim_native_owner_and_token(self):
        store = self.enable()
        first = self.hook()
        first_bytes = first.read_bytes()
        first_agent = store.agents()[0]
        token_file = store.credentials / (first_agent["id"] + ".token")
        token, stamp = token_file.read_bytes(), token_file.stat().st_mtime_ns
        self.assertEqual(self.hook(), first)
        self.assertEqual(first.read_bytes(), first_bytes)
        self.assertEqual(len(store.agents()), 1)
        self.assertEqual(token_file.read_bytes(), token)
        self.assertEqual(token_file.stat().st_mtime_ns, stamp)

    def test_duplicate_hooks_in_parallel_do_not_clone_identity(self):
        store = self.enable()
        paths, errors = [], []
        def call():
            try:
                paths.append(self.hook())
            except (BusError, OSError, sqlite3.Error) as exc:
                errors.append(type(exc).__name__)
        threads = [threading.Thread(target=call) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        self.assertFalse(errors)
        self.assertEqual(len(paths), 3)
        # A contending optional hook may skip at its short SQLite timeout;
        # successful duplicate registrations must still share one identity.
        contexts = {path for path in paths if path is not None}
        self.assertEqual(len(contexts), 1)
        self.assertEqual(len(store.agents()), 1)
        self.assertEqual(self.hook(), next(iter(contexts)))

    def test_raw_generic_prompt_and_transcript_never_make_human_root_or_persist(self):
        store = self.enable()
        path = self.hook(prompt="raw-secret-prompt", transcript_path="foreign-transcript",
                         last_assistant_message="raw-secret-response")
        self.assertEqual(store.events()["events"], [])
        with store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM roots").fetchone()[0], 0)
        for target in self.root.rglob("*"):
            if target.is_file():
                self.assertNotIn(b"raw-secret-prompt", target.read_bytes())
                self.assertNotIn(b"raw-secret-response", target.read_bytes())
                self.assertNotIn(b"foreign-transcript", target.read_bytes())
        self.assertIsNotNone(path)

    def test_existing_role_is_preserved_and_managed_owner_is_rejected(self):
        store = self.enable()
        original, token = store.register("codex", "CEO", role="CEO", native_session_id="native-original")
        context = json.loads(self.hook().read_text())
        self.assertEqual(context["agent_id"], original["id"])
        self.assertEqual(store.agent(original["id"])["role"], "CEO")
        self.assertEqual(store.redeem_auto(context)["token"], token)
        store.update_agent(original["id"], mode="managed")
        with self.assertRaises(BusError):
            self.hook()
        self.assertEqual(len(store.agents()), 1)

    def test_human_controls_block_bootstrap_and_survive_reopen(self):
        store = self.enable()
        path = self.hook()
        actor = json.loads(path.read_text())["agent_id"]
        for state in ("paused", "stopped", "needs_human"):
            store.update_agent(actor, state=state)
            with self.assertRaises(BusError):
                self.hook()
            self.assertEqual(BusStore(self.root).agent(actor)["state"], state)
        store.configure_auto(suspended=True)
        self.assertIsNone(self.hook())
        self.assertTrue(BusStore(self.root).auto_state()["suspended"])
        store.configure_auto(suspended=False)
        store.set_paused(True)
        with self.assertRaises(BusError):
            self.hook()
        self.assertTrue(store.paused())

    def test_transport_requires_exact_prior_hook_and_never_uses_session_fallback(self):
        store = self.enable()
        self.assertIsNone(auto_enroll.trusted_transport_binding(None, "native-original", self.root))
        self.assertIsNone(auto_enroll.trusted_transport_binding("native-original", "transport", self.root))
        path = self.hook()
        self.assertIsNone(auto_enroll.trusted_transport_binding("foreign-thread", "native-original", self.root))
        self.assertEqual(auto_enroll.trusted_transport_binding("native-original", "transport", self.root), path)
        record = json.loads(path.read_text())
        self.assertEqual(record["native_session_id"], "native-original")
        self.assertEqual(record["native_hook_source"], "codex:SessionStart")
        self.assertEqual(record["transport_binding"], "transport")
        self.assertEqual(auto_enroll.trusted_transport_binding("native-original", "transport", self.root), path)
        self.assertEqual(len(store.agents()), 1)

    def test_linked_or_reparse_ancestor_rejected_before_store_construction(self):
        from types import SimpleNamespace
        self.enable()
        parent = self.root.parent
        original = Path.lstat
        def reparse_stat(path):
            info = original(path)
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400) if path == parent else info
        with patch.object(Path, "lstat", reparse_stat), patch.object(auto_enroll, "BusStore", wraps=BusStore) as constructor:
            with self.assertRaises(BusError):
                self.hook()
            constructor.assert_not_called()

    def test_hardlinked_context_rejected_and_original_preserved(self):
        self.enable()
        path = self.hook()
        alias = self.root / "alias.json"
        os.link(path, alias)
        before = path.read_bytes()
        with self.assertRaises(BusError):
            self.hook()
        self.assertEqual(alias.read_bytes(), before)

    def test_auto_epoch_changes_only_on_new_enable_lifecycle(self):
        store = self.enable()
        first = store.auto_state()["epoch"]
        store.configure_auto(suspended=True)
        store.configure_auto(suspended=False)
        store.touch_auto_control()
        self.assertEqual(store.auto_state()["epoch"], first)
        store.configure_auto(enabled=False)
        self.assertEqual(store.auto_state()["epoch"], first)
        store.configure_auto(enabled=True)
        self.assertGreater(store.auto_state()["epoch"], first)

    def test_busy_database_hook_returns_in_a_bounded_time_without_starting_daemon(self):
        store = self.enable()
        with store.connection() as db, patch.object(auto_enroll.Client, "start") as start, patch.object(auto_enroll, "BusStore", wraps=BusStore) as constructor:
            db.execute("BEGIN IMMEDIATE")
            began = time.monotonic()
            self.assertIsNone(self.hook())
            self.assertLess(time.monotonic() - began, 5)
            self.assertEqual(constructor.call_args.kwargs["timeout"], 0.2)
            start.assert_not_called()

    def test_policy_role_preference_only_applies_to_new_signup(self):
        from policy import default_team
        team = default_team()
        first = next(role for role in team["roles"].values() if role["preferred_provider"] == "codex")
        first["label"] = "Custom reviewer"
        store = self.enable()
        (store.directory / "team.json").write_text(json.dumps(team))
        self.hook()
        self.assertEqual(store.agents()[0]["role"], "Custom reviewer")
        first["label"] = "Other preference"
        (store.directory / "team.json").write_text(json.dumps(team))
        self.hook()
        self.assertEqual(store.agents()[0]["role"], "Custom reviewer")

    def test_operator_native_environment_bootstrap_exact_cwd_and_honest_source(self):
        store = self.enable()
        native = str(uuid.uuid4())
        with patch.dict(os.environ, {"CODEX_THREAD_ID": native}), patch.object(Path, "cwd", return_value=self.root):
            context_file = auto_enroll.bootstrap_native_environment(self.root)
            context = json.loads(context_file.read_text())
            self.assertEqual(context["native_session_id"], native)
            self.assertEqual(context["hook_source"], "codex:native_environment")
            self.assertEqual(context["native_hook_source"], "codex:native_environment")
            self.assertEqual(auto_enroll.bootstrap_native_environment(self.root), context_file)
            self.assertIsNone(auto_enroll.bootstrap_hook("codex", {"session_id": native, "cwd": str(self.root)}, event="native_environment"))
            self.assertIsNone(auto_enroll.trusted_transport_binding("foreign", native, self.root))
            self.assertEqual(auto_enroll.trusted_transport_binding(native, "actual-transport", self.root), context_file)
        self.assertEqual(len(store.agents()), 1)
        self.assertEqual(store.events()["events"], [])

    def test_operator_native_environment_cannot_bind_global_other_root_or_infer_id(self):
        store = self.enable()
        other = self.root / "other"
        other.mkdir()
        BusStore(other).configure_auto(enabled=True)
        native = str(uuid.uuid4())
        with patch.dict(os.environ, {"CODEX_THREAD_ID": native, "SHAREBUS_WORKSPACE": str(other)}), patch.object(Path, "cwd", return_value=self.root):
            self.assertIsNone(auto_enroll.bootstrap_native_environment(other))
            for key in ("SHAREBUS_AGENT_ID", "CLAUDE_CODE_ENTRYPOINT", "CLAUDECODE", "CLAUDE_CODE"):
                with patch.dict(os.environ, {key: "owned-by-another-host"}):
                    self.assertIsNone(auto_enroll.bootstrap_native_environment(self.root))
        for value in ("public-native-id", "", "bad\nidentity"):
            with patch.dict(os.environ, {"CODEX_THREAD_ID": value}), patch.object(Path, "cwd", return_value=self.root):
                self.assertIsNone(auto_enroll.bootstrap_native_environment(self.root))
        self.assertEqual(store.agents(), [])
        self.assertEqual(BusStore(other).agents(), [])

    def test_codex_delivery_default_explicit_choice_and_epoch_are_durable(self):
        store = self.enable()
        initial = store.auto_state()
        self.assertEqual(initial["codex_delivery"], "stop_hook")
        self.assertFalse(initial["codex_delivery_configured"])
        store.configure_auto(codex_delivery="stop_hook")
        self.assertTrue(store.auto_state()["codex_delivery_configured"])
        store.configure_auto(codex_delivery="native_queue")
        self.assertEqual(store.auto_state()["epoch"], initial["epoch"])
        store.configure_auto(enabled=False, suspended=True)
        reopened = BusStore(self.root).auto_state()
        self.assertEqual(reopened["codex_delivery"], "native_queue")
        self.assertTrue(reopened["codex_delivery_configured"])
        mirror = json.loads((store.directory / "auto.json").read_text())
        self.assertEqual(mirror["codex_delivery"], "native_queue")
        self.assertTrue(mirror["codex_delivery_configured"])
        for value in ("resume", True, [], {}):
            with self.assertRaises(BusError):
                store.configure_auto(codex_delivery=value)

    def test_native_queue_requires_consumed_private_proof_and_preserves_role_token(self):
        store = self.enable()
        store.configure_auto(codex_delivery="native_queue")
        original, token = store.register("codex", role="CEO", native_session_id="native-original")
        record = json.loads(self.hook().read_text())
        self.assertFalse(store.auto_codex_attested(original["id"]))
        self.assertEqual(store.reconcile_auto_codex(original["id"])["mode"], "external")
        attached = store.redeem_auto(record)
        self.assertTrue(store.auto_codex_attested(original["id"]))
        self.assertEqual(attached["agent"]["mode"], "native_attached")
        self.assertEqual(attached["agent"]["role"], "CEO")
        self.assertEqual(attached["agent"]["id"], original["id"])
        self.assertEqual(attached["token"], token)
        self.assertEqual(len(store.agents()), 1)
        public_only, _ = store.register("codex", role="CTO", native_session_id="public-native-id")
        self.assertFalse(store.auto_codex_attested(public_only["id"]))
        self.assertEqual(store.reconcile_auto_codex(public_only["id"])["mode"], "external")

    def test_queue_configuration_and_future_hook_preserve_actor_controls(self):
        store = self.enable()
        original, _ = store.register("codex", role="CEO", native_session_id="native-original")
        record = json.loads(self.hook().read_text())
        store.redeem_auto(record)
        for state in ("paused", "stopped", "needs_human"):
            store.update_agent(original["id"], state=state)
            store.configure_auto(codex_delivery="native_queue")
            actor = store.agent(original["id"])
            self.assertEqual((actor["state"], actor["role"], actor["mode"]), (state, "CEO", "external"))
        store.update_agent(original["id"], state="idle")
        self.assertEqual(store.agent(original["id"])["mode"], "native_attached")
        store.configure_auto(codex_delivery="stop_hook")
        self.assertEqual(store.agent(original["id"])["mode"], "external_attached")
        # A future genuine hook can attach a previously attested, idle actor
        # without asking the user to repeat a join or changing its role.
        with store.connection() as db:
            db.execute("UPDATE config SET value='native_queue' WHERE key='auto_codex_delivery'")
        self.hook()
        self.assertEqual(store.agent(original["id"])["mode"], "native_attached")
        self.assertEqual(store.agent(original["id"])["role"], "CEO")

    def test_inactive_native_queue_cannot_claim_or_spend_budget(self):
        store = self.enable()
        store.configure_auto(codex_delivery="native_queue")
        record = json.loads(self.hook().read_text())
        actor = store.redeem_auto(record)["agent"]["id"]
        event = store.send("user", actor, "queued bounded task", "task")
        store.configure_auto(enabled=False)
        self.assertIsNone(store.claim(actor))
        with store.connection() as db:
            self.assertEqual(db.execute("SELECT used FROM roots WHERE id=?", (event["root_id"],)).fetchone()[0], 0)
        self.assertEqual(store.jobs()[0]["state"], "queued")
        store.configure_auto(enabled=True)
        self.assertIsNotNone(store.claim(actor))

    def test_restart_preserves_paused_stopped_controls_while_marking_uncertain_jobs(self):
        store = self.enable()
        expected = {}
        for state, awaiting in (("paused", False), ("stopped", True), ("idle", True)):
            agent, _ = store.register("claude", role="CEO", mode="external_attached", native_session_id="restart-" + state)
            store.send("user", agent["id"], "unfinished native work", "task")
            job = store.claim(agent["id"])
            if awaiting:
                store.await_native(job["id"], "Accepted; not completed")
            if state != "idle":
                store.update_agent(agent["id"], state=state)
            expected[agent["id"]] = state if state in {"paused", "stopped"} else "needs_human"
        store.configure_auto(suspended=True)
        store.set_paused(True)
        self.assertEqual(store.recover(), 3)
        self.assertEqual({agent["id"]: agent["state"] for agent in store.agents()}, expected)
        self.assertTrue(all(agent["role"] == "CEO" and agent["active_root"] is None for agent in store.agents()))
        self.assertTrue(all(job["state"] == "needs_human" for job in store.jobs()))
        self.assertTrue(store.auto_state()["enabled"])
        self.assertTrue(store.auto_state()["suspended"])
        self.assertTrue(store.auto_state()["paused"])


class AutoHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sharebus-auto-http-")
        self.root = Path(self.temporary.name).resolve()
        self.environment = isolated_environment()
        self.environment.start()
        self.server = BusHTTPServer(self.root, providers="none", enable_native=False, discovery=[])
        self.store = self.server.store
        self.store.configure_auto(enabled=True)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.runtime_file = self.root / ".sharebus/private/runtime.json"
        self.runtime_file.write_text(json.dumps(self.server.runtime()))
        self.path = auto_enroll.bootstrap_hook("codex", {"session_id": "native-real", "cwd": str(self.root)})
        self.record = json.loads(self.path.read_text())

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(2)
        self.server.close()
        self.environment.stop()
        self.temporary.cleanup()

    def request(self, path, payload=None, token=None, headers=None):
        prepared = {"X-Sharebus-Request": "1"}
        if token:
            prepared["Authorization"] = "Bearer " + token
        data = None
        if payload is not None:
            prepared["Content-Type"] = "application/json"
            data = json.dumps(payload).encode()
        prepared.update(headers or {})
        with urlopen(Request(self.server.url + path, data=data, headers=prepared), timeout=10) as response:
            return json.load(response)

    def connect(self):
        return auto_enroll.connect_context(self.path, "codex", self.root, request_timeout=10)

    def test_private_proof_response_loss_retry_same_claim_recovers_same_credential(self):
        first = self.request("/api/auto/redeem", self.record)
        again = self.request("/api/auto/redeem", self.record)
        self.assertEqual(first, again)
        self.assertEqual(len(self.store.agents()), 1)
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM auto_proofs").fetchone()
            self.assertIsNotNone(row["consumed_at"])
            self.assertNotEqual(row["code_hash"], self.record["code"])
            self.assertNotEqual(row["claim_hash"], self.record["claim_id"])

    def test_replay_rejects_other_claim_source_workspace_native_and_purpose(self):
        for key, value in (("claim_id", "another-claim"), ("hook_source", "codex:Stop"),
                           ("workspace_id", "another-workspace"), ("native_session_id", "foreign"),
                           ("purpose", "human_recovery")):
            with self.assertRaises(HTTPError) as failed:
                self.request("/api/auto/redeem", {**self.record, key: value})
            self.assertEqual(failed.exception.code, 403)
        with self.assertRaises(BusError):
            self.store.redeem_recovery("codex", "native-real", self.record["code"])

    def test_expired_proof_rejected_without_token_rotation(self):
        with self.store.connection() as db:
            db.execute("UPDATE auto_proofs SET expires_at=?", (time.time() - 1,))
        with self.assertRaises(HTTPError) as failed:
            self.request("/api/auto/redeem", self.record)
        self.assertEqual(failed.exception.code, 403)

    def test_private_proof_cannot_bypass_host_origin_or_csrf(self):
        for headers in ({"Origin": "https://foreign.example"}, {"X-Sharebus-Request": ""},
                        {"Host": "foreign.example"}):
            with self.assertRaises(HTTPError) as failed:
                self.request("/api/auto/redeem", self.record, headers=headers)
            self.assertEqual(failed.exception.code, 403)

    def test_auto_config_requires_operator_and_strict_boolean_values(self):
        token = self.request("/api/auto/redeem", self.record)["token"]
        with self.assertRaises(HTTPError) as failed:
            self.request("/api/auto/config", {"enabled": False}, token)
        self.assertEqual(failed.exception.code, 403)
        for value in ("true", 1, [], {}):
            with self.assertRaises(HTTPError):
                self.request("/api/auto/config", {"enabled": value}, self.store.operator_token)
        result = self.request("/api/auto/config", {"suspended": True}, self.store.operator_token)
        self.assertTrue(result["auto"]["suspended"])
        self.assertFalse(result["auto"]["active"])

    def test_connect_and_duplicate_turns_keep_private_credential_and_expired_binding(self):
        with patch.object(auto_enroll.Client, "start") as start:
            binding = self.connect()
            first = Path(binding["token_file"]).read_bytes()
            before = Path(binding["token_file"]).stat().st_mtime_ns
            with patch.object(auto_enroll.time, "time", return_value=time.time() + 600):
                self.assertEqual(self.connect(), binding)
            start.assert_not_called()
        self.assertEqual(Path(binding["token_file"]).read_bytes(), first)
        self.assertEqual(Path(binding["token_file"]).stat().st_mtime_ns, before)
        self.assertNotIn(first.decode(), json.dumps(binding))
        receipt = self.root / ".sharebus/private/auto/bindings" / (self.record["context_id"] + ".json")
        self.assertNotIn(first, receipt.read_bytes())
        self.assertNotIn(self.record["code"], json.dumps(self.store.auto_state()))

    def test_connector_recovers_consumed_proof_after_response_loss(self):
        original = auto_enroll._request
        lost = []
        def response_lost(root, path, *args, **kwargs):
            result = original(root, path, *args, **kwargs)
            if path == "/api/auto/redeem" and not lost:
                lost.append(True)
                raise BusError("Simulated response loss", 409)
            return result
        with patch.object(auto_enroll, "_request", side_effect=response_lost):
            with self.assertRaises(BusError):
                self.connect()
            binding = self.connect()
        self.assertEqual(binding["agent_id"], self.record["agent_id"])
        self.assertEqual(self.record, json.loads(self.path.read_text()))
        self.assertEqual(len(self.store.agents()), 1)

    def test_controls_block_unbound_connect_and_start_but_bound_state_is_readable(self):
        binding = self.connect()
        self.store.configure_auto(suspended=True)
        self.assertEqual(self.connect(), binding)
        with patch.object(auto_enroll.Client, "start") as start:
            with self.assertRaises(BusError):
                auto_enroll.connect_context(self.path, "codex", self.root, start_daemon=True)
            start.assert_not_called()
        receipt = self.root / ".sharebus/private/auto/bindings" / (self.record["context_id"] + ".json")
        receipt.unlink()
        with self.assertRaises(BusError):
            self.connect()

    def test_wrong_runtime_identity_never_receives_proof(self):
        runtime = self.server.runtime()
        runtime["nonce"] = "incorrect-process-nonce"
        self.runtime_file.write_text(json.dumps(runtime))
        with patch.object(self.store, "redeem_auto", wraps=self.store.redeem_auto) as redeem:
            with self.assertRaises(BusError):
                self.connect()
            redeem.assert_not_called()

    def test_malformed_or_expired_proof_cannot_start_daemon(self):
        for expiry in (float("nan"), float("inf"), True, "later"):
            self.path.write_text(json.dumps({**self.record, "expires_at": expiry}))
            with patch.object(auto_enroll.Client, "start") as start, self.assertRaises(BusError):
                auto_enroll.connect_context(self.path, "codex", self.root, start_daemon=True)
            start.assert_not_called()
        self.path.write_text(json.dumps({**self.record, "expires_at": time.time() + 1000}))
        with self.store.connection() as db:
            db.execute("UPDATE auto_proofs SET expires_at=?", (time.time() - 1,))
        with patch.object(auto_enroll.Client, "start") as start, self.assertRaises(BusError):
            auto_enroll.connect_context(self.path, "codex", self.root, start_daemon=True)
        start.assert_not_called()

    def test_linked_or_modified_credential_cannot_start_daemon(self):
        token_file = self.root / ".sharebus/private/credentials" / (self.record["agent_id"] + ".token")
        alias = self.root / "credential-alias"
        os.link(token_file, alias)
        with patch.object(auto_enroll.Client, "start") as start, self.assertRaises(BusError):
            auto_enroll.connect_context(self.path, "codex", self.root, start_daemon=True)
        start.assert_not_called()
        alias.unlink()
        token_file.write_text("modified-private-credential")
        with patch.object(auto_enroll.Client, "start") as start, self.assertRaises(BusError):
            auto_enroll.connect_context(self.path, "codex", self.root, start_daemon=True)
        start.assert_not_called()

    def test_pending_is_read_only_budget_aware_and_never_contains_body(self):
        binding = self.connect()
        token = Path(binding["token_file"]).read_text()
        event = self.store.send("user", binding["agent_id"], "private-job-body", "task")
        before = self.store.jobs()
        first = self.request("/api/agent/pending", token=token)
        again = self.request("/api/agent/pending", token=token)
        self.assertEqual(first, again)
        self.assertTrue(first["wake_allowed"])
        self.assertEqual(before, self.store.jobs())
        self.assertNotIn("private-job-body", json.dumps(first))
        self.assertEqual(first["queued"][0]["budget_remaining"], 24)
        self.store.claim(binding["agent_id"])
        self.assertFalse(self.request("/api/agent/pending", token=token)["wake_allowed"])
        with self.store.connection() as db:
            db.execute("UPDATE roots SET deadline=? WHERE id=?", (time.time() - 1, event["event"]["root_id"]))
        self.assertEqual(self.request("/api/agent/pending", token=token)["queued"], [])

    def test_observe_only_sanitized_metadata_no_control_or_job_mutation(self):
        binding = self.connect()
        token = Path(binding["token_file"]).read_text()
        epoch = self.store.auto_state()["updated_at"]
        result = self.request("/api/auto/observe", {"state": "watching", "note": "ready"}, token)
        self.assertEqual(result["auto"]["updated_at"], epoch)
        self.assertEqual(self.store.agent(binding["agent_id"])["state"], "idle")
        with self.assertRaises(HTTPError):
            self.request("/api/auto/observe", {"state": "watching", "note": "private raw secret"}, token)
        self.assertNotIn("private raw secret", json.dumps(self.store.auto_state()))

    def cli(self, command, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = auto_enroll.main([command, "--context-file", str(self.path), "--request-timeout", "10", *extra])
        return code, json.loads(stdout.getvalue()) if stdout.getvalue() else None, stderr.getvalue()

    def test_cli_cached_schema_connect_read_publish_send_ack_remain_agent_owned(self):
        code, connected, _ = self.cli("connect")
        self.assertEqual(code, 0)
        self.assertEqual(set(connected), {"connected", "agent_id", "provider", "native_session_id", "workspace"})
        actor = connected["agent_id"]
        code, _, _ = self.cli("send", "--to", "all", "--body", "no-human-root")
        self.assertEqual(code, 1)
        self.store.send("user", actor, "bounded-task", "task")
        code, inbox, _ = self.cli("read", "--after", "0")
        self.assertEqual(code, 0)
        self.assertEqual(len(inbox["jobs"]), 1)
        self.assertEqual(self.cli("publish", "--text", "snapshot", "--filename", "proof.txt")[0], 0)
        self.assertEqual(self.cli("send", "--to", "user", "--body", "completed", "--kind", "result")[0], 0)
        self.assertEqual(self.cli("ack", "--job-id", inbox["jobs"][0]["id"])[0], 0)
        self.assertEqual(self.store.active_jobs(), [])

    def test_cli_failure_output_never_contains_proof_or_token(self):
        binding = self.connect()
        token = Path(binding["token_file"]).read_text()
        with patch.object(auto_enroll, "connect_context", side_effect=BusError(token + self.record["code"] + self.record["claim_id"])):
            code, result, error = self.cli("status")
        self.assertEqual(code, 1)
        self.assertIsNone(result)
        for secret in (token, self.record["code"], self.record["claim_id"]):
            self.assertNotIn(secret, error)

    def test_cli_ack_does_not_claim_next_queued_job_or_ack_another_owner(self):
        binding = self.connect()
        actor = binding["agent_id"]
        self.store.send("user", actor, "first", "task")
        self.store.send("user", actor, "second", "task")
        _, inbox, _ = self.cli("read")
        first = inbox["jobs"][0]["id"]
        code, acknowledged, _ = self.cli("ack", "--job-id", first)
        self.assertEqual(code, 0)
        self.assertEqual(acknowledged["jobs"], [])
        self.assertEqual(sorted(job["state"] for job in self.store.jobs()), ["completed", "queued"])
        other, _ = self.store.register("claude", native_session_id="another-native")
        foreign = self.store.send("user", other["id"], "foreign", "task")["jobs"][0]["id"]
        self.assertEqual(self.cli("ack", "--job-id", foreign)[0], 1)
        self.assertEqual(next(job for job in self.store.jobs() if job["id"] == foreign)["state"], "queued")

    def test_policy_api_requires_operator_preserves_notes_and_rejects_stale_revision(self):
        from policy import default_team
        binding = self.connect()
        token = Path(binding["token_file"]).read_text()
        team = default_team()
        team["notes"] = "private-policy-note"
        policy_file = self.root / ".sharebus/team.json"
        policy_file.write_text(json.dumps(team))
        current = self.request("/api/policy", token=token)
        self.assertNotIn("private-policy-note", json.dumps(current))
        body = {"team": current["team"], "expected_revision": current["revision"]}
        with self.assertRaises(HTTPError) as failed:
            self.request("/api/policy", body, token)
        self.assertEqual(failed.exception.code, 403)
        body["team"]["roles"][next(iter(body["team"]["roles"]))]["label"] = "Edited label"
        changed = self.request("/api/policy", body, self.store.operator_token)
        self.assertNotEqual(changed["revision"], current["revision"])
        self.assertTrue(changed["preference_only"])
        self.assertFalse(changed["runtime_enforced"])
        self.assertEqual(json.loads(policy_file.read_text())["notes"], "private-policy-note")
        with self.assertRaises(HTTPError) as failed:
            self.request("/api/policy", body, self.store.operator_token)
        self.assertEqual(failed.exception.code, 409)
        self.assertEqual(self.store.agent(binding["agent_id"])["native_session_id"], "native-real")

    def test_config_api_exposes_queue_choice_and_only_private_redeem_attaches(self):
        actor = self.record["agent_id"]
        self.assertFalse(self.store.auto_state()["codex_delivery_configured"])
        configured = self.request("/api/auto/config", {"codex_delivery": "native_queue"}, self.store.operator_token)
        self.assertEqual(configured["auto"]["codex_delivery"], "native_queue")
        self.assertEqual(self.store.agent(actor)["mode"], "external_attached")
        binding = self.connect()
        self.assertEqual(self.store.agent(actor)["mode"], "native_attached")
        token = Path(binding["token_file"]).read_text()
        pending = self.request("/api/agent/pending", token=token)
        self.assertEqual(pending["auto"]["codex_delivery"], "native_queue")
        self.assertFalse(pending["wake_allowed"])
        with self.assertRaises(HTTPError):
            self.request("/api/auto/config", {"codex_delivery": "stop_hook"}, token)

    def test_connect_deferred_queue_attachment_does_not_claim_or_create_roots(self):
        binding = self.connect()
        actor = binding["agent_id"]
        self.store.send("user", actor, "active-external-work", "task")
        job = self.store.claim(actor)
        self.store.configure_auto(codex_delivery="native_queue")
        self.assertEqual(self.store.agent(actor)["mode"], "external_attached")
        self.store.finish(job["id"], "completed")
        self.assertEqual(self.store.agent(actor)["mode"], "external_attached")
        self.assertEqual(self.connect()["agent_id"], actor)
        self.assertEqual(self.store.agent(actor)["mode"], "native_attached")
        self.assertEqual(self.store.active_jobs(), [])
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*),SUM(used) FROM roots").fetchone()[:], (1, 1))

    def test_codex_start_control_requires_attestation_active_queue_and_existing_idle_actor(self):
        self.store.configure_auto(codex_delivery="native_queue")
        body = {"action": "start_agent", "provider": "codex", "native_session_id": "native-real", "mode": "native_attached"}
        with self.assertRaises(HTTPError):
            self.request("/api/control", body, self.store.operator_token)
        self.store.update_agent(self.record["agent_id"], role="CEO")
        self.connect()
        attached = self.request("/api/control", {**body, "role": "Ignored replacement"}, self.store.operator_token)["agent"]
        self.assertEqual((attached["id"], attached["native_session_id"], attached["role"]),
                         (self.record["agent_id"], "native-real", "CEO"))
        self.store.update_agent(attached["id"], state="paused")
        with self.assertRaises(HTTPError):
            self.request("/api/control", body, self.store.operator_token)
        self.assertEqual(self.store.agent(attached["id"])["state"], "paused")
        self.store.update_agent(attached["id"], state="idle")
        self.store.configure_auto(suspended=True)
        with self.assertRaises(HTTPError):
            self.request("/api/control", body, self.store.operator_token)
        self.assertEqual(len(self.store.agents()), 1)

    def test_cached_schema_cli_exact_callback_completes_and_only_then_verifies_channel(self):
        self.store.configure_auto(codex_delivery="native_queue")
        binding = self.connect()
        actor = binding["agent_id"]
        event = self.store.send("user", actor, "bounded native work", "task", budget=1)
        job = self.store.claim(actor)
        self.store.await_native(job["id"], "Queue accepted; completion remains unverified")
        self.assertFalse(self.store.agent(actor)["channel_verified"])
        self.assertEqual(self.server.state(actor)["own_agent"]["attachment_status"], "native_bridge_pending_verification")
        active = self.store.active_jobs()[0]
        with self.store.connection() as db:
            root = db.execute("SELECT deadline,used,budget FROM roots WHERE id=?", (job["root_id"],)).fetchone()
            self.assertEqual(active["root_deadline"], root["deadline"])
            self.assertEqual(root["used"], root["budget"])
        callback = ["--to", "user", "--kind", "result", "--body", "native exact result",
                    "--job-id", job["id"], "--event-id", job["event_id"], "--native-session-id", binding["native_session_id"]]
        code, result, _ = self.cli("send", *callback)
        self.assertEqual(code, 0)
        self.assertTrue(result["completed"])
        self.assertEqual(result["root_id"], event["root_id"])
        self.assertTrue(self.store.agent(actor)["channel_verified"])
        self.assertEqual(self.store.active_jobs(), [])
        self.assertEqual(self.store.jobs()[0]["state"], "completed")
        self.assertEqual(self.cli("send", *callback)[0], 1)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM roots").fetchone()[0], 1)

    def test_cli_native_callback_rejects_partial_wrong_kind_recipient_and_identity_before_network(self):
        common = ["--body", "result body", "--to", "user", "--kind", "result"]
        complete = ["--job-id", "job-id", "--event-id", "event-id", "--native-session-id", "native-real"]
        cases = [common + complete[:2], common + complete[:4],
                 ["--body", "result body", "--to", "all", "--kind", "result", *complete],
                 ["--body", "result body", "--to", "user", "--kind", "message", *complete],
                 common + [*complete[:-1], "foreign-native"]]
        with patch.object(auto_enroll, "connect_context") as connect, patch.object(auto_enroll, "_request") as request:
            for case in cases:
                self.assertEqual(self.cli("send", *case)[0], 1)
            connect.assert_not_called()
            request.assert_not_called()

    def test_uncorrelated_or_wrong_native_callback_does_not_verify_delivery(self):
        binding = self.connect()
        actor = binding["agent_id"]
        self.store.send("user", actor, "queued native work", "task")
        job = self.store.claim(actor)
        self.store.await_native(job["id"], "Queue accepted")
        self.store.native_result(actor, binding["native_session_id"], "passive result")
        self.assertFalse(self.store.agent(actor)["channel_verified"])
        token = Path(binding["token_file"]).read_text()
        with self.assertRaises(HTTPError):
            self.request("/api/native/result", {"job_id": job["id"], "event_id": "wrong-event",
                         "native_session_id": binding["native_session_id"], "last_assistant_message": "wrong callback"}, token)
        self.assertFalse(self.store.agent(actor)["channel_verified"])
        self.assertEqual(self.store.jobs()[0]["state"], "awaiting_native_reply")


if __name__ == "__main__":
    unittest.main()
