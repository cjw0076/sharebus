"""Automatic activation and invitation planning without native/model operations."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import auto_mode
from sharebus import Client as NativeClient, main
from store import BusStore


class FakeClient:
    instances: list["FakeClient"] = []
    live = False
    modern = True
    delivery = True

    def __init__(self, workspace):
        self.workspace = Path(workspace)
        self.private = self.workspace / ".sharebus/private"
        self.started: list[str] = []
        self.controls: list[str] = []
        self.opened = 0
        self.__class__.instances.append(self)

    def runtime(self, *, verify=True):
        if not self.__class__.live:
            raise RuntimeError("not running")
        return {"url": "http://127.0.0.1:1234", "pid": 1234, "nonce": "fixture-native-process-nonce",
                "workspace_id": BusStore(self.workspace).workspace_id,
                "workspace": str(self.workspace)}

    def request(self, path, body=None):
        store = BusStore(self.workspace)
        if path == "/api/control":
            action = body["action"]
            self.controls.append(action)
            if action == "resume":
                store.set_paused(False)
            elif action == "stop":
                self.__class__.live = False
                store.set_paused(True)
            else:
                raise AssertionError("Unexpected control")
        elif path != "/api/state":
            raise AssertionError("Unexpected native/API request")
        result = {"agents": store.agents(), "jobs": store.jobs(), "active_jobs": store.active_jobs(),
                  "workspace": {"path": str(self.workspace), "id": store.workspace_id},
                  "paused": store.paused(), "discovery": [], "discovery_state": "ready"}
        if self.__class__.modern:
            result["auto"] = store.auto_state()
            if not self.__class__.delivery:
                result["auto"].pop("codex_delivery", None)
                result["auto"].pop("codex_delivery_configured", None)
        return result

    def start(self, agents="none"):
        self.started.append(agents)
        if agents != "none":
            raise AssertionError("Automatic mode must not launch managed workers")
        self.__class__.live = True
        self.__class__.modern = True
        self.__class__.delivery = True
        self.private.mkdir(parents=True, exist_ok=True)
        (self.private / "runtime.json").write_text("{}", encoding="utf-8")
        return self.runtime()

    def open(self):
        self.opened += 1


class AutoModeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="sharebus-auto-mode-")
        self.root = Path(self.directory.name).resolve()
        FakeClient.instances = []
        FakeClient.live = False
        FakeClient.modern = True
        FakeClient.delivery = True
        self.client_patch = patch.object(auto_mode, "Client", FakeClient)
        self.install_patch = patch.object(auto_mode, "_install", return_value={"applied": True, "files": []})
        self.discover_patch = patch.object(auto_mode, "_discover", return_value=[])
        self.origin_patch = patch.object(auto_mode, "_connect_origin", return_value=None)
        self.queue_patch = patch.object(auto_mode, "_probe_codex_queue", return_value={"supported": False})
        self.client_patch.start()
        self.installer = self.install_patch.start()
        self.discovery = self.discover_patch.start()
        self.origin = self.origin_patch.start()
        self.queue_probe = self.queue_patch.start()

    def tearDown(self):
        self.queue_patch.stop()
        self.origin_patch.stop()
        self.discover_patch.stop()
        self.install_patch.stop()
        self.client_patch.stop()
        self.directory.cleanup()

    def candidate(self, provider="codex", native="native-ceo", **extra):
        return {"provider": provider, "native_session_id": native, "workspace": str(self.root), **extra}

    def test_disabled_status_and_off_have_no_filesystem_effect(self):
        self.assertFalse(auto_mode.configure(self.root, "status")["auto"]["enabled"])
        self.assertFalse(auto_mode.configure(self.root, "off")["auto"]["enabled"])
        self.assertFalse((self.root / ".sharebus").exists())
        self.installer.assert_not_called()
        self.discovery.assert_not_called()
        self.queue_probe.assert_not_called()

    def test_one_activation_installs_starts_none_opens_once_and_is_idempotent(self):
        first = auto_mode.configure(self.root, candidates=[self.candidate()])
        again = auto_mode.configure(self.root, candidates=[self.candidate()])
        self.assertTrue(first["auto"]["active"])
        self.assertTrue(first["console_opened"])
        self.assertFalse(again["console_opened"])
        self.assertEqual(first["pending_invites"], again["pending_invites"])
        self.assertEqual(first["auto"]["updated_at"], again["auto"]["updated_at"])
        self.assertEqual([agent for client in FakeClient.instances for agent in client.started], ["none"])
        self.assertEqual(BusStore(self.root).agents(), [])
        self.assertEqual(first["native_messages_sent"], 0)
        self.assertEqual(first["native_apps_started"], 0)

    def test_explicit_watch_timeout_is_validated_before_effects_and_forwarded(self):
        for value in (-1, 0, float("inf"), float("nan"), 604801, True):
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, "7 days"):
                auto_mode.configure(self.root, watch_timeout=value)
        self.assertFalse((self.root / ".sharebus").exists())
        self.installer.assert_not_called()
        auto_mode.configure(self.root, watch_timeout=120, candidates=[])
        self.assertEqual(self.installer.call_args.kwargs["watch_timeout"], 120)

    def test_timeout_setting_does_not_mutate_other_control_actions(self):
        for action in ("off", "resume", "status", "invite"):
            with self.subTest(action=action), self.assertRaises(RuntimeError):
                auto_mode.configure(self.root, action, watch_timeout=60)
        self.assertFalse((self.root / ".sharebus").exists())

    def test_supported_queue_is_selected_before_origin_without_managed_workers(self):
        self.queue_probe.return_value = {"supported": True, "version": "0.163.0", "executable": "bundled-codex.exe"}
        seen = []
        self.origin.side_effect = lambda root: seen.append(BusStore(root).auto_state()["codex_delivery"])
        result = auto_mode.configure(self.root, candidates=[])
        self.assertEqual(result["auto"]["codex_delivery"], "native_queue")
        self.assertTrue(result["auto"]["codex_delivery_configured"])
        self.assertIs(self.installer.call_args.kwargs["codex_native_queue"], True)
        self.assertEqual(seen, ["native_queue"])
        self.assertEqual([value for client in FakeClient.instances for value in client.started], ["none"])

    def test_unknown_queue_support_uses_unconfigured_stop_fallback(self):
        result = auto_mode.configure(self.root, candidates=[])
        self.assertEqual(result["auto"]["codex_delivery"], "stop_hook")
        self.assertFalse(result["auto"]["codex_delivery_configured"])
        self.assertIsNone(self.installer.call_args.kwargs["codex_native_queue"])

    def test_existing_delivery_choices_and_manual_controls_are_preserved_without_probe(self):
        store = BusStore(self.root)
        store.configure_auto(enabled=True, suspended=True, codex_delivery="stop_hook")
        store.set_paused(True)
        for mode in ("stop_hook", "native_queue"):
            store.configure_auto(codex_delivery=mode)
            with self.subTest(mode=mode):
                result = auto_mode.configure(self.root, candidates=[])
                self.assertEqual(result["auto"]["codex_delivery"], mode)
                self.assertTrue(result["auto"]["suspended"])
                self.assertTrue(result["auto"]["paused"])
                self.assertIs(self.installer.call_args.kwargs["codex_native_queue"], mode == "native_queue")
        self.queue_probe.assert_not_called()
        self.origin.assert_not_called()

    def test_existing_installer_queue_choice_is_preserved_when_probe_is_unknown(self):
        self.installer.return_value = {"applied": True, "codex_native_queue": True}
        result = auto_mode.configure(self.root, candidates=[])
        self.assertEqual(result["auto"]["codex_delivery"], "native_queue")
        self.assertTrue(result["auto"]["codex_delivery_configured"])

    def test_read_only_status_distinguishes_unset_and_selected_delivery(self):
        store = BusStore(self.root)
        self.assertFalse(auto_mode.status(self.root)["auto"]["codex_delivery_configured"])
        store.configure_auto(codex_delivery="native_queue")
        result = auto_mode.status(self.root)
        self.assertTrue(result["auto"]["codex_delivery_configured"])
        self.assertEqual(result["auto"]["codex_delivery"], "native_queue")

    def test_optional_probe_failure_is_static_and_never_becomes_an_enqueue(self):
        self.queue_patch.stop()
        with patch("codex_bridge.probe_native_queue", side_effect=RuntimeError("PRIVATE_PROBE_OUTPUT")) as probe:
            result = auto_mode._probe_codex_queue(self.root)
        probe.assert_called_once_with(self.root)
        self.assertEqual(result, {"supported": False, "reason": "unavailable"})

    def test_installer_option_and_effective_receipt_roundtrip_in_an_isolated_home(self):
        self.install_patch.stop()
        synthetic_home = self.root / "synthetic-home"
        synthetic_home.mkdir()
        first = auto_mode._install(self.root, synthetic_home, Path(sys.executable),
                                   watch_timeout=1, codex_native_queue=True)
        again = auto_mode._install(self.root, synthetic_home, Path(sys.executable), codex_native_queue=None)
        self.assertIs(first["codex_native_queue"], True)
        self.assertIs(again["codex_native_queue"], True)

    def test_repeated_on_preserves_suspension_and_global_pause(self):
        store = BusStore(self.root)
        store.configure_auto(enabled=True, suspended=True)
        store.set_paused(True)
        before = store.auto_state()["updated_at"]
        result = auto_mode.configure(self.root, candidates=[self.candidate()])
        self.assertTrue(result["auto"]["enabled"])
        self.assertTrue(result["auto"]["suspended"])
        self.assertTrue(result["auto"]["paused"])
        self.assertFalse(result["running"])
        self.assertEqual(result["pending_invites"], [])
        self.assertEqual(store.auto_state()["updated_at"], before)
        self.assertFalse(any(client.started or client.controls for client in FakeClient.instances))
        self.origin.assert_not_called()

    def test_explicit_resume_preserves_individually_stopped_paused_and_review_actors(self):
        store = BusStore(self.root)
        store.configure_auto(enabled=True, suspended=True)
        store.set_paused(True)
        ids = []
        for state in ("paused", "stopped", "needs_human"):
            actor, _ = store.register("claude", state, native_session_id="native-" + state)
            store.update_agent(actor["id"], state=state)
            ids.append((actor["id"], state))
        result = auto_mode.configure(self.root, "resume", candidates=[])
        self.assertTrue(result["auto"]["active"])
        self.assertEqual([(actor, store.agent(actor)["state"]) for actor, _ in ids], ids)
        self.assertEqual([control for client in FakeClient.instances for control in client.controls], ["resume"])
        self.installer.assert_not_called()

    def test_off_stops_automatic_participation_without_resuming_or_shutting_down(self):
        auto_mode.configure(self.root, candidates=[])
        result = auto_mode.configure(self.root, "off")
        self.assertFalse(result["auto"]["enabled"])
        self.assertFalse(any(client.controls for client in FakeClient.instances))
        self.assertTrue(FakeClient.live)

    def test_discovery_exact_workspace_only_no_titles_prompts_or_credentials(self):
        other = self.root / "other"
        other.mkdir()
        rows = [self.candidate(title="private-title", prompt="private-prompt", token="private-token"),
                self.candidate("claude", "native-cmo"), self.candidate("codex", "native-foreign", workspace=str(other)),
                self.candidate("gemini", "unsupported"), self.candidate(native="bad\nidentity"),
                self.candidate(provider=["codex"])]
        self.discovery.return_value = rows
        result = auto_mode.configure(self.root)
        self.assertEqual({row["native_session_id"] for row in result["pending_invites"]}, {"native-ceo", "native-cmo"})
        public = json.dumps(result)
        for value in ("private-title", "private-prompt", "private-token"):
            self.assertNotIn(value, public)
        self.assertEqual(BusStore(self.root).agents(), [])

    def test_same_provider_roles_separate_and_initiating_native_not_invited(self):
        result = auto_mode.configure(self.root, candidates=[self.candidate(native="ceo"), self.candidate(native="cto")],
                                     origin_provider="codex", origin_native_id="ceo")
        self.assertEqual([row["native_session_id"] for row in result["pending_invites"]], ["cto"])
        self.assertEqual({row["state"] for row in result["invitations"]}, {"origin", "pending"})

    def test_actual_environment_origin_connects_and_excludes_only_itself(self):
        self.origin.return_value = {"connected": True, "evidence": "native_environment", "provider": "codex",
            "native_session_id": "actual-native-ceo", "agent_id": "existing-ceo", "workspace": str(self.root)}
        result = auto_mode.configure(self.root, candidates=[self.candidate(native="actual-native-ceo"),
            self.candidate(native="other-native-cto")], origin_provider="claude", origin_native_id="arbitrary-id")
        self.origin.assert_called_once_with(self.root)
        self.assertEqual(result["native_origin"]["native_session_id"], "actual-native-ceo")
        self.assertEqual([row["native_session_id"] for row in result["pending_invites"]], ["other-native-cto"])

    def test_uncertain_origin_connection_keeps_active_auto_and_exposes_no_private_error(self):
        self.origin.side_effect = RuntimeError("PRIVATE_CLAIM_CODE_TOKEN")
        result = auto_mode.configure(self.root, candidates=[])
        self.assertTrue(result["auto"]["active"])
        self.assertEqual(result["native_origin"], {"connected": False, "status": "pending"})
        self.assertNotIn("PRIVATE_CLAIM_CODE_TOKEN", json.dumps(result))

    def test_origin_bridge_passes_only_workspace_to_core_and_returns_public_receipt(self):
        self.origin_patch.stop()
        context = self.root / ".sharebus/private/auto/contexts/native.json"
        binding = {"workspace": str(self.root), "provider": "codex", "native_session_id": "actual-native-ceo",
                   "agent_id": "existing-ceo", "token_file": "PRIVATE_TOKEN_FILE", "context_file": "PRIVATE_CONTEXT"}
        with patch("auto_enroll.bootstrap_native_environment", return_value=context) as bootstrap, \
             patch("auto_enroll.connect_context", return_value=binding) as connect:
            receipt = auto_mode._connect_origin(self.root)
        bootstrap.assert_called_once_with(self.root)
        connect.assert_called_once_with(context, "codex", self.root, request_timeout=3, start_daemon=False)
        self.assertTrue(receipt["connected"])
        self.assertNotIn("PRIVATE", json.dumps(receipt))

    def test_claim_before_send_and_uncertainty_not_retried_across_activation(self):
        first = auto_mode.configure(self.root, candidates=[self.candidate()])
        key = first["pending_invites"][0]["key"]
        claimed = auto_mode.configure(self.root, "invite", invite_key=key, invite_state="dispatching")
        self.assertTrue(claimed["claimed"])
        self.assertFalse(auto_mode.configure(self.root, "invite", invite_key=key, invite_state="dispatching")["claimed"])
        auto_mode.configure(self.root, "invite", invite_key=key, invite_state="uncertain")
        retry = auto_mode.configure(self.root, candidates=[self.candidate()])
        self.assertEqual(retry["pending_invites"], [])
        self.assertEqual(retry["invitations"][0]["state"], "uncertain")

    def test_control_changes_preserve_invitation_epoch_and_uncertain_delivery(self):
        first = auto_mode.configure(self.root, candidates=[self.candidate()])
        key = first["pending_invites"][0]["key"]
        auto_mode.configure(self.root, "invite", invite_key=key, invite_state="dispatching")
        auto_mode.configure(self.root, "invite", invite_key=key, invite_state="uncertain")
        store = BusStore(self.root)
        store.touch_auto_control()
        store.configure_auto(suspended=True)
        resumed = auto_mode.configure(self.root, "resume", candidates=[self.candidate()])
        self.assertEqual(resumed["auto"]["epoch"], first["auto"]["epoch"])
        self.assertEqual(resumed["pending_invites"], [])
        self.assertEqual(resumed["invitations"][0]["key"], key)
        self.assertEqual(auto_mode.status(self.root)["invitations"][0]["state"], "uncertain")

    def test_disabled_mode_blocks_new_dispatch_and_records_existing_uncertainty(self):
        first = auto_mode.configure(self.root, candidates=[self.candidate()])
        key = first["pending_invites"][0]["key"]
        auto_mode.configure(self.root, "invite", invite_key=key, invite_state="dispatching")
        auto_mode.configure(self.root, "off")
        with self.assertRaises(RuntimeError):
            auto_mode.configure(self.root, "invite", invite_key=key, invite_state="dispatching")
        result = auto_mode.configure(self.root, "invite", invite_key=key, invite_state="uncertain")
        self.assertEqual(result["state"], "uncertain")

    def test_delivery_requires_dispatch_claim_and_registration_resolves_pending(self):
        first = auto_mode.configure(self.root, candidates=[self.candidate()])
        key = first["pending_invites"][0]["key"]
        self.assertEqual(auto_mode.configure(self.root, "invite", invite_key=key, invite_state="delivered")["state"], "pending")
        store = BusStore(self.root)
        store.register("codex", "existing CEO", native_session_id="native-ceo")
        again = auto_mode.configure(self.root, candidates=[self.candidate()])
        self.assertEqual(again["pending_invites"], [])
        self.assertEqual(again["invitations"][0]["state"], "registered")

    def test_status_returns_remaining_plan_without_creating_new_invites(self):
        first = auto_mode.configure(self.root, candidates=[self.candidate()])
        current = auto_mode.status(self.root)
        self.assertEqual(current["pending_invites"][0]["key"], first["pending_invites"][0]["key"])

    def test_quiet_old_broker_upgrade_single_stop_none_start_and_pause_restoration(self):
        store = BusStore(self.root)
        store.configure_auto(enabled=True)
        client = FakeClient(self.root)
        client.start("none")
        FakeClient.modern = False
        with patch("daemon._pid_exists", return_value=False):
            result = auto_mode.configure(self.root, candidates=[])
        self.assertTrue(result["auto"]["active"])
        controls = [control for instance in FakeClient.instances for control in instance.controls]
        self.assertEqual(controls, ["stop", "resume"])
        self.assertEqual([value for instance in FakeClient.instances for value in instance.started], ["none", "none"])

    def test_auto_without_delivery_capability_is_upgraded_once_before_origin(self):
        store = BusStore(self.root)
        store.configure_auto(enabled=True)
        FakeClient(self.root).start("none")
        FakeClient.delivery = False
        with patch("daemon._pid_exists", return_value=False):
            result = auto_mode.configure(self.root, candidates=[])
        self.assertIn(result["auto"]["codex_delivery"], {"stop_hook", "native_queue"})
        self.assertEqual([control for client in FakeClient.instances for control in client.controls], ["stop", "resume"])
        self.assertEqual([value for client in FakeClient.instances for value in client.started], ["none", "none"])

    def test_restart_without_delivery_capability_fails_without_resuming_or_retrying(self):
        store = BusStore(self.root)
        store.configure_auto(enabled=True)
        client = FakeClient(self.root)
        client.start("none")
        FakeClient.delivery = False
        runtime, state = client.runtime(), client.request("/api/state")
        original_start = client.start

        def old_start(agents):
            result = original_start(agents)
            FakeClient.delivery = False
            return result

        with patch.object(client, "start", side_effect=old_start), patch("daemon._pid_exists", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "current automatic-delivery support"):
                auto_mode._upgrade(client, store, runtime, state)
        self.assertEqual(client.controls, ["stop"])
        self.assertEqual(client.started, ["none", "none"])

    def test_upgrade_keeps_existing_pause_and_suspension(self):
        store = BusStore(self.root)
        store.configure_auto(enabled=True, suspended=True)
        store.set_paused(True)
        client = FakeClient(self.root)
        client.start("none")
        FakeClient.modern = False
        with patch("daemon._pid_exists", return_value=False):
            auto_mode._upgrade(client, store, client.runtime(), client.request("/api/state"))
        self.assertTrue(store.auto_state()["paused"])
        self.assertTrue(store.auto_state()["suspended"])
        self.assertEqual(client.controls, ["stop"])

    def test_uncertain_upgrade_stop_is_never_retried_or_followed_by_start(self):
        store = BusStore(self.root)
        store.configure_auto(enabled=True)
        client = FakeClient(self.root)
        client.start("none")
        FakeClient.modern = False
        runtime, state = client.runtime(), client.request("/api/state")
        with patch.object(client, "request", side_effect=RuntimeError("Uncertain stop")) as request:
            with self.assertRaisesRegex(RuntimeError, "Uncertain stop"):
                auto_mode._upgrade(client, store, runtime, state)
        self.assertEqual(request.call_args_list, [unittest.mock.call("/api/control", {"action": "stop"})])
        self.assertEqual(client.started, ["none"])

    def test_active_old_broker_blocks_upgrade_without_control_or_replacement(self):
        store = BusStore(self.root)
        store.configure_auto(enabled=True)
        actor, _ = store.register("codex", "existing", native_session_id="original")
        store.send("user", actor["id"], "current native work", "task")
        store.claim(actor["id"])
        FakeClient(self.root).start("none")
        FakeClient.modern = False
        with self.assertRaises(RuntimeError):
            auto_mode.configure(self.root, candidates=[])
        self.assertFalse(any(instance.controls for instance in FakeClient.instances))
        self.assertEqual([value for instance in FakeClient.instances for value in instance.started], ["none"])

    def test_upgrade_requires_fresh_exact_workspace_and_process_identity(self):
        store = BusStore(self.root)
        client = FakeClient(self.root)
        client.start("none")
        FakeClient.modern = False
        runtime, state = client.runtime(), client.request("/api/state")
        for field, value in (("pid", 99), ("nonce", "changed-native-process-nonce"),
                             ("workspace_id", "foreign-room")):
            changed = {**runtime, field: value}
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "identity"):
                auto_mode._upgrade(client, store, changed, state)
        state["workspace"] = {"path": str(self.root), "id": "foreign-room"}
        with self.assertRaisesRegex(RuntimeError, "identity"):
            auto_mode._upgrade(client, store, runtime, state)
        self.assertEqual(client.controls, [])

    def test_untracked_native_activity_blocks_legacy_upgrade(self):
        store = BusStore(self.root)
        store.configure_auto(enabled=True)
        actor, _ = store.register("codex", "existing CEO", native_session_id="original")
        store.update_agent(actor["id"], state="running")
        FakeClient(self.root).start("none")
        FakeClient.modern = False
        with self.assertRaisesRegex(RuntimeError, "active native work"):
            auto_mode.configure(self.root, candidates=[])
        self.assertFalse(any(client.controls for client in FakeClient.instances))

    @unittest.skipUnless(sys.platform == "win32", "Windows scheduler ownership path")
    def test_reused_stale_pid_reaches_only_normal_scheduler_start(self):
        store = BusStore(self.root)
        store.configure_auto(enabled=True)
        FakeClient(self.root).start("none")
        original_request = FakeClient.request
        failures = 0

        def request(client, path, body=None):
            nonlocal failures
            if path == "/api/state" and failures == 0:
                failures += 1
                raise RuntimeError("Stale process nonce")
            return original_request(client, path, body)

        with patch.object(FakeClient, "request", request), patch("daemon._pid_exists", return_value=True) as pid_probe:
            result = auto_mode.configure(self.root, candidates=[])
        pid_probe.assert_not_called()
        self.assertTrue(result["auto"]["active"])
        self.assertFalse(any(client.controls for client in FakeClient.instances))
        self.assertEqual([item for client in FakeClient.instances for item in client.started], ["none", "none"])

    def test_cli_default_and_off_status_help_do_not_reinterpret_managed_auto(self):
        captured = io.StringIO()
        with patch("auto_mode.configure", return_value={"auto": {"enabled": True}}) as configure, redirect_stdout(captured):
            self.assertEqual(main(["--workspace", str(self.root), "auto", "--no-open"]), 0)
        self.assertEqual(configure.call_args.args[1], "on")
        self.assertFalse(configure.call_args.kwargs["open_console"])
        self.assertTrue(json.loads(captured.getvalue())["auto"]["enabled"])

    def test_cli_exposes_explicit_bounded_native_wait_configuration(self):
        with patch("auto_mode.configure", return_value={"auto": {"enabled": True}}) as configure, redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--workspace", str(self.root), "auto", "--watch-timeout", "120"]), 0)
        self.assertEqual(configure.call_args.kwargs["watch_timeout"], 120)

    def test_legacy_invalid_runtime_is_rejected_before_network_or_pid_use(self):
        private = self.root / ".sharebus/private"
        private.mkdir(parents=True)
        runtime_file = private / "runtime.json"
        client = NativeClient(self.root)
        for runtime in ([], {"url": "http://127.0.0.1:1234", "workspace": str(self.root)},
                        {"url": "http://127.0.0.1:bad", "workspace": str(self.root)}):
            runtime_file.write_text(json.dumps(runtime))
            with self.subTest(runtime=runtime), patch.object(client.opener, "open") as opener:
                with self.assertRaises(RuntimeError):
                    client.runtime()
                opener.assert_not_called()

    def test_health_requires_affirmative_exact_workspace_nonce_and_process(self):
        store = BusStore(self.root)
        runtime = {"url": "http://127.0.0.1:1234", "workspace": str(self.root),
                   "workspace_id": store.workspace_id, "pid": 1234, "nonce": "fixture-native-process-nonce"}
        (store.private / "runtime.json").write_text(json.dumps(runtime))
        client = NativeClient(self.root)
        health = {"ok": True, **{key: runtime[key] for key in ("workspace_id", "pid", "nonce")}}
        for field, value in (("ok", False), ("nonce", "another-process"), ("pid", 5678),
                             ("workspace_id", "another-workspace")):
            response = io.BytesIO(json.dumps({**health, field: value}).encode())
            with self.subTest(field=field), patch.object(client.opener, "open", return_value=response):
                with self.assertRaisesRegex(RuntimeError, "identity"):
                    client.runtime()


@unittest.skipUnless(sys.platform == "win32", "Windows owned Task Scheduler launcher")
class WindowsStartupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="sharebus-startup-receipt-")
        self.root = Path(self.directory.name).resolve()
        self.store = BusStore(self.root)
        self.client = NativeClient(self.root)
        self.runtime = {"workspace": str(self.root), "workspace_id": self.store.workspace_id,
                        "url": "http://127.0.0.1:1234", "pid": 1234, "nonce": "fixture-process-nonce"}
        self.overrides = {}
        self.scripts = []
        self.real_run = subprocess.run
        self.run_patch = patch("sharebus.subprocess.run", side_effect=self.launch)
        self.run = self.run_patch.start()
        self.runtime_patch = patch.object(self.client, "runtime", side_effect=[RuntimeError("offline"), self.runtime])
        self.runtime_read = self.runtime_patch.start()
        self.enable_patch = patch.object(self.client, "_enable")
        self.enable_patch.start()

    def tearDown(self):
        self.enable_patch.stop()
        self.runtime_patch.stop()
        self.run_patch.stop()
        self.directory.cleanup()

    def launch(self, command, **kwargs):
        self.assertEqual(kwargs["timeout"], 60)
        self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
        script = command[-1]
        self.scripts.append(script)
        launch_id = script.split("$sharebusLaunchId='", 1)[1].split("'", 1)[0]
        receipt = {"task_name": "Sharebus-" + self.store.workspace_id, "workspace_id": self.store.workspace_id,
                   "launch_id": launch_id, "state": 3, "last_result": 1, "baseline_run_ticks": 90,
                   "requested_run_ticks": 100, "last_run_ticks": 101, **self.overrides}
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(receipt), stderr="")

    def test_fresh_owned_failed_run_exits_without_readiness_wait_or_relaunch(self):
        with patch("sharebus.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "failed during this start"):
                self.client.start("none")
        self.assertEqual(self.run.call_count, 1)
        self.assertEqual(self.runtime_read.call_count, 1)
        sleep.assert_not_called()

    def test_old_error_from_unchanged_baseline_is_not_the_new_run_failure(self):
        self.overrides.update(baseline_run_ticks=101, last_run_ticks=101)
        self.assertEqual(self.client.start("none"), self.runtime)
        self.assertEqual(self.run.call_count, 1)
        self.assertEqual(self.runtime_read.call_count, 2)

    def test_error_from_before_this_start_is_not_the_new_run_failure(self):
        self.overrides.update(last_run_ticks=99)
        self.assertEqual(self.client.start("none"), self.runtime)
        self.assertEqual(self.run.call_count, 1)

    def test_running_or_success_status_is_not_a_confirmed_exit_failure(self):
        for state, result in ((4, 1), (3, 0), (3, 0x41301), (3, 0x41303), (2, 1)):
            self.runtime_read.side_effect = [RuntimeError("offline"), self.runtime]
            self.overrides.update(state=state, last_result=result)
            with self.subTest(state=state, result=result):
                self.assertEqual(self.client.start("none"), self.runtime)

    def test_foreign_or_invalid_receipt_cannot_confirm_a_start_failure(self):
        for override in ({"workspace_id": "foreign-workspace"}, {"launch_id": "older-start"},
                         {"task_name": "foreign-task"}, {"last_run_ticks": "101"}):
            self.runtime_read.side_effect = [RuntimeError("offline"), self.runtime]
            self.overrides = override
            with self.subTest(override=override):
                self.assertEqual(self.client.start("none"), self.runtime)

    def test_launcher_timeout_is_not_retried_or_followed_by_an_extra_probe(self):
        self.run.side_effect = subprocess.TimeoutExpired("fixture-launcher", 60)
        self.assertEqual(self.client.start("none"), self.runtime)
        self.assertEqual(self.run.call_count, 1)

    def test_launcher_powershell_parses_without_running_task_scheduler(self):
        self.overrides.update(state=4)
        self.client.start("none")
        source = self.root / "launcher-source.ps1"
        source.write_text(self.scripts[0], encoding="utf-8")
        parser = self.root / "parse-only.ps1"
        parser.write_text("\n".join([
            "param([string]$Source)",
            "$tokens=$null; $errors=$null",
            "$null=[System.Management.Automation.Language.Parser]::ParseFile($Source,[ref]$tokens,[ref]$errors)",
            "if($errors.Count){$errors | ForEach-Object {Write-Output $_.Message}; exit 1}",
        ]), encoding="utf-8")
        powershell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = self.real_run([str(powershell), "-NoProfile", "-NonInteractive", "-File", str(parser), str(source)],
                               capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_probe_owner_uses_resolved_sid_and_preserves_action_identity(self):
        self.overrides.update(state=4)
        self.client.start("none")
        validation = "  $sharebusProbeDefinition=" + self.scripts[0].split("  $sharebusProbeDefinition=", 1)[1].split("  $sharebusReceipt=", 1)[0]
        source = self.root / "validate-owner-only.ps1"
        source.write_text("\n".join([
            "$sharebusIdentity=[Security.Principal.WindowsIdentity]::GetCurrent()",
            "$sharebusDescription='fixture-owned-task'",
            "$sharebusExecute='C:\\fixture\\pythonw.exe'",
            "$sharebusDirectory='C:\\fixture\\room'",
            "$sharebusArguments='fixture-daemon --agents none'",
            "$results=@()",
            "foreach($case in @('short-name','full-name','sid','foreign-sid','foreign-action')){",
            "  $principal=($sharebusIdentity.Name -split '\\\\')[-1]",
            "  if($case -eq 'full-name'){$principal=$sharebusIdentity.Name}",
            "  if($case -eq 'sid'){$principal=$sharebusIdentity.User.Value}",
            "  if($case -eq 'foreign-sid'){$principal='S-1-5-18'}",
            "  $action=[pscustomobject]@{Path=$sharebusExecute;WorkingDirectory=$sharebusDirectory;Arguments=$sharebusArguments}",
            "  if($case -eq 'foreign-action'){$action.Path='C:\\foreign\\pythonw.exe'}",
            "  $actions=[pscustomobject]@{Count=1;Value=$action}",
            "  $actions | Add-Member -MemberType ScriptMethod -Name Item -Value {param($index) return $this.Value}",
            "  $definition=[pscustomobject]@{RegistrationInfo=[pscustomobject]@{Description=$sharebusDescription};Actions=$actions;Principal=[pscustomobject]@{UserId=$principal;RunLevel=0;LogonType=3};Triggers=[pscustomobject]@{Count=0}}",
            "  $sharebusProbe=[pscustomobject]@{Definition=$definition}",
            "  $accepted=$true",
            "  try{",
            validation,
            "  }catch{$accepted=$false}",
            "  $results+=[pscustomobject]@{case=$case;accepted=$accepted}",
            "}",
            "$results | ConvertTo-Json -Compress",
        ]), encoding="utf-8")
        powershell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = self.real_run([str(powershell), "-NoProfile", "-NonInteractive", "-File", str(source)],
                               capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        outcomes = {row["case"]: row["accepted"] for row in json.loads(result.stdout)}
        self.assertEqual(outcomes, {"short-name": True, "full-name": True, "sid": True,
                                    "foreign-sid": False, "foreign-action": False})


if __name__ == "__main__":
    unittest.main()
