from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

def wait_for_app(timeout: float = 15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:5000", timeout=1):
                return
        except Exception:
            time.sleep(0.4)
    raise RuntimeError("Target application did not start")


def run(command: list[str], allow_nonzero: bool = False):
    result = subprocess.run(command, cwd=ROOT, env=os.environ.copy())
    if result.returncode and not allow_nonzero:
        raise SystemExit(result.returncode)


def main():
    if not os.getenv("GEMINI_API_KEY"):
        raise SystemExit("Set GEMINI_API_KEY before running the demo")
    server = subprocess.Popen([sys.executable, "mock_app.py"], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        wait_for_app()
        run([
            sys.executable,
            "automation.py",
            "discover",
            "--name",
            "lookup_savings_balance",
            "--goal",
            "Look up member 12345 and return the current savings balance",
            "--param",
            "member_id=12345",
        ])
        run([
            sys.executable,
            "automation.py",
            "replay",
            "--artifact",
            "artifacts/lookup_savings_balance.json",
            "--param",
            "member_id=67890",
            "--headless",
        ])
        run([
            sys.executable,
            "automation.py",
            "replay",
            "--artifact",
            "artifacts/lookup_savings_balance.json",
            "--param",
            "member_id=99999",
            "--headless",
        ])
        
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    main()
