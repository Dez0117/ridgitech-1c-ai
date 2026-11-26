import asyncio
from contextlib import asynccontextmanager
import json
import ssl
import time
import unittest

import httpx

from backend.integrations.onec_data_backend import OneCDataBackend
from services.onec_data.client import ODataReader
from services.onec_data.config import DataSettings
from services.onec_data.models import CATALOGS
from services.onec_data.observability import request_scope
from tools import BackendError


@asynccontextmanager
async def reader_for(handler, **changes):
    settings = DataSettings(_env_file=None, odata_base_url="https://onec.invalid/odata",
                            odata_username="reader", odata_password="private-password",
                            odata_requests_per_second=50, odata_retry_backoff_seconds=0, **changes)
    reader = ODataReader(settings, transport=httpx.MockTransport(handler))
    try:
        yield reader
    finally:
        await reader.close()


class ODataClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_connect_retry_has_safe_logs_correlation_and_metrics(self):
        requests = []
        def handle(request):
            requests.append(request)
            if len(requests) == 1:
                raise httpx.ConnectTimeout("private-password and Authorization header", request=request)
            return httpx.Response(200, json={"value": []})
        with self.assertLogs("onec_data", level="INFO") as captured:
            async with reader_for(handle) as reader:
                with request_scope("request-123", 2) as trace:
                    response = await reader.get(CATALOGS["direction"], page=2)
                    self.assertEqual(trace.metrics["upstream_requests"], 2)
                    self.assertEqual(trace.metrics["upstream_retries"], 1)
        self.assertEqual(response.status_code, 200)
        records = [json.loads(item.getMessage()) for item in captured.records]
        self.assertTrue(all(item["request_id"] == "request-123" for item in records))
        self.assertTrue(any(item.get("error_type") == "ConnectTimeout" for item in records))
        self.assertTrue(any(item["event"] == "odata_retry" for item in records))
        self.assertNotIn("private-password", "".join(captured.output))
        self.assertNotIn("Authorization", "".join(captured.output))

    async def test_connect_retry_is_bounded_and_read_timeout_is_not_retried(self):
        for exception, count, stage in ((httpx.ConnectTimeout, 2, "connect"), (httpx.ReadTimeout, 1, "read"), (httpx.PoolTimeout, 1, "pool")):
            with self.subTest(exception=exception.__name__):
                requests = []
                def handle(request):
                    requests.append(request)
                    raise exception("private-password", request=request)
                async with reader_for(handle) as reader:
                    with self.assertRaises(BackendError) as caught:
                        await reader.get("$metadata")
                self.assertEqual(len(requests), count)
                self.assertEqual(caught.exception.code, "TIMEOUT")
                self.assertEqual(caught.exception.details, {"stage": stage, "error_type": exception.__name__, "attempts": count})

    async def test_only_transient_http_statuses_are_retried(self):
        for status in (400, 401, 403, 404, 429, 500, 502, 503, 504, 302):
            with self.subTest(status=status):
                requests = []
                def handle(request):
                    requests.append(request)
                    return httpx.Response(status, headers={"Retry-After": "0"}, json={"ignored": "private-password"}) if len(requests) == 1 else httpx.Response(200, json={})
                async with reader_for(handle) as reader:
                    if status in {429, 502, 503, 504}:
                        self.assertEqual((await reader.get("$metadata")).status_code, 200)
                        self.assertEqual(len(requests), 2)
                    else:
                        with self.assertRaises(BackendError) as caught:
                            await reader.get("$metadata")
                        self.assertEqual(len(requests), 1)
                        self.assertFalse(caught.exception.retryable)

    async def test_retry_after_must_fit_remaining_deadline(self):
        requests = []
        def handle(request):
            requests.append(request)
            return httpx.Response(503, headers={"Retry-After": "30"})
        async with reader_for(handle) as reader:
            with request_scope("short", 0.1):
                with self.assertRaises(BackendError) as caught:
                    await reader.get("$metadata")
        self.assertEqual(caught.exception.code, "ODATA_ERROR")
        self.assertEqual(len(requests), 1)

    async def test_invalid_certificate_is_not_retried(self):
        requests = []
        def handle(request):
            requests.append(request)
            try:
                raise ssl.SSLCertVerificationError("private-password")
            except ssl.SSLCertVerificationError as cause:
                raise httpx.ConnectError("private-password", request=request) from cause
        async with reader_for(handle) as reader:
            with self.assertRaises(BackendError) as caught:
                await reader.get("$metadata")
        self.assertEqual(len(requests), 1)
        self.assertEqual(caught.exception.details["stage"], "tls")
        self.assertFalse(caught.exception.retryable)

    async def test_deadline_cancels_slow_transport_and_queue(self):
        async def handle(request):
            await asyncio.sleep(1)
            return httpx.Response(200, json={})
        async with reader_for(handle) as reader:
            with request_scope("slow", 0.02):
                with self.assertRaises(BackendError) as caught:
                    await reader.get("$metadata")
            self.assertEqual(caught.exception.details["stage"], "deadline")
            self.assertEqual(caught.exception.details["attempts"], 1)
            reader.last_call = time.monotonic()
            reader.settings.odata_requests_per_second = 1
            with request_scope("queue", 0.01):
                with self.assertRaises(BackendError) as queued:
                    await reader.get("$metadata")
            self.assertEqual(queued.exception.details["attempts"], 0)

    async def test_socket_timeouts_are_separate_and_connections_are_bounded(self):
        active, maximum = 0, 0
        async def handle(request):
            nonlocal active, maximum
            self.assertEqual(request.extensions["timeout"], {"connect": 0.1, "read": 0.2, "write": 0.3, "pool": 0.4})
            active += 1
            maximum = max(maximum, active)
            try:
                await asyncio.sleep(0.05)
                return httpx.Response(200, json={})
            finally:
                active -= 1
        async with reader_for(handle, odata_max_connections=2, odata_connect_timeout_seconds=0.1,
                              odata_read_timeout_seconds=0.2, odata_write_timeout_seconds=0.3, odata_pool_timeout_seconds=0.4) as reader:
            async def execute(index):
                with request_scope(f"parallel-{index}", 2) as trace:
                    await reader.get("$metadata")
                    return dict(trace.metrics)
            metrics = await asyncio.gather(*(execute(index) for index in range(4)))
        self.assertEqual(maximum, 2)
        self.assertTrue(all(item["upstream_requests"] == 1 for item in metrics))

    async def test_pool_timeout_bounds_semaphore_wait_without_sending_request(self):
        requests = []
        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={})
        async with reader_for(handle, odata_max_connections=1, odata_pool_timeout_seconds=0.01) as reader:
            await reader.semaphore.acquire()
            started = time.monotonic()
            try:
                with request_scope("pool-wait", 1) as trace:
                    with self.assertRaises(BackendError) as caught:
                        await reader.get("$metadata")
                    self.assertEqual(trace.metrics["upstream_requests"], 0)
            finally:
                reader.semaphore.release()
            self.assertEqual(caught.exception.details, {"stage": "pool", "error_type": "PoolTimeout", "attempts": 0})
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertEqual(requests, [])
            self.assertEqual((await reader.get("$metadata")).status_code, 200)

    async def test_page_iterator_yields_before_fetching_the_next_page(self):
        requests = []
        def handle(request):
            requests.append(request)
            skip = int(request.url.params["$skip"])
            return httpx.Response(200, json={"value": [{"Ref_Key": str(skip)}] if skip < 2 else []})
        async with reader_for(handle, odata_page_size=1) as reader:
            pages = reader.iter_pages(CATALOGS["organization"], select="Ref_Key", order_by="Ref_Key")
            first = await anext(pages)
            self.assertEqual(first.rows, [{"Ref_Key": "0"}])
            self.assertEqual(len(requests), 1)
            rest = [page async for page in pages]
            self.assertEqual(rest[-1].number, 3)

    async def test_adapter_preserves_safe_details_and_permanent_auth_error(self):
        for raw in ({"stage": "connect", "error_type": "ConnectTimeout", "attempts": 2, "password": "private-password"}, {"stage": [], "error_type": {}}):
            def handle(request):
                return httpx.Response(504, json={"error": {"code": "TIMEOUT", "message": "private-password", "retryable": True, "details": raw}})
            backend = OneCDataBackend("http://data.invalid", transport=httpx.MockTransport(handle))
            try:
                with self.assertRaises(BackendError) as caught:
                    await backend.request("GET", "v1/bootstrap", timeout=1, request_id="safe-id")
                self.assertEqual(caught.exception.details["request_id"], "safe-id")
                self.assertNotIn("private-password", str(caught.exception) + json.dumps(caught.exception.details))
            finally:
                await backend.close()
        def denied(request):
            return httpx.Response(502, json={"error": {"code": "ODATA_AUTH", "retryable": False}})
        backend = OneCDataBackend("http://data.invalid", transport=httpx.MockTransport(denied))
        try:
            with self.assertRaises(BackendError) as caught:
                await backend.bootstrap()
            self.assertEqual(caught.exception.code, "ODATA_AUTH")
            self.assertFalse(caught.exception.retryable)
        finally:
            await backend.close()
