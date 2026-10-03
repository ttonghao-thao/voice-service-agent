"""Run local independent-service network and browser tests without Docker or GPUs."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests/support"))
from network_stack import NetworkStack  # noqa: E402


def counts(path):
    if not path.exists():
        return {}
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    totals = {
        key: sum(int(s.attrib.get(key, 0)) for s in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }
    totals["passed"] = totals["tests"] - totals["failures"] - totals["errors"] - totals["skipped"]
    return totals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network-only", action="store_true")
    parser.add_argument("--browser-only", action="store_true")
    parser.add_argument("--mode", choices=["direct", "external", "both"], default="both")
    args = parser.parse_args()
    if args.network_only and args.browser_only:
        parser.error("Select at most one of --network-only and --browser-only")
    output = ROOT / "artifacts/simulation"
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "simulation": True,
        "real_upstream_inference": False,
        "database": "SQLite, Alembic 0007",
        "transport": "loopback HTTP/WebSocket/SSE",
        "checks": [],
    }
    try:
        if not args.browser_only:
            junit = output / "network.xml"
            command = [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "tests/integration/test_simulated_network.py",
                "--junitxml",
                str(junit),
            ]
            if args.mode != "both":
                command += ["-k", "mode_" + args.mode]
            result = subprocess.run(command, cwd=ROOT, check=False)
            report["checks"].append(
                {"name": "network", "mode": args.mode, "exit_code": result.returncode, **counts(junit)}
            )
            if result.returncode:
                return result.returncode
        if not args.network_only:
            modes = ["direct", "external"] if args.mode == "both" else [args.mode]
            for mode in modes:
                with tempfile.TemporaryDirectory(prefix="voice-api-simulation-") as directory:
                    with NetworkStack(directory, mode, portal_port=8000) as stack:
                        env = {
                            **os.environ,
                            **{k: v for k, v in stack.env.items() if k.startswith("SIMULATION_")},
                        }
                        junit = output / mode / "browser.xml"
                        junit.parent.mkdir(parents=True, exist_ok=True)
                        env["PLAYWRIGHT_JUNIT_OUTPUT_FILE"] = str(junit)
                        with (output / (mode + "-vite.log")).open("w") as log:
                            vite = subprocess.Popen(
                                ["npm", "run", "dev", "--", "--port", "5173", "--strictPort"],
                                cwd=ROOT / "apps/web",
                                env=env,
                                stdout=log,
                                stderr=subprocess.STDOUT,
                                start_new_session=True,
                            )
                            try:
                                with httpx.Client(trust_env=False, timeout=0.3) as http:
                                    deadline = time.monotonic() + 15
                                    while time.monotonic() < deadline and vite.poll() is None:
                                        try:
                                            if http.get("http://localhost:5173").status_code == 200:
                                                break
                                        except httpx.HTTPError:
                                            pass
                                        time.sleep(0.05)
                                    else:
                                        raise RuntimeError("Vite failed to start on port 5173")
                                result = subprocess.run(
                                    [
                                        "npx",
                                        "--no-install",
                                        "playwright",
                                        "test",
                                        "--config",
                                        "../../tests/support/playwright.simulated.config.ts",
                                        "--output",
                                        str(output / mode / "browser-results"),
                                        "--reporter=line,junit",
                                    ],
                                    cwd=ROOT / "apps/web",
                                    env=env,
                                    check=False,
                                )
                                report["checks"].append(
                                    {
                                        "name": "browser",
                                        "mode": mode,
                                        "exit_code": result.returncode,
                                        **counts(junit),
                                    }
                                )
                                if result.returncode:
                                    return result.returncode
                            finally:
                                NetworkStack.stop(vite)
                                for path in Path(directory).glob("*.log"):
                                    (output / (mode + "-" + path.name)).write_text(path.read_text())
        return 0
    except Exception as error:
        report["checks"].append({"name": "orchestration", "exit_code": 1, "error_type": type(error).__name__})
        raise
    finally:
        (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
