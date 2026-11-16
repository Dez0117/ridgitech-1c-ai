import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from backend.integrations.agent_service import AgentService, settings
from backend.integrations.onec_data_backend import OneCDataBackend


class AgentServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_total_timeout_during_bootstrap_preserves_error_code(self):
        async def slow_bootstrap(*args, **kwargs):
            await asyncio.sleep(1)

        with (
            patch.object(settings, "analytics_backend", "onec"),
            patch.object(settings, "agent_timeout_seconds", 0.01),
            patch.object(OneCDataBackend, "context", new=AsyncMock(side_effect=slow_bootstrap)),
            patch.object(OneCDataBackend, "close", new=AsyncMock()) as close,
            patch("backend.integrations.agent_service.create_qwen_model") as create_model,
        ):
            result = await AgentService().run("Поступления за январь", organization_id="organization", history="")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error["code"], "TIMEOUT")
        self.assertTrue(result.error["retryable"])
        self.assertEqual(result.results, [])
        close.assert_awaited_once()
        create_model.assert_not_called()
