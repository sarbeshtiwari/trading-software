"""Disposable local database server for destructive recovery tests, never owner services."""

import asyncio
import os
import re
import socket
from contextlib import asynccontextmanager
from uuid import uuid4

from sqlalchemy.engine import URL


async def docker(*arguments, environment=None, checked=True):
    process = await asyncio.create_subprocess_exec(
        "docker", *arguments, env=environment,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        if checked and process.returncode:
            raise RuntimeError(f"Isolated Docker {arguments[0]} failed ({process.returncode})")
        return process.returncode, stdout.decode().strip()
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()


class TestDatabaseServer:
    def __init__(self):
        self.identity = uuid4().hex
        self.name = "ats-recovery-test-" + self.identity
        self.identifier = None
        self.url = None
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            self.host_port = reservation.getsockname()[1]

    async def verify(self):
        if not self.identifier or not re.fullmatch(r"[a-f0-9]{64}", self.identifier):
            raise RuntimeError("Test container identity unavailable")
        _code, label = await docker(
            "inspect", "--format", '{{index .Config.Labels "ats.recovery.test"}}', self.identifier
        )
        if label != self.identity:
            raise RuntimeError("Refusing to control a container not owned by this test")

    async def start(self):
        await self.verify()
        await docker("start", self.identifier)
        for _attempt in range(60):
            code, _output = await docker(
                "exec", self.identifier, "pg_isready", "-h", "127.0.0.1",
                "-U", "ats_test", "-d", "postgres",
                checked=False,
            )
            if code == 0:
                return
            await asyncio.sleep(0.5)
        raise RuntimeError("Isolated PostgreSQL did not become ready")

    async def crash(self):
        await self.verify()
        await docker("kill", "--signal", "KILL", self.identifier)
        _code, running = await docker("inspect", "--format", "{{.State.Running}}", self.identifier)
        if running != "false":
            raise RuntimeError("Isolated database crash not confirmed")


@asynccontextmanager
async def isolated_server():
    server = TestDatabaseServer()
    password = uuid4().hex
    image = os.environ.get("ATS_TEST_POSTGRES_IMAGE", "timescale/timescaledb:2.17.2-pg16")
    await docker("image", "inspect", "--format", "{{.Id}}", image)
    try:
        _code, server.identifier = await docker(
            "create", "--pull", "never", "--name", server.name,
            "--label", "ats.recovery.test=" + server.identity,
            "--publish", f"127.0.0.1:{server.host_port}:5432", "--env", "POSTGRES_USER=ats_test",
            "--env", "POSTGRES_PASSWORD", "--env", "POSTGRES_DB=postgres", image,
            environment={**os.environ, "POSTGRES_PASSWORD": password},
        )
        await server.start()
        _code, binding = await docker("port", server.identifier, "5432/tcp")
        if not re.fullmatch(r"127\.0\.0\.1:\d+", binding):
            raise RuntimeError("Isolated database must bind to loopback only")
        if int(binding.rsplit(":", 1)[1]) != server.host_port:
            raise RuntimeError("Isolated database endpoint changed")
        server.url = URL.create("postgresql+asyncpg", username="ats_test", password=password,
                                host="127.0.0.1", port=int(binding.rsplit(":", 1)[1]),
                                database="postgres")
        yield server
    finally:
        if server.identifier:
            await server.verify()
            await docker("rm", "--force", "--volumes", server.identifier)
