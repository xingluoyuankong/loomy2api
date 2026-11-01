"""Regression: /v1/responses must not drop tool_calls (2026-10-28)."""
import unittest
from loomy2api.server import Handler


def make_handler():
    return object.__new__(Handler)


class ResponsesToolsTestCase(unittest.TestCase):
    def test_tool_calls_become_function_call_items(self):
        h = make_handler()
        obj = {"choices": [{"message": {
            "role": "assistant", "content": "",
            "reasoning_content": "think",
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "calculator", "arguments": '{"a":7,"b":8}'}}],
        }, "finish_reason": "tool_calls"}]}
        resp = h._responses_from_chat(obj, "GLM-5.3-Flash")
        kinds = [o["type"] for o in resp["output"]]
        self.assertIn("function_call", kinds)
        fc = [o for o in resp["output"] if o["type"] == "function_call"][0]
        self.assertEqual(fc["name"], "calculator")
        self.assertEqual(fc["arguments"], '{"a":7,"b":8}')
        self.assertEqual(fc["call_id"], "call_1")
        self.assertNotIn("message", kinds)

    def test_plain_text_still_becomes_message(self):
        h = make_handler()
        obj = {"choices": [{"message": {
            "role": "assistant", "content": "hi", "tool_calls": [],
        }, "finish_reason": "stop"}]}
        resp = h._responses_from_chat(obj, "m")
        self.assertEqual(resp["output"][0]["content"][0]["text"], "hi")


if __name__ == "__main__":
    unittest.main()
