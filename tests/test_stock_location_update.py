import json
import unittest
from unittest.mock import AsyncMock

from client import InvenTreeClient, StockItem, StockLocation
import server


class StockLocationUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_update_targets_stock_item(self):
        client = InvenTreeClient("https://inventree.example", "token")
        client._update = AsyncMock(return_value={"pk": 10})

        result = await client.stock_update(10, {"status": 50})

        self.assertEqual(result, {"pk": 10})
        client._update.assert_awaited_once_with(StockItem, 10, {"status": 50})

    async def test_client_update_location_targets_stock_location(self):
        client = InvenTreeClient("https://inventree.example", "token")
        client._update = AsyncMock(return_value={"pk": 20})

        result = await client.stock_update_location(20, {"name": "Shelf A"})

        self.assertEqual(result, {"pk": 20})
        client._update.assert_awaited_once_with(StockLocation, 20, {"name": "Shelf A"})

    async def test_server_routes_update_location_to_location_method(self):
        original_client = server._client
        fake_client = unittest.mock.Mock()
        fake_client.stock_update_location = AsyncMock(return_value={"pk": 20, "name": "Shelf A"})
        server._client = fake_client
        try:
            response = await server.stock(
                operation="update_location",
                pk=20,
                data={"name": "Shelf A"},
            )
        finally:
            server._client = original_client

        self.assertEqual(json.loads(response), {"pk": 20, "name": "Shelf A"})
        fake_client.stock_update_location.assert_awaited_once_with(20, {"name": "Shelf A"})

    async def test_server_rejects_update_location_without_data(self):
        original_client = server._client
        server._client = unittest.mock.Mock()
        try:
            response = await server.stock(operation="update_location", pk=20, data={})
        finally:
            server._client = original_client

        payload = json.loads(response)
        self.assertIn("stock.update_location", payload["error"])
        self.assertIn("data required", payload["error"])


if __name__ == "__main__":
    unittest.main()
