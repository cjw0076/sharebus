"""Exact lifecycle regressions using owned SQLite and finite fake adapters only."""
from contextlib import contextmanager, nullcontext
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import adapters as native_adapters
from adapters import NativeAdapter, TurnResult
from hosted_runtime import HostedAuthorityDenied
from store import BusError, BusStore
from supervisor import Supervisor


@pytest.fixture(autouse=True)
def isolation(tmp_path, monkeypatch):
    home = tmp_path / "provider-home"
    home.mkdir()
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(key, str(home))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("No real child is authorized"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("No native executable is authorized"))
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: pytest.fail("No network is authorized"))
    candidate = Path(__file__).resolve().parents[3]
    for name in ("store", "supervisor", "hosted_runtime", "adapters"):
        assert Path(sys.modules[name].__file__).resolve().is_relative_to(candidate)


@pytest.fixture
def hosted(tmp_path):
    root = tmp_path / "hosted"
    root.mkdir()
    store = BusStore(root)
    supervisor = Supervisor(store, [], hosted_execution_ready=lambda: True)
    actor = supervisor.hosted.configure(["claude"],
        [{"provider": "claude", "installed": True, "managed_supported": True}])["sessions"][0]["agent_id"]
    try:
        yield SimpleNamespace(store=store, supervisor=supervisor, actor=actor)
    finally:
        supervisor.close()


def job_row(store, job):
    with store.connection() as db:
        return dict(db.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone())


def attempt_row(store, job):
    with store.connection() as db:
        row = db.execute("SELECT * FROM avalokita_hosted_attempts WHERE job_id=?", (job["id"],)).fetchone()
        return dict(row) if row else None


@pytest.mark.parametrize("scenario", [
    "once", "always_only", "multiple_once", "missing_action", "external_path",
    "secret", "oversized_name", "duplicate_id", "unknown_kind", "missing_title", "missing_kind",
    "path_read", "path_delete", "path_ambiguous", "blank_title",
])
def test_acp_approval_keeps_actual_bounded_options_and_existing_scope_checks(hosted, scenario):
    store, ledger = hosted.store, hosted.supervisor.hosted
    state = ledger.configure(["antigravity"], [{"provider": "antigravity", "installed": True, "managed_supported": True}])
    actor = next(item["agent_id"] for item in state["sessions"] if item["provider"] == "antigravity")
    store.send("user", actor, "One synthetic ACP input", "task", budget=1)
    job = store.claim(actor, execution_owner="managed_runtime")
    ledger.begin(store.agent(actor), job, 1)
    ledger.event(actor, job, 1, {"type": "session", "session_id": "original-owned-ACP-session"})
    ledger.event(actor, job, 1, {"type": "input_attempt", "session_id": "original-owned-ACP-session"})
    options = [{"optionId": "actual-once", "kind": "allow_once", "name": "Once", "ignored": "PRIVATE_UNSELECTED"},
               {"optionId": "actual-always", "kind": "allow_always", "name": "Always"},
               {"optionId": "actual-reject", "kind": "reject_once", "name": "Reject"}]
    details = {"command": "python -V", "cwd": str(store.workspace), "options": options,
               "tool_call": {"toolCallId": "actual-tool", "title": "Execute python version", "kind": "execute",
                             "body": "PRIVATE_UNSELECTED"}, "ignored": "PRIVATE_UNSELECTED"}
    if scenario == "always_only":
        options.pop(0)
    elif scenario == "multiple_once":
        options.append({"optionId": "other-once", "kind": "allow_once", "name": "Another once"})
    elif scenario == "missing_action":
        details.pop("command")
    elif scenario == "external_path":
        details["cwd"] = str(store.workspace.parent)
    elif scenario == "secret":
        details["command"] = "echo password=PRIVATE_SECRET"
    elif scenario == "oversized_name":
        options[0]["name"] = "x" * 513
    elif scenario == "duplicate_id":
        options[1]["optionId"] = "actual-once"
    elif scenario == "unknown_kind":
        options[0]["kind"] = "unknown_native_kind"
    elif scenario == "missing_title":
        details["tool_call"].pop("title")
    elif scenario == "missing_kind":
        details["tool_call"].pop("kind")
    elif scenario.startswith("path_"):
        details.pop("command")
        details["paths"] = [str(store.workspace / "source.py")]
        kind = {"path_read": "read", "path_delete": "delete", "path_ambiguous": "other"}[scenario]
        details["tool_call"].update(title=kind + " source.py", kind=kind)
    elif scenario == "blank_title":
        details["tool_call"]["title"] = "   "
    ledger.approval(actor, job, 1, {"approval_id": "actual-card", "provider": "antigravity",
        "method": "session/request_permission", "session_id": "original-owned-ACP-session", "details": details})
    approval = ledger.state()["approvals"][0]
    approvable = scenario in {"once", "path_read", "path_delete"}
    assert approval["can_approve"] is approvable
    assert "PRIVATE_" not in json.dumps(approval)
    if scenario in {"path_read", "path_delete", "path_ambiguous"}:
        assert approval["details"]["tool_call"] == {key: details["tool_call"][key] for key in ("title", "kind")}
    if scenario in {"oversized_name", "duplicate_id", "unknown_kind"}:
        assert approval["details"]["options"] == []
    elif scenario != "secret":
        assert approval["details"]["options"] == [{key: option[key] for key in ("optionId", "kind", "name")} for option in options]
    replies = []
    adapter = SimpleNamespace(respond_approval=lambda *args: replies.append(args))
    if approvable:
        for invalid in ({"behavior": "allow"}, {"decision": "allow_always"},
                        {"decision": "accept", "optionId": "actual-always"}):
            with pytest.raises(BusError, match="Invalid single-request ACP approval response"):
                ledger.reply(actor, "actual-card", invalid, adapter)
        assert replies == []
        assert ledger.reply(actor, "actual-card", {"decision": "accept"}, adapter)["accepted"]
        assert ledger.reply(actor, "actual-card", {"decision": "accept"}, adapter)["duplicate_suppressed"]
        assert replies == [("actual-card", {"decision": "accept"})]
    else:
        with pytest.raises(BusError, match="details are incomplete"):
            ledger.reply(actor, "actual-card", {"decision": "accept"}, adapter)
        assert replies == []
        assert ledger.reply(actor, "actual-card", {"decision": "cancel"}, adapter)["accepted"]
        assert replies == [("actual-card", {"decision": "cancel"})]
    assert attempt_row(store, job)["input_attempted"] == 1
    with store.connection() as db:
        assert db.execute("SELECT used,budget FROM roots WHERE id=?", (job["root_id"],)).fetchone()[:] == (1, 1)
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


