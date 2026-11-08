"""Pinned Reason native transport, with exact bindings and one durable attempt.

This uses the fork's local ``avalokita-deliver`` protocol, not the upstream
daemon/MCP/mail network. The caller retains the human goal/job/parent authority
and supplies a live durable control gate. A queue/socket receipt is not a model
response. Construction never discovers, configures, or starts a native agent.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
from queue import Queue
import sqlite3
import subprocess
from threading import Thread
import time
from typing import Any

CONTRACT = "avalokita.reason/1"
QUEUE_CONTRACT = "avalokita.codex-queue/1"
UPSTREAM_REVISION = "b3557ff454cd26a04c2a18804ba83ae5db8b119d"
MAX_FRAME = 96 * 1024


class ReasonEngineError(ValueError):
    """Redacted contract/authority failure; never includes native output."""

    def __init__(self, message: str, *, reason_code: str = "native_unavailable_or_uncertain"):
        super().__init__(message)
        self.reason_code = reason_code


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _hash(value: bytes | str) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


@dataclass(frozen=True)
class NativeBinding:
    provider: str
    native_id: str
    workspace: str
    registry_path: str | None = None
    pid: int | None = None
    started_at_ms: int | None = None
    socket_path: str | None = None
    rollout_path: str | None = None
    executable: str | None = None
    executable_sha256: str | None = None

    @property
    def sha256(self) -> str:
        return _hash(_json(asdict(self)))


def _plain(path: Path, *, require_file: bool = False) -> None:
    if not path.is_absolute() or not path.parent.is_dir():
        raise ReasonEngineError("An existing absolute private path is required")
    for part in (path, *path.parents):
        try:
            stat = part.lstat()
        except FileNotFoundError:
            if part == path and not require_file:
                continue
            raise ReasonEngineError("Pinned path is unavailable") from None
        if part.is_symlink() or getattr(stat, "st_file_attributes", 0) & 0x400:
            raise ReasonEngineError("Linked paths are not permitted")
    if require_file and (not path.is_file() or path.stat().st_nlink != 1):
        raise ReasonEngineError("An unlinked regular file is required")


def _bounded_io(operation: Callable[[], Any], deadline: float) -> Any:
    """Bound blocking stdio without ever retrying the operation."""
    result: Queue = Queue(maxsize=1)

    def run():
        try:
            result.put((True, operation()))
        except Exception:
            result.put((False, None))

    worker = Thread(target=run, daemon=True)
    worker.start()
    worker.join(max(0, deadline - time.monotonic()))
    if worker.is_alive():
        raise TimeoutError
    success, value = result.get_nowait()
    if not success:
        raise ReasonEngineError("Reason stdio is unavailable")
    return value


class ReasonEngineAdapter:
    def __init__(self, *, engine_path: Path | str, engine_sha256: str, ledger_path: Path | str,
                 authorize_send: Callable[[Mapping[str, Any]], bool] | None = None,
                 timeout: float = 30, process_factory=None):
        self.engine_path = Path(engine_path)
        self.engine_sha256 = engine_sha256
        self.ledger_path = Path(ledger_path)
        self.authorize_send = authorize_send
        self.timeout = timeout
        self.process_factory = process_factory or subprocess.Popen

    @contextmanager
    def _ledger(self):
        _plain(self.ledger_path)
        if self.ledger_path.exists():
            _plain(self.ledger_path, require_file=True)
        db = sqlite3.connect(self.ledger_path, timeout=3)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE IF NOT EXISTS avalokita_reason_dispatch (dispatch_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, binding_key TEXT NOT NULL, state TEXT NOT NULL, receipt TEXT NOT NULL)")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _identifier(value):
        if not isinstance(value, str) or not 1 <= len(value) <= 200 or any(ord(c) < 32 for c in value):
            raise ReasonEngineError("An exact bounded identity is required")

    def _authorize(self, record, expected):
        if (not isinstance(record, Mapping) or any(record.get(k) != v for k, v in expected.items())
                or record.get("human_authorized") is not True or record.get("root_active") is not True
                or record.get("recipient_registered") is not True or record.get("workspace_paused") is not False
                or record.get("recipient_paused") is not False or record.get("claim_state") != "claimed"
                or type(record.get("budget_remaining")) is not int or record["budget_remaining"] <= 0
                or expected["deadline"] <= time.time() or self.authorize_send is None
                or self.authorize_send(record) is not True):
            raise ReasonEngineError("Exact active durable human authority is required")
        for key in ("goal_id", "input_id"):
            self._identifier(record.get(key))

    @staticmethod
    def _validate_queue_receipt(value, receipt, binding):
        allowed = {"contract", "queued_submission_id", "native_id", "selected_input_sha256",
                   "engine_request_sha256", "authorization_sha256", "deadline_unix_ms",
                   "executable_sha256", "stdout_sha256"}
        if (not isinstance(value, dict) or set(value) != allowed or value.get("contract") != QUEUE_CONTRACT
                or not isinstance(binding, NativeBinding) or binding.provider != "codex"
                or receipt.get("binding_sha256") != binding.sha256 or receipt.get("native_input_attempted") is not True):
            raise ReasonEngineError("Codex queue receipt is invalid")
        for key in ("queued_submission_id", "native_id"):
            identifier = value[key]
            if (not isinstance(identifier, str) or not 1 <= len(identifier.encode("utf-8")) <= 512
                    or any(c.isspace() or ord(c) < 32 or 0x7f <= ord(c) <= 0x9f for c in identifier)):
                raise ReasonEngineError("Codex queue identity is invalid")
        for key in ("selected_input_sha256", "engine_request_sha256", "authorization_sha256",
                    "executable_sha256", "stdout_sha256"):
            if not isinstance(value[key], str) or len(value[key]) != 64 or any(c not in "0123456789abcdef" for c in value[key]):
                raise ReasonEngineError("Codex queue digest is invalid")
        expected = dict(native_id=binding.native_id, selected_input_sha256=receipt.get("prompt_sha256"),
                        engine_request_sha256=receipt.get("engine_request_sha256"),
                        authorization_sha256=receipt.get("authorization_sha256"),
                        deadline_unix_ms=receipt.get("deadline_unix_ms"), executable_sha256=binding.executable_sha256)
        if (type(value["deadline_unix_ms"]) is not int or not 0 < value["deadline_unix_ms"] < 2**63
                or any(value[key] != item for key, item in expected.items())):
            raise ReasonEngineError("Codex queue receipt changed the submitted input")
        return dict(value)

    def _queue_receipt(self, db, receipt, binding):
        """Read optional immutable facts only when the original attempt anchors them.

        Old engines have no queue table. Neither stored public fields nor final
        stdout alone can supply queue facts, including after an observation won
        the race with the engine's last transport write.
        """
        try:
            if db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN (?,?)",
                          ("avalokita_reason_queue_receipts", "avalokita_reason_attempts")).fetchone()[0] != 2:
                return None
            row = db.execute("""SELECT q.fingerprint,q.binding_key,q.native_id,q.queued_submission_id,
                q.selected_input_sha256,q.authorization_sha256,q.deadline_unix_ms,q.receipt,
                a.fingerprint,a.binding_key,a.receipt FROM avalokita_reason_queue_receipts q
                JOIN avalokita_reason_attempts a ON a.dispatch_id=q.dispatch_id WHERE q.dispatch_id=?""",
                (receipt["dispatch_id"],)).fetchone()
            if not row:
                return None
            value = self._validate_queue_receipt(json.loads(row[7]), receipt, binding)
            attempt = json.loads(row[10])
            if (row[0] != row[8] or row[1] != row[9] or not isinstance(row[1], str) or len(row[1]) != 64
                    or any(c not in "0123456789abcdef" for c in row[1])
                    or tuple(row[index] for index in (0, 2, 3, 4, 5, 6)) != tuple(value[key] for key in (
                        "engine_request_sha256", "native_id", "queued_submission_id", "selected_input_sha256",
                        "authorization_sha256", "deadline_unix_ms"))
                    or not isinstance(attempt, dict) or attempt.get("dispatch_id") != receipt["dispatch_id"]
                    or attempt.get("root_id") != receipt["root_id"] or attempt.get("native_input_attempted") is not True):
                return None
            return value
        except (ReasonEngineError, ValueError, TypeError, KeyError, sqlite3.Error):
            return None

    def _remember(self, dispatch, fingerprint, receipt, *, binding=None):
        with self._ledger() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT receipt FROM avalokita_reason_dispatch WHERE dispatch_id=? AND fingerprint=?",
                                  (dispatch, fingerprint)).fetchone()
            if previous:
                previous = json.loads(previous[0])
                if previous.get("native_observation"):
                    receipt = {**receipt, **{key: previous[key] for key in (
                        "state", "native_accepted", "native_result_verified", "native_observation")}}
            receipt.pop("queue_receipt", None)
            queued = self._queue_receipt(db, receipt, binding)
            if queued is not None:
                receipt["queue_receipt"] = queued
            db.execute("UPDATE avalokita_reason_dispatch SET state=?,receipt=? WHERE dispatch_id=? AND fingerprint=?",
                       (receipt["state"], json.dumps(receipt), dispatch, fingerprint))
        return receipt

    def reconcile_native_observation(self, dispatch_id: str, root_id: str, binding: NativeBinding,
                                     observation: Mapping[str, Any], *, validate_observation: Callable[[Mapping[str, Any]], bool]):
        """Settle only an externally authenticated exact input/result; never send.

        A peer tool call proves input processing, not a finished model turn.
        The trusted callback verifies the source host and original causal input
        from the product ledger, including after pause/expiry. It is mandatory.
        """
        for value in (dispatch_id, root_id):
            self._identifier(value)
        allowed = {"kind", "actor_id", "goal_id", "input_id", "selected_response_sha256"}
        if (not isinstance(binding, NativeBinding) or not isinstance(observation, Mapping)
                or set(observation) != allowed or not callable(validate_observation)):
            raise ReasonEngineError("An exact trusted native observation is required")
        observation = json.loads(_json(dict(observation)))
        for key in ("actor_id", "goal_id", "input_id"):
            self._identifier(observation.get(key))
        response_hash = observation.get("selected_response_sha256")
        if (observation.get("kind") not in {"input_processed", "result_completed"}
                or not isinstance(response_hash, str) or len(response_hash) != 64
                or any(c not in "0123456789abcdef" for c in response_hash)):
            raise ReasonEngineError("Native observation metadata is invalid")
        with self._ledger() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT receipt FROM avalokita_reason_dispatch WHERE dispatch_id=?", (dispatch_id,)).fetchone()
            if not row:
                raise ReasonEngineError("The original native input does not exist")
            receipt = json.loads(row[0])
            if (receipt.get("root_id") != root_id or receipt.get("binding_sha256") != binding.sha256
                    or receipt.get("target_actor_id") != observation["actor_id"]
                    or receipt.get("goal_id") != observation["goal_id"] or receipt.get("input_id") != observation["input_id"]
                    or receipt.get("native_input_attempted") is not True or not receipt.get("engine_request_sha256")):
                raise ReasonEngineError("Observation does not identify the original exact native input")
            engine = db.execute("SELECT fingerprint,receipt FROM avalokita_reason_attempts WHERE dispatch_id=?", (dispatch_id,)).fetchone()
            if not engine or engine[0] != receipt["engine_request_sha256"]:
                raise ReasonEngineError("Observation does not match the durable engine input attempt")
            engine_receipt = json.loads(engine[1])
            if (engine_receipt.get("root_id") != root_id or engine_receipt.get("dispatch_id") != dispatch_id
                    or engine_receipt.get("native_input_attempted") is not True or validate_observation(observation) is not True):
                raise ReasonEngineError("Native observation was not authenticated by the original host")
            result_completed = receipt.get("native_result_verified") is True or observation["kind"] == "result_completed"
            settled = {"state": "native_result_verified" if result_completed else "native_input_verified",
                       "native_accepted": True, "native_result_verified": result_completed,
                       "native_observation": receipt.get("native_observation") if receipt.get("native_result_verified") is True else observation}
            # Native callbacks may beat the engine's final socket/queue receipt.
            # A later Rust transport write must not downgrade authenticated
            # observation. Restoration is atomic within the same SQLite write.
            db.execute("""CREATE TRIGGER IF NOT EXISTS avalokita_reason_observation_monotonic
                AFTER UPDATE ON avalokita_reason_attempts
                WHEN (OLD.state='native_result_verified' AND NEW.state!='native_result_verified')
                    OR (OLD.state='native_input_verified' AND NEW.state NOT IN ('native_input_verified','native_result_verified'))
                BEGIN UPDATE avalokita_reason_attempts SET state=OLD.state,receipt=OLD.receipt
                    WHERE dispatch_id=OLD.dispatch_id; END""")
            receipt.update(settled)
            engine_receipt.update(settled)
            queued = self._queue_receipt(db, receipt, binding)
            for saved in (receipt, engine_receipt):
                saved.pop("queue_receipt", None)
                if queued is not None:
                    saved["queue_receipt"] = queued
            db.execute("UPDATE avalokita_reason_dispatch SET state=?,receipt=? WHERE dispatch_id=?",
                       (receipt["state"], json.dumps(receipt), dispatch_id))
            db.execute("UPDATE avalokita_reason_attempts SET state=?,receipt=? WHERE dispatch_id=? AND fingerprint=?",
                       (engine_receipt["state"], json.dumps(engine_receipt), dispatch_id, engine[0]))
        return receipt

    def send_authorized(self, binding: NativeBinding, prompt: str, *, source_actor_id: str,
                        target_actor_id: str, root_id: str, dispatch_id: str, deadline: float,
                        authorization_record: Mapping[str, Any]) -> dict[str, Any]:
        """Submit once to an exact original conversation; never guess, resume, or fall back.

        The trusted gate must re-read current goal/job/parent, exact actor/binding,
        pause/stop/deadline and reserved budget. Static booleans are insufficient.
        Parent causal IDs may be included in the record and are pinned by its hash.
        """
        if not isinstance(binding, NativeBinding) or binding.provider not in {"claude-code", "codex"}:
            raise ReasonEngineError("A supported exact native binding is required")
        for value in (binding.native_id, source_actor_id, target_actor_id, root_id, dispatch_id):
            self._identifier(value)
        if not isinstance(prompt, str) or not 1 <= len(prompt.encode("utf-8")) <= 65536:
            raise ReasonEngineError("A bounded native prompt is required")
        if (type(deadline) not in {int, float} or not math.isfinite(deadline)
                or type(self.timeout) not in {int, float} or not math.isfinite(self.timeout) or not 0 < self.timeout <= 60):
            raise ReasonEngineError("A bounded finite deadline is required")
        _plain(Path(binding.workspace))
        if not isinstance(authorization_record, Mapping):
            raise ReasonEngineError("A durable authorization record is required")
        # Freeze causal metadata as well as the explicit routing fields. A
        # mutable caller record cannot change parent/job authority after ready.
        authorization_record = json.loads(_json(dict(authorization_record)))
        expected = dict(source_actor_id=source_actor_id, target_actor_id=target_actor_id, root_id=root_id,
                        dispatch_id=dispatch_id, binding_sha256=binding.sha256, prompt_sha256=_hash(prompt), deadline=deadline)
        self._authorize(authorization_record, expected)
        request = dict(contract=CONTRACT, dispatch_id=dispatch_id, source_actor_id=source_actor_id,
                       target_actor_id=target_actor_id, root_id=root_id, deadline_unix_ms=int(deadline * 1000),
                       authorization_sha256=_hash(_json(dict(authorization_record))), operation_timeout_ms=int(self.timeout * 1000),
                       ledger_path=str(self.ledger_path), binding=asdict(binding), prompt=prompt)
        # The outer identity also pins causal metadata from the caller's durable gate.
        fingerprint = _hash(_json({"request": {key: value for key, value in request.items() if key != "operation_timeout_ms"},
                                   "authority": dict(authorization_record)}))
        binding_key = _hash(_json([binding.provider, binding.native_id, os.path.normcase(str(Path(binding.workspace).resolve()))]))
        receipt = dict(state="prepared", native_input_attempted=False, transport_accepted=False,
                       native_accepted=False, native_result_verified=False, automatic_retry=False,
                       fallback_attempted=False, dispatch_id=dispatch_id, root_id=root_id,
                       binding_sha256=binding.sha256, prompt_sha256=_hash(prompt),
                        authorization_sha256=request["authorization_sha256"], deadline_unix_ms=request["deadline_unix_ms"],
                       source_actor_id=source_actor_id, target_actor_id=target_actor_id,
                       goal_id=authorization_record["goal_id"], input_id=authorization_record["input_id"],
                       engine_sha256=self.engine_sha256, upstream_revision=UPSTREAM_REVISION)
        with self._ledger() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT fingerprint,receipt FROM avalokita_reason_dispatch WHERE dispatch_id=?", (dispatch_id,)).fetchone()
            if prior:
                if prior[0] != fingerprint:
                    raise ReasonEngineError("Dispatch identity was reused with different content or authority")
                previous = json.loads(prior[1])
                previous.pop("queue_receipt", None)
                queued = self._queue_receipt(db, previous, binding)
                if queued is not None:
                    previous["queue_receipt"] = queued
                db.execute("UPDATE avalokita_reason_dispatch SET receipt=? WHERE dispatch_id=? AND fingerprint=?",
                           (json.dumps(previous), dispatch_id, fingerprint))
                return {**previous, "duplicate_suppressed": True}
            if db.execute("SELECT 1 FROM avalokita_reason_dispatch WHERE binding_key=? AND state IN ('prepared','delivery_unknown')",
                          (binding_key,)).fetchone():
                raise ReasonEngineError("An unresolved previous native input prevents a new dispatch")
            db.execute("INSERT INTO avalokita_reason_dispatch VALUES (?,?,?,?,?)",
                       (dispatch_id, fingerprint, binding_key, receipt["state"], json.dumps(receipt)))
        process = None
        committed = False
        io_deadline = time.monotonic() + min(self.timeout, max(0, deadline - time.time()))
        try:
            if os.name == "nt" and binding.provider == "codex":
                args = ["queue", "--thread", binding.native_id, "--message", prompt]
                exe = binding.executable or ""
                if "\0" in exe or any("\0" in arg for arg in args):
                    raise ReasonEngineError("Native arguments cannot contain NUL", reason_code="invalid_native_argument")
                # Rust's argv[0] is always quoted. Remaining arguments use
                # Python's documented Microsoft CRT quoting, not POSIX shlex.
                command = '"' + exe + '" ' + subprocess.list2cmdline(args)
                if len(command.encode("utf-16-le")) // 2 + 1 > 32767:
                    raise ReasonEngineError("Native command line exceeds its UTF-16 bound", reason_code="native_command_line_limit")
            _plain(self.engine_path, require_file=True)
            with self.engine_path.open("rb") as stream:
                engine_hash = hashlib.file_digest(stream, "sha256").hexdigest()
            if engine_hash != self.engine_sha256:
                raise ReasonEngineError("Reason executable pin changed")
            environment = os.environ.copy()
            for key in ("CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_SESSION_ID", "CLAUDE_PID", "CODEX_THREAD_ID"):
                environment.pop(key, None)
            process = self.process_factory([str(self.engine_path), "avalokita-deliver"], cwd=binding.workspace,
                env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

            def send(value):
                payload = _json(value) + b"\n"
                if len(payload) > MAX_FRAME:
                    raise ReasonEngineError("Reason protocol frame exceeds its bound")
                process.stdin.write(payload)
                process.stdin.flush()

            def receive():
                payload = process.stdout.readline(MAX_FRAME + 1)
                if not payload.endswith(b"\n") or len(payload) > MAX_FRAME:
                    raise ReasonEngineError("Reason protocol frame is invalid")
                value = json.loads(payload)
                if (not isinstance(value, dict) or value.get("contract") != CONTRACT
                        or value.get("dispatch_id") != dispatch_id or value.get("root_id") != root_id):
                    raise ReasonEngineError("Reason protocol identity changed")
                return value

            _bounded_io(lambda: send(request), io_deadline)
            ready = _bounded_io(receive, io_deadline)
            if ready.get("type") == "ready":
                request_hash = ready.get("request_sha256")
                if (ready.get("target_actor_id") != target_actor_id or ready.get("native_id") != binding.native_id
                        or not isinstance(request_hash, str) or len(request_hash) != 64
                        or any(c not in "0123456789abcdef" for c in request_hash)):
                    raise ReasonEngineError("Reason ready frame changed the exact target")
                # Called only after the engine has opened/validated the selected
                # target, immediately before it records and submits native input.
                self._authorize(authorization_record, expected)
                receipt.update(state="delivery_unknown", native_input_attempted=True)
                receipt["engine_request_sha256"] = request_hash
                self._remember(dispatch_id, fingerprint, receipt, binding=binding)
                committed = True
                _bounded_io(lambda: send(dict(contract=CONTRACT, dispatch_id=dispatch_id,
                                             request_sha256=request_hash, authorize=True)), io_deadline)
                final = _bounded_io(receive, io_deadline)
            else:
                final = ready
            if (final.get("type") != "receipt" or final.get("state") not in {"not_sent", "delivery_unknown", "transport_accepted"}
                    or type(final.get("native_input_attempted")) is not bool
                    or final.get("native_accepted") is not False or final.get("native_result_verified") is not False
                    or final.get("automatic_retry") is not False or final.get("fallback_attempted") is not False
                    or (not committed and final.get("native_input_attempted") and final.get("duplicate_suppressed") is not True)):
                raise ReasonEngineError("Reason delivery receipt is invalid")
            if ((final["state"] == "transport_accepted" and (final["native_input_attempted"] is not True
                    or final.get("transport_accepted") is not True
                    or final.get("transport") not in {"claude_socket_written", "codex_queue_accepted"}))
                    or (final["state"] == "delivery_unknown" and (final["native_input_attempted"] is not True
                        or final.get("transport_accepted") is not False))
                    or (final["state"] == "not_sent" and (final["native_input_attempted"] is not False
                        or final.get("transport_accepted") is not False))):
                raise ReasonEngineError("Reason delivery state and receipt disagree")
            if "queue_receipt" in final:
                queued = self._validate_queue_receipt(final["queue_receipt"], receipt, binding)
                with self._ledger() as db:
                    anchored = self._queue_receipt(db, receipt, binding)
                if (final["native_input_attempted"] is not True or queued != anchored
                        or (final["state"] == "transport_accepted" and final.get("transport") != "codex_queue_accepted")):
                    raise ReasonEngineError("Codex queue receipt is not anchored to the durable input attempt")
            receipt.update({key: final[key] for key in ("state", "native_input_attempted", "transport_accepted",
                           "native_accepted", "native_result_verified", "reason_code", "transport", "duplicate_suppressed") if key in final})
        except (ReasonEngineError, OSError, ValueError, TimeoutError) as error:
            receipt.update(state="delivery_unknown" if committed else "not_sent",
                           native_input_attempted=committed, transport_accepted=False,
                           reason_code=getattr(error, "reason_code", "native_unavailable_or_uncertain"))
        finally:
            if process is not None:
                # Only the one-shot engine process belongs to us. Existing
                # Claude/Codex processes are never terminated or restarted.
                if process.poll() is None:
                    process.kill()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        stream.close()
        receipt = self._remember(dispatch_id, fingerprint, receipt, binding=binding)
        return receipt
