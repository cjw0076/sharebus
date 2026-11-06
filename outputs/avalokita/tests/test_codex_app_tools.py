from __future__ import annotations

import hashlib
import ctypes
import json
import os
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adapters.codex_app_tools import (
    CodexAppToolsAdapter, CodexPipeError, INSTALLED_EXECUTABLE, MAX_FRAME,
    APP_MANIFEST_SHA256, EXECUTABLE_SHA256, MAIN_MODULE_PATH, MAIN_MODULE_SHA256,
    MCP_CONFIG_SHA256, PIPE_PREFIX, PROVENANCE, SUPPORTED_PROVENANCES,
    PipeTimeout, SERVER_SHA256, ServerIdentity, encode_frame,
)
from adapters.codex_app_tools import _PENDING_IO, _Win32Pipe, Win32PipeSystem, _installed_main_hash
from adapters import codex_app_tools as adapter_module

SOURCE = "native-source-thread"
NAME = PIPE_PREFIX + "-00000000-0000-0000-0000-000000000001"
NAME2 = PIPE_PREFIX + "-00000000-0000-0000-0000-000000000002"
SID = "S-1-5-21-test"
METADATA_TOOL = {"namespace": "codex_app", "name": "list_artifacts", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}
SEND_TOOL = {"namespace": "codex_app", "name": "send_message_to_thread", "inputSchema": {"type": "object", "properties": {"threadId": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["threadId", "prompt"]}}


def rpc_result(request_id, value):
    return encode_frame({"jsonrpc": "2.0", "id": request_id, "result": value})


def inventory(tools=None):
    return rpc_result(1, {"tools": tools if tools is not None else [METADATA_TOOL]})


def artifacts(value=None):
    return rpc_result(2, {"success": True, "contentItems": [{"type": "inputText", "text": json.dumps(value if value is not None else {"artifacts": []})}]})


class FakePipe:
    def __init__(self, response=b"", identity=None, fail_read=False):
        self.response = bytearray(response)
        self.identity = identity or ServerIdentity(123, str(INSTALLED_EXECUTABLE), SID)
        self.writes = []
        self.closed = False
        self.fail_read = fail_read

    def read_exact(self, count, deadline):
        if self.fail_read or len(self.response) < count:
            raise PipeTimeout("Synthetic timeout")
        value = bytes(self.response[:count])
        del self.response[:count]
        return value

    def write_all(self, data, deadline):
        if self.closed:
            raise CodexPipeError("Closed")
        self.writes.append(json.loads(data[4:]))

    def close(self):
        self.closed = True


class FakeSystem:
    def __init__(self, pipe=None, candidates=None, pipes=None):
        self.pipe = pipe or FakePipe()
        self.names = [NAME] if candidates is None else candidates
        self.pipes = pipes or {}
        self.opens = []
        self.discovery_calls = 0

    def candidates(self, limit):
        self.discovery_calls += 1
        return self.names

    def current_sid(self):
        return SID

    def open(self, name):
        self.opens.append(name)
        return self.pipes.get(name, self.pipe)


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"CODEX_THREAD_ID": SOURCE}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def adapter(self, response, **kwargs):
        self.pipe = FakePipe(response)
        self.system = FakeSystem(self.pipe)
        return CodexAppToolsAdapter(system=self.system, **kwargs)

    def test_construction_does_not_discover_or_write(self):
        system = FakeSystem()
        CodexAppToolsAdapter(system=system)
        self.assertEqual(system.discovery_calls, 0)
        self.assertEqual(system.pipe.writes, [])

    def test_provenance_pins_are_full_sha256_values(self):
        for value in (APP_MANIFEST_SHA256, EXECUTABLE_SHA256, MAIN_MODULE_SHA256,
                      MCP_CONFIG_SHA256, SERVER_SHA256):
            self.assertEqual(len(value), 64)
            self.assertEqual(len(bytes.fromhex(value)), 32)

    def test_discovery_verifies_and_closes_without_protocol_write(self):
        adapter = self.adapter(b"")
        result = adapter.discover()
        self.assertTrue(result["same_handle_server_verified"])
        self.assertTrue(self.pipe.closed)
        self.assertEqual(self.pipe.writes, [])
        self.assertNotIn(NAME, json.dumps(result))
        self.assertNotIn(SID, json.dumps(result))

    def test_wrong_pid_executable_or_sid_never_writes(self):
        for identity in [ServerIdentity(0, str(INSTALLED_EXECUTABLE), SID),
                         ServerIdentity(123, "C:\\other\\Codex.exe", SID),
                         ServerIdentity(123, str(INSTALLED_EXECUTABLE), "S-1-other")]:
            with self.subTest(identity=identity):
                pipe = FakePipe(identity=identity)
                with self.assertRaises(CodexPipeError):
                    CodexAppToolsAdapter(system=FakeSystem(pipe)).probe_metadata(SOURCE)
                self.assertEqual(pipe.writes, [])
                self.assertTrue(pipe.closed)

    def test_multiple_verified_candidates_fail_closed(self):
        first, second = FakePipe(), FakePipe()
        system = FakeSystem(candidates=[NAME, NAME2], pipes={NAME: first, NAME2: second})
        with self.assertRaises(CodexPipeError):
            CodexAppToolsAdapter(system=system).probe_metadata(SOURCE)
        self.assertTrue(first.closed and second.closed)
        self.assertEqual(first.writes + second.writes, [])

    def test_candidate_overflow_duplicate_and_wrong_prefix(self):
        for names in [[NAME] * 17, [NAME, NAME], ["\\\\.\\pipe\\unrelated"]]:
            with self.subTest(names=len(names)):
                system = FakeSystem(candidates=names)
                with self.assertRaises(CodexPipeError):
                    CodexAppToolsAdapter(system=system).discover()
                self.assertEqual(system.opens, [])

    def test_current_thread_metadata_only_exact_contract_and_redaction(self):
        raw = {"artifacts": [{"private_test_body": "never published"}]}
        adapter = self.adapter(inventory() + artifacts(raw))
        result = adapter.probe_metadata(SOURCE)
        self.assertEqual(result["artifact_count"], 1)
        self.assertEqual(result["tools_list_calls"], 1)
        self.assertEqual(result["list_artifacts_calls"], 1)
        self.assertFalse(result["model_invoked"])
        self.assertFalse(result["message_history_read"])
        self.assertFalse(result["native_result_verified"])
        self.assertEqual(len(self.pipe.writes), 2)
        first, second = self.pipe.writes
        self.assertEqual(first, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"threadStartKind": "all"}})
        params = second["params"]
        self.assertEqual(params["namespace"], "codex_app")
        self.assertEqual(params["tool"], "list_artifacts")
        self.assertEqual(params["arguments"], {})
        self.assertEqual(params["threadId"], SOURCE)
        self.assertEqual(params["callerSource"], "codex")
        self.assertTrue(params["callId"].startswith("mcp-call-"))
        self.assertEqual(params["turnId"], "mcp-turn-2")
        text = json.dumps(result)
        for secret in (SOURCE, NAME, SID, "never published", "private_test_body"):
            self.assertNotIn(secret, text)

    def test_wrong_or_missing_source_never_discovers(self):
        adapter = self.adapter(b"")
        with self.assertRaises(CodexPipeError):
            adapter.probe_metadata("another-thread")
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(CodexPipeError):
                adapter.probe_metadata(SOURCE)
        self.assertEqual(self.system.discovery_calls, 0)

    def test_invalid_inventory_or_changed_schema_stops_after_list(self):
        invalid = [rpc_result(1, {"tools": None}), inventory([METADATA_TOOL, METADATA_TOOL]),
                   inventory([]), inventory([{**METADATA_TOOL, "inputSchema": {"type": "object", "properties": {"thread": {"type": "string"}}}}])]
        for response in invalid:
            with self.subTest(response_sha=hashlib.sha256(response).hexdigest()):
                adapter = self.adapter(response)
                with self.assertRaises(CodexPipeError):
                    adapter.probe_metadata(SOURCE)
                self.assertEqual(len(self.pipe.writes), 1)
                self.assertTrue(self.pipe.closed)

    def test_malformed_frames_fail_without_followup(self):
        malformed_payload = b'{"jsonrpc":"2.0","id":1,"id":1,"result":{}}'
        responses = [struct.pack("<I", 0), struct.pack("<I", MAX_FRAME + 1),
                     struct.pack("<I", 1) + b"\xff", struct.pack("<I", len(malformed_payload)) + malformed_payload,
                     encode_frame({"jsonrpc": "2.0", "id": 8, "result": {}}),
                     encode_frame({"jsonrpc": "2.0", "id": True, "result": {}}),
                     encode_frame({"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "private error"}}),
                     encode_frame({"jsonrpc": "2.0", "id": 1, "result": {}, "error": {}})]
        for response in responses:
            with self.subTest(response_sha=hashlib.sha256(response).hexdigest()):
                adapter = self.adapter(response)
                with self.assertRaises(CodexPipeError) as error:
                    adapter.probe_metadata(SOURCE)
                self.assertNotIn("private error", str(error.exception))
                self.assertEqual(len(self.pipe.writes), 1)

    def test_metadata_tool_failure_does_not_retry(self):
        for result in [dict(success=False, contentItems=[]),
                       dict(success=True, contentItems=[]),
                       dict(success=True, contentItems=[dict(type="inputText", text='{"artifacts":null}')])]:
            with self.subTest(result=result):
                adapter = self.adapter(inventory() + rpc_result(2, result))
                with self.assertRaises(CodexPipeError):
                    adapter.probe_metadata(SOURCE)
                self.assertEqual(len(self.pipe.writes), 2)
                self.assertEqual(len(self.system.opens), 1)

    def test_read_timeout_closes_and_never_reconnects(self):
        adapter = self.adapter(b"")
        with self.assertRaises(PipeTimeout):
            adapter.probe_metadata(SOURCE)
        self.assertEqual(len(self.system.opens), 1)
        self.assertEqual(len(self.pipe.writes), 1)
        self.assertTrue(self.pipe.closed)
        self.assertEqual(adapter.last_probe_diagnostics["phase"], "tools_list_header_read")
        self.assertEqual(adapter.last_probe_diagnostics["tools_list_calls"], 1)
        self.assertEqual(adapter.last_probe_diagnostics["list_artifacts_calls"], 0)
        self.assertGreaterEqual(adapter.last_probe_diagnostics["elapsed_seconds"], 0)

    def test_frame_uses_utf8_byte_length_and_little_endian(self):
        frame = encode_frame({"prompt": "한글"})
        self.assertEqual(struct.unpack("<I", frame[:4])[0], len(frame[4:]))
        self.assertEqual(json.loads(frame[4:])["prompt"], "한글")
        with self.assertRaises(CodexPipeError):
            encode_frame({"prompt": "x" * MAX_FRAME})


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"CODEX_THREAD_ID": SOURCE}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.deadline = time.time() + 60
        self.record = dict(source_thread_id=SOURCE, target_thread_id="target-thread", root_id="root-id",
                           dispatch_id="dispatch-id", prompt_sha256=hashlib.sha256(b"scoped proof").hexdigest(),
                           deadline=self.deadline, human_authorized=True, root_active=True, recipient_registered=True,
                           workspace_paused=False, recipient_paused=False, budget_remaining=1, claim_state="claimed")
        self.pipe = FakePipe(inventory([SEND_TOOL]) + rpc_result(2, {"success": True, "contentItems": [
            {"type": "inputText", "text": json.dumps({"threadId": "target-thread"})}]}))
        self.system = FakeSystem(self.pipe)
        self.gate_calls = []

    def gate(self, record):
        self.gate_calls.append(record)
        return True

    def adapter(self, gate=None):
        return CodexAppToolsAdapter(system=self.system, ledger_path=Path(self.temp.name) / "dispatch.sqlite3", authorize_send=gate or self.gate)

    def send(self, adapter, record=None, **overrides):
        args = dict(source_thread_id=SOURCE, root_id="root-id", dispatch_id="dispatch-id", deadline=self.deadline,
                    authorization_record=self.record if record is None else record)
        args.update(overrides)
        return adapter.send_authorized("target-thread", "scoped proof", **args)

    def test_authorized_once_acceptance_is_not_result(self):
        result = self.send(self.adapter())
        self.assertEqual(result["state"], "native_accepted")
        self.assertTrue(result["native_accepted"])
        self.assertFalse(result["native_result_verified"])
        self.assertTrue(result["native_input_attempted"])
        self.assertEqual(len(self.gate_calls), 2)
        self.assertEqual(self.pipe.writes[1]["params"]["arguments"], {"threadId": "target-thread", "prompt": "scoped proof"})

    def test_send_does_not_mix_previous_probe_diagnostics(self):
        adapter = self.adapter()
        previous = {"phase": "complete", "native_send_calls": 0, "bytes_read_completed": 100}
        adapter.last_probe_diagnostics = dict(previous)
        self.assertEqual(self.send(adapter)["state"], "native_accepted")
        self.assertEqual(adapter.last_probe_diagnostics, previous)

    def test_success_without_exact_effective_target_is_unknown_and_never_replayed(self):
        for index, items in enumerate([[], [{"type": "inputText", "text": "{}"}],
                                       [{"type": "inputText", "text": "not JSON"}],
                                       [{"type": "inputText", "text": '{"threadId":"another-thread"}'}]]):
            with self.subTest(index=index):
                self.pipe = FakePipe(inventory([SEND_TOOL]) + rpc_result(2, {"success": True, "contentItems": items}))
                self.system = FakeSystem(self.pipe)
                adapter = self.adapter()
                dispatch_id = "ack-dispatch-" + str(index)
                record = {**self.record, "dispatch_id": dispatch_id}
                result = self.send(adapter, record, dispatch_id=dispatch_id)
                self.assertEqual(result["state"], "delivery_unknown")
                self.assertFalse(result["native_accepted"])
                self.assertFalse(result["native_result_verified"])
                duplicate = self.send(adapter, record, dispatch_id=dispatch_id)
                self.assertTrue(duplicate["duplicate_suppressed"])
                self.assertEqual(len(self.system.opens), 1)

    def test_untrusted_record_without_runtime_gate_cannot_send(self):
        adapter = CodexAppToolsAdapter(system=self.system, ledger_path=Path(self.temp.name) / "dispatch.sqlite3")
        with self.assertRaises(CodexPipeError):
            self.send(adapter)
        self.assertEqual(self.system.opens, [])
        self.assertEqual(self.pipe.writes, [])

    def test_missing_wrong_paused_stopped_expired_unauthorized_fail_before_io(self):
        mutations = {"source_thread_id": "wrong", "target_thread_id": "wrong", "root_id": "wrong",
                     "dispatch_id": "wrong", "prompt_sha256": "wrong", "deadline": time.time() - 1,
                     "human_authorized": False, "root_active": False, "recipient_registered": False,
                     "workspace_paused": True, "recipient_paused": True, "budget_remaining": 0,
                     "claim_state": "unclaimed"}
        for key, value in mutations.items():
            with self.subTest(key=key):
                with self.assertRaises(CodexPipeError):
                    self.send(self.adapter(), {**self.record, key: value})
        self.assertEqual(self.pipe.writes, [])
        self.assertEqual(self.system.opens, [])

    def test_deadline_expiry_prevents_io(self):
        with self.assertRaises(CodexPipeError):
            self.send(self.adapter(), deadline=time.time() - 1)
        self.assertEqual(self.system.opens, [])

    def test_runtime_control_rechecked_immediately_before_native_send(self):
        values = iter([True, False])
        result = self.send(self.adapter(gate=lambda _: next(values)))
        self.assertEqual(result["state"], "not_sent")
        self.assertFalse(result["native_input_attempted"])
        self.assertEqual(result["failure_phase"], "authorization_recheck")
        self.assertEqual(len(self.pipe.writes), 1)
        self.assertEqual(self.pipe.writes[0]["method"], "tools/list")

    def test_duplicate_dispatch_returns_receipt_and_never_sends_again(self):
        adapter = self.adapter()
        first = self.send(adapter)
        second = self.send(adapter)
        self.assertTrue(second["duplicate_suppressed"])
        self.assertEqual(first["state"], second["state"])
        self.assertEqual(len(self.system.opens), 1)
        self.assertEqual(len(self.pipe.writes), 2)

    def test_uncertain_native_send_is_durable_no_fallback_or_resend(self):
        self.pipe.response = bytearray(inventory([SEND_TOOL]))
        adapter = self.adapter()
        first = self.send(adapter)
        self.assertEqual(first["state"], "delivery_unknown")
        self.assertTrue(first["native_input_attempted"])
        self.assertEqual(first["reason_code"], "native_timeout")
        self.assertFalse(first["automatic_retry"])
        self.assertFalse(first["fallback_attempted"])
        second = self.send(self.adapter())
        self.assertTrue(second["duplicate_suppressed"])
        self.assertEqual(second["state"], "delivery_unknown")
        self.assertEqual(len(self.system.opens), 1)

    def test_before_send_failure_is_not_sent_and_not_automatically_retried(self):
        self.system.names = []
        adapter = self.adapter()
        result = self.send(adapter)
        self.assertEqual(result["state"], "not_sent")
        self.assertFalse(result["native_input_attempted"])
        self.assertEqual(result["reason_code"], "native_unavailable")
        self.assertEqual(result["failure_phase"], "owner_verification")
        self.system.names = [NAME]
        self.assertTrue(self.send(adapter)["duplicate_suppressed"])
        self.assertEqual(len(self.system.opens), 0)

    def test_changed_message_schema_never_attempts_native_input(self):
        changed = [{"threadId": {"type": "string"}, "prompt": None},
                   {"threadId": {"type": "string"}, "prompt": {"type": "number"}}]
        for index, properties in enumerate(changed):
            with self.subTest(index=index):
                self.pipe = FakePipe(inventory([{**SEND_TOOL, "inputSchema": {
                    "type": "object", "properties": properties, "required": ["threadId", "prompt"]}}]))
                self.system = FakeSystem(self.pipe)
                dispatch = "schema-dispatch-" + str(index)
                result = self.send(self.adapter(), {**self.record, "dispatch_id": dispatch}, dispatch_id=dispatch)
                self.assertEqual(result["state"], "not_sent")
                self.assertFalse(result["native_input_attempted"])
                self.assertEqual(result["reason_code"], "schema_changed")
                self.assertEqual(result["failure_phase"], "schema_validation")
                self.assertEqual([item["method"] for item in self.pipe.writes], ["tools/list"])

    def test_pin_failure_is_proven_not_sent_and_remains_suppressed(self):
        for reason in ("pin_unavailable", "pin_mismatch"):
            with self.subTest(reason=reason):
                adapter = self.adapter()
                dispatch = "pin-dispatch-" + reason
                record = {**self.record, "dispatch_id": dispatch}
                with patch.object(adapter, "_connect", side_effect=CodexPipeError("Redacted", reason_code=reason)):
                    first = self.send(adapter, record, dispatch_id=dispatch)
                self.assertEqual(first["state"], "not_sent")
                self.assertFalse(first["native_input_attempted"])
                self.assertEqual(first["reason_code"], reason)
                self.assertEqual(first["failure_phase"], "owner_verification")
                second = self.send(adapter, record, dispatch_id=dispatch)
                self.assertTrue(second["duplicate_suppressed"])
                self.assertFalse(second["native_input_attempted"])
                self.assertEqual(self.system.opens, [])

    def test_explicit_native_tool_failure_is_not_retried_or_reinterpreted_as_not_sent(self):
        self.pipe.response = bytearray(inventory([SEND_TOOL]) + rpc_result(2, {"success": False, "contentItems": []}))
        adapter = self.adapter()
        first = self.send(adapter)
        self.assertEqual(first["state"], "native_tool_failed")
        self.assertTrue(first["native_input_attempted"])
        self.assertFalse(first["automatic_retry"])
        self.assertFalse(first["fallback_attempted"])
        self.assertTrue(self.send(adapter)["duplicate_suppressed"])
        self.assertEqual(len(self.system.opens), 1)
        self.assertEqual([item["method"] for item in self.pipe.writes], ["tools/list", "tools/call"])

    def test_reused_dispatch_different_root_rejected(self):
        adapter = self.adapter()
        self.send(adapter)
        changed = {**self.record, "root_id": "different-root"}
        with self.assertRaises(CodexPipeError):
            self.send(adapter, changed, root_id="different-root")
        self.assertEqual(len(self.system.opens), 1)

    def test_ledger_does_not_store_prompt_body_or_response(self):
        self.send(self.adapter())
        path = Path(self.temp.name) / "dispatch.sqlite3"
        db = sqlite3.connect(path)
        try:
            dump = "\n".join(db.iterdump())
        finally:
            db.close()
        self.assertNotIn("scoped proof", dump)
        self.assertIn(self.record["prompt_sha256"], dump)


class InstalledProvenanceTests(unittest.TestCase):
    def test_current_inspection_manifest_matches_allowlist_without_replacing_prior_version(self):
        manifest = json.loads((Path(__file__).resolve().parents[1] / "adapters/inspection-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["codex_manifest_identity"]["package_version"], "26.1013.4894.0")
        prior = manifest["codex_version_inspections"][0]
        self.assertEqual(prior["application_version"], "26.1016.6573.0")
        self.assertTrue(prior["actual_rea_static_analysis"])
        self.assertEqual(prior["rea_exit_code"], 0)
        self.assertFalse(prior["full_main_attempt"]["whole_application_analysis_succeeded"])
        current = manifest["codex_version_inspections"][-1]
        self.assertEqual(current["application_version"], PROVENANCE["application_version"])
        self.assertEqual(current["exact_frontend_executable"], str(INSTALLED_EXECUTABLE))
        self.assertEqual(current["executable_sha256"], EXECUTABLE_SHA256)
        files = {row["path"]: row["sha256"] for row in current["sources"]}
        self.assertEqual(files[MAIN_MODULE_PATH], MAIN_MODULE_SHA256)
        self.assertEqual(files["codex-app-tools/server.mjs"], SERVER_SHA256)
        self.assertEqual(files["codex-app-tools/.mcp.json"], MCP_CONFIG_SHA256)
        self.assertEqual(files["AppxManifest.xml"], APP_MANIFEST_SHA256)
        self.assertFalse(current["actual_rea_static_analysis"])
        self.assertNotIn("rea_exit_code", current)
        self.assertEqual(current["scope_counters"]["native_messages_sent"], 0)
        self.assertEqual(current["scope_counters"]["native_rpc_calls"], 0)
        self.assertFalse(current["protocol_comparison"]["live_catalog_or_native_delivery_verified"])

    def test_exact_current_and_legacy_records_keep_original_six_key_contract(self):
        keys = {"application_version", "bundled_server_sha256", "package_manifest_sha256",
                "main_module_sha256", "public_api", "metadata_is_native_turn_attestation"}
        self.assertIsInstance(SUPPORTED_PROVENANCES, tuple)
        self.assertEqual([row["application_version"] for row in SUPPORTED_PROVENANCES],
                         ["26.1013.4894.0", "26.1016.6573.0", "26.1016.8622.0"])
        for row in SUPPORTED_PROVENANCES:
            self.assertEqual(set(row), keys)
            self.assertFalse(row["public_api"])
            self.assertFalse(row["metadata_is_native_turn_attestation"])
        self.assertEqual(PROVENANCE, SUPPORTED_PROVENANCES[-1])
        self.assertIn(PROVENANCE["application_version"], str(INSTALLED_EXECUTABLE))

    def test_unknown_and_legacy_executable_paths_fail_before_discovery(self):
        paths = [Path(r"C:\other\ChatGPT.exe")] + [
            Path(str(INSTALLED_EXECUTABLE).replace(PROVENANCE["application_version"], version))
            for version in ("26.1013.4894.0", "26.1016.6573.0", "26.1016.9999.0")
        ]
        for executable in paths:
            with self.subTest(executable=executable):
                self.assertNotEqual(executable, INSTALLED_EXECUTABLE)
                system = FakeSystem()
                adapter = CodexAppToolsAdapter(installed_executable=executable, system=system)
                with self.assertRaises(CodexPipeError) as error:
                    adapter.discover()
                self.assertEqual(error.exception.reason_code, "pin_unavailable")
                self.assertEqual(system.discovery_calls, 0)
                self.assertEqual(system.pipe.writes, [])

    def test_all_current_disk_pins_are_checked_before_opening_any_pipe(self):
        plugin = INSTALLED_EXECUTABLE.parent / "resources/plugins/openai-bundled/plugins/codex-app-tools"
        materials = {INSTALLED_EXECUTABLE: b"executable", plugin / "server.mjs": b"server",
                     plugin / ".mcp.json": b"config",
                     INSTALLED_EXECUTABLE.parent.parent / "AppxManifest.xml": b"manifest"}
        constants = dict(zip(["EXECUTABLE_SHA256", "SERVER_SHA256", "MCP_CONFIG_SHA256", "APP_MANIFEST_SHA256"],
                             [hashlib.sha256(value).hexdigest() for value in materials.values()]))
        for changed in [None, *materials, "main"]:
            with self.subTest(changed=changed):
                system = FakeSystem()
                values = dict(materials)
                if changed in values:
                    values[changed] = b"changed version or hash"

                def read(path):
                    return values[path]

                with patch.multiple(adapter_module, **constants), patch.object(Path, "read_bytes", read), \
                        patch.object(adapter_module, "_installed_main_hash",
                                     return_value="0" * 64 if changed == "main" else MAIN_MODULE_SHA256), \
                        patch.object(adapter_module, "Win32PipeSystem", return_value=system):
                    if changed is None:
                        result = CodexAppToolsAdapter().discover()
                        self.assertEqual(result["application_version"], PROVENANCE["application_version"])
                        self.assertEqual(len(system.opens), 1)
                    else:
                        with self.assertRaises(CodexPipeError) as error:
                            CodexAppToolsAdapter().discover()
                        self.assertEqual(error.exception.reason_code, "pin_mismatch")
                        self.assertEqual(system.discovery_calls, 0)
                    self.assertEqual(system.pipe.writes, [])

    def test_missing_current_material_reports_pin_unavailable_without_native_io(self):
        with patch.object(Path, "read_bytes", side_effect=OSError), \
                patch.object(adapter_module, "Win32PipeSystem") as transport:
            with self.assertRaises(CodexPipeError) as error:
                CodexAppToolsAdapter().discover()
            self.assertEqual(error.exception.reason_code, "pin_unavailable")
            transport.assert_not_called()

    def test_selected_asar_hash_reads_exact_packed_module_and_rejects_link_or_bad_header(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.asar"
            content = b"inert selected module"
            for invalid in [None, "link", "unpacked", "short", "oversize"]:
                with self.subTest(invalid=invalid):
                    node = {"size": len(content), "offset": "0"}
                    if invalid in ("link", "unpacked"):
                        node[invalid] = True
                    tree = {"files": {MAIN_MODULE_PATH.rsplit("/", 1)[-1]: node}}
                    for part in reversed(MAIN_MODULE_PATH.split("/")[:-1]):
                        tree = {"files": {part: tree}}
                    raw = json.dumps(tree).encode()
                    header = struct.pack("<IIII", 4, len(raw)+8, len(raw)+4, len(raw))
                    path.write_bytes(header + raw + (content[:-1] if invalid == "short" else content))
                    if invalid == "oversize":
                        path.write_bytes(struct.pack("<IIII", 4, 32*1024*1024, 0, len(raw)) + raw)
                    if invalid is None:
                        self.assertEqual(_installed_main_hash(path), hashlib.sha256(content).hexdigest())
                    else:
                        with self.assertRaises(CodexPipeError) as error:
                            _installed_main_hash(path)
                        self.assertEqual(error.exception.reason_code, "pin_mismatch")


class Win32CancellationTests(unittest.TestCase):
    def test_immediate_io_completion_does_not_wait_or_cancel(self):
        class Kernel:
            def __init__(self):
                self.closed = []

            def CreateEventW(self, *args):
                return 77

            def WriteFile(self, *args):
                return True

            def WaitForSingleObject(self, *args):
                raise AssertionError("Completed I/O must not wait")

            def CancelIoEx(self, *args):
                raise AssertionError("Completed I/O must not be cancelled")

            def GetOverlappedResult(self, handle, operation, output, wait):
                output._obj.value = 1
                return True

            def CloseHandle(self, handle):
                self.closed.append(handle)

        kernel = Kernel()
        pipe = _Win32Pipe(kernel, 12345, ServerIdentity(123, str(INSTALLED_EXECUTABLE), SID))
        value = pipe._io(ctypes.create_string_buffer(b"a"), 1, time.monotonic() + 1, writing=True)
        self.assertEqual(value, 1)
        self.assertEqual(kernel.closed, [77])
        self.assertEqual(pipe.diagnostics["bytes_written_completed"], 1)

    def test_server_identity_is_queried_twice_on_the_same_handle_before_io(self):
        class Kernel:
            def __init__(self, changed=False):
                self.handles = []
                self.closed = []
                self.changed = changed

            def CreateFileW(self, *args):
                return 500

            def GetNamedPipeServerProcessId(self, handle, output):
                self.handles.append(handle)
                output._obj.value = 999 if self.changed and len(self.handles) == 2 else 123
                return True

            def OpenProcess(self, rights, inherit, pid):
                return 700

            def QueryFullProcessImageNameW(self, process, flags, output, size):
                output.value = str(INSTALLED_EXECUTABLE)
                return True

            def GetExitCodeProcess(self, process, output):
                output._obj.value = 259
                return True

            def CloseHandle(self, handle):
                self.closed.append(handle)

        for changed in (False, True):
            with self.subTest(changed=changed):
                system = Win32PipeSystem.__new__(Win32PipeSystem)
                system.kernel = Kernel(changed)
                with patch.object(system, "_sid", return_value=SID):
                    if changed:
                        with self.assertRaises(CodexPipeError):
                            system.open(NAME)
                    else:
                        pipe = system.open(NAME)
                        self.assertEqual(pipe.identity, ServerIdentity(123, str(INSTALLED_EXECUTABLE), SID))
                        pipe.close()
                self.assertEqual(system.kernel.handles, [500, 500])
                self.assertCountEqual(system.kernel.closed, [700, 500])

    def test_timeout_cancels_only_own_operation_and_keeps_pending_buffer_alive(self):
        class Kernel:
            def __init__(self):
                self.cancelled = []
                self.closed = []

            def CreateEventW(self, *args):
                return 77

            def WriteFile(self, *args):
                return False

            def WaitForSingleObject(self, event, milliseconds):
                return 258

            def CancelIoEx(self, handle, operation):
                self.cancelled.append((handle, operation))
                return True

            def CloseHandle(self, handle):
                self.closed.append(handle)

        kernel = Kernel()
        pipe = _Win32Pipe(kernel, 12345, ServerIdentity(123, str(INSTALLED_EXECUTABLE), SID))
        before = len(_PENDING_IO)
        buffer = ctypes.create_string_buffer(b"a")
        try:
            with patch("ctypes.get_last_error", return_value=997):
                with self.assertRaises(PipeTimeout):
                    pipe._io(buffer, 1, time.monotonic() + 0.01, writing=True)
            self.assertEqual(len(kernel.cancelled), 1)
            self.assertEqual(kernel.cancelled[0][0], 12345)
            self.assertIsNotNone(kernel.cancelled[0][1])
            self.assertEqual(len(_PENDING_IO), before + 1)
            self.assertIs(_PENDING_IO[-1][-1], buffer)
            self.assertNotIn(77, kernel.closed)
            self.assertEqual(pipe.diagnostics["last_win32_code"], 997)
            self.assertEqual(pipe.diagnostics["last_wait_result"], 258)
            self.assertEqual(pipe.diagnostics["bytes_written_completed"], 0)
            pipe.close()
            self.assertEqual(kernel.closed, [12345])
        finally:
            del _PENDING_IO[before:]


if __name__ == "__main__":
    unittest.main()