@pytest.mark.parametrize("reason_code", [
    "native_denied", "native_permission_required", "native_result_failed",
    "native_transport_unknown", "native_not_sent", "native_human_required", "native_command_failed",
    "native_observer_failed", None, "missing", "PRIVATE_NATIVE_VALUE",
])
def test_hosted_attention_preserves_only_fixed_reason_codes_without_replay(hosted, reason_code):
    store, supervisor, actor = hosted.store, hosted.supervisor, hosted.actor
    store.send("user", actor, "One owned input with a failed native result", "task", budget=1)
    job = store.claim(actor, execution_owner="managed_runtime")
    writes = []

    class Adapter:
        pid = None
        def __init__(self, *args, on_event=None, **kwargs):
            self.on_event = on_event
        def start(self, resume_id=None):
            self.on_event({"type": "session", "session_id": "original-owned-session"})
            return "original-owned-session"
        def turn(self, *args, **kwargs):
            self.on_event({"type": "input_attempt", "session_id": "original-owned-session"})
            writes.append(1)
            self.on_event({"type": "turn", "status": "accepted", "session_id": "original-owned-session", "turn_id": "original-turn"})
            result = SimpleNamespace(success=False, status="PRIVATE_NATIVE_VALUE",
                                     text="PRIVATE_NATIVE_VALUE", error="PRIVATE_NATIVE_VALUE")
            if reason_code != "missing":
                result.reason_code = reason_code
            return result
        def close(self):
            pass
        def interrupt(self):
            pass

    supervisor.adapter_factory = Adapter
    supervisor._run(actor, job)
    expected = "Native CLI requires attention; attempted input is never automatically replayed"
    if reason_code not in (None, "missing", "PRIVATE_NATIVE_VALUE"):
        expected += " (reason_code=" + reason_code + ")"
    current = job_row(store, job)
    attempt = attempt_row(store, job)
    assert current["error"] == store.agent(actor)["error"] == expected
    assert current["state"] == "awaiting_native_reply"
    assert attempt["state"] == "delivery_unknown" and attempt["input_attempted"] == 1
    assert store.agent(actor)["native_session_id"] == "original-owned-session"
    assert store.agent(actor)["active_root"] == job["root_id"]
    assert supervisor.hosted.resume_reserved(actor) is None and writes == [1]
    with store.connection() as db:
        assert db.execute("SELECT used,budget FROM roots WHERE id=?", (job["root_id"],)).fetchone()[:] == (1, 1)
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM avalokita_hosted_attempts").fetchone()[0] == 1
        assert "PRIVATE_NATIVE_VALUE" not in repr([tuple(row) for row in db.execute("SELECT body,metadata FROM events")])


