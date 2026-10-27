"""Human CLI for the persistent local Sharebus workspace."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import webbrowser


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Sharebus refused an HTTP redirect")


class Client:
    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).resolve(strict=True)
        self.private = self.workspace / ".sharebus" / "private"
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def runtime(self, *, verify: bool = True) -> dict[str, Any]:
        try:
            runtime = json.loads((self.private / "runtime.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise RuntimeError("Sharebus is not running here; use start") from None
        if not isinstance(runtime, dict):
            raise RuntimeError("Invalid private daemon runtime")
        try:
            parsed = urlsplit(runtime.get("url", ""))
            port = parsed.port
        except (AttributeError, TypeError, ValueError):
            raise RuntimeError("Invalid private daemon address") from None
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not port or parsed.path
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise RuntimeError("Invalid private daemon address")
        if (not isinstance(runtime.get("workspace"), str)
                or Path(runtime["workspace"]).resolve() != self.workspace):
            raise RuntimeError("Runtime belongs to another workspace")
        if (type(runtime.get("pid")) is not int or runtime["pid"] <= 0
                or not isinstance(runtime.get("nonce"), str) or len(runtime["nonce"]) < 16
                or not isinstance(runtime.get("workspace_id"), str) or not runtime["workspace_id"]):
            raise RuntimeError("Invalid private daemon process identity")
        if verify:
            try:
                request = Request(runtime["url"] + "/health")
                with self.opener.open(request, timeout=20) as response:
                    raw = response.read(65537)
                if len(raw) > 65536:
                    raise RuntimeError("Invalid daemon health response")
                health = json.loads(raw)
            except (OSError, ValueError):
                raise RuntimeError("Sharebus daemon is unavailable; use start") from None
            if (not isinstance(health, dict) or health.get("ok") is not True
                    or any(health.get(key) != runtime.get(key) for key in ("workspace_id", "nonce", "pid"))):
                raise RuntimeError("Runtime process identity does not match; no control command was sent")
        return runtime

    def _enable(self, runtime: dict[str, Any], *, register_sidecar: bool = True) -> None:
        if type(register_sidecar) is not bool:
            raise ValueError("Sidecar registration selection must be a boolean")
        (self.workspace / ".sharebus" / "enabled").write_text("Sharebus explicitly enabled in this workspace.\n", encoding="utf-8")
        if register_sidecar and Path(__file__).with_name("sidecar_registry.py").is_file():
            try:
                from sidecar_registry import register_workspace
                register_workspace(self.workspace, runtime)
            except Exception as exc:
                print(f"Sharebus is running; native sidecar registration requires attention ({type(exc).__name__}).", file=sys.stderr)

    def request(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        runtime = self.runtime()
        token = (self.private / "operator.token").read_text(encoding="utf-8").strip()
        headers = {"Authorization": "Bearer " + token, "X-Sharebus-Request": "1"}
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(runtime["url"] + path, data=payload, headers=headers)
        try:
            with self.opener.open(request, timeout=15) as response:
                return json.load(response)
        except HTTPError as exc:
            try:
                message = json.load(exc).get("error", str(exc.code))
            except (OSError, ValueError):
                message = str(exc.code)
            raise RuntimeError(message) from None

    def start(self, agents: str = "none", *, background: bool = True,
              register_sidecar: bool = True) -> dict[str, Any]:
        if type(register_sidecar) is not bool:
            raise ValueError("Sidecar registration selection must be a boolean")
        try:
            runtime = self.runtime()
            self._enable(runtime, register_sidecar=register_sidecar)
            return runtime
        except RuntimeError:
            pass
        self.private.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, *(["-B"] if os.environ.get("AVALOKITA_PACKAGED_RUNTIME") == "1" or sys.dont_write_bytecode else []),
                   str(Path(__file__).with_name("daemon.py")), "--workspace", str(self.workspace), "--agents", agents]
        if not background:
            subprocess.run(command, cwd=self.workspace, check=True)
            return {"stopped": True}
        started = time.monotonic()
        deadline = started + 120
        process = None
        if os.name == "nt":
            system_powershell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            powershell = str(system_powershell) if system_powershell.is_file() else shutil.which("powershell.exe") or shutil.which("pwsh.exe")
            if not powershell:
                raise RuntimeError("PowerShell is required for a persistent hidden Windows daemon")
            pythonw = Path(sys.executable).with_name("pythonw.exe")
            if not pythonw.is_file():
                raise RuntimeError("A windowless Python runtime is required for the Windows background service")
            from store import BusStore
            workspace_id = BusStore(self.workspace).workspace_id
            task_name = "Sharebus-" + workspace_id
            task_description = "Sharebus manual workspace service " + workspace_id
            launch_id = uuid.uuid4().hex
            def literal(value: str) -> str:
                return "'" + value.replace("'", "''") + "'"
            arguments = subprocess.list2cmdline(command[1:])
            prior_arguments = arguments
            metadata_file = self.private / "launcher.json"
            if metadata_file.exists():
                try:
                    prior = json.loads(metadata_file.read_text(encoding="utf-8"))
                    if prior.get("task_name") == task_name and prior.get("workspace_id") == workspace_id:
                        prior_arguments = str(prior.get("arguments", arguments))
                except (OSError, ValueError):
                    pass
            # Task Scheduler owns the process outside a calling Codex turn's
            # JobObject. There are no time, login, boot, or automatic triggers.
            # The task runs only as this already logged-in user, at Limited level.
            script = "\n".join([
                "$sharebusIdentity=[Security.Principal.WindowsIdentity]::GetCurrent()",
                "$sharebusTaskName=" + literal(task_name),
                "$sharebusDescription=" + literal(task_description),
                "$sharebusExecute=" + literal(str(pythonw)),
                "$sharebusArguments=" + literal(arguments),
                "$sharebusPriorArguments=" + literal(prior_arguments),
                "$sharebusDirectory=" + literal(str(self.workspace)),
                "$sharebusLaunchId=" + literal(launch_id),
                "$sharebusWorkspaceId=" + literal(workspace_id),
                "$sharebusService=New-Object -ComObject 'Schedule.Service' -ErrorAction Stop",
                "$sharebusService.Connect()",
                "$sharebusFolder=$sharebusService.GetFolder('\\')",
                "$sharebusExisting=$null",
                "$sharebusBaselineTicks=[Int64]0",
                "try{$sharebusExisting=$sharebusFolder.GetTask($sharebusTaskName)}catch{$sharebusError=$_.Exception; $sharebusMissing=$false; while($sharebusError){if(($sharebusError.HResult -band 65535) -eq 2){$sharebusMissing=$true}; $sharebusError=$sharebusError.InnerException}; if(-not $sharebusMissing){throw}}",
                "if($sharebusExisting){",
                "  $sharebusDefinition=$sharebusExisting.Definition",
                "  $sharebusAction=$sharebusDefinition.Actions.Item(1)",
                "  $sharebusPrincipal=$sharebusDefinition.Principal.UserId",
                "  if($sharebusPrincipal -like 'S-1-*'){$sharebusPrincipalSid=$sharebusPrincipal}else{$sharebusAccount=New-Object Security.Principal.NTAccount($sharebusPrincipal); $sharebusPrincipalSid=$sharebusAccount.Translate([Security.Principal.SecurityIdentifier]).Value}",
                "  if($sharebusDefinition.RegistrationInfo.Description -ne $sharebusDescription -or $sharebusDefinition.Actions.Count -ne 1 -or $sharebusAction.Path -ine $sharebusExecute -or $sharebusAction.WorkingDirectory -ine $sharebusDirectory -or $sharebusAction.Arguments -notin @($sharebusArguments,$sharebusPriorArguments) -or $sharebusPrincipalSid -ne $sharebusIdentity.User.Value -or $sharebusDefinition.Triggers.Count -ne 0 -or $sharebusDefinition.Principal.RunLevel -ne 0 -or $sharebusDefinition.Principal.LogonType -ne 3){throw 'Existing task does not match this workspace service'}",
                "  $sharebusWaitEnd=[DateTime]::UtcNow.AddSeconds(30)",
                "  while($sharebusExisting.State -eq 4 -and [DateTime]::UtcNow -lt $sharebusWaitEnd){Start-Sleep -Milliseconds 200; $sharebusExisting=$sharebusFolder.GetTask($sharebusTaskName)}",
                "  if($sharebusExisting.State -eq 4){Write-Output 'existing-task-running'; exit 0}",
                "  $sharebusBaselineTicks=[Int64]$sharebusExisting.LastRunTime.ToUniversalTime().Ticks",
                "}",
                "$sharebusDefinition=$sharebusService.NewTask(0)",
                "$sharebusDefinition.RegistrationInfo.Description=$sharebusDescription",
                "$sharebusDefinition.Principal.UserId=$sharebusIdentity.Name",
                "$sharebusDefinition.Principal.LogonType=3",
                "$sharebusDefinition.Principal.RunLevel=0",
                "$sharebusDefinition.Settings.Hidden=$true",
                "$sharebusDefinition.Settings.AllowDemandStart=$true",
                "$sharebusDefinition.Settings.Enabled=$true",
                "$sharebusDefinition.Settings.ExecutionTimeLimit='PT0S'",
                "$sharebusDefinition.Settings.Priority=4",
                "$sharebusDefinition.Settings.MultipleInstances=2",
                "$sharebusDefinition.Settings.DisallowStartIfOnBatteries=$false",
                "$sharebusDefinition.Settings.StopIfGoingOnBatteries=$false",
                "$sharebusAction=$sharebusDefinition.Actions.Create(0)",
                "$sharebusAction.Path=$sharebusExecute",
                "$sharebusAction.Arguments=$sharebusArguments",
                "$sharebusAction.WorkingDirectory=$sharebusDirectory",
                "$sharebusRegistered=$sharebusFolder.RegisterTaskDefinition($sharebusTaskName,$sharebusDefinition,6,$sharebusIdentity.Name,$null,3,$null)",
                "$sharebusBaselineTicks=[Math]::Max($sharebusBaselineTicks,[Int64]$sharebusRegistered.LastRunTime.ToUniversalTime().Ticks)",
                "$sharebusRequestedTicks=[DateTime]::UtcNow.Ticks",
                "$sharebusRequestedTicks-=$sharebusRequestedTicks % [TimeSpan]::TicksPerSecond",
                "$sharebusRegistered.Run($null) | Out-Null",
                "$sharebusProbeEnd=[DateTime]::UtcNow.AddSeconds(3)",
                "do{",
                "  $sharebusProbe=$sharebusFolder.GetTask($sharebusTaskName)",
                "  $sharebusProbeDefinition=$sharebusProbe.Definition",
                "  $sharebusProbeAction=$sharebusProbeDefinition.Actions.Item(1)",
                "  $sharebusProbePrincipal=$sharebusProbeDefinition.Principal.UserId",
                "  if($sharebusProbePrincipal -like 'S-1-*'){$sharebusProbePrincipalSid=$sharebusProbePrincipal}else{$sharebusProbeAccount=New-Object Security.Principal.NTAccount($sharebusProbePrincipal); $sharebusProbePrincipalSid=$sharebusProbeAccount.Translate([Security.Principal.SecurityIdentifier]).Value}",
                "  if($sharebusProbeDefinition.RegistrationInfo.Description -ne $sharebusDescription -or $sharebusProbeDefinition.Actions.Count -ne 1 -or $sharebusProbeAction.Path -ine $sharebusExecute -or $sharebusProbeAction.WorkingDirectory -ine $sharebusDirectory -or $sharebusProbeAction.Arguments -ne $sharebusArguments -or $sharebusProbePrincipalSid -ne $sharebusIdentity.User.Value -or $sharebusProbeDefinition.Triggers.Count -ne 0 -or $sharebusProbeDefinition.Principal.RunLevel -ne 0 -or $sharebusProbeDefinition.Principal.LogonType -ne 3){throw 'Workspace task identity changed during startup'}",
                "  $sharebusReceipt=@{task_name=$sharebusTaskName;workspace_id=$sharebusWorkspaceId;launch_id=$sharebusLaunchId;baseline_run_ticks=$sharebusBaselineTicks;requested_run_ticks=$sharebusRequestedTicks;last_run_ticks=[Int64]$sharebusProbe.LastRunTime.ToUniversalTime().Ticks;state=[Int32]$sharebusProbe.State;last_result=[Int64]$sharebusProbe.LastTaskResult}",
                "  if($sharebusReceipt.state -eq 3 -and $sharebusReceipt.last_run_ticks -gt $sharebusBaselineTicks -and $sharebusReceipt.last_run_ticks -ge $sharebusRequestedTicks -and $sharebusReceipt.last_result -ne 0 -and ($sharebusReceipt.last_result -lt 267008 -or $sharebusReceipt.last_result -gt 267263)){break}",
                "  Start-Sleep -Milliseconds 200",
                "}while([DateTime]::UtcNow -lt $sharebusProbeEnd)",
                "$sharebusReceipt | ConvertTo-Json -Compress",
            ])
            try:
                launched = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", script],
                                          capture_output=True, text=True, timeout=60,
                                          creationflags=subprocess.CREATE_NO_WINDOW)
                if launched.returncode:
                    (self.private / "launcher-error.log").write_text(launched.stderr, encoding="utf-8")
                    raise RuntimeError("Windows refused the per-user background task; inspect .sharebus/private/launcher-error.log")
                try:
                    receipt = json.loads(launched.stdout)
                except ValueError:
                    receipt = None
                own_receipt = (isinstance(receipt, dict) and receipt.get("task_name") == task_name
                               and receipt.get("workspace_id") == workspace_id and receipt.get("launch_id") == launch_id)
                if (own_receipt and all(type(receipt.get(key)) is int for key in
                                        ("state", "last_result", "baseline_run_ticks", "requested_run_ticks", "last_run_ticks"))
                        and receipt["state"] == 3 and receipt["last_result"] != 0
                        and receipt["last_result"] & 0xFFFFFF00 != 0x41300
                        and receipt["last_run_ticks"] > receipt["baseline_run_ticks"]
                        and receipt["last_run_ticks"] >= receipt["requested_run_ticks"]):
                    raise RuntimeError("Background service failed during this start; inspect .sharebus/private/startup.log and daemon.log")
                metadata = {"kind": "windows_task_scheduler", "task_name": task_name,
                            "workspace_id": workspace_id, "workspace": str(self.workspace),
                            "execute": str(pythonw), "arguments": arguments, "manual_only": True,
                            "logon_type": "Interactive", "run_level": "Limited", "created_at": time.time()}
                if own_receipt:
                    temporary = self.private / ("launcher-" + uuid.uuid4().hex + ".tmp")
                    temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
                    os.replace(temporary, metadata_file)
            except subprocess.TimeoutExpired:
                # Registration/start may have finished before the response.
                # Never retry a second launch after an uncertain outcome.
                print("Sharebus is verifying a delayed background task...", file=sys.stderr)
        else:
            with (self.private / "daemon.log").open("ab") as log:
                process = subprocess.Popen(command, cwd=self.workspace, stdin=subprocess.DEVNULL,
                                           stdout=log, stderr=log, close_fds=True, start_new_session=True)
        reported = False
        while time.monotonic() < deadline:
            try:
                runtime = self.runtime()
                self._enable(runtime, register_sidecar=register_sidecar)
                return runtime
            except RuntimeError:
                if process is not None and process.poll() is not None:
                    raise RuntimeError("Daemon did not start; inspect .sharebus/private/daemon.log") from None
                if not reported and time.monotonic() - started > 10:
                    print("Sharebus background service is starting; checking readiness...", file=sys.stderr)
                    reported = True
                time.sleep(0.25)
        raise RuntimeError("Background service is not ready after 120 seconds and may still be starting. Re-run start/status; inspect .sharebus/private/startup.log")

    def open(self) -> None:
        runtime = self.runtime()
        token = (self.private / "operator.token").read_text(encoding="utf-8").strip()
        # Fragment is removed by the UI after exchange; never sent in HTTP requests.
        webbrowser.open(runtime["url"] + "/#token=" + token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Persistent workspace collaboration with human intervention")
    parser.add_argument("--workspace", default=str(Path.cwd()))
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start")
    start.add_argument("--agents", default="none", help="none (default), auto, or comma-separated verified CLI providers")
    start.add_argument("--background", action="store_true", help="Default; keep daemon after CLI exits")
    start.add_argument("--foreground", action="store_true")
    start.add_argument("--open", action="store_true")
    auto = sub.add_parser("auto", help="Install and connect existing native conversations; never launch managed workers",
                         description=("Enable existing native conversations. Stop hooks wait within the current turn "
                                      "without model calls where fallback is needed, for 24 hours by default "
                                      "(configurable up to 7 days). Supported bundled Codex native queue delivery "
                                      "is selected automatically; existing delivery choices are preserved. "
                                      "After fallback timeout, the next ordinary native input rearms listening."))
    auto.add_argument("action", nargs="?", default="on", choices=("on", "off", "status", "resume", "invite"),
                      help="on preserves manual controls; resume explicitly reopens the workspace, preserving individual actor controls")
    auto.add_argument("--no-open", action="store_true", help="Keep the human console closed")
    auto.add_argument("--watch-timeout", type=float,
                      help="Native Stop fallback wait seconds for auto on (default 24 hours; maximum 7 days)")
    auto.add_argument("--native-candidates", type=Path, help="Public exact-workspace metadata from native app tools")
    auto.add_argument("--origin-provider", choices=("codex", "claude", "antigravity"))
    auto.add_argument("--origin-native-id", help="Exclude the actual initiating native conversation from invitations")
    auto.add_argument("--invite-key", help="Invitation key to claim before an app sends")
    auto.add_argument("--invite-state", choices=("dispatching", "delivered", "uncertain", "failed"))
    for name in ("status", "open", "pause", "resume", "stop"):
        sub.add_parser(name)
    send = sub.add_parser("send")
    send.add_argument("body")
    send.add_argument("--to", default="all")
    send.add_argument("--kind", default="task")
    send.add_argument("--budget", default=24, type=int)
    chat = sub.add_parser("chat")
    chat.add_argument("--to", default="all")
    events = sub.add_parser("events")
    events.add_argument("--after", type=int, default=0)
    recovery = sub.add_parser("recover")
    recovery.add_argument("agent_id", help="Issue a private, single-use five-minute rejoin code")
    args = parser.parse_args(argv)
    client = Client(args.workspace)
    try:
        if args.command == "start":
            runtime = client.start(args.agents, background=not args.foreground)
            print(json.dumps({"running": not args.foreground, "url": runtime.get("url"), "workspace": str(client.workspace)}, ensure_ascii=False, indent=2))
            if args.open and not args.foreground:
                client.open()
        elif args.command == "auto":
            from auto_mode import configure, load_candidates
            candidates = load_candidates(args.native_candidates) if args.native_candidates else None
            result = configure(client.workspace, args.action, open_console=not args.no_open, candidates=candidates,
                               origin_provider=args.origin_provider, origin_native_id=args.origin_native_id,
                               invite_key=args.invite_key, invite_state=args.invite_state,
                               watch_timeout=args.watch_timeout)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "status":
            print(json.dumps(client.request("/api/state"), ensure_ascii=False, indent=2))
        elif args.command == "open":
            client.open()
            print("Sharebus human console opened.")
        elif args.command in {"pause", "resume", "stop"}:
            print(json.dumps(client.request("/api/control", {"action": args.command}), ensure_ascii=False, indent=2))
        elif args.command == "send":
            print(json.dumps(client.request("/api/message", {"to": args.to, "body": args.body, "kind": args.kind, "budget": args.budget}), ensure_ascii=False, indent=2))
        elif args.command == "events":
            print(json.dumps(client.request(f"/api/events?after={args.after}"), ensure_ascii=False, indent=2))
        elif args.command == "recover":
            print(json.dumps(client.request("/api/agent/recovery", {"agent_id": args.agent_id}), ensure_ascii=False, indent=2))
        elif args.command == "chat":
            print("Sharebus human chat. /quit exits this client; agents and daemon remain available.")
            while True:
                try:
                    body = input(f"human → {args.to}> ").strip()
                except EOFError:
                    break
                if body == "/quit":
                    break
                if body:
                    result = client.request("/api/message", {"to": args.to, "body": body, "kind": "task"})
                    print(f"Queued {len(result['jobs'])} job(s), event {result['event']['id']}")
        return 0
    except (RuntimeError, OSError, URLError, ValueError) as exc:
        print(f"Sharebus: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
