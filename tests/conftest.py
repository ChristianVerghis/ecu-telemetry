"""Shared helpers for integration tests.

The integration suite drives the real firmware binary and the real backend as
subprocesses; nothing here imports backend code directly.
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIRMWARE_DIR = REPO_ROOT / "firmware"
BACKEND_DIR = REPO_ROOT / "backend"
AGENT_BIN = Path(os.environ.get("ECU_AGENT", FIRMWARE_DIR / "build" / "ecu_agent"))


def free_port() -> int:
    """Return a TCP port that is currently free (also used for UDP; collisions are unlikely)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def free_ports(n: int) -> list[int]:
    ports: set[int] = set()
    while len(ports) < n:
        ports.add(free_port())
    return sorted(ports)


def wait_for_http(url: str, timeout: float = 30.0, proc: subprocess.Popen | None = None) -> None:
    """Poll `url` until it returns 200 or `timeout` elapses. Fails fast if `proc` dies."""
    import httpx

    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f"process exited early with rc={proc.returncode} while waiting for {url}")
        try:
            r = httpx.get(url, timeout=2.0)
            if r.status_code == 200:
                return
            last_err = RuntimeError(f"{url} -> {r.status_code}")
        except Exception as e:  # noqa: BLE001
            last_err = e
        time.sleep(0.25)
    raise TimeoutError(f"{url} not ready after {timeout}s: {last_err}")


def wait_until(pred, timeout: float, interval: float = 0.5, what: str = "condition"):
    """Poll `pred()` until truthy; return its value. Raise TimeoutError otherwise."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = pred()
            if last:
                return last
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(interval)
    raise TimeoutError(f"{what} not met after {timeout}s (last={last!r})")


def terminate(proc: subprocess.Popen | None, grace: float = 5.0) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.terminate()
    try:
        proc.wait(grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.wait(grace)


def backend_python() -> Path | None:
    """Interpreter that can run the backend: the venv if present, else the current one if it has fastapi."""
    venv = BACKEND_DIR / ".venv" / "bin" / "python"
    if venv.exists():
        return venv
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
        return Path(sys.executable)
    except ImportError:
        return None


def ensure_agent_binary() -> Path:
    """Build the firmware with cmake if the binary is missing. Skips the test if that's impossible."""
    if AGENT_BIN.exists():
        return AGENT_BIN
    if not shutil.which("cmake"):
        pytest.skip("cmake not installed and firmware/build/ecu_agent missing")
    if not (FIRMWARE_DIR / "CMakeLists.txt").exists():
        pytest.skip("firmware/CMakeLists.txt missing — firmware component not present yet")
    build_dir = FIRMWARE_DIR / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["cmake", "-S", str(FIRMWARE_DIR), "-B", str(build_dir), "-DCMAKE_BUILD_TYPE=Release"],
                       check=True, capture_output=True, text=True, timeout=300)
        subprocess.run(["cmake", "--build", str(build_dir), "--parallel"],
                       check=True, capture_output=True, text=True, timeout=900)
    except subprocess.CalledProcessError as e:
        pytest.fail(f"firmware build failed:\n{e.stdout}\n{e.stderr}")
    if not AGENT_BIN.exists():
        pytest.fail(f"firmware built but {AGENT_BIN} not produced")
    return AGENT_BIN


@pytest.fixture(scope="session")
def agent_binary() -> Path:
    return ensure_agent_binary()


@pytest.fixture
def backend(tmp_path):
    """Start the backend on ephemeral ports with a temp DB; yield a dict with urls/ports."""
    py = backend_python()
    if py is None:
        pytest.skip("backend/.venv missing and fastapi/uvicorn not importable")
    if not (BACKEND_DIR / "app" / "main.py").exists():
        pytest.skip("backend/app/main.py missing — backend component not present yet")
    http_port, udp_port, tcp_port = free_ports(3)
    env = dict(os.environ)
    env.update({
        "ECU_HTTP_PORT": str(http_port),
        "ECU_UDP_PORT": str(udp_port),
        "ECU_TCP_PORT": str(tcp_port),
        "ECU_DB_PATH": str(tmp_path / "telemetry.db"),
        "PYTHONUNBUFFERED": "1",
    })
    env.pop("ANTHROPIC_API_KEY", None)  # force heuristic diagnosis; no network in tests
    log = open(tmp_path / "backend.log", "wb")
    proc = subprocess.Popen(
        [str(py), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(http_port), "--log-level", "warning"],
        cwd=str(BACKEND_DIR), env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
    )
    base = f"http://127.0.0.1:{http_port}"
    try:
        wait_for_http(f"{base}/health", timeout=40, proc=proc)
    except Exception:
        terminate(proc)
        log.close()
        pytest.fail(f"backend failed to start; log:\n{(tmp_path / 'backend.log').read_text(errors='replace')[-4000:]}")
    yield {"base": base, "http_port": http_port, "udp_port": udp_port, "tcp_port": tcp_port,
           "proc": proc, "log": tmp_path / "backend.log"}
    terminate(proc)
    log.close()