@pytest.mark.parametrize("failure,reason_code", [
    ("human", "native_human_required"), ("timeout", "native_startup_timeout"),
    ("command", "native_command_failed"), ("observer", "native_observer_failed"),
    ("unknown", None), ("foreign_code", None),
])
def test_hosted_startup_preserves_fixed_diagnosis_without_input_or_replay(hosted, monkeypatch, failure, reason_code):
    store, supervisor, actor = hosted.store, hosted.supervisor, hosted.actor
    store.send("user", actor, "One owned input whose startup fails", "task", budget=1)
    job = store.claim(actor, execution_owner="managed_runtime")

    class Adapter(NativeAdapter):
        def _command(self, resume_id):
            raise RuntimeError("PRIVATE_NATIVE_VALUE")
        def _start(self, resume_id=None):
            if failure == "command":
                return super()._start(resume_id)
            if failure == "human":
                raise native_adapters.NativeNeedsHuman("PRIVATE_NATIVE_VALUE")
            if failure == "timeout":
                raise TimeoutError("PRIVATE_NATIVE_VALUE")
            if failure == "observer":
                self._emit({"type": "status", "status": "starting"}, required=True)
            error = RuntimeError("PRIVATE_NATIVE_VALUE")
            if failure == "foreign_code":
                error.reason_code = "PRIVATE_NATIVE_VALUE"
            raise error
        def turn(self, *args, **kwargs):
            pytest.fail("Startup failure cannot send native input")

    if failure == "observer":
        def reject_event(*args, **kwargs):
            raise ValueError("PRIVATE_NATIVE_VALUE")
        monkeypatch.setattr(supervisor.hosted, "event", reject_event)
    supervisor.adapter_factory = Adapter
    supervisor._run(actor, job)
    expected = "Native CLI startup or delivery requires review; automatic replay is disabled"
    if reason_code is not None:
        expected += " (reason_code=" + reason_code + ")"
    current = job_row(store, job)
    attempt = attempt_row(store, job)
    assert current["error"] == store.agent(actor)["error"] == expected
    assert current["state"] == "needs_human"
    assert attempt["state"] == "not_sent" and attempt["input_attempted"] == 0
    assert store.agent(actor)["native_session_id"] is None
    assert supervisor.hosted.resume_reserved(actor) is None
    with store.connection() as db:
        assert db.execute("SELECT used,budget FROM roots WHERE id=?", (job["root_id"],)).fetchone()[:] == (1, 1)
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM avalokita_hosted_attempts").fetchone()[0] == 1
        assert "PRIVATE_NATIVE_VALUE" not in repr([tuple(row) for row in db.execute("SELECT body,metadata FROM events")])


@pytest.mark.parametrize("phase", ["turn", "complete"])
def test_timeout_after_start_is_not_reported_as_startup_failure(hosted, monkeypatch, phase):
    store, supervisor, actor = hosted.store, hosted.supervisor, hosted.actor
    store.send("user", actor, "One attempted native input", "task", budget=1)
    job = store.claim(actor, execution_owner="managed_runtime")
    writes = []

    class Adapter:
        pid = None
        def __init__(self, *args, on_event=None, **kwargs):
            self.on_event = on_event
        def start(self, resume_id=None):
            self.on_event({"type": "session", "session_id": "original-owned-session"})
        def turn(self, *args, **kwargs):
            self.on_event({"type": "input_attempt", "session_id": "original-owned-session"})
            writes.append(1)
            if phase == "turn":
                raise TimeoutError("PRIVATE_NATIVE_VALUE")
            return TurnResult("Selected result", "original-owned-session", True, status="completed")
        def close(self):
            pass
        def interrupt(self):
            pass

    if phase == "complete":
        def reject_completion(*args, **kwargs):
            raise TimeoutError("PRIVATE_NATIVE_VALUE")
        monkeypatch.setattr(supervisor.hosted, "complete", reject_completion)
    supervisor.adapter_factory = Adapter
    supervisor._run(actor, job)
    assert job_row(store, job)["error"] == "Native CLI startup or delivery requires review; automatic replay is disabled"
    assert job_row(store, job)["state"] == "awaiting_native_reply"
    assert attempt_row(store, job)["state"] == "delivery_unknown"
    assert attempt_row(store, job)["input_attempted"] == 1
    assert supervisor.hosted.resume_reserved(actor) is None and writes == [1]


@pytest.mark.parametrize("control", ["pause", "stop", "expiry"])
def test_hosted_control_during_start_retains_typed_unsent_cleanup(hosted, control):
    store, supervisor, actor = hosted.store, hosted.supervisor, hosted.actor
    store.send("user", actor, "One original owned input", "task", budget=1)
    job = store.claim(actor, execution_owner="managed_runtime")
    writes = []

    class Adapter:
        pid = None
        def __init__(self, *args, on_event=None, **kwargs):
            self.on_event = on_event
        def start(self, resume_id=None):
            self.on_event({"type": "session", "session_id": "original-owned-session"})
            if control == "pause":
                store.set_paused(True)
            elif control == "stop":
                store.update_agent(actor, state="stopped")
            else:
                with store.connection() as db:
                    db.execute("UPDATE roots SET deadline=? WHERE id=?", (time.time() - 1, job["root_id"]))
            return "original-owned-session"
        def turn(self, *args, **kwargs):
            writes.append("unreachable-input")
            raise AssertionError("Control was revoked before input")
        def close(self):
            pass
        def interrupt(self):
            pass

    supervisor.adapter_factory = Adapter
    supervisor._run(actor, job)
    expected = {"pause": "avalokita_paused", "stop": "avalokita_stopped", "expiry": "avalokita_expired"}[control]
    assert writes == [] and attempt_row(store, job)["input_attempted"] == 0
    assert job_row(store, job)["state"] == expected
    assert store.agent(actor)["active_root"] is None
    assert attempt_row(store, job)["state"] == ("paused" if control == "pause" else "refused")


