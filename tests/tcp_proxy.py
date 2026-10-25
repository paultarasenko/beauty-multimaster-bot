"""Мини TCP-прокси для тестов: имитирует «Redis пропал и вернулся» без остановки настоящего Redis."""

import asyncio


class TcpProxy:
    def __init__(self, target_host: str, target_port: int) -> None:
        self._target = (target_host, target_port)
        self._server: asyncio.AbstractServer | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task] = set()
        self.port = 0

    @property
    def running(self) -> bool:
        return self._server is not None

    async def start(self, port: int = 0) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        """«Redis упал»: перестаём принимать соединения и рвём все открытые."""
        server, self._server = self._server, None
        if server is not None:
            server.close()  # новые соединения не принимаем
        # Сначала рвём открытые соединения: в Python 3.12 wait_closed() ждёт их завершения.
        for writer in list(self._writers):
            writer.close()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._writers.clear()
        if server is not None:
            await asyncio.wait_for(server.wait_closed(), 5)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(*self._target)
        except OSError:
            writer.close()
            return
        self._writers.update({writer, upstream_writer})

        async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while data := await src.read(65536):
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, asyncio.CancelledError):
                pass
            finally:
                dst.close()

        for coro in (pipe(reader, upstream_writer), pipe(upstream_reader, writer)):
            task = asyncio.create_task(coro)
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
