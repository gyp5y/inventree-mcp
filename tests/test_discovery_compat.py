import json
import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "bin" / "python"


class DiscoveryCompatibilityTests(unittest.TestCase):
    def test_legacy_sdk_rejects_discovery_then_initializes(self):
        requests = [
            {
                "jsonrpc": "2.0",
                "id": "discover-1",
                "method": "server/discover",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientInfo": {
                            "name": "test-client",
                            "version": "1.0.0",
                        },
                        "io.modelcontextprotocol/clientCapabilities": {},
                    }
                },
            },
            {
                "jsonrpc": "2.0",
                "id": "initialize-1",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "1.0.0"},
                },
            },
        ]
        completed = subprocess.run(
            [str(PYTHON), "server.py"],
            cwd=ROOT,
            env={**os.environ, "LOG_LEVEL": "INFO"},
            input="".join(json.dumps(request) + "\n" for request in requests),
            text=True,
            capture_output=True,
            timeout=10,
            check=True,
        )
        responses = [json.loads(line) for line in completed.stdout.splitlines()]

        self.assertEqual(responses[0]["id"], "discover-1")
        self.assertEqual(responses[0]["error"]["code"], -32601)
        self.assertEqual(responses[1]["id"], "initialize-1")
        self.assertEqual(responses[1]["result"]["protocolVersion"], "2025-11-25")
        self.assertNotIn("Failed to validate request", completed.stderr)


if __name__ == "__main__":
    unittest.main()