def test_before_begin_held_A_resumes_after_unrelated_B_completed(hosted):
    store, supervisor, actor = hosted.store, hosted.supervisor, hosted.actor
    store.send("user", actor, "Held exact original A", "task", budget=1)
    held = store.claim(actor, execution_owner="managed_runtime")
    original = job_row(store, held)
    with patch.object(supervisor.hosted, "begin", side_effect=HostedAuthorityDenied("Pause before begin", state="avalokita_paused")):
        supervisor._run(actor, held)
    assert job_row(store, held)["generation"] == 0 and attempt_row(store, held) is None
    writes = []

    class Adapter:
        pid = None
        def __init__(self, *args, on_event=None, **kwargs):
            self.on_event = on_event
        def start(self, resume_id=None):
            self.on_event({"type": "session", "session_id": "continuing-owned-session"})
            return "continuing-owned-session"
        def turn(self, *args, **kwargs):
            self.on_event({"type": "input_attempt", "session_id": "continuing-owned-session"})
            writes.append(1)
            self.on_event({"type": "turn", "status": "accepted", "session_id": "continuing-owned-session", "turn_id": "original-turn"})
            return TurnResult("Selected output", "continuing-owned-session", True, status="completed")
        def close(self):
            pass
        def interrupt(self):
            pass

    supervisor.adapter_factory = Adapter
    store.send("user", actor, "Unrelated original B", "task", budget=1)
    second = store.claim(actor, execution_owner="managed_runtime")
    supervisor._run(actor, second)
    assert job_row(store, second)["state"] == "completed" and writes == [1]
    assert store.agent(actor)["generation"] == 1
    def validate(db):
        current = db.execute("SELECT * FROM jobs WHERE id=?", (held["id"],)).fetchone()
        return all(current[key] == original[key] for key in ("id", "agent_id", "event_id", "root_id")) and current["state"] == "avalokita_paused"
    supervisor.hosted._admission = lambda *args, **kwargs: nullcontext({"job_id": held["id"], "validate": validate})
    resumed = supervisor.hosted.resume_reserved(actor)
    assert resumed is not None and resumed["remaining_turns"] == 0
    assert resumed["generation"] == store.agent(actor)["generation"] == 1
    assert all(resumed[key] == original[key] for key in ("id", "agent_id", "event_id", "root_id"))
    with store.connection() as db:
        assert db.execute("SELECT used,budget FROM roots WHERE id=?", (held["root_id"],)).fetchone()[:] == (1, 1)
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


def test_cached_generic_adapter_cannot_consume_rebound_session(tmp_path):
    root = tmp_path / "generic"
    root.mkdir()
    store = BusStore(root)
    actor, _ = store.register("claude", "Generic owner", mode="managed_resume", native_session_id="session-A")
    writes, starts = [], []

    class Adapter:
        pid = 424242  # Synthetic resident; never OS process authority.
        def __init__(self, provider, *args, on_event=None, **kwargs):
            self.provider = provider
            self.on_event = on_event
            self.session_id = "session-A"
        def start(self, resume_id=None):
            starts.append(resume_id)
            self.on_event({"type": "session", "session_id": self.session_id})
            return self.session_id
        def turn(self, *args, **kwargs):
            self.on_event({"type": "input_attempt", "session_id": self.session_id, "turn_id": "turn-A"})
            writes.append(self.session_id)
            return TurnResult("Selected generic output", self.session_id, True)
        def close(self):
            self.pid = None
        def interrupt(self):
            pass

    supervisor = Supervisor(store, [], adapter_factory=Adapter)
    try:
        store.send("user", actor["id"], "Authorized A", "task", budget=1)
        first = store.claim(actor["id"], execution_owner="managed_runtime")
        supervisor._run(actor["id"], first)
        assert job_row(store, first)["state"] == "completed" and writes == ["session-A"]
        store.update_agent(actor["id"], native_session_id="session-B", generation=store.agent(actor["id"])["generation"] + 1)
        expected = store.agent(actor["id"])
        store.send("user", actor["id"], "Authorized B", "task", budget=1)
        second = store.claim(actor["id"], execution_owner="managed_runtime")
        supervisor._run(actor["id"], second)
        assert writes == ["session-A"] and starts == ["session-A"]
        current = store.agent(actor["id"])
        assert current["native_session_id"] == expected["native_session_id"] == "session-B"
        assert current["generation"] == expected["generation"]
        assert job_row(store, second)["state"] != "completed"
    finally:
        supervisor.close()


