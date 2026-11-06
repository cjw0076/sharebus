"""Real stdio MCP handshake. Keep stdin open until each response arrives."""
import json
import queue
import subprocess
import sys
import threading


def mcp_exchange(script, messages, *, env=None, timeout=15):
    process = subprocess.Popen([sys.executable, str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env, bufsize=1)
    inbox = queue.Queue()
    output = []
    def read():
        for line in process.stdout:
            inbox.put(line)
        inbox.put(None)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        for message in messages:
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()
            if "id" in message:
                while True:
                    line = inbox.get(timeout=timeout)
                    if line is None:
                        raise AssertionError("MCP process exited before its response: " + process.stderr.read())
                    output.append(line)
                    response = json.loads(line)
                    if response.get("id") == message["id"]:
                        break
        process.stdin.close()
        process.wait(timeout=timeout)
        reader.join(timeout=2)
        while not inbox.empty():
            line = inbox.get_nowait()
            if line is not None:
                output.append(line)
        return subprocess.CompletedProcess(process.args, process.returncode, "".join(output), process.stderr.read())
    finally:
        if process.poll() is None:
            process.stdin.close()
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
        if not process.stdin.closed:
            process.stdin.close()
