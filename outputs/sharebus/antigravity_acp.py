"""Official ACP SDK bridge for one owned Antigravity subscription process.

The SDK alone frames, reads, writes and correlates JSON-RPC. The bounded byte
reader compensates for SDK 0.12.1 _transport.py's unbounded overrun accumulation.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import importlib.metadata
import json
import logging
from pathlib import Path
import subprocess
import threading
import time
from typing import Any
import uuid


class _BoundedReader(asyncio.StreamReader):
    def __init__(self, reader: asyncio.StreamReader, limit: int):
        super().__init__(limit=limit)
        self.reader, self.bound = reader, limit

    async def readuntil(self, separator: Any = b"\n") -> bytes:
        # Yield between frames so SDK callback tasks cannot accumulate unchecked.
        await asyncio.sleep(0)
        try:
            value = await self.reader.readuntil(separator)
        except asyncio.LimitOverrunError:
            raise ValueError("ACP wire frame exceeded its size limit.") from None
        if len(value) > self.bound:
            raise ValueError("ACP wire frame exceeded its size limit.")
        return value


class AntigravityACP:
    def __init__(self, adapter: Any, *, needs_human: type[RuntimeError], allowed_models: frozenset[str],
                 max_messages: int = 64, max_output_chars: int = 262144,
                 max_approval_bytes: int = 65536, max_wire_bytes: int = 1048576):
        self.adapter, self.needs_human = adapter, needs_human
        self.allowed_models = frozenset(allowed_models)
        self.max_messages, self.max_output_chars = max_messages, max_output_chars
        self.max_approval_bytes, self.max_wire_bytes = max_approval_bytes, max_wire_bytes
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._process: Any = None
        self._conn: Any = None
        self._sdk: Any = None
        self._stderr: Any = None
        self._session: str | None = None
        self._model: str | None = None
        self._model_selectors: dict[str, str] = {}
        self._configs: list[dict] = []
        self._config_epoch = 0
        self._state_epoch = 0
        self._state_id: Any = None
        self._state_response_id: Any = None
        self._phase: str | None = None
        self._last_state: dict = {}
        self._prompt_id: Any = None
        self._prompt_active = False
        self._prompt_done = True
        self._input_token: Any = None
        self._permission_identity: Any = None
        self._error: str | None = None
        self._text = ""
        self._cancelled = False
        self._cancel_sent = False
        self._sdk_log_prefix: str | None = None
        self._log_filter = self._filter_sdk_log

    def _filter_sdk_log(self, record: logging.LogRecord) -> bool:
        owner_id = self._thread.ident if self._thread else None
        if owner_id is None or record.thread != owner_id or self._sdk_log_prefix is None:
            return True
        try:
            path = Path(record.pathname).resolve().as_posix().casefold()
            if not path.startswith(self._sdk_log_prefix):
                return True
            self.adapter._reader_state.setdefault("sdk_diagnostic", "native_acp_sdk_diagnostic")
        except Exception:
            # Never fall back to exposing exception text on this owner thread.
            pass
        return False

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process and self._process.returncode is None else None

    def _owner(self) -> None:
        ready = threading.Event()
        def run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            ready.set()
            self._loop.run_forever()
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()
        self._thread = threading.Thread(target=run, name="sharebus-acp-owner", daemon=True)
        self._thread.start()
        if not ready.wait(5):
            raise RuntimeError("ACP owner loop did not start.")

    def _run(self, coroutine: Any, timeout: float) -> Any:
        if self._loop is None or not self._loop.is_running():
            coroutine.close()
            raise RuntimeError("ACP owner loop is unavailable.")
        if threading.current_thread() is self._thread:
            coroutine.close()
            raise RuntimeError("ACP synchronous operation cannot run inside its owner callback.")
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        try:
            return future.result(timeout=max(0.001, timeout))
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError("ACP response timed out.") from None
        except self.needs_human:
            raise
        except Exception as error:
            if getattr(error, "reason_code", None) == "native_observer_failed":
                raise
            raise RuntimeError(self._error or "ACP could not complete the native request.") from None

    def _fault(self, reason: str) -> None:
        self._error = self._error or reason
        self.adapter._reader_state["error"] = self._error
        self.adapter._needs_human = True
        if self._process and self._process.returncode is None:
            try:
                self._process.terminate()
            except ProcessLookupError:
                pass

    def _observe(self, event: Any) -> None:
        # Official SDK observer: retain correlation and selected public metadata,
        # never a raw event, tool body, history, user echo or private thought.
        try:
            packet = event.message
            method, params = packet.get("method"), packet.get("params") or {}
            if packet.get("jsonrpc") != "2.0":
                self._fault("ACP returned an invalid protocol frame.")
                return
            if event.direction.value == "outgoing":
                if method in {"session/new", "session/load", "session/set_config_option"}:
                    self._state_id = packet["id"]
                elif method == "session/prompt":
                    self._prompt_id = packet["id"]
                return
            if method is None:
                if self._phase == "state" and "result" in packet:
                    self._state_response_id = packet.get("id")
                    self._last_state = self._model_state(packet["result"])
                    if "configOptions" in self._last_state and self._config_epoch == self._state_epoch:
                        self._configs = self._last_state["configOptions"]
                if self._phase == "prompt":
                    self._prompt_done = True
                return
            if method == "$/cancel_request":
                identity = self._permission_identity
                if identity and params.get("requestId") == identity[0]:
                    self._fault("ACP permission is no longer active.")
                return
            if method == "session/request_permission":
                tool_id = (params.get("toolCall") or {}).get("toolCallId")
                request_id = packet.get("id")
                if (not self._prompt_active or self._prompt_done or self._permission_identity is not None
                        or params.get("sessionId") != self._session
                        or type(request_id) not in {int, str} or len(str(request_id)) > 256
                        or not isinstance(tool_id, str) or not 0 < len(tool_id) <= 256):
                    self.adapter._delivery_unknown |= self.adapter._input_attempted
                    self._fault("ACP permission has no active input binding.")
                    return
                self._permission_identity = (request_id, tool_id, self._input_token)
                return
            if method != "session/update":
                self._fault("ACP client capability is unsupported; use the native host.")
                return
            if self._session and params.get("sessionId") != self._session:
                self._fault("ACP returned a different session ID; adoption refused.")
                return
            update = params.get("update") or {}
            kind = update.get("sessionUpdate")
            if kind == "config_option_update":
                configs = self._model_state({"configOptions": update["configOptions"]})["configOptions"]
                if configs != self._configs:
                    self._config_epoch += 1
                self._configs = configs
                if self._model_selectors and not self._matches(self._configs, self._model_selectors):
                    self._fault("ACP model selection changed; input refused.")
            elif kind == "agent_message_chunk" and self._prompt_active and not self._prompt_done:
                content = update.get("content") or {}
                if content.get("type") == "text":
                    text = content.get("text")
                    if not isinstance(text, str) or len(self._text) + len(text) > self.max_output_chars:
                        self._fault("ACP public output exceeded its size limit.")
                    else:
                        self._text += text
                        self.adapter._accepted_turn()
        except Exception:
            self._fault("ACP returned invalid bounded metadata.")

    @staticmethod
    def _matches(configs: list[dict], targets: dict[str, str]) -> bool:
        return all(len([item for item in configs if item["id"] == key and item["currentValue"] == value]) == 1
                   for key, value in targets.items())

    def _model_state(self, result: dict) -> dict:
        selected = {key: result[key] for key in ("sessionId", "stopReason") if key in result}
        if any(not isinstance(value, str) or not 0 < len(value) <= 256 for value in selected.values()):
            raise ValueError
        if "models" in result:
            models = result["models"]
            available = models["availableModels"]
            if not isinstance(available, list) or len(available) > self.max_messages:
                raise ValueError
            selected["models"] = {"currentModelId": models["currentModelId"],
                "availableModels": [{"modelId": model["modelId"]} for model in available]}
            if any(not isinstance(value, str) or not 0 < len(value) <= 256 for value in
                   [models["currentModelId"], *[model["modelId"] for model in available]]):
                raise ValueError
        if "configOptions" in result:
            configs = result["configOptions"]
            if not isinstance(configs, list) or len(configs) > self.max_messages:
                raise ValueError
            selected["configOptions"] = []
            for config in configs:
                category = config.get("category", config.get("id"))
                if category not in {"model", "thought_level"}:
                    continue
                options = config["options"]
                if not isinstance(options, list) or len(options) > self.max_messages:
                    raise ValueError
                values: list[dict] = []
                for option in options:
                    group = option.get("options", [option])
                    if not isinstance(group, list) or len(group) > self.max_messages:
                        raise ValueError
                    values.extend({"value": item["value"]} for item in group)
                if len(values) > self.max_messages:
                    raise ValueError
                fields = {key: config[key] for key in ("id", "type", "currentValue")}
                if any(not isinstance(value, str) or not 0 < len(value) <= 256
                       for value in [*fields.values(), *[item["value"] for item in values]]):
                    raise ValueError
                selected["configOptions"].append(fields | {"options": values, "category": category})
        return selected

    async def _set_config(self, config_id: str, value: str) -> dict:
        self._phase, self._state_id, self._state_response_id = "state", None, None
        self._state_epoch, self._last_state = self._config_epoch, {}
        await self._conn.set_config_option(config_id=config_id, session_id=self._session, value=value)
        if self._error or self._state_response_id != self._state_id:
            raise RuntimeError(self._error or "ACP configuration response has no exact request binding.")
        return self._last_state

    async def _select_model(self, state: dict, model_id: str | None) -> None:
        configs = state.get("configOptions", [])
        if model_id is None:
            # An unresolved advisory policy supplies no override. Preserve only
            # a current selection actually advertised by this native session.
            models = state.get("models", {})
            current = models.get("currentModelId")
            if current and current not in [item["modelId"] for item in models["availableModels"]]:
                raise self.needs_human("ACP current model was not advertised; input refused.")
            model_configs = [config for config in configs if config["category"] == "model"]
            thoughts = [config for config in configs if config["category"] == "thought_level"]
            if configs:
                if len(model_configs) != 1 or len(thoughts) > 1:
                    raise self.needs_human("ACP current model selectors are ambiguous; input refused.")
                for config in [*model_configs, *thoughts]:
                    if (config["type"] != "select" or config["id"] in self._model_selectors
                            or config["currentValue"] not in [item["value"] for item in config["options"]]):
                        raise self.needs_human("ACP current model selection is unverified; input refused.")
                    self._model_selectors[config["id"]] = config["currentValue"]
                base = model_configs[0]["currentValue"]
                if current and current != base and (len(thoughts) != 1
                        or current != base + "-" + thoughts[0]["currentValue"]):
                    raise self.needs_human("ACP current model selectors disagree; input refused.")
                current = current or base
            if not current:
                raise self.needs_human("ACP current model is unverified; input refused.")
            if not current.startswith("gemini-") or current not in self.allowed_models:
                raise self.needs_human("ACP current model is outside the supported Gemini catalog; input refused.")
            self._model = current
            return
        if configs:
            model_configs = [config for config in configs if config["category"] == "model"]
            if len(model_configs) != 1:
                raise self.needs_human("ACP model selector is ambiguous; no fallback.")
            config = model_configs[0]
            if (config.get("type") != "select" or not isinstance(config.get("id"), str)
                    or not 0 < len(config["id"]) <= 256):
                raise self.needs_human("ACP did not advertise the exact Gemini model; no fallback.")
            model_values = [option["value"] for option in config["options"]]
            targets = {config["id"]: model_id}
            if model_id not in model_values:
                # AGY 1.5 advertises exact legacy IDs alongside separate model
                # and thought_level selectors. Require all three advertised IDs
                # and one exact pair; never invent a suffix or change mode.
                thoughts = [item for item in configs if item["category"] == "thought_level"]
                exact = [item["modelId"] for item in state.get("models", {}).get("availableModels", [])]
                if len(thoughts) != 1 or thoughts[0]["type"] != "select" or model_id not in exact:
                    raise self.needs_human("ACP did not advertise the exact Gemini model; no fallback.")
                thought = thoughts[0]
                pairs = [(base, option["value"]) for base in model_values for option in thought["options"]
                         if base + "-" + option["value"] == model_id]
                if len(pairs) != 1 or thought["id"] == config["id"]:
                    raise self.needs_human("ACP exact model selectors are ambiguous; no fallback.")
                targets = {config["id"]: pairs[0][0], thought["id"]: pairs[0][1]}
            confirmed_values: dict[str, str] = {}
            for config_id, target in targets.items():
                offered = [item for item in configs if item["id"] == config_id]
                if (len(offered) != 1 or offered[0]["type"] != "select"
                        or target not in [option["value"] for option in offered[0]["options"]]):
                    raise self.needs_human("ACP changed the advertised model options; no fallback.")
                selected = await self._set_config(config_id, target)
                configs = selected.get("configOptions", [])
                confirmed_values[config_id] = target
                if any(len([item for item in configs if item["id"] == key and item["currentValue"] == value]) != 1
                       for key, value in confirmed_values.items()):
                    raise self.needs_human("ACP did not confirm the exact Gemini model; no fallback.")
            if "models" in selected and selected["models"]["currentModelId"] != model_id:
                raise self.needs_human("ACP returned a different model ID; no fallback.")
            self._model_selectors = targets
        else:
            # Legacy models state has no stable v1 setting method. Do not guess one.
            models = state.get("models", {})
            if (models.get("currentModelId") != model_id
                    or model_id not in [item.get("modelId") for item in models.get("availableModels", [])]):
                raise self.needs_human("ACP did not confirm the exact Gemini model; no fallback.")
        self._model = model_id
        return

    def start(self, command: list[str], environment: dict[str, str], resume_id: str | None,
              model_id: str | None) -> str:
        if resume_id is not None and (not isinstance(resume_id, str) or not 0 < len(resume_id) <= 256):
            raise ValueError("An exact native resume ID is required.")
        if self.adapter.session_id is not None and resume_id != self.adapter.session_id:
            raise RuntimeError("ACP already owns another native session.")
        if model_id is not None and model_id not in self.allowed_models:
            raise self.needs_human("An exact supported Gemini model ID is required; no fallback.")
        if self._thread and self._thread.is_alive():
            raise RuntimeError("ACP already owns a native process.")
        self._owner()
        try:
            remaining = min(self.adapter.startup_timeout,
                (self.adapter._deadline or float("inf")) - time.monotonic())
            return self._run(self._start(command, environment, resume_id, model_id), remaining)
        except Exception:
            self.close()
            raise

    async def _start(self, command: list[str], environment: dict[str, str], resume_id: str | None,
                     model_id: str | None) -> str:
        import acp
        from acp.schema import ClientCapabilities, EnvVariable, Implementation, McpServerStdio
        if importlib.metadata.version("agent-client-protocol") != "0.12.1":
            raise RuntimeError("ACP SDK 0.12.1 is required.")
        self._sdk = acp
        # Pinned SDK uses logging.exception() on root, not an `acp` subtree.
        # Scope by this owner thread and the real package path; preserve every
        # other source/thread and all logger levels, handlers and propagation.
        self._sdk_log_prefix = Path(acp.__file__).resolve().parent.as_posix().casefold() + "/"
        logging.getLogger().addFilter(self._log_filter)
        a = self.adapter
        self._session, self._model = resume_id, None
        self._error = None
        self._model_selectors.clear()
        self._configs, self._last_state = [], {}
        self._config_epoch = self._state_epoch = 0
        self._process = await asyncio.create_subprocess_exec(*command, cwd=a.workspace, env=environment,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=self.max_wire_bytes, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        async def drain() -> None:
            while await self._process.stderr.read(4096):
                pass
        self._stderr = asyncio.create_task(drain())
        self._conn = acp.connect_to_agent(self, self._process.stdin,
            _BoundedReader(self._process.stdout, self.max_wire_bytes), observers=[self._observe])
        initialized = await self._conn.initialize(protocol_version=1, client_capabilities=ClientCapabilities(),
            client_info=Implementation(name="avalokita", version="0.1.0"))
        if initialized.protocol_version != 1:
            raise self.needs_human("ACP protocol version 1 is required.")
        if resume_id and not initialized.agent_capabilities.load_session:
            raise self.needs_human("ACP cannot load this exact session; no fallback.")
        binding = {"SHAREBUS_MANAGED": "1", "SHAREBUS_WORKSPACE": str(a.workspace),
            "SHAREBUS_AGENT_ID": a.agent_id, "SHAREBUS_AGENT_TOKEN_FILE": str(a.token_file)}
        server = McpServerStdio(name="sharebus", command=a.mcp_python,
            args=["-B", str(Path(__file__).resolve().with_name("mcp_server.py")),
                  "--workspace", str(a.workspace), "--provider", "antigravity"],
            env=[EnvVariable(name=key, value=value) for key, value in binding.items()])
        if resume_id:
            self._phase, self._state_epoch = "state", self._config_epoch
            await self._conn.load_session(cwd=str(a.workspace), session_id=resume_id, mcp_servers=[server])
        else:
            self._phase, self._state_epoch = "state", self._config_epoch
            await self._conn.new_session(cwd=str(a.workspace), mcp_servers=[server])
        if self._state_response_id != self._state_id:
            raise RuntimeError("ACP session response has no exact request binding.")
        state = self._last_state
        session = resume_id or state.get("sessionId")
        if (not isinstance(session, str) or not 0 < len(session) <= 256
                or "sessionId" in state and state["sessionId"] != session):
            raise RuntimeError("ACP did not confirm the exact session ID.")
        self._session = session
        a._remember_session(session)
        await self._select_model(state, model_id)
        # No await between final notification-state validation and arming.
        if self._error or self._model_selectors and not self._matches(self._configs, self._model_selectors):
            raise self.needs_human("ACP model changed during initialization; input refused.")
        self._phase = None
        return session

    def turn(self, prompt: str, timeout: float):
        return self._run(self._turn(prompt), timeout)

    async def _turn(self, prompt: str):
        a = self.adapter
        if self._error or not self._model or self._session != a.session_id or self._prompt_active or a._delivery_unknown:
            raise self.needs_human("ACP session or model is unverified; input refused.")
        if self._model_selectors and not self._matches(self._configs, self._model_selectors):
            raise self.needs_human("ACP model selection changed; input refused.")
        content = [self._sdk.text_block(prompt)]
        self._prompt_active, self._prompt_done = True, False
        self._phase = "prompt"
        self._prompt_id, self._input_token, self._text = None, object(), ""
        self._cancelled = self._cancel_sent = False
        try:
            a._check_workspace()
            a._emit({"type": "input_attempt", "session_id": self._session}, required=True)
            if a._interrupted.is_set() or self._error or time.monotonic() >= a._deadline:
                raise RuntimeError("ACP input is no longer authorized.")
            a._input_attempted = True
            response = await self._conn.prompt(session_id=self._session, prompt=content)
            if self._error:
                raise RuntimeError(self._error)
            stop = response.stop_reason
            if stop not in {"end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"}:
                raise RuntimeError("ACP did not return a valid stop reason.")
            a._accepted_turn()
            success = stop == "end_turn" and not a._needs_human and not self._cancelled
            return a._result(self._text, success, None if success else "ACP native turn did not complete successfully.",
                reason_code=None if success else "native_permission_required" if a._needs_human else "native_result_failed")
        finally:
            self._phase = None
            self._prompt_active, self._prompt_done = False, True
            self._text = ""
            self._input_token = self._permission_identity = None

    def _active(self, approval: dict) -> bool:
        a = self.adapter
        return (self._prompt_active and self._prompt_id is not None and not self._prompt_done and not self._error and a._input_attempted
            and not a._delivery_unknown and not a._interrupted.is_set() and a.session_id == self._session
            and approval["token"] is self._input_token and approval["identity"] == self._permission_identity
            and a._deadline is not None and time.monotonic() < a._deadline)

    async def request_permission(self, session_id: str, tool_call: Any, options: list[Any], **kwargs: Any):
        from acp.schema import RequestPermissionResponse
        a = self.adapter
        identity = self._permission_identity
        tool = tool_call.model_dump(by_alias=True, exclude_none=True)
        selected = {key: tool[key] for key in ("toolCallId", "title", "kind") if key in tool}
        offered = [{key: option.model_dump(by_alias=True)[key] for key in ("optionId", "name", "kind")} for option in options]
        details: dict[str, Any] = {"tool_call": selected, "options": offered}
        raw = tool.get("rawInput") or {}
        if isinstance(raw, dict):
            for target, aliases in (("command", ("command", "CommandLine", "command_line", "commandLine")),
                    ("cwd", ("Cwd", "WorkingDirectory", "working_dir", "workingDir", "cwd"))):
                values = [raw[key] for key in aliases if isinstance(raw.get(key), str) and raw[key]]
                if values and len(set(values)) == 1 and len(values[0]) <= 8192:
                    details[target] = values[0]
        locations = tool.get("locations") or []
        if isinstance(locations, list) and len(locations) <= self.max_messages:
            paths = [item.get("path") for item in locations if isinstance(item, dict)]
            if paths and all(isinstance(path, str) and 0 < len(path) <= 8192 for path in paths):
                details["paths"] = paths
        view = {"approval_id": uuid.uuid4().hex, "provider": "antigravity", "method": "session/request_permission",
                "session_id": self._session, "request_id": identity[0] if identity else None, "details": details}
        approval = {"view": view, "identity": identity, "token": self._input_token,
                    "ready": threading.Event(), "future": asyncio.get_running_loop().create_future(), "options": offered}
        approval["can_approve"] = bool(selected.get("title") and (
            selected.get("kind") == "execute" and details.get("command")
            or selected.get("kind") in {"read", "edit", "delete", "move", "search"} and details.get("paths")))
        if (session_id != self._session or not identity or selected.get("toolCallId") != identity[1]
                or not self._active(approval) or not 0 < len(offered) <= self.max_messages
                or len({item["optionId"] for item in offered}) != len(offered)
                or any(not isinstance(value, str) or not 0 < len(value) <= 8192 for value in selected.values())
                or len(json.dumps(view, ensure_ascii=False).encode()) > self.max_approval_bytes):
            self._fault("ACP permission has no valid bounded input binding.")
            raise self._sdk.RequestError.invalid_params()
        previous = a._needs_human
        a._needs_human = True
        a._accepted_turn()
        a._emit({"type": "needs_human", "session_id": self._session,
                 "reason": "Native tool approval or user input is required."})
        with a._approval_lock:
            if a._approval:
                self._fault("Another native approval is already pending.")
                raise self._sdk.RequestError.invalid_params()
            a._approval = approval
        try:
            if not a.on_approval:
                raise self.needs_human("Human approval handler is unavailable.")
            a.on_approval(copy.deepcopy(view))
            outcome = await asyncio.wait_for(approval["future"], max(0.001, a._deadline - time.monotonic()))
            if not self._active(approval):
                raise self.needs_human("ACP approval is no longer active.")
            a._needs_human = previous or approval["declined"]
            if outcome["outcome"] == "cancelled":
                self._cancelled = True
                asyncio.create_task(self._cancel())
            return RequestPermissionResponse(outcome=outcome)
        except Exception:
            self._fault("ACP approval expired or is no longer active.")
            raise self._sdk.RequestError.invalid_params() from None
        finally:
            with a._approval_lock:
                if a._approval is approval:
                    a._approval = None
            if self._permission_identity == identity:
                self._permission_identity = None

    def respond_approval(self, approval_id: str, result: dict) -> None:
        with self.adapter._approval_lock:
            approval = self.adapter._approval
            if (not approval or approval["view"]["approval_id"] != approval_id
                    or approval["ready"].is_set() or not self._active(approval)):
                raise ValueError("ACP approval is stale or no longer active.")
            if not isinstance(result, dict) or set(result) not in ({"decision"}, {"decision", "optionId"}):
                raise ValueError("A single-request approval decision is required.")
            decision = result.get("decision")
            if decision == "accept" and not approval["can_approve"]:
                raise ValueError("ACP action details require review in the native host.")
            if decision == "cancel" and set(result) == {"decision"}:
                outcome = {"outcome": "cancelled"}
            elif decision in {"accept", "decline"}:
                kind = "allow_once" if decision == "accept" else "reject_once"
                matched = [item for item in approval["options"] if item["kind"] == kind
                    and ("optionId" not in result or item["optionId"] == result["optionId"])]
                if len(matched) != 1:
                    raise ValueError("An exact offered single-use option is required.")
                outcome = {"outcome": "selected", "optionId": matched[0]["optionId"]}
            else:
                raise ValueError("An explicit accept, decline or cancel decision is required.")
            approval["declined"] = decision != "accept"
            loop = self._loop
            if loop is None or not loop.is_running():
                raise ValueError("ACP approval transport is unavailable.")
            approval["ready"].set()
            loop.call_soon_threadsafe(approval["future"].set_result, outcome)

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        # Projection happens synchronously in the official stream observer, in
        # wire order, before SDK notification tasks are scheduled.
        return None

    async def _unsupported(self, *args: Any, **kwargs: Any):
        self._fault("ACP client capability is unsupported; use the native host.")
        raise self._sdk.RequestError.method_not_found("Unsupported ACP client capability")

    write_text_file = read_text_file = create_terminal = terminal_output = _unsupported
    release_terminal = wait_for_terminal_exit = kill_terminal = create_elicitation = _unsupported
    complete_elicitation = _unsupported

    async def _cancel(self) -> None:
        if not self._prompt_active or self._prompt_done or self._cancel_sent:
            return
        self._cancelled = self._cancel_sent = True
        with self.adapter._approval_lock:
            approval = self.adapter._approval
            if approval and not approval["future"].done():
                approval["declined"] = True
                approval["ready"].set()
                approval["future"].set_result({"outcome": "cancelled"})
        await asyncio.sleep(0)
        await self._conn.cancel(session_id=self._session)

    def cancel(self) -> None:
        if self._loop and self._loop.is_running():
            self._run(self._cancel(), 2)

    async def _close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
        process = self._process
        # SDK 0.12.1 stops its sender without closing the supplied writer.
        # After that sole writer stops, EOF lets the owned child exit normally.
        if process and process.stdin:
            process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
        if process and process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), 1)
            except asyncio.TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 1)
                except asyncio.TimeoutError:
                    process.kill()
                    await asyncio.wait_for(process.wait(), 1)
        if self._stderr:
            self._stderr.cancel()
            await asyncio.gather(self._stderr, return_exceptions=True)
            self._stderr = None
        if process:
            # asyncio.Process has no public close(). Dispose only this exited,
            # owned subprocess transport, including Windows Proactor pipe handles.
            transport = getattr(process, "_transport", None)
            if transport is not None:
                transport.close()
            await asyncio.sleep(0)

    def close(self) -> None:
        if not self._loop or not self._loop.is_running():
            if not self._thread or not self._thread.is_alive():
                logging.getLogger().removeFilter(self._log_filter)
            return
        try:
            self._run(self._close(), 5)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread and threading.current_thread() is not self._thread:
                self._thread.join(5)
            if not self._thread or not self._thread.is_alive():
                logging.getLogger().removeFilter(self._log_filter)
