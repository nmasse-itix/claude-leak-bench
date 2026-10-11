"""Recording reverse proxy for the Anthropic API.

Claude Code is pointed at it with ANTHROPIC_BASE_URL. Every request body is
recorded (decompressed) before being forwarded unchanged, so a test can check
what actually left the machine. Responses are streamed back untouched.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import threading
import zlib
from dataclasses import dataclass, field

import aiohttp
from aiohttp import web

DEFAULT_UPSTREAM = "https://api.anthropic.com"

# Hop-by-hop headers (RFC 9110) plus the ones aiohttp recomputes.
_SKIP_REQUEST_HEADERS = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "upgrade"}
_SKIP_RESPONSE_HEADERS = {"content-length", "connection", "keep-alive", "transfer-encoding", "upgrade"}


@dataclass
class Exchange:
    method: str
    path: str
    status: int | None
    body: str  # decoded request body, best effort


def _decode(body: bytes, encoding: str) -> str:
    encoding = encoding.lower().strip()
    try:
        if encoding == "gzip":
            body = gzip.decompress(body)
        elif encoding == "deflate":
            body = zlib.decompress(body)
        elif encoding == "zstd":
            import zstandard

            body = zstandard.ZstdDecompressor().decompressobj().decompress(body)
        elif encoding == "br":
            import brotli  # optional

            body = brotli.decompress(body)
    except Exception:  # keep the raw bytes: a failed decode must not hide data
        pass
    return body.decode("utf-8", errors="replace")


@dataclass
class RecordingProxy:
    upstream: str = field(default_factory=lambda: os.environ.get("BENCH_UPSTREAM", DEFAULT_UPSTREAM))
    exchanges: list[Exchange] = field(default_factory=list)
    port: int = 0
    host: str = "127.0.0.1"
    # "oauth:<token>" or "apikey:<key>": replaces whatever credential the client
    # sent, so the sandboxed client only ever holds a dummy one.
    auth: str | None = None
    record_file: str | None = None  # append each exchange as a JSON line

    def __post_init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runner: web.AppRunner | None = None
        self._session: aiohttp.ClientSession | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def wire_text(self) -> str:
        """Every recorded request body, concatenated."""
        with self._lock:
            return "\n".join(e.body for e in self.exchanges)

    def start(self) -> "RecordingProxy":
        ready = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(ready,), daemon=True)
        self._thread.start()
        if not ready.wait(10):
            raise RuntimeError("proxy did not start")
        return self

    def stop(self) -> None:
        if self._loop is None:
            return
        fut = asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
        fut.result(10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(10)

    def _run_forever_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._loop.run_until_complete(self._startup())
        print(f"listening on {self.host}:{self.port}", flush=True)
        try:
            self._loop.run_forever()
        except KeyboardInterrupt:
            pass

    def _run(self, ready: threading.Event) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._startup())
        ready.set()
        self._loop.run_forever()

    async def _startup(self) -> None:
        self._session = aiohttp.ClientSession(auto_decompress=False, trust_env=True,
                                              timeout=aiohttp.ClientTimeout(total=None, sock_read=600))
        app = web.Application(client_max_size=256 * 1024 * 1024)
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    async def _shutdown(self) -> None:
        if self._runner:
            await self._runner.cleanup()
        if self._session:
            await self._session.close()

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        assert self._session is not None
        body = await request.read()
        exchange = Exchange(request.method, request.path_qs, None,
                            _decode(body, request.headers.get("Content-Encoding", "")))
        with self._lock:
            self.exchanges.append(exchange)
            if self.record_file:
                with open(self.record_file, "a") as f:
                    f.write(json.dumps({"method": exchange.method, "path": exchange.path,
                                        "body": exchange.body}) + "\n")

        headers = {k: v for k, v in request.headers.items() if k.lower() not in _SKIP_REQUEST_HEADERS}
        if self.auth:
            headers = _inject_auth(headers, self.auth)
        url = self.upstream.rstrip("/") + request.path_qs
        async with self._session.request(request.method, url, headers=headers, data=body) as upstream:
            exchange.status = upstream.status
            response = web.StreamResponse(status=upstream.status, reason=upstream.reason)
            for k, v in upstream.headers.items():
                if k.lower() not in _SKIP_RESPONSE_HEADERS:
                    response.headers.add(k, v)
            await response.prepare(request)
            async for chunk in upstream.content.iter_any():
                await response.write(chunk)
            await response.write_eof()
            return response


def _inject_auth(headers: dict[str, str], auth: str) -> dict[str, str]:
    kind, _, secret = auth.strip().partition(":")
    out = {k: v for k, v in headers.items() if k.lower() not in ("authorization", "x-api-key")}
    if kind == "oauth":
        out["Authorization"] = f"Bearer {secret}"
    elif kind == "apikey":
        out["x-api-key"] = secret
    else:
        raise ValueError(f"unknown auth kind {kind!r}")
    return out


def main() -> None:
    """Container entry point: listen on 0.0.0.0:8080, record to /records."""
    auth_file = os.environ.get("BENCH_AUTH_FILE", "/run/secrets/bench-auth")
    auth = open(auth_file).read() if os.path.exists(auth_file) else None
    proxy = RecordingProxy(host="0.0.0.0", port=int(os.environ.get("BENCH_PORT", "8080")), auth=auth,
                           record_file=os.environ.get("BENCH_RECORD_FILE", "/records/wire.jsonl"))
    proxy._run_forever_main()


if __name__ == "__main__":
    main()
