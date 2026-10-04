"""Tests-only loopback TCP relay; never records or fabricates database traffic."""

import asyncio


class NetworkProxy:
    def __init__(self, host, port):
        if host not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Recovery proxy requires a local test database")
        self.host, self.port = host, port
        self.server = None
        self.listen_port = 0
        self.connections = set()
        self.tasks = set()

    async def start(self):
        self.server = await asyncio.start_server(self._accept, "127.0.0.1", self.listen_port)
        self.listen_port = self.server.sockets[0].getsockname()[1]

    async def _copy(self, reader, writer):
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()

    async def _accept(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.connections.add(writer)
        remote = None
        copies = []
        try:
            upstream, remote = await asyncio.open_connection(self.host, self.port)
            self.connections.add(remote)
            copies = [asyncio.create_task(self._copy(reader, remote)),
                      asyncio.create_task(self._copy(upstream, writer))]
            await asyncio.wait(copies, return_when=asyncio.FIRST_COMPLETED)
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            for copy in copies:
                copy.cancel()
            await asyncio.gather(*copies, return_exceptions=True)
            for stream in (writer, remote):
                if stream is not None:
                    stream.close()
                    self.connections.discard(stream)
            self.tasks.discard(task)

    async def disconnect(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        for connection in list(self.connections):
            connection.transport.abort()
        pending = list(self.tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
