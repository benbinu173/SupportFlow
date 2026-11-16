"""Supervise the free demo's HTTP server, API, worker, and singleton scheduler.

The normal production stack keeps these in separate services. This entrypoint is
only for the single-instance free preview and stops everything if a child fails.
"""

import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import suppress
from pathlib import Path

STOP = threading.Event()
SHUTDOWN_SECONDS = 20
logger = logging.getLogger(__name__)

# Render's ingress appends Cloudflare and private proxy hops to X-Forwarded-For.
# Trust those hops so Uvicorn selects the rightmost remaining client address.
# The API is bound to loopback and receives this chain only through our nginx.
# Cloudflare's published ranges, checked 2026-11-16:
# https://www.cloudflare.com/ips-v4/ and https://www.cloudflare.com/ips-v6/
PREVIEW_TRUSTED_PROXIES = ",".join(
    (
        "127.0.0.1",
        "10.0.0.0/8",
        "173.245.48.0/20",
        "103.21.244.0/22",
        "103.22.200.0/22",
        "103.31.4.0/22",
        "141.101.64.0/18",
        "108.162.192.0/18",
        "190.93.240.0/20",
        "188.114.96.0/20",
        "197.234.240.0/22",
        "198.41.128.0/17",
        "162.158.0.0/15",
        "104.16.0.0/13",
        "104.24.0.0/14",
        "172.64.0.0/13",
        "131.0.72.0/22",
        "2400:cb00::/32",
        "2606:4700::/32",
        "2803:f800::/32",
        "2405:b500::/32",
        "2405:8100::/32",
        "2a06:98c0::/29",
        "2c0f:f248::/32",
    )
)


def request_stop(signum: int, frame: object) -> None:
    STOP.set()


def stop_processes(
    processes: list[subprocess.Popen[bytes]], grace_seconds: int = SHUTDOWN_SECONDS
) -> None:
    for process in processes:
        with suppress(ProcessLookupError):
            if sys.platform == "win32":
                process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    for process in processes:
        try:
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                if sys.platform == "win32":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def start(command: list[str]) -> subprocess.Popen[bytes]:
    # Only the fixed executable/argument lists below call this helper; no shell.
    return subprocess.Popen(command, start_new_session=True)  # noqa: S603


def migrate() -> None:
    # Keep settings/database imports in a short-lived process to release memory.
    process = start([sys.executable, "-m", "scripts.preview_migrate"])
    try:
        deadline = time.monotonic() + 180
        while process.poll() is None and not STOP.wait(0.5):
            if time.monotonic() >= deadline:
                raise RuntimeError("Migration timed out")
        if STOP.is_set() or process.poll() != 0:
            raise RuntimeError("Migration did not complete")
    finally:
        stop_processes([process])


def main() -> int:
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    if os.environ.get("ENVIRONMENT") != "production" or os.environ.get(
        "KNOWLEDGE_ENABLED", "false"
    ).lower() not in {"false", "0"}:
        raise RuntimeError("The preview requires production settings with knowledge disabled")
    port = int(os.environ.get("PORT", "10000"))
    if port < 1024 or port > 65535 or port in {8000, 18012, 18013, 19099}:
        raise ValueError("Unsupported preview port")
    template = Path("/app/preview-nginx.conf").read_text(encoding="utf-8")
    runtime_dir = tempfile.TemporaryDirectory(prefix="supportflow-nginx-")
    config_path = str(Path(runtime_dir.name) / "nginx.conf")
    Path(config_path).write_text(template.replace("__PORT__", str(port)), encoding="utf-8")
    subprocess.run(["/usr/sbin/nginx", "-t", "-c", config_path], check=True, timeout=10)  # noqa: S603
    migrate()
    commands = {
        "api": [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
            "--forwarded-allow-ips",
            PREVIEW_TRUSTED_PROXIES,
        ],
        # One prefork slot preserves Celery's hard task timeouts. The solo and
        # threads pools would save memory but cannot terminate a stuck task.
        "worker": [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "app.workers.celery_app",
            "worker",
            "--concurrency=1",
            "--loglevel=info",
            "--without-gossip",
            "--without-mingle",
            "-Q",
            "notifications,sla,ai,maintenance",
        ],
        "scheduler": [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "scripts.preview_beat:celery_app",
            "beat",
            "--loglevel=info",
            "--max-interval=5",
            "--scheduler",
            "scripts.preview_beat:LeasedScheduler",
            "-s",
            str(Path(runtime_dir.name) / "beat-schedule"),
        ],
        "web": ["/usr/sbin/nginx", "-c", config_path, "-g", "daemon off;"],
    }
    processes: dict[str, subprocess.Popen[bytes]] = {}
    try:
        for name, command in commands.items():
            if STOP.is_set():
                return 0
            processes[name] = start(command)
        while not STOP.wait(0.5):
            for name, process in processes.items():
                if process.poll() is not None:
                    logger.error("Preview %s exited; stopping the container", name)
                    return 1
        return 0
    finally:
        stop_processes(list(processes.values()))
        runtime_dir.cleanup()


if __name__ == "__main__":
    try:
        result = main()
    except Exception:
        # Connection and settings exceptions can contain connection credentials.
        logger.error("Preview startup failed; check settings, migrations, and dependency access")
        result = 1
    sys.exit(result)
