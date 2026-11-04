"""Supervise the original fraud API/UI and Agent 6 from this repository only."""

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import webbrowser
from contextlib import ExitStack
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from agent6_mlops_lifecycle.assurance.bootstrap import configure_fraud_benchmark
from agent6_mlops_lifecycle.cli.monitor import ensure_monitor_token
from agent6_mlops_lifecycle.paths import ENV_FILE, ROOT


def stop_child(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=False,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-port", type=int, default=8021)
    parser.add_argument("--port", type=int, default=8026)
    parser.add_argument("--app-port", type=int, default=8521)
    parser.add_argument(
        "--api-only", action="store_true", help="Run API and monitor without Streamlit"
    )
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument("--model-python", type=Path, default=Path(sys.executable))
    args = parser.parse_args(argv)
    ports = [args.api_port, args.port] + ([] if args.api_only else [args.app_port])
    if len(set(ports)) != len(ports) or any(not 1024 <= port <= 65535 for port in ports):
        parser.error("Select distinct localhost ports between 1024 and 65535")
    for port in ports:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
    model_python = args.model_python.resolve(strict=True)
    settings = ensure_monitor_token()
    from agent6_mlops_lifecycle.lifecycle.settings import LifecycleSettings

    if len(LifecycleSettings().integration_token.get_secret_value()) < 32:
        with ENV_FILE.open("a", encoding="utf-8") as stream:
            stream.write("\nAGENT6_LIFECYCLE_INTEGRATION_TOKEN=" + secrets.token_urlsafe(32) + "\n")
    data = settings.state_database.parent
    data.mkdir(parents=True, exist_ok=True)
    registry = configure_fraud_benchmark(
        ROOT,
        settings.benchmark_database,
        data / "benchmark-registry.json",
        settings.scope(),
        include_streamlit=True,
    )
    business_database_url = settings.business_database_url.get_secret_value() or "sqlite:///" + str(
        settings.source_database
    )
    env = os.environ.copy()
    env.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "MLFLOW_DISABLE_AGENT_HINT": "1",
            "AGENT6_MONITOR_TOKEN": settings.token.get_secret_value(),
            "AGENT6_MONITOR_OUTCOME_TOKEN": settings.outcome_token.get_secret_value(),
            "AGENT6_OUTCOME_SERVICE_TOKEN": settings.outcome_token.get_secret_value(),
            "AGENT6_PREDICTION_DATABASE": str(settings.source_database),
            "AGENT6_TENANT_ID": settings.tenant_id,
            "AGENT6_PROJECT_ID": settings.project_id,
            "AGENT6_MODEL_ID": settings.model_id,
            "AGENT6_BUSINESS_SOURCE_IDS": ",".join(settings.business_source_ids),
            "AGENT6_MONITOR_PREDICTION_URL": f"http://127.0.0.1:{args.api_port}/predict",
            "AGENT6_MONITOR_APPLICATION_HEALTH_URL": ""
            if args.api_only
            else f"http://127.0.0.1:{args.app_port}/_stcore/health",
            "AGENT6_MONITOR_BUSINESS_DATABASE_URL": business_database_url,
            "AGENT6_MONITOR_BENCHMARK_REGISTRY": str(registry),
            "AGENT6_MONITOR_BENCHMARK_PYTHON": str(model_python),
            "AGENT6_LIFECYCLE_MODEL_PYTHON": str(model_python),
        }
    )
    commands = [
        (
            "api",
            [
                str(model_python),
                "-B",
                "-m",
                "uvicorn",
                "API.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(args.api_port),
                "--no-access-log",
            ],
            args.api_port,
            "/health",
        )
    ]
    if not args.api_only:
        commands.append(
            (
                "streamlit",
                [
                    str(model_python),
                    "-B",
                    "-m",
                    "streamlit",
                    "run",
                    "app.py",
                    "--server.address=127.0.0.1",
                    f"--server.port={args.app_port}",
                    "--server.headless=true",
                    "--server.fileWatcherType=none",
                    "--browser.gatherUsageStats=false",
                ],
                args.app_port,
                "/_stcore/health",
            )
        )
    commands.append(
        (
            "monitor",
            [
                sys.executable,
                "-B",
                "-m",
                "agent6_mlops_lifecycle.cli.monitor",
                "--port",
                str(args.port),
            ],
            args.port,
            "/health",
        )
    )
    children: list[subprocess.Popen[bytes]] = []
    launched_at = time.time()
    monitor_url = f"http://127.0.0.1:{args.port}"
    try:
        with ExitStack() as stack:
            try:
                for name, command, port, route in commands:
                    log = stack.enter_context((data / f"{name}.log").open("ab"))
                    process = subprocess.Popen(
                        command,
                        cwd=ROOT,
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
                    children.append(process)
                    deadline = time.monotonic() + 90
                    while True:
                        if process.poll() is not None:
                            raise RuntimeError(f"{name} exited; inspect {data / (name + '.log')}")
                        try:
                            with urlopen(f"http://127.0.0.1:{port}{route}", timeout=2) as response:
                                if response.status == 200:
                                    break
                        except (URLError, TimeoutError):
                            pass
                        if time.monotonic() >= deadline:
                            raise RuntimeError(f"{name} startup timed out; inspect its local log")
                        time.sleep(0.2)
                (data / "runner.pid").write_text(str(os.getpid()), encoding="utf-8")
                print(
                    json.dumps(
                        {
                            "api": f"http://127.0.0.1:{args.api_port}",
                            "dashboard": monitor_url,
                            "application": None
                            if args.api_only
                            else f"http://127.0.0.1:{args.app_port}",
                            "mode": "AUTOMATIC_REAL_SERVING",
                            "database": str(settings.source_database),
                        }
                    ),
                    flush=True,
                )
                if args.open_browser:
                    if not args.api_only:
                        webbrowser.open(f"http://127.0.0.1:{args.app_port}")
                    webbrowser.open(
                        monitor_url + "/#token=" + quote(settings.token.get_secret_value(), safe="")
                    )
                while True:
                    stop = data / "stop.request"
                    if stop.exists() and stop.stat().st_mtime >= launched_at:
                        break
                    if any(process.poll() is not None for process in children):
                        raise RuntimeError("An owned service exited; inspect its local log")
                    time.sleep(0.5)
            finally:
                if children and children[-1].poll() is None:
                    request = Request(
                        monitor_url + "/api/admin/stop-worker",
                        data=b"{}",
                        method="POST",
                        headers={"Authorization": "Bearer " + settings.token.get_secret_value()},
                    )
                    try:
                        with urlopen(request, timeout=15):
                            pass
                    except (URLError, TimeoutError):
                        pass
                for process in reversed(children):
                    stop_child(process)
    except KeyboardInterrupt:
        print("Owned services stopped; stored evidence retained.")
