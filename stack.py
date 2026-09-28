"""Run the whole platform locally without Docker.

    python stack.py            # start everything, Ctrl-C to stop
    python stack.py --fresh    # wipe .data first

Starts 7 Finalize processes (gateway, event bus, 5 domain services) plus the
single Analyze monolith process - which is itself the first lesson: count the
processes, ports and databases on each side.
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
SHARED = ROOT / "finalize" / "shared"

# name -> (directory under finalize/services, port offset)
FINALIZE = {
    "event_bus": ("event_bus", 10),
    "template": ("template_service", 1),
    "agreement": ("agreement_service", 2),
    "approval": ("approval_service", 3),
    "review": ("review_service", 4),
    "collaboration": ("collaboration_service", 5),
    "gateway": ("gateway", 0),
}


@dataclass
class Stack:
    base_port: int = 8000
    analyze_port: int = 9000
    data_dir: Path = ROOT / ".data"
    procs: dict = field(default_factory=dict)

    def url(self, name: str) -> str:
        if name == "analyze":
            return f"http://127.0.0.1:{self.analyze_port}"
        return f"http://127.0.0.1:{self.base_port + FINALIZE[name][1]}"

    def env(self) -> dict:
        env = {**os.environ, "PYTHONPATH": str(SHARED), "DATA_DIR": str(self.data_dir), "LOG_LEVEL": "WARNING"}
        env.update({f"{n.upper()}_SERVICE_URL": self.url(n) for n in FINALIZE})
        return env

    def start_service(self, name: str) -> None:
        env = self.env()
        if name == "analyze":
            env.update(ANALYZE_DB=str(self.data_dir / "analyze.db"), FINALIZE_EVENT_BUS_URL=self.url("event_bus"),
                       ANALYZE_PUBLIC_URL=self.url("analyze"))
            app_dir, port = ROOT / "analyze", self.analyze_port
            target = "analyze_app.main:app"
        else:
            app_dir, port = ROOT / "finalize" / "services" / FINALIZE[name][0], self.base_port + FINALIZE[name][1]
            target = "main:app"
        self.procs[name] = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", target, "--app-dir", str(app_dir), "--port", str(port),
             "--log-level", "warning"],
            env=env, cwd=ROOT,
        )

    def stop_service(self, name: str) -> None:
        proc = self.procs.pop(name, None)
        if proc:
            proc.terminate()
            proc.wait(timeout=10)

    def wait_healthy(self, names=None, timeout: float = 30) -> None:
        deadline = time.time() + timeout
        for name in names or self.procs:
            while True:
                try:
                    if httpx.get(f"{self.url(name)}/health", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.time() > deadline:
                    raise RuntimeError(f"{name} did not become healthy")
                time.sleep(0.2)

    def up(self, fresh: bool = False) -> "Stack":
        if fresh and self.data_dir.exists():
            shutil.rmtree(self.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for name in [*FINALIZE, "analyze"]:
            self.start_service(name)
        self.wait_healthy()
        return self

    def down(self) -> None:
        for name in list(self.procs):
            self.stop_service(name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fresh", action="store_true", help="delete all data before starting")
    args = parser.parse_args()
    stack = Stack().up(fresh=args.fresh)
    print("Finalize gateway  :", stack.url("gateway"), "(header X-API-Key: dev-key)  docs per service at /docs")
    print("Analyze monolith  :", stack.url("analyze"), " dashboard at /  API docs at /docs")
    print("Running", len(stack.procs), "processes. Ctrl-C to stop.")
    try:
        while all(p.poll() is None for p in stack.procs.values()):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stack.down()
