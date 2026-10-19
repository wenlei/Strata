"""serve/test_control_tags.py - the opt-in "control_tags" mode: thinking in content as <think>...</think>.

    python -m unittest serve.test_control_tags -v
"""
from __future__ import annotations

import threading
import unittest

from serve.frontend import Event, ToolCall
from serve.server import openai_chunks, openai_collect


class FakeService:
    def model_for(self, req):
        return "test-model"


def run(events):
    for kind, value in events:
        yield kind, value
    yield "done", {"finish": "stop", "completion_tokens": 2, "prompt_tokens": 1}


def run_tool(events):
    for kind, value in events:
        yield kind, value
    yield "done", {"finish": "tool_calls", "completion_tokens": 2, "prompt_tokens": 1}


def deltas(chunks):
    return [c["choices"][0]["delta"] for c in chunks if c is not None]


class ControlTags(unittest.TestCase):
    def chunks(self, events, tags):
        return list(openai_chunks(FakeService(), {}, [1], True, None, 10, threading.Event(),
                                  run=run(events), tags=tags))

    def test_thinking_is_wrapped_in_content(self):
        chunks = self.chunks([("event", Event("reasoning", "think")), ("event", Event("content", "answer"))], True)
        self.assertEqual([d.get("content") for d in deltas(chunks) if d.get("content")],
                         ["<think>think", "</think>", "answer"])
        self.assertFalse(any(d.get("reasoning_content") for d in deltas(chunks)))
        message = openai_collect(chunks)["choices"][0]["message"]
        self.assertEqual(message["content"], "<think>think</think>answer")
        self.assertNotIn("reasoning_content", message)

    def test_without_the_option_reasoning_is_separate(self):
        chunks = self.chunks([("event", Event("reasoning", "think")), ("event", Event("content", "answer"))], False)
        self.assertEqual([d.get("reasoning_content") for d in deltas(chunks) if d.get("reasoning_content")], ["think"])
        self.assertEqual([d.get("content") for d in deltas(chunks) if d.get("content")], ["answer"])
        message = openai_collect(chunks)["choices"][0]["message"]
        self.assertEqual(message["reasoning_content"], "think")
        self.assertEqual(message["content"], "answer")

    def test_a_reply_that_ends_in_thinking_closes_the_tag(self):
        chunks = self.chunks([("event", Event("reasoning", "thinking"))], True)
        self.assertEqual([d.get("content") for d in deltas(chunks) if d.get("content")],
                         ["<think>thinking", "</think>"])
        self.assertEqual(openai_collect(chunks)["choices"][0]["message"]["content"],
                         "<think>thinking</think>")

    def test_no_thinking_adds_no_tags(self):
        chunks = self.chunks([("event", Event("content", "answer"))], True)
        self.assertEqual([d.get("content") for d in deltas(chunks) if d.get("content")], ["answer"])

    def test_tool_calls_stay_structured(self):
        call = ToolCall("add", {"a": 1})
        chunks = list(openai_chunks(FakeService(), {}, [1], True, None, 10, threading.Event(),
                                    run=run_tool([("event", Event("reasoning", "think")),
                                                  ("event", Event("tool_start", call=call)),
                                                  ("event", Event("tool_args", text='{"a": 1}', call=call)),
                                                  ("event", Event("tool_call", call=call))]), tags=True))
        contents = [d.get("content") for d in deltas(chunks) if d.get("content")]
        self.assertEqual(contents, ["<think>think", "</think>"])
        message = openai_collect(chunks)["choices"][0]["message"]
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "add")
        self.assertEqual(message["tool_calls"][0]["function"]["arguments"], '{"a": 1}')


if __name__ == "__main__":
    unittest.main()
