import unittest
from loomy2api.server import _compress_messages, _estimate_tokens


def msg(role, text, tool_calls=None):
    m = {"role": role, "content": text}
    if tool_calls:
        m["tool_calls"] = tool_calls
    return m


class CompressTestCase(unittest.TestCase):
    def test_small_unchanged(self):
        msgs = [msg("system", "sys"), msg("user", "hi"), msg("assistant", "hello"), msg("user", "calc 7*8")]
        out, info = _compress_messages(msgs, None, 1048576, 131072)
        self.assertIsNone(info)
        self.assertEqual(out, msgs)

    def test_drops_oldest_blocks_keeps_system_last_and_tool_pair(self):
        msgs = [msg("system", "sys")]
        for i in range(10):
            msgs.append(msg("user", f"q{i} " + "x" * 200))
            msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"call_{i}", "type": "function", "function": {"name": "calc", "arguments": "{}"}}]})
            msgs.append(msg("tool", f"result{i}"))
        msgs.append(msg("user", "final question"))
        # context 10000, output reserve 0: total far over 8500, target 7000
        out, info = _compress_messages(msgs, None, 800, 0)
        self.assertIsNotNone(info)
        self.assertEqual(out[0]["role"], "system")
        self.assertEqual(out[-1]["content"], "final question")
        # tool pairs must not be split: every tool msg follows its assistant
        for j, m in enumerate(out):
            if m["role"] == "tool":
                self.assertEqual(out[j - 1]["role"], "assistant")
                self.assertTrue(out[j - 1].get("tool_calls"))
        self.assertLess(info["compressed_tokens"], info["original_tokens"])

    def test_single_huge_block_unchanged(self):
        msgs = [msg("user", "x" * 100000)]
        out, info = _compress_messages(msgs, None, 800, 0)
        self.assertIsNone(info)
        self.assertEqual(out, msgs)

    def test_unknown_context_unchanged(self):
        msgs = [msg("user", "x" * 100000)]
        out, info = _compress_messages(msgs, None, 0, 0)
        self.assertIsNone(info)

    def test_chinese_estimate_higher_than_ascii(self):
        self.assertGreater(_estimate_tokens([msg("user", "你好世界")], None),
                            _estimate_tokens([msg("user", "hi")], None))


if __name__ == "__main__":
    unittest.main()
