"""R4.1 strict completion IDs and atomic duplicate convergence."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store import BusError
from test_single_consumer_completion_r4 import (complete, http_room, install_fake_kernel,
                                                reserve, row, store, work)

INVALID = []
for label, value in (("empty", ""), ("zero", 0), ("false", False), ("list", []), ("dict", {})):
    INVALID.extend([pytest.param(value, value, id=label + "-both"),
                    pytest.param(None, value, id=label + "-left-none"),
                    pytest.param(value, None, id=label + "-right-none")])


def logical_snapshot(store):
    """Hash every logical table and schema without printing synthetic capabilities."""
    with store.connection() as db:
        schema = [tuple(value) for value in db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")]
        result = {"schema": hashlib.sha256(repr(schema).encode()).hexdigest()}
        for entry in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            name = entry[0]
            rows = [tuple(value) for value in db.execute('SELECT * FROM "' + name.replace('"', '""') + '" ORDER BY rowid')]
            result[name] = (len(rows), hashlib.sha256(repr(rows).encode()).hexdigest())
        return result


@pytest.mark.parametrize("job_id,event_id", INVALID)
@pytest.mark.parametrize("surface", ["library", "http"])
def test_malformed_explicit_ids_never_write_or_call_handler(store, surface, job_id, event_id):
    @contextmanager
    def selected_surface():
        if surface == "http":
            with http_room(store) as (_, request):
                yield request
        else:
            yield None

    with selected_surface() as request:
        values = work(store, "avalokita_direct")
        actor, token, actual_job, _, _ = values
        unregister, calls = install_fake_kernel(store, values)
        before = logical_snapshot(store)
        try:
            payload = dict(native_session_id=actor["native_session_id"], job_id=job_id, event_id=event_id,
                           last_assistant_message="Synthetic malformed correlation", requires_review_if_uncorrelated=True)
            if surface == "http":
                status, _ = request("/api/native/result", payload, token)
            else:
                try:
                    store.native_result(actor["id"], actor["native_session_id"], payload["last_assistant_message"],
                                        job_id, event_id, requires_review_if_uncorrelated=True)
                    status = 200
                except BusError as error:
                    status = error.status
            observation = dict(status=status, all_logical_tables_unchanged=logical_snapshot(store) == before,
                               handler_calls=len(calls), job_state=row(store, actual_job)["state"])
            assert observation == dict(status=400, all_logical_tables_unchanged=True,
                                       handler_calls=0, job_state="awaiting_native_reply")
        finally:
            unregister()


@pytest.mark.parametrize("surface", ["library", "http"])
def test_only_actual_none_pair_preserves_passive_result(store, surface):
    with http_room(store) as (_, request):
        values = work(store, "avalokita_direct")
        actor, token, job_id, _, _ = values
        unregister, calls = install_fake_kernel(store, values)
        try:
            if surface == "http":
                status, result = request("/api/native/result", dict(native_session_id=actor["native_session_id"],
                    last_assistant_message="Synthetic passive result", job_id=None, event_id=None), token)
                assert status == 200
            else:
                result = store.native_result(actor["id"], actor["native_session_id"], "Synthetic passive result", None, None)
            assert result["completed"] is False and not calls
            assert row(store, job_id)["state"] == "awaiting_native_reply"
            assert store.agent(actor["id"])["channel_verified"] is False
        finally:
            unregister()


@pytest.mark.parametrize("surface", ["library", "http"])
def test_valid_correlated_broker_result_still_completes(store, surface):
    with http_room(store) as (_, request):
        values = work(store)
        actor, token, job_id, event_id, _ = values
        reserve(store, values)
        if surface == "http":
            status, result = request("/api/native/result", dict(native_session_id=actor["native_session_id"],
                last_assistant_message="Synthetic exact result", job_id=job_id, event_id=event_id), token)
            assert status == 200
        else:
            result = complete(store, values, "Synthetic exact result")
        assert result["completed"] is True and row(store, job_id)["state"] == "completed"


@pytest.mark.parametrize("surface", ["library", "http"])
@pytest.mark.parametrize("same_result", [True, False], ids=["same", "conflicting"])
def test_concurrent_selected_results_converge_in_final_writer(store, surface, same_result, monkeypatch):
    with http_room(store) as (_, request):
        values = work(store)
        actor, token, job_id, event_id, root_id = values
        reserve(store, values)
        barrier = threading.Barrier(2)
        connection = store.connection
        synchronized = []

        @contextmanager
        def both_read_snapshots():
            with connection() as db:
                reading = []
                db.set_trace_callback(lambda sql: reading.append(True) if sql.strip().upper() == "BEGIN" else None)
                yield db
                if reading:
                    synchronized.append(threading.get_ident())
                    barrier.wait(3)

        monkeypatch.setattr(store, "connection", both_read_snapshots)
        bodies = ["Synthetic concurrent selected A", "Synthetic concurrent selected A" if same_result else "Synthetic concurrent selected B"]

        def submit(body):
            if surface == "http":
                return request("/api/native/result", dict(native_session_id=actor["native_session_id"],
                    last_assistant_message=body, job_id=job_id, event_id=event_id), token)
            try:
                return 200, complete(store, values, body)
            except BusError as error:
                return error.status, {"error": str(error)}

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, bodies))
        monkeypatch.setattr(store, "connection", connection)
        assert len(synchronized) == 2
        with connection() as db:
            selected = db.execute("SELECT body FROM events WHERE kind='result' AND recipient='user' AND sender=? AND root_id=? "
                "AND json_extract(metadata,'$.job_id')=? AND json_extract(metadata,'$.state')='completed'",
                (actor["id"], root_id, job_id)).fetchall()
        observation = dict(statuses=sorted(value[0] for value in results), result_events=len(selected),
                           state=row(store, job_id)["state"])
        assert observation == dict(statuses=[200, 200] if same_result else [200, 409], result_events=1, state="completed")
        assert selected[0][0] in bodies
        for body, (status, result) in zip(bodies, results):
            if status == 200:
                assert result["completed"] is True and selected[0][0] == body


@pytest.mark.parametrize("change", ["pause", "rebind", "new_root"])
def test_duplicate_final_recheck_preserves_control_and_current_binding(store, change):
    values = work(store)
    actor, _, job_id, event_id, _ = values
    reserve(store, values)
    complete(store, values, "Synthetic selected result")
    if change == "pause":
        store.set_paused(True)
    elif change == "rebind":
        store.update_agent(actor["id"], native_session_id="rebound-synthetic-native")
    else:
        store.send("user", actor["id"], "Another authorized synthetic root", "task")
        store.claim(actor["id"])
    before = logical_snapshot(store)
    with pytest.raises(BusError):
        store.native_result(actor["id"], actor["native_session_id"], "Synthetic selected result", job_id, event_id)
    assert logical_snapshot(store) == before