@pytest.mark.parametrize("change", ["null-generation", "future-generation", "public-session", "public-generation",
    "owner-provider", "owner-address", "owner-generation", "attempted", "unknown", "turn-id", "wrong-event", "native-reserved"])
def test_held_resume_cannot_adopt_unproved_or_rebound_authority(hosted, change):
    store, ledger, actor = hosted.store, hosted.supervisor.hosted, hosted.actor
    store.send("user", actor, "Exact held authority", "task", budget=1)
    job = store.claim(actor, execution_owner="managed_runtime")
    original = store.agent(actor)
    ledger.begin(original, job, 1)
    assert ledger.refuse(job, 1, HostedAuthorityDenied("Held", state="avalokita_paused"), original)
    with store.connection() as db:
        if change == "null-generation":
            db.execute("UPDATE jobs SET generation=NULL WHERE id=?", (job["id"],))
        elif change == "future-generation":
            db.execute("UPDATE jobs SET generation=99 WHERE id=?", (job["id"],))
        elif change == "public-session":
            db.execute("UPDATE agents SET native_session_id='unowned-public-session' WHERE id=?", (actor,))
        elif change == "public-generation":
            db.execute("UPDATE agents SET generation=generation+1 WHERE id=?", (actor,))
        elif change == "owner-provider":
            db.execute("UPDATE avalokita_hosted_owners SET provider='codex' WHERE actor=?", (actor,))
        elif change == "owner-address":
            db.execute("UPDATE avalokita_hosted_owners SET owner_id='foreign-owner' WHERE actor=?", (actor,))
        elif change == "owner-generation":
            db.execute("UPDATE avalokita_hosted_owners SET generation=generation+1 WHERE actor=?", (actor,))
        elif change == "attempted":
            db.execute("UPDATE avalokita_hosted_attempts SET input_attempted=1 WHERE job_id=?", (job["id"],))
        elif change == "unknown":
            db.execute("UPDATE avalokita_hosted_attempts SET state='delivery_unknown' WHERE job_id=?", (job["id"],))
        elif change == "turn-id":
            db.execute("UPDATE avalokita_hosted_attempts SET native_turn_id='existing-turn' WHERE job_id=?", (job["id"],))
        elif change == "wrong-event":
            db.execute("UPDATE avalokita_hosted_attempts SET event_id='foreign-event' WHERE job_id=?", (job["id"],))
        else:
            db.execute("UPDATE jobs SET native_input_reserved_at=? WHERE id=?", (time.time(), job["id"]))
    before = job_row(store, job), attempt_row(store, job), store.agent(actor)
    ledger._admission = lambda *args, **kwargs: nullcontext({"job_id": job["id"], "validate": lambda db: True})
    assert ledger.resume_reserved(actor) is None
    assert (job_row(store, job), attempt_row(store, job), store.agent(actor)) == before


def test_held_resume_rechecks_fresh_admission_before_generation_cas(hosted):
    store, ledger, actor = hosted.store, hosted.supervisor.hosted, hosted.actor
    store.send("user", actor, "Held exact admission", "task", budget=1)
    job = store.claim(actor, execution_owner="managed_runtime")
    original = store.agent(actor)
    assert ledger.refuse(job, 1, HostedAuthorityDenied("Held", state="avalokita_paused"), original)
    before = job_row(store, job), store.agent(actor)
    checks = []
    def denied(db):
        checks.append(db.in_transaction)
        return False
    ledger._admission = lambda *args, **kwargs: nullcontext({"job_id": job["id"], "validate": denied})
    with pytest.raises(HostedAuthorityDenied):
        ledger.resume_reserved(actor)
    assert checks == [True] and (job_row(store, job), store.agent(actor)) == before


def test_attempted_hosted_input_cannot_be_refused_as_unsent(hosted):
    store, ledger, actor = hosted.store, hosted.supervisor.hosted, hosted.actor
    store.send("user", actor, "One attempted input", "task", budget=1)
    job = store.claim(actor, execution_owner="managed_runtime")
    original = store.agent(actor)
    ledger.begin(original, job, 1)
    ledger.event(actor, job, 1, {"type": "session", "session_id": "original-session"})
    ledger.event(actor, job, 1, {"type": "input_attempt", "session_id": "original-session"})
    before = job_row(store, job), attempt_row(store, job), store.agent(actor)
    assert not ledger.refuse(job, 1, HostedAuthorityDenied("Late pause", state="avalokita_paused"), original)
    assert (job_row(store, job), attempt_row(store, job), store.agent(actor)) == before


