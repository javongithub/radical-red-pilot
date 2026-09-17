#!/usr/bin/env python3
"""Launch the local pilot and mGBA without a Codex session or UI automation."""
from __future__ import annotations

import argparse
import fcntl
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"
DASHBOARD = "http://127.0.0.1:8765"
WORKSPACE_EMULATOR = ROOT.parent.parent / "work/emulator/dev/mGBA.app/Contents/MacOS/mGBA"
BUNDLED_EMULATOR = ROOT / "emulator/mGBA.app/Contents/MacOS/mGBA"
OLLAMA = ROOT / "ollama/ollama"
MODEL = "qwen3-vl:4b-instruct-q4_K_M"


def emulator_path() -> Path:
    for path in (BUNDLED_EMULATOR, WORKSPACE_EMULATOR):
        if path.is_file():
            return path
    raise RuntimeError("The supplied mGBA development build is missing. Keep the pilot's workspace together.")


def local_json(port: int, path: str) -> dict | None:
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        client.request("GET", path)
        response = client.getresponse()
        result = json.loads(response.read(2 * 1024 * 1024))
        if response.status == 200 and isinstance(result, dict):
            return result
    except (OSError, ValueError, http.client.HTTPException):
        pass
    finally:
        client.close()
    return None


def status() -> dict | None:
    result = local_json(8765, "/api/state")
    if result is not None and "connected" in result and "epoch" in result:
        return result
    return None


def local_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("CODEX_")}
    environment.update(OLLAMA_MODELS=str(ROOT / "models"), OLLAMA_HOST="127.0.0.1:11434",
                       OLLAMA_CONTEXT_LENGTH="8192", OLLAMA_NUM_PARALLEL="1", OLLAMA_NO_CLOUD="1",
                       RADICAL_RED_OLLAMA_URL="http://127.0.0.1:11434", RADICAL_RED_LOCAL_MODEL=MODEL)
    return environment


def ensure_ollama() -> bool:
    """Start local inference if needed; never download or select a remote model."""
    healthy = local_json(11434, "/api/version")
    if healthy is None:
        if not OLLAMA.is_file():
            raise RuntimeError("The bundled local AI runtime is missing: ollama/ollama")
        if not managed_running("ollama"):
            detach("ollama", [str(OLLAMA), "serve"], str(OLLAMA))
        deadline = time.monotonic() + 20
        while healthy is None and time.monotonic() < deadline:
            time.sleep(0.2)
            healthy = local_json(11434, "/api/version")
        if healthy is None:
            raise RuntimeError("The local AI runtime could not start. See runtime/ollama.log.")
    tags = local_json(11434, "/api/tags") or {}
    return any(item.get("name") == MODEL for item in tags.get("models", []) if isinstance(item, dict))


def managed_running(name: str) -> bool:
    """Check a PID only after matching its recorded executable command."""
    path = RUNTIME / (name + ".pid.json")
    try:
        record = json.loads(path.read_text())
        pid = int(record["pid"])
        os.kill(pid, 0)
        result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="],
                                capture_output=True, text=True, check=True)
        return str(record["identity"]) in result.stdout
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return False


def detach(name: str, command: list[str], identity: str) -> int:
    with (RUNTIME / (name + ".log")).open("ab") as log:
        child = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT,
                                 start_new_session=True, close_fds=True, env=local_environment())
    (RUNTIME / (name + ".pid.json")).write_text(json.dumps({"pid": child.pid, "identity": identity}))
    return child.pid


def emulation_command(executable: Path) -> list[str]:
    return [str(executable), "-3", "--script", str(ROOT / "bridge.lua"),
            "-C", "pauseOnFocusLost=0", "-C", "rewindEnable=0",
            "-C", "fastForwardRatio=10", "-C", "fastForwardHeldRatio=10",
            "-C", "autosave=0", "-C", "autoload=0",
            "-C", "savegamePath=" + str(RUNTIME),
            "-C", "savestatePath=" + str(RUNTIME),
            "-C", "screenshotPath=" + str(RUNTIME),
            str(RUNTIME / "radical-red-demo.gba")]


def launch(open_browser: bool = True, check: bool = False) -> None:
    executable = emulator_path()
    for needed in (ROOT / "server.py", ROOT / "local_driver.py", ROOT / "stall.py", ROOT / "bridge.lua",
                   RUNTIME / "radical-red-demo.gba", OLLAMA):
        if not needed.is_file():
            raise RuntimeError("Required pilot file is missing: " + str(needed))
    if check:
        print(json.dumps({"python": sys.executable, "emulator": str(executable),
                          "ollama": str(OLLAMA), "model": MODEL, "cloud_disabled": True,
                          "command": emulation_command(executable), "dashboard": DASHBOARD}, indent=2))
        return
    RUNTIME.mkdir(exist_ok=True)
    # Serializes double-clicks; releasing this lock does not terminate child services.
    with (RUNTIME / "launcher.lock").open("w") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        live = status()
        if live is not None and live.get("provider") != "Local AI":
            raise RuntimeError("A different or older controller is using port 8765. Close it before launching this local-only pilot.")
        model_ready = ensure_ollama()
        if live is None:
            if not managed_running("server"):
                detach("server", [sys.executable, "-u", str(ROOT / "server.py")], str(ROOT / "server.py"))
            deadline = time.monotonic() + 10
            while live is None and time.monotonic() < deadline:
                time.sleep(0.15)
                live = status()
            if live is None:
                raise RuntimeError("The local controller could not start. See runtime/server.log.")
        if live.get("provider") != "Local AI":
            raise RuntimeError("Controller does not identify as Local AI; no game was launched.")
        if not live.get("connected") and not managed_running("emulator"):
            detach("emulator", emulation_command(executable), str(executable))
        if open_browser:
            subprocess.run(["/usr/bin/open", DASHBOARD], check=True)
        print("Radical Red Pilot is running at " + DASHBOARD)
        print("The game and controller continue running after this launcher closes.")
        if not model_ready:
            print("Local vision model is not installed yet. Manual control works; AI will pause until model setup finishes.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Show the launch configuration without starting anything")
    parser.add_argument("--no-browser", action="store_true", help="Keep the browser closed")
    options = parser.parse_args()
    try:
        launch(open_browser=not options.no_browser, check=options.check)
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print("Pilot could not start: " + str(error), file=sys.stderr)
        raise SystemExit(1)
