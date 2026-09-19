from __future__ import annotations

import unittest
from unittest.mock import patch


class ServerDataRoutesTests(unittest.TestCase):
    def test_data_health_dashboard_route_is_registered_via_blueprint(self) -> None:
        from scripts import quant_api_server

        with patch(
            "quantdesk.data_health_dashboard.build_cached_data_health_dashboard",
            return_value={"ok": True, "health_level": "green", "write": False},
        ) as build_dashboard:
            response = quant_api_server.app.test_client().get("/api/data-health/dashboard")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload.get("ok"))
        self.assertEqual(payload.get("health_level"), "green")
        build_dashboard.assert_called_once_with()
        self.assertNotIn("get_data_health_dashboard", quant_api_server.__dict__)


if __name__ == "__main__":
    unittest.main()
