"""Completion authority regressions using owned SQLite, loopback and fake kernels."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from store import BusError, BusStore


@pytest.fixture
def store(tmp_path):
    return BusStore(tmp_path)


def row(store, job_id):
    with store.connection() as db:
        return dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def snapshot(store):
    with store.connection() as db:
        return {name: [tuple(value) for value in db.execute(f"SELECT * FROM {name} ORDER BY rowid")]
                for name in ("agents", "roots", "events", "jobs")}


def work(store, domain="broker_native", *, owner="native_dispatch"):
    mode = "native_attached" if owner == "native_dispatch" else "managed_new" if owner == "managed_runtime" else "external_attached"
    actor, token = store.register("claude", mode=mode, native_session_id="synthetic-" + str(time.time_ns()))
    sent = store.send("user", actor["id"], "Exact synthetic selected input", "task", budget=1)
    job_id = sent["jobs"][0]["id"]
    if domain == "avalokita_direct":
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE events SET metadata=? WHERE id=?",
                       (json.dumps({"avalokita_native_pending": [actor["id"]]}), sent["event"]["id"]))
            BusStore._claim_execution_in(db, actor["id"], job_id, execution_owner=owner,
                                        native_completion_domain=domain)
            db.execute("UPDATE jobs SET state='awaiting_native_reply',started_at=?,generation=0 WHERE id=?", (time.time(), job_id))
            db.execute("UPDATE agents SET state='awaiting_native_reply',active_root=? WHERE id=?", (sent["root_id"], actor["id"]))
            db.execute("UPDATE roots SET used=1 WHERE id=?", (sent["root_id"],))
    else:
        store.claim(actor["id"])
        if owner == "native_dispatch":
            store.await_native(job_id, "Synthetic broker receipt")
    return actor, token, job_id, sent["event"]["id"], sent["root_id"]


def reserve(store, values, *, direct=False):
    actor, _, job_id, event_id, root_id = values
    if not direct:
        return store.reserve_native_input(actor["id"], job_id, event_id, root_id, actor["native_session_id"])
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        return BusStore._reserve_native_input_in(db, actor["id"], job_id, event_id, root_id,
                                                actor["native_session_id"], native_completion_domain="avalokita_direct")


def complete(store, values, text="Exact synthetic selected result"):
    actor, _, job_id, event_id, _ = values
    return store.native_result(actor["id"], actor["native_session_id"], text, job_id, event_id)


def test_native_claim_records_domain_and_manual_managed_keep_null(store):
    for owner in ("native_dispatch", "manual_inbox", "managed_runtime"):
        values = work(store, owner=owner)
        job = row(store, values[2])
        assert job["native_completion_domain"] == ("broker_native" if owner == "native_dispatch" else None)


@pytest.mark.parametrize("domain", [None, "reason", "codex_ipc", "unknown"])
def test_internal_native_claim_requires_explicit_valid_domain(store, domain):
    actor, _ = store.register("claude", mode="native_attached", native_session_id="synthetic-native")
    sent = store.send("user", actor["id"], "Synthetic task", "task")
    before = snapshot(store)
    with pytest.raises(BusError), store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        BusStore._claim_execution_in(db, actor["id"], sent["jobs"][0]["id"],
                                    execution_owner="native_dispatch", native_completion_domain=domain)
    assert snapshot(store) == before


@pytest.mark.parametrize("replacement", [None, "native_origin", "avalokita_direct", "invalid"])
def test_nonnull_domain_cannot_change_or_clear_even_by_sql(store, replacement):
    values = work(store)
    before = snapshot(store)
    with pytest.raises(sqlite3.IntegrityError), store.connection() as db:
        db.execute("UPDATE jobs SET native_completion_domain=? WHERE id=?", (replacement, values[2]))
    assert snapshot(store) == before


def test_schema_rejects_invalid_insert_and_owner_domain_contradiction(store):
    for owner, domain in (("native_dispatch", "reason"), ("manual_inbox", "broker_native"), ("managed_runtime", "avalokita_direct")):
        with pytest.raises(sqlite3.IntegrityError), store.connection() as db:
            db.execute("INSERT INTO jobs(id,event_id,agent_id,root_id,state,created_at,execution_owner,native_completion_domain) VALUES('bad','e','a','r','queued',0,?,?)", (owner, domain))
    values = work(store)
    with pytest.raises(sqlite3.IntegrityError), store.connection() as db:
        db.execute("UPDATE jobs SET execution_owner='managed_runtime' WHERE id=?", (values[2],))


def test_owned_legacy_null_is_not_adopted_or_backfilled(store):
    actor, _ = store.register("claude", mode="native_attached", native_session_id="synthetic-native")
    sent = store.send("user", actor["id"], "Preserved legacy input", "task")
    job_id = sent["jobs"][0]["id"]
    with store.connection() as db:
        db.execute("UPDATE jobs SET execution_owner='native_dispatch' WHERE id=?", (job_id,))
    before = snapshot(store)
    with pytest.raises(BusError), store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        BusStore._claim_execution_in(db, actor["id"], job_id, execution_owner="native_dispatch",
                                    native_completion_domain="broker_native", allow_existing=True)
    with pytest.raises(sqlite3.IntegrityError), store.connection() as db:
        db.execute("UPDATE jobs SET native_completion_domain='broker_native' WHERE id=?", (job_id,))
    assert store.claim(actor["id"]) is None
    assert snapshot(store) == before


def test_legacy_native_null_cannot_reserve_or_complete(store):
    actor, _ = store.register("claude", mode="native_attached", native_session_id="synthetic-legacy")
    sent = store.send("user", actor["id"], "Preserved unknown native input", "task", budget=1)
    values = actor, "unused", sent["jobs"][0]["id"], sent["event"]["id"], sent["root_id"]
    with store.connection() as db:
        db.execute("UPDATE jobs SET execution_owner='native_dispatch',state='awaiting_native_reply',started_at=?,generation=0,native_input_reserved_at=? WHERE id=?",
                   (time.time(), time.time(), values[2]))
        db.execute("UPDATE agents SET active_root=?,state='awaiting_native_reply' WHERE id=?", (values[4], actor["id"]))
        db.execute("UPDATE roots SET used=1 WHERE id=?", (values[4],))
    before = snapshot(store)
    with pytest.raises(BusError):
        complete(store, values)
    with pytest.raises(BusError):
        reserve(store, values)
    assert snapshot(store) == before


def test_first_owner_domain_race_has_one_writer_winner(store):
    actor, _ = store.register("claude", mode="native_attached", native_session_id="synthetic-race")
    sent = store.send("user", actor["id"], "One synthetic owner", "task")
    job_id = sent["jobs"][0]["id"]
    barrier = threading.Barrier(2)

    def claim_domain(domain):
        barrier.wait(3)
        try:
            with store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                BusStore._claim_execution_in(db, actor["id"], job_id, execution_owner="native_dispatch",
                                            native_completion_domain=domain)
            return domain
        except BusError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim_domain, ("broker_native", "avalokita_direct")))
    winners = [domain for domain in results if domain is not None]
    assert len(winners) == 1
    assert row(store, job_id)["native_completion_domain"] == winners[0]
    assert row(store, job_id)["native_input_reserved_at"] is None


def test_serialized_concurrent_migration_preserves_owned_legacy_null(store):
    values = work(store, owner="manual_inbox")
    with store.connection() as db:
        db.execute("DROP TRIGGER jobs_native_completion_insert")
        db.execute("DROP TRIGGER jobs_native_completion_update")
        db.execute("ALTER TABLE jobs DROP COLUMN native_completion_domain")
    barrier = threading.Barrier(4)

    def reopen(_):
        barrier.wait(3)
        opened = BusStore(store.workspace, timeout=3)
        return row(opened, values[2])

    with ThreadPoolExecutor(max_workers=4) as pool:
        observed = list(pool.map(reopen, range(4)))
    assert all(job["native_completion_domain"] is None and job["execution_owner"] == "manual_inbox" for job in observed)
    assert all(job["started_at"] == observed[0]["started_at"] for job in observed)


@pytest.mark.parametrize("domain", ["broker_native", "avalokita_direct"])
def test_public_broker_and_internal_direct_reservations_are_separate(store, domain):
    values = work(store, domain)
    before = snapshot(store)
    if domain == "avalokita_direct":
        with pytest.raises(BusError):
            reserve(store, values)
    else:
        with pytest.raises(BusError):
            reserve(store, values, direct=True)
    assert snapshot(store) == before
    reserve(store, values, direct=domain == "avalokita_direct")
    assert row(store, values[2])["native_input_reserved_at"] is not None


@pytest.mark.parametrize("marker", ["avalokita_native_pending", "native_ipc_ids", "avalokita_peer_dispatch", "canonical_dispatch", "canonical_peer"])
def test_broker_domain_direct_marker_conflict_denies_input_and_success(store, marker):
    values = work(store)
    actor, _, job_id, event_id, root_id = values
    reserve(store, values)
    with store.connection() as db:
        if marker.startswith("canonical_"):
            table = "avalokita_native_dispatch" if marker == "canonical_dispatch" else "avalokita_native_peers"
            actor_column = "actor" if marker == "canonical_dispatch" else "recipient"
            db.execute(f"CREATE TABLE {table}(job_id TEXT,event_id TEXT,root_id TEXT,{actor_column} TEXT)")
            db.execute(f"INSERT INTO {table} VALUES(?,?,?,?)", (job_id, event_id, root_id, actor["id"]))
        else:
            metadata = {marker: "exact-peer" if marker == "avalokita_peer_dispatch" else [actor["id"]]}
            db.execute("UPDATE events SET metadata=? WHERE id=?", (json.dumps(metadata), event_id))
    before = snapshot(store)
    with pytest.raises(BusError):
        complete(store, values)
    with pytest.raises(BusError), store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        BusStore._validate_native_input_in(db, actor["id"], job_id, event_id, root_id,
                                          actor["native_session_id"], require_reserved=True)
    assert snapshot(store) == before


@pytest.mark.parametrize("verified", [False, True])
def test_native_origin_completes_without_new_input_or_channel_proof(store, verified):
    actor, _ = store.register("claude", mode="native_attached", native_session_id="synthetic-origin")
    store.update_agent(actor["id"], channel_verified=verified)
    sent = store.send("user", "all", "Already present human turn", "task", native_origin=actor["id"], budget=1)
    values = actor, "unused", sent["origin_job_id"], sent["event"]["id"], sent["root_id"]
    assert row(store, values[2])["native_completion_domain"] == "native_origin"
    original_reservation = row(store, values[2])["native_input_reserved_at"]
    with pytest.raises(BusError):
        reserve(store, values)
    assert complete(store, values)["completed"] is True
    assert row(store, values[2])["native_input_reserved_at"] == original_reservation
    assert store.agent(actor["id"])["channel_verified"] is verified


@pytest.mark.parametrize("marker", ["avalokita_native_pending", "native_ipc_ids", "avalokita_peer_dispatch"])
def test_native_origin_with_conflicting_direct_marker_cannot_complete(store, marker):
    actor, _ = store.register("claude", mode="native_attached", native_session_id="synthetic-origin")
    sent = store.send("user", "all", "Already present human turn", "task", native_origin=actor["id"], budget=1)
    values = actor, "unused", sent["origin_job_id"], sent["event"]["id"], sent["root_id"]
    with store.connection() as db:
        metadata = {"native_origin": actor["id"], marker: "exact-peer" if marker == "avalokita_peer_dispatch" else [actor["id"]]}
        db.execute("UPDATE events SET metadata=? WHERE id=?", (json.dumps(metadata), sent["event"]["id"]))
    before = snapshot(store)
    with pytest.raises(BusError):
        complete(store, values)
    assert snapshot(store) == before


def test_root_direct_marker_for_another_actor_preserves_broker_success(store):
    values = work(store)
    with store.connection() as db:
        db.execute("UPDATE events SET metadata=? WHERE id=?",
                   (json.dumps({"avalokita_native_pending": ["unrelated-synthetic-actor"]}), values[3]))
    reserve(store, values)
    assert complete(store, values)["completed"] is True


def test_workspace_retirement_denies_handler_invocation(store):
    from store import WorkspaceRetired
    values = work(store, "avalokita_direct")
    unregister, calls = install_fake_kernel(store, values)
    store._workspace_guard = lambda: (_ for _ in ()).throw(WorkspaceRetired())
    with pytest.raises(WorkspaceRetired):
        complete(store, values)
    assert not calls
    unregister()


def test_generic_store_finish_and_native_result_cannot_complete_direct(store):
    values = work(store, "avalokita_direct")
    actor, _, job_id, event_id, _ = values
    reserve(store, values, direct=True)
    before = snapshot(store)
    for operation in (lambda: store.finish(job_id, "completed"),
                      lambda: store.finish(job_id, "completed", expected_native=(actor["id"], event_id, actor["native_session_id"])),
                      lambda: complete(store, values)):
        with pytest.raises(BusError):
            operation()
        assert snapshot(store) == before
    with pytest.raises(BusError), store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        store._finish_in(db, job_id, "completed", expected_native=(actor["id"], event_id, actor["native_session_id"]))
    assert snapshot(store) == before


def test_broker_success_is_idempotent_and_cannot_replace_native_binding(store):
    values = work(store)
    actor, _, job_id, event_id, _ = values
    reserve(store, values)
    before = snapshot(store)
    with pytest.raises(BusError):
        store.finish(job_id, "completed", native_session_id="another-native-session",
                     expected_native=(actor["id"], event_id, actor["native_session_id"]))
    assert snapshot(store) == before
    assert complete(store, values)["completed"] is True
    after = snapshot(store)
    assert complete(store, values)["duplicate_suppressed"] is True
    with pytest.raises(BusError):
        complete(store, values, "Conflicting final")
    assert snapshot(store) == after


def install_fake_kernel(store, values):
    calls = []

    def kernel(**payload):
        calls.append(payload)
        actor, _, job_id, event_id, root_id = values
        assert payload == dict(actor=actor["id"], native_session_id=actor["native_session_id"],
                               text=payload["text"], job_id=job_id, event_id=event_id)
        # A separate guarded writer succeeds only if native_result closed its
        # own connection before calling this owned synthetic kernel.
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            assert BusStore._native_completion_domain_in(db, job) == "avalokita_direct"
            if job["state"] == "completed":
                prior = db.execute("SELECT body FROM events WHERE kind='result' AND json_extract(metadata,'$.job_id')=?", (job_id,)).fetchone()
                if prior[0] != payload["text"]:
                    raise BusError("Conflicting selected result", 409)
                return {"completed": True, "job_id": job_id, "duplicate_suppressed": True}
            db.execute("UPDATE jobs SET state='completed',finished_at=? WHERE id=?", (time.time(), job_id))
            db.execute("UPDATE agents SET active_root=NULL,state='idle' WHERE id=?", (actor["id"],))
            store._insert_event(db, "result", actor["id"], "user", payload["text"], root_id, {"job_id": job_id, "state": "completed"})
        return {"completed": True, "job_id": job_id}

    return store.register_native_completion_handler(kernel), calls


def test_local_handler_runs_outside_writer_and_unregister_is_exact(store):
    values = work(store, "avalokita_direct")
    unregister, calls = install_fake_kernel(store, values)
    with pytest.raises(BusError):
        store.register_native_completion_handler(lambda **payload: {})
    assert complete(store, values)["completed"] is True
    after = snapshot(store)
    assert complete(store, values)["duplicate_suppressed"] is True
    assert snapshot(store) == after
    with pytest.raises(BusError):
        complete(store, values, "Conflicting final")
    assert snapshot(store) == after
    unregister()
    unregister()
    with pytest.raises(BusError):
        complete(store, values)
    assert len(calls) == 3
    new_unregister, _ = install_fake_kernel(store, values)
    unregister()
    assert complete(store, values)["completed"] is True
    new_unregister()


@contextmanager
def http_room(store):
    from daemon import BusHTTPServer
    server = BusHTTPServer(store.workspace, store=store, providers="none", enable_native=False, discovery=[], hosted_only=True)
    (store.private / "runtime.json").write_text(json.dumps(server.runtime()), encoding="utf-8")
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), name="completion-r4-http")
    thread.start()

    def request(path, payload, token):
        value = Request(server.url + path, data=json.dumps(payload).encode(), headers={
            "Content-Type": "application/json", "X-Sharebus-Request": "1", "Authorization": "Bearer " + token})
        try:
            with urlopen(value, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)
    try:
        yield server, request
    finally:
        server.shutdown()
        thread.join(3)
        server.close()
        assert not thread.is_alive()


def test_http_handler_absence_auth_schema_idempotency_and_closing(store):
    with http_room(store) as (server, request):
        values = work(store, "avalokita_direct")
        actor, token, job_id, event_id, _ = values
        body = dict(native_session_id=actor["native_session_id"], job_id=job_id,
                    event_id=event_id, last_assistant_message="Exact synthetic selected result")
        before = snapshot(store)
        assert request("/api/native/result", body, token)[0] == 409
        assert snapshot(store) == before
        unregister, calls = install_fake_kernel(store, values)
        assert request("/api/native/result", body, store.operator_token)[0] == 403
        for extra in ({"native_completion_domain": "broker_native"}, {"verified": True}):
            assert request("/api/native/result", {**body, **extra}, token)[0] == 400
        assert not calls and snapshot(store) == before
        assert request("/api/native/result", body, token)[0] == 200
        after = snapshot(store)
        assert request("/api/native/result", body, token)[1]["duplicate_suppressed"] is True
        assert request("/api/native/result", {**body, "last_assistant_message": "Conflicting final"}, token)[0] == 409
        assert snapshot(store) == after
        server.stopping = True
        assert request("/api/native/result", body, token)[0] == 409
        unregister()


def test_mcp_exact_completion_reaches_canonical_handler_during_pause(store, monkeypatch):
    import mcp_server
    with http_room(store) as (_, _):
        values = work(store, "avalokita_direct")
        actor, token, job_id, event_id, _ = values
        unregister, calls = install_fake_kernel(store, values)
        store.set_paused(True)
        state = {"paused": True, "own_agent": store.agent(actor["id"])}
        monkeypatch.setattr(mcp_server.BusClient, "authenticated", lambda *args, **kwargs: (store.workspace, token, state))
        monkeypatch.setattr(mcp_server, "_host_metadata", lambda context: {})
        server = mcp_server.build_server("claude", store.workspace)
        result = asyncio.run(server._tool_manager.call_tool("sharebus_send", dict(to="user", kind="result",
            body="Exact synthetic selected result", job_id=job_id, event_id=event_id,
            native_session_id=actor["native_session_id"]), context=server.get_context()))
        assert not result.isError and len(calls) == 1
        assert store.paused() is True
        unregister()


def test_real_mcp_stdio_direct_handler_and_standalone_denial(store):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    with http_room(store) as (_, _):
        values = work(store, "avalokita_direct")
        actor, token, job_id, event_id, _ = values
        body = dict(to="user", kind="result", body="Exact synthetic selected result", job_id=job_id,
                    event_id=event_id, native_session_id=actor["native_session_id"], session_token=token)

        async def exercise():
            parameters = StdioServerParameters(command=sys.executable, args=["-B", str(HERE / "mcp_server.py"),
                "--provider", "claude", "--workspace", str(store.workspace)])
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    assert (await session.call_tool("sharebus_send", body)).isError
                    unregister, calls = install_fake_kernel(store, values)
                    try:
                        assert not (await session.call_tool("sharebus_send", body)).isError
                        after = snapshot(store)
                        assert not (await session.call_tool("sharebus_send", body)).isError
                        assert (await session.call_tool("sharebus_send", {**body, "body": "Conflicting final"})).isError
                        assert snapshot(store) == after and len(calls) == 3
                    finally:
                        unregister()
        asyncio.run(exercise())
