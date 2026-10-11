"""Recording reverse proxy for the Anthropic API, run in its own container.

Claude Code is pointed at it with ANTHROPIC_BASE_URL. Every request body is
recorded (decompressed) before being forwarded unchanged, so a test can check
what actually left the machine. Responses are streamed back untouched.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import zlib

import aiohttp
from aiohttp import web

DEFAULT_UPSTREAM = "https://api.anthropic.com"

# Hop-by-hop headers (RFC 9110) plus the ones aiohttp recomputes.
_SKIP_REQUEST_HEADERS = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "upgrade"}
_SKIP_RESPONSE_HEADERS = {"content-length", "connection", "keep-alive", "transfer-encoding", "upgrade"}


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


class RecordingProxy:
    """Forwards every request to `upstream` and appends its body to `record_file`."""

    def __init__(self, upstream: str, auth: str | None, record_file: str) -> None:
        self.upstream = upstream
        # "oauth:<token>" or "apikey:<key>": replaces whatever credential the client
        # sent, so the sandboxed client only ever holds a dummy one.
        self.auth = auth
        self.record_file = record_file
        self._session: aiohttp.ClientSession | None = None

    async def serve(self, host: str, port: int) -> None:
        self._session = aiohttp.ClientSession(auto_decompress=False, trust_env=True,
                                              timeout=aiohttp.ClientTimeout(total=None, sock_read=600))
        app = web.Application(client_max_size=256 * 1024 * 1024)
        app.router.add_route("*", "/{tail:.*}", self._handle)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        print(f"listening on {host}:{port}", flush=True)
        await asyncio.Event().wait()

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        assert self._session is not None
        body = await request.read()
        with open(self.record_file, "a") as f:
            f.write(json.dumps({"method": request.method, "path": request.path_qs,
                                "body": _decode(body, request.headers.get("Content-Encoding", ""))}) + "\n")

        headers = {k: v for k, v in request.headers.items() if k.lower() not in _SKIP_REQUEST_HEADERS}
        if self.auth:
            headers = _inject_auth(headers, self.auth)
        url = self.upstream.rstrip("/") + request.path_qs
        async with self._session.request(request.method, url, headers=headers, data=body) as upstream:
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
    proxy = RecordingProxy(os.environ.get("BENCH_UPSTREAM", DEFAULT_UPSTREAM), auth,
                           os.environ.get("BENCH_RECORD_FILE", "/records/wire.jsonl"))
    try:
        asyncio.run(proxy.serve("0.0.0.0", int(os.environ.get("BENCH_PORT", "8080"))))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
