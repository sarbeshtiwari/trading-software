"""Run historical services outside the API process and its global state."""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _launch_inputs(manifest, recording, settings):
    paths = [str(Path(path).resolve(strict=True)) for path in (manifest, recording, settings)]
    backend = Path(__file__).resolve().parents[2]
    inherited = {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "HOME", "USERPROFILE"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in inherited}
    environment["PYTHONPATH"] = str(backend)
    environment["PYTHONIOENCODING"] = "utf-8"
    return paths, environment


def launch(manifest, recording, settings, *, timeout_seconds=3600):
    paths, environment = _launch_inputs(manifest, recording, settings)
    with tempfile.TemporaryDirectory(prefix="ats-history-") as directory:
        return subprocess.run(
            [sys.executable, "-m", "app.backtest.child", *paths],
            cwd=directory,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_seconds,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )


async def launch_async(manifest, recording, settings, *, timeout_seconds=3600):
    paths, environment = _launch_inputs(manifest, recording, settings)
    with tempfile.TemporaryDirectory(prefix="ats-history-") as directory:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "app.backtest.child",
            *paths,
            cwd=directory,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        communication = asyncio.create_task(process.communicate())
        try:
            stdout, stderr = await asyncio.wait_for(asyncio.shield(communication), timeout_seconds)
            return subprocess.CompletedProcess(
                paths, process.returncode, stdout.decode("utf-8"), stderr.decode("utf-8")
            )
        finally:
            if process.returncode is None:
                process.kill()
            await communication


def main():
    parser = argparse.ArgumentParser(description="Isolated PAPER historical execution")
    parser.add_argument("manifest")
    parser.add_argument("recording")
    parser.add_argument("settings")
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    arguments = parser.parse_args()
    try:
        if arguments.timeout_seconds <= 0:
            raise ValueError("positive timeout required")
        result = launch(
            arguments.manifest,
            arguments.recording,
            arguments.settings,
            timeout_seconds=arguments.timeout_seconds,
        )
        report = json.loads(result.stdout)
        if (
            not isinstance(report, dict)
            or set(report) - {"status", "run_id", "error_type", "simulated"}
            or report.get("simulated") is not True
        ):
            raise ValueError("invalid child response")
        print(json.dumps(report))
        return result.returncode if result.returncode in (0, 1, 2, 3) else 2
    except Exception as error:
        print(
            json.dumps({"status": "FAILED", "error_type": type(error).__name__, "simulated": True})
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
