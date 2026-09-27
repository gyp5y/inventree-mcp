import asyncio
import unittest

from server import mcp


class NativeFileSchemaTests(unittest.TestCase):
    def test_image_tools_publish_native_file_parameter(self):
        tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}

        for tool_name in ("part", "attachment"):
            with self.subTest(tool=tool_name):
                tool = tools[tool_name]
                self.assertEqual(tool.meta["openai/fileParams"], ["image_file"])
                schema = tool.inputSchema
                file_schema = schema["$defs"]["OpenAIFile"]
                self.assertEqual(
                    set(file_schema["properties"]),
                    {"download_url", "file_id", "mime_type", "file_name"},
                )
                self.assertEqual(
                    set(file_schema["required"]), {"download_url", "file_id"}
                )
                self.assertFalse(file_schema["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