def test_competing_uncertain_attempt_blocks_unattempted_generation_adoption(hosted):
    store, ledger, actor = hosted.store, hosted.supervisor.hosted, hosted.actor
    store.send("user", actor, "Held original A", "task", budget=1)
    held = store.claim(actor, execution_owner="managed_runtime")
    assert ledger.refuse(held, 1, HostedAuthorityDenied("Held A", state="avalokita_paused"), store.agent(actor))
    store.send("user", actor, "Separate original B", "task", budget=1)
    other = store.claim(actor, execution_owner="managed_runtime")
    original = store.agent(actor)
    ledger.begin(original, other, 1)
    assert ledger.refuse(other, 1, HostedAuthorityDenied("Held B", state="avalokita_paused"), original)
    with store.connection() as db:
        db.execute("UPDATE avalokita_hosted_attempts SET state='delivery_unknown' WHERE job_id=?", (other["id"],))
    before = job_row(store, held), job_row(store, other), attempt_row(store, other), store.agent(actor)
    ledger._admission = lambda *args, **kwargs: nullcontext({"job_id": held["id"], "validate": lambda db: True})
    assert ledger.resume_reserved(actor) is None
    assert (job_row(store, held), job_row(store, other), attempt_row(store, other), store.agent(actor)) == before


def test_hosted_post_start_pid_is_observed_inside_original_admission_writer(hosted):
    store, ledger, actor = hosted.store, hosted.supervisor.hosted, hosted.actor
    store.send("user", actor, "Owned post-start observation", "task", budget=1)
    job = store.claim(actor, execution_owner="managed_runtime")
    ledger.begin(store.agent(actor), job, 1)
    ledger.event(actor, job, 1, {"type": "session", "session_id": "original-session"})
    history, active = [], []
    @contextmanager
    def admission(*args, phase, **kwargs):
        history.append(phase)
        active.append(True)
        def validate(db):
            history.append((bool(active), db.in_transaction))
            return bool(active) and db.in_transaction
        try:
            yield {"job_id": job["id"], "validate": validate}
        finally:
            active.pop()
    ledger._admission = admission
    updated = ledger.post_start(store.agent(actor), job, 1, 424242)
    assert history == ["start", (True, True)] and not active
    with store.connection() as db:
        owner = db.execute("SELECT * FROM avalokita_hosted_owners WHERE actor=?", (actor,)).fetchone()
        assert owner["pid"] == store.agent(actor)["pid"] == updated["pid"] == 424242
        assert owner["generation"] == updated["generation"] == 1
    assert attempt_row(store, job)["input_attempted"] == 0 and job_row(store, job)["native_input_reserved_at"] is None


@pytest.mark.parametrize("scenario", ["warm", "event-session", "result-session", "actual-provider", "actual-session", "postclaim-session", "postclaim-provider"])
def test_generic_cache_checks_actual_event_and_result_identity(tmp_path, scenario):
    root = tmp_path / "generic"
    root.mkdir()
    store = BusStore(root)
    actor, _ = store.register("claude", "Generic exact owner", mode="managed_resume", native_session_id="session-A")
    writes, starts, instances = [], [], []

    class Adapter:
        pid = 424242  # Synthetic resident, never an OS child.
        def __init__(self, provider, *args, on_event=None, **kwargs):
            self.provider, self.session_id, self.on_event = provider, "session-A", on_event
            instances.append(self)
        def start(self, resume_id=None):
            starts.append(resume_id)
            self.on_event({"type": "session", "session_id": self.session_id})
            return self.session_id
        def turn(self, *args, **kwargs):
            later = len(writes) == 1
            if later and scenario == "postclaim-session":
                store.update_agent(actor["id"], native_session_id="session-B", generation=2)
            elif later and scenario == "postclaim-provider":
                with store.connection() as db:
                    db.execute("UPDATE agents SET provider='codex' WHERE id=?", (actor["id"],))
            native = "foreign-event-session" if later and scenario == "event-session" else self.session_id
            # Real NativeAdapter has session/turn here, and no provider field.
            self.on_event({"type": "input_attempt", "session_id": native, "turn_id": "fixture-turn"})
            writes.append(self.session_id)
            return TurnResult("Selected output", "foreign-result-session" if later and scenario == "result-session" else self.session_id, True)
        def close(self):
            self.pid = None
        def interrupt(self):
            pass

    supervisor = Supervisor(store, [], adapter_factory=Adapter)
    try:
        store.send("user", actor["id"], "First original input", "task", budget=1)
        first = store.claim(actor["id"], execution_owner="managed_runtime")
        supervisor._run(actor["id"], first)
        assert job_row(store, first)["state"] == "completed"
        if scenario == "actual-provider":
            instances[0].provider = "codex"
        elif scenario == "actual-session":
            instances[0].session_id = "session-B"
        store.send("user", actor["id"], "Second original input", "task", budget=1)
        second = store.claim(actor["id"], execution_owner="managed_runtime")
        supervisor._run(actor["id"], second)
        assert starts == ["session-A"]
        current = store.agent(actor["id"])
        if scenario == "warm":
            assert job_row(store, second)["state"] == "completed" and writes == ["session-A", "session-A"]
            assert current["generation"] == 1 and current["channel_verified"]
        else:
            assert job_row(store, second)["state"] != "completed"
            assert writes == (["session-A", "session-A"] if scenario == "result-session" else ["session-A"])
        assert current["native_session_id"] == ("session-B" if scenario == "postclaim-session" else "session-A")
        assert current["provider"] == ("codex" if scenario == "postclaim-provider" else "claude")
    finally:
        supervisor.close()


