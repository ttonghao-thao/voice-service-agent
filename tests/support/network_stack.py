"""Process lifecycle for the isolated, reproducible simulated API test stack."""

import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]


class NetworkStack:
    def __init__(self, directory, mode, portal_port=0):
        self.directory, self.mode, self.portal_port = Path(directory), mode, portal_port
        self.cleanup = ExitStack()
        self.env = {
            **os.environ,
            "PYTHONPATH": str(ROOT / "apps/api") + os.pathsep + str(ROOT / "tests/support"),
            "SIMULATION_MODE": mode,
            "DATABASE_URL": f"sqlite+aiosqlite:///{self.directory}/portal.db",
        }
        self.processes = []

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            self.cuekb = self.start("cuekb_app")
            self.voice = self.start("voicechat_app")
            self.llm = self.start("llm_app")
            self.env.update(
                SIMULATION_CUEKB_URL=self.cuekb, SIMULATION_VOICE_URL=self.voice, SIMULATION_LLM_URL=self.llm
            )
            migration = subprocess.run(
                [sys.executable, "-m", "alembic", "-c", "apps/api/alembic.ini", "upgrade", "head"],
                cwd=ROOT,
                env=self.env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            if migration.returncode:
                raise RuntimeError("Isolated migration failed: " + migration.stderr)
            self.api = self.start("portal_app", port=self.portal_port, health="/health/ready")
            return self
        except BaseException:
            self.cleanup.close()
            raise

    def __exit__(self, *exc):
        self.cleanup.close()

    def start(self, factory, port=0, health="/health"):
        if not port:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
        log_path = self.directory / (factory + ".log")
        log = self.cleanup.enter_context(log_path.open("w"))
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "simulated_services:" + factory,
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-access-log",
            ],
            cwd=ROOT,
            env=self.env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.processes.append(process)
        self.cleanup.callback(self.stop, process)
        url = f"http://127.0.0.1:{port}"
        with httpx.Client(trust_env=False, timeout=0.3) as client:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    if client.get(url + health).status_code == 200:
                        return url
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
        raise RuntimeError(f"Simulator {factory} did not start: {log_path.read_text()}")

    @staticmethod
    def stop(process):
        if process.poll() is not None:
            return
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
