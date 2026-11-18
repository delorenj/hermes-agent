"""Read-only Telegram requests may fail over after a response timeout; sends must not replay."""

import httpx
import pytest

from plugins.platforms.telegram import telegram_network as tnet


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [
    ("POST", "/botTOKEN/getFile"),
    ("GET", "/file/botTOKEN/voice/file.oga"),
])
@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ReadError])
@pytest.mark.parametrize("fallback_ips", [["149.154.166.110"], ["149.154.166.110", "149.154.167.220"]])
async def test_readonly_retrieval_fails_over_and_discards_failed_pool(monkeypatch, method, path, error_type, fallback_ips):
    calls, instances = [], []

    class Transport(httpx.AsyncBaseTransport):
        closed = False

        async def handle_async_request(self, request):
            calls.append(request)
            # Consume the POST body, just as a real transport does before a read timeout.
            await request.aread()
            if request.url.host == "149.154.166.110":
                raise error_type("stalled response")
            return httpx.Response(200, content=b"retrieved")

        async def aclose(self):
            self.closed = True

    def factory(**kwargs):
        transport = Transport()
        instances.append(transport)
        return transport

    monkeypatch.setattr(tnet.httpx, "AsyncHTTPTransport", factory)
    transport = tnet.TelegramFallbackTransport(fallback_ips)
    transport._sticky_ip = "149.154.166.110"
    request = httpx.Request(method, "https://api.telegram.org" + path, content=b"file_id=opaque")
    try:
        response = await transport.handle_async_request(request)
        assert response.status_code == 200
        assert [r.url.host for r in calls] == ["149.154.166.110", "149.154.167.220"]
        assert calls[1].content == b"file_id=opaque"
        assert calls[1].headers["host"] == "api.telegram.org"
        assert calls[1].extensions["sni_hostname"] == "api.telegram.org"
        assert transport._sticky_ip == "149.154.167.220"
        assert instances[1].closed
        assert "149.154.166.110" not in transport._fallbacks
    finally:
        await transport.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["sendVoice", "sendAudio", "sendMessage", "deleteMessage"])
async def test_ambiguous_send_read_timeout_is_not_replayed(monkeypatch, path):
    calls = []

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            calls.append(request.url.host)
            raise httpx.ReadTimeout("response lost after possible delivery")

        async def aclose(self):
            pass

    monkeypatch.setattr(tnet.httpx, "AsyncHTTPTransport", lambda **kw: Transport())
    transport = tnet.TelegramFallbackTransport(["149.154.166.110", "149.154.167.220"])
    try:
        with pytest.raises(httpx.ReadTimeout):
            await transport.handle_async_request(httpx.Request("POST", "https://api.telegram.org/botTOKEN/" + path))
        assert calls == ["149.154.166.110"]
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_streaming_post_is_not_replayed(monkeypatch):
    calls = []

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            calls.append(request.url.host)
            raise httpx.ReadTimeout("response lost")

        async def aclose(self):
            pass

    async def body():
        yield b"file_id=opaque"

    monkeypatch.setattr(tnet.httpx, "AsyncHTTPTransport", lambda **kw: Transport())
    transport = tnet.TelegramFallbackTransport(["149.154.166.110", "149.154.167.220"])
    try:
        with pytest.raises(httpx.ReadTimeout):
            await transport.handle_async_request(httpx.Request("POST", "https://api.telegram.org/botTOKEN/getFile", content=body()))
        assert len(calls) == 1
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_partial_download_body_failure_is_retried_from_zero(monkeypatch):
    calls, streams = [], []

    class Stream(httpx.AsyncByteStream):
        closed = False

        def __init__(self, fail):
            self.fail = fail
            streams.append(self)

        async def __aiter__(self):
            yield b"partial" if self.fail else b"complete"
            if self.fail:
                raise httpx.ReadTimeout("body stalled")

        async def aclose(self):
            self.closed = True

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            calls.append(request.url.host)
            return httpx.Response(200, stream=Stream(request.url.host == "149.154.166.110"))

        async def aclose(self):
            pass

    monkeypatch.setattr(tnet.httpx, "AsyncHTTPTransport", lambda **kw: Transport())
    transport = tnet.TelegramFallbackTransport(["149.154.166.110", "149.154.167.220"])
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.get("https://api.telegram.org/file/botTOKEN/voice/file.oga")
    assert response.content == b"complete"
    assert calls == ["149.154.166.110", "149.154.167.220"]
    assert all(stream.closed for stream in streams)


@pytest.mark.asyncio
async def test_exhaustion_tries_each_path_once_and_releases_pools(monkeypatch):
    calls = []

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            calls.append(request.url.host)
            raise httpx.ReadTimeout("unavailable")

        async def aclose(self):
            pass

    monkeypatch.setattr(tnet.httpx, "AsyncHTTPTransport", lambda **kw: Transport())
    transport = tnet.TelegramFallbackTransport(["149.154.166.110", "149.154.167.220"])
    transport._sticky_ip = "149.154.166.110"
    try:
        with pytest.raises(httpx.ReadTimeout):
            await transport.handle_async_request(httpx.Request("POST", "https://api.telegram.org/botTOKEN/getFile"))
        assert calls == ["149.154.166.110", "149.154.167.220", "api.telegram.org"]
        assert transport._fallbacks == {}
        assert transport._sticky_ip is tnet._UNSET
    finally:
        await transport.aclose()


@pytest.mark.parametrize("method,path", [
    ("GET", "/botTOKEN/sendVoice"),
    ("POST", "/file/botTOKEN/voice/file.oga"),
    ("POST", "/botTOKEN/getUpdates"),
    ("POST", "/botTOKEN/getFile/extra"),
])
def test_only_exact_file_retrievals_are_replayable(method, path):
    assert not tnet._is_replayable_media_retrieval(httpx.Request(method, "https://api.telegram.org" + path))