def stream_factory(store, monkeypatch, *, observed=None, before_input=None, fail_without_session=False):
    """Actual NativeAdapter lifecycle; its process/pipes are finite in-memory objects."""
    writes, starts, instances, processes, commands = [], [], [], [], []
    executable = str(store.workspace / "synthetic-native.exe")
    monkeypatch.setattr(native_adapters, "resolve_executable", lambda provider: executable)
    monkeypatch.setattr(native_adapters, "discover_native_sessions", lambda *args, **kwargs: [])

    class Input(io.StringIO):
        def __init__(self, adapter):
            super().__init__()
            self.adapter = adapter
        def write(self, value):
            count = super().write(value)
            adapter = self.adapter
            native = observed or adapter._expected_session or ("observed-" + adapter.provider)
            writes.append((adapter.provider, adapter.session_id, adapter._expected_session))
            turn = "actual-adapter-turn-" + str(len(writes))
            if adapter.provider == "claude":
                frame = {"type": "result", "result": "Selected owned output", "subtype": "success", "turn_id": turn}
                if fail_without_session:
                    frame["is_error"] = True
                else:
                    frame["session_id"] = native
            adapter._messages.put_nowait(frame)
            return count

    class Process:
        def __init__(self, adapter):
            self.pid = 424242 + len(processes)  # Synthetic; never an OS process.
            self.returncode = None
            self.stdin, self.stdout, self.stderr = Input(adapter), io.StringIO(), io.StringIO()
            processes.append(self)
        def poll(self):
            return self.returncode
        def wait(self, timeout=None):
            self.returncode = 0
            for stream in (self.stdin, self.stdout, self.stderr):
                stream.close()
            return 0
        def terminate(self):
            self.wait()
        def kill(self):
            self.wait()

    def popen(command, *args, **kwargs):
        adapter = instances[-1]
        assert command == commands[-1] and command[0] == executable
        assert Path(kwargs["cwd"]).resolve() == store.workspace
        return Process(adapter)
    monkeypatch.setattr(native_adapters.subprocess, "Popen", popen)

    def factory(provider, workspace, actor, token_file, **options):
        adapter = NativeAdapter(provider, workspace, actor, token_file, **options)
        instances.append(adapter)
        original_command = adapter._command
        def command(resume_id):
            starts.append(resume_id)
            value = original_command(resume_id)
            if provider == "claude":
                flag = "--resume" if resume_id else "--session-id"
                assert value[value.index(flag) + 1] == adapter._expected_session
                assert adapter._expected_session is not None
            commands.append(value)
            return value
        adapter._command = command
        adapter._check_subscription = lambda *args, **kwargs: None
        if before_input is not None:
            original = adapter._stream_turn
            def stream(prompt, timeout):
                before_input(adapter)
                return original(prompt, timeout)
            adapter._stream_turn = stream
        return adapter
    return SimpleNamespace(factory=factory, writes=writes, starts=starts, instances=instances, processes=processes,
                           commands=commands)


@pytest.mark.parametrize("provider", ["claude"])
@pytest.mark.parametrize("mode", ["managed_new", "managed_resume"])
def test_actual_none_start_observes_first_session_then_warm_and_restart(tmp_path, monkeypatch, provider, mode):
    root = tmp_path / "actual-stream"
    root.mkdir()
    store = BusStore(root)
    resume = "original-" + provider if mode == "managed_resume" else None
    actor, _ = store.register(provider, "Exact delayed-start owner", mode=mode, native_session_id=resume)
    wire = stream_factory(store, monkeypatch)
    supervisor = Supervisor(store, [], adapter_factory=wire.factory)
    native = resume
    try:
        generations = []
        for number in range(3):
            if number == 2:
                supervisor.restart(actor["id"])
            store.send("user", actor["id"], "Original owned input " + str(number), "task", budget=1)
            job = store.claim(actor["id"], execution_owner="managed_runtime")
            supervisor._run(actor["id"], job)
            assert job_row(store, job)["state"] == "completed"
            native = native or wire.instances[0]._expected_session or ("observed-" + provider)
            assert store.agent(actor["id"])["native_session_id"] == wire.instances[-1].session_id == native
            assert store.agent(actor["id"])["channel_verified"]
            generations.append(job_row(store, job)["generation"])
        assert wire.starts == [resume, native] and generations == [1, 1, 2]
        before_write_native = native if provider == "antigravity" else None
        assert len(wire.writes) == 3 and wire.writes[0] == (provider, before_write_native, wire.instances[0]._expected_session)
        assert wire.writes[-1] == (provider, before_write_native, native)
    finally:
        supervisor.close()
    assert all(process.poll() == 0 and all(stream.closed for stream in (process.stdin, process.stdout, process.stderr))
               for process in wire.processes)


