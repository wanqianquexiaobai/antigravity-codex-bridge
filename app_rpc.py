"""Bounded-lifetime stdio client for the version-pinned Codex App Server."""
import asyncio
import json
import os
import signal


class TransportError(ConnectionError):
    pass


class RpcError(Exception):
    def __init__(self, error):
        self.error = error
        super().__init__(str(error.get('message', 'App Server request failed')))


class AppServer:
    def __init__(self, binary, environment, cwd=None, stderr=None):
        self.binary, self.environment, self.cwd, self.stderr = binary, environment, cwd, stderr
        self.sequence = 0
        self.pending = {}
        self.events = asyncio.Queue(maxsize=1024)
        self.process = None
        self.reader = None
        self.closed = False

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(str(self.binary), '--no-daemon',
            '-c', 'agents.enabled=false', 'app-server', '--stdio', cwd=self.cwd,
            env=self.environment, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=self.stderr or asyncio.subprocess.DEVNULL, start_new_session=True,
            limit=8 * 1024 * 1024)
        self.reader = asyncio.create_task(self._read())
        await self.call('initialize', {'clientInfo': {'name': 'antigravity_codex_bridge',
            'version': '2.0.0'}, 'capabilities': {'experimentalApi': True}})
        await self.send({'method': 'initialized', 'params': {}})
        return self

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if 'id' in message and 'method' not in message:
                    future = self.pending.pop(message['id'], None)
                    if future and not future.done():
                        if 'error' in message:
                            future.set_exception(RpcError(message['error']))
                        else:
                            future.set_result(message.get('result', {}))
                else:
                    await self.events.put(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(TransportError('App Server connection closed'))
            self.pending.clear()
            if not self.closed:
                await self.events.put({'method': '_disconnected', 'params': {}})

    async def send(self, message):
        if not self.process or self.process.returncode is not None:
            raise TransportError('App Server is not running')
        try:
            self.process.stdin.write((json.dumps(message, ensure_ascii=False) + '\n').encode())
            await self.process.stdin.drain()
        except (OSError, RuntimeError) as exc:
            raise TransportError('App Server input closed') from exc

    async def call(self, method, params=None, timeout=30):
        self.sequence += 1
        request_id = 'bridge-' + str(self.sequence)
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({'id': request_id, 'method': method, 'params': params or {}})
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise TransportError('App Server request timed out: ' + method) from exc
        finally:
            self.pending.pop(request_id, None)

    async def close(self):
        self.closed = True
        if self.process and self.process.returncode is None:
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except asyncio.TimeoutError:
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(self.process.wait(), 2)
                except asyncio.TimeoutError:
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await self.process.wait()
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