def test_actual_none_start_wrong_resume_observation_preserves_original_binding(tmp_path, monkeypatch):
    root = tmp_path / "wrong-resume"
    root.mkdir()
    store = BusStore(root)
    actor, _ = store.register("claude", "Exact resume owner", mode="managed_resume", native_session_id="original-session")
    wire = stream_factory(store, monkeypatch, observed="foreign-session")
    supervisor = Supervisor(store, [], adapter_factory=wire.factory)
    try:
        store.send("user", actor["id"], "One original resume", "task", budget=1)
        job = store.claim(actor["id"], execution_owner="managed_runtime")
        supervisor._run(actor["id"], job)
        assert len(wire.writes) == 1  # Wrong response identity is learned after the lawful first write.
        assert job_row(store, job)["state"] != "completed"
        assert store.agent(actor["id"])["native_session_id"] == "original-session"
        assert not store.agent(actor["id"])["channel_verified"]
    finally:
        supervisor.close()
    assert all(process.poll() == 0 for process in wire.processes)


def test_actual_none_start_cannot_adopt_public_rebind_before_first_write(tmp_path, monkeypatch):
    root = tmp_path / "rebound-start"
    root.mkdir()
    store = BusStore(root)
    actor, _ = store.register("claude", "Exact resume owner", mode="managed_resume", native_session_id="original-session")
    def before_input(adapter):
        store.update_agent(actor["id"], native_session_id="later-public-session", generation=2)
    wire = stream_factory(store, monkeypatch, before_input=before_input)
    supervisor = Supervisor(store, [], adapter_factory=wire.factory)
    try:
        store.send("user", actor["id"], "One original resume", "task", budget=1)
        job = store.claim(actor["id"], execution_owner="managed_runtime")
        supervisor._run(actor["id"], job)
        assert wire.writes == [] and job_row(store, job)["state"] != "completed"
        assert store.agent(actor["id"])["native_session_id"] == "later-public-session"
        assert store.agent(actor["id"])["generation"] == 2
    finally:
        supervisor.close()
    assert all(process.poll() == 0 for process in wire.processes)


def test_actual_pending_startup_cannot_be_reused_for_a_different_job(tmp_path, monkeypatch):
    root = tmp_path / "pending-start"
    root.mkdir()
    store = BusStore(root)
    actor, _ = store.register("claude", "Exact fresh owner", mode="managed_new")
    wire = stream_factory(store, monkeypatch, fail_without_session=True)
    supervisor = Supervisor(store, [], adapter_factory=wire.factory)
    try:
        store.send("user", actor["id"], "First original input", "task", budget=1)
        first = store.claim(actor["id"], execution_owner="managed_runtime")
        supervisor._run(actor["id"], first)
        assert len(wire.writes) == 1 and job_row(store, first)["state"] == "failed"
        assert store.agent(actor["id"])["native_session_id"] is None
        store.send("user", actor["id"], "Different original input", "task", budget=1)
        second = store.claim(actor["id"], execution_owner="managed_runtime")
        supervisor._run(actor["id"], second)
        assert len(wire.writes) == 1 and job_row(store, second)["state"] != "completed"
        assert wire.starts == [None] and store.agent(actor["id"])["native_session_id"] is None
    finally:
        supervisor.close()
    assert all(process.poll() == 0 for process in wire.processes)


def test_actual_generated_expected_identity_cannot_be_cleared_before_input(tmp_path, monkeypatch):
    root = tmp_path / "cleared-expected"
    root.mkdir()
    store = BusStore(root)
    actor, _ = store.register("claude", "Exact generated owner", mode="managed_new")
    expected = []
    def before_input(adapter):
        expected.append(adapter._expected_session)
        adapter._expected_session = None
    wire = stream_factory(store, monkeypatch, before_input=before_input)
    supervisor = Supervisor(store, [], adapter_factory=wire.factory)
    try:
        store.send("user", actor["id"], "One original fresh startup", "task", budget=1)
        job = store.claim(actor["id"], execution_owner="managed_runtime")
        supervisor._run(actor["id"], job)
        assert len(expected) == 1 and expected[0] is not None
        assert wire.writes == [] and job_row(store, job)["state"] != "completed"
        assert store.agent(actor["id"])["native_session_id"] is None
        assert not store.agent(actor["id"])["channel_verified"]
    finally:
        supervisor.close()
    assert all(process.poll() == 0 for process in wire.processes)
