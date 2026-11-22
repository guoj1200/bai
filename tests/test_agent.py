"""Offline tests for providers + agent loop + skills (no Blender needed).

Run:  python -m unittest discover -s tests
"""

import importlib.util
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "bai")


def _load(name):
    spec = importlib.util.spec_from_file_location(f"bai_{name}", os.path.join(PKG, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


providers = _load("providers")
skills_mod = _load("skills")
agent_mod = _load("agent")

IMG = {"media_type": "image/png", "data": "iVBORw0KGgo="}


class MockLLM(BaseHTTPRequestHandler):
    """Scripted 3-step conversation: load_skill -> screenshot+code -> final answer."""

    requests = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        MockLLM.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        step = len(MockLLM.requests)
        if self.path.endswith("/chat/completions"):
            resp = self._openai(step)
        else:
            resp = self._anthropic(step)
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _openai(self, step):
        if step == 1:
            calls = [{"id": "c1", "type": "function",
                      "function": {"name": "load_skill", "arguments": json.dumps({"name": "lighting"})}}]
            return {"choices": [{"message": {"role": "assistant", "content": "Loading skill.", "tool_calls": calls},
                                 "finish_reason": "tool_calls"}]}
        if step == 2:
            calls = [
                {"id": "c2", "type": "function", "function": {"name": "get_viewport_screenshot", "arguments": "{}"}},
                {"id": "c3", "type": "function",
                 "function": {"name": "execute_blender_code", "arguments": json.dumps({"code": "print(1)"})}},
            ]
            return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": calls},
                                 "finish_reason": "tool_calls"}]}
        return {"choices": [{"message": {"role": "assistant", "content": "Done: added a light."},
                             "finish_reason": "stop"}]}

    def _anthropic(self, step):
        if step == 1:
            return {"type": "message", "stop_reason": "tool_use", "content": [
                {"type": "text", "text": "Loading skill."},
                {"type": "tool_use", "id": "t1", "name": "load_skill", "input": {"name": "lighting"}}]}
        if step == 2:
            return {"type": "message", "stop_reason": "tool_use", "content": [
                {"type": "tool_use", "id": "t2", "name": "get_viewport_screenshot", "input": {}},
                {"type": "tool_use", "id": "t3", "name": "execute_blender_code", "input": {"code": "print(1)"}}]}
        return {"type": "message", "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "Done: added a light."}]}


def fake_blender(name, args):
    if name == "get_viewport_screenshot":
        return "Viewport screenshot attached.", [IMG], False
    if name == "execute_blender_code":
        return "stdout:\n1", [], False
    return "{}", [], False


class AgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), MockLLM)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.skills = skills_mod.SkillLibrary(os.path.join(PKG, "skills"))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        MockLLM.requests = []

    def _run(self, kind, base):
        provider = providers.make_provider(kind, base_url=base, api_key="sk-test", model="m", max_tokens=1000)
        system = agent_mod.build_system_prompt("4.2.0", self.skills)
        tools = agent_mod.tool_specs(self.skills)
        events = []
        history = [{"role": "user", "content": "Light my scene", "images": [IMG]}]
        a = agent_mod.Agent(provider, self.skills, system, tools, fake_blender,
                            on_event=lambda k, d: events.append(k))
        final = a.run(history)
        self.assertEqual(final, "Done: added a light.")
        self.assertEqual(len(MockLLM.requests), 3)
        self.assertEqual([m["role"] for m in history],
                         ["user", "assistant", "tool", "assistant", "tool", "tool", "assistant"])
        self.assertIn("Lighting", history[2]["content"])  # skill body loaded
        self.assertIn("tool_call", events)
        return MockLLM.requests

    def test_openai_flow(self):
        reqs = self._run("openai", f"http://127.0.0.1:{self.port}/v1")
        path, headers, body = reqs[2]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(headers["authorization"], "Bearer sk-test")
        roles = [m["role"] for m in body["messages"]]
        # images from the screenshot tool are re-sent as a user message after all tool results
        self.assertEqual(roles, ["system", "user", "assistant", "tool", "assistant", "tool", "tool", "user"])
        self.assertEqual(body["messages"][-1]["content"][1]["type"], "image_url")
        self.assertEqual(body["messages"][1]["content"][1]["type"], "image_url")
        self.assertEqual(json.loads(body["messages"][4]["tool_calls"][1]["function"]["arguments"]), {"code": "print(1)"})
        names = {t["function"]["name"] for t in body["tools"]}
        self.assertEqual(names, {"load_skill", "read_skill_file", "get_scene_info",
                                 "execute_blender_code", "get_viewport_screenshot"})
        self.assertIn("blender-director", body["messages"][0]["content"])

    def test_anthropic_flow(self):
        reqs = self._run("anthropic", f"http://127.0.0.1:{self.port}")
        path, headers, body = reqs[2]
        self.assertEqual(path, "/v1/messages")
        self.assertEqual(headers["x-api-key"], "sk-test")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertNotIn("temperature", body)
        roles = [m["role"] for m in body["messages"]]
        self.assertEqual(roles, ["user", "assistant", "user", "assistant", "user"])
        last = body["messages"][-1]["content"]
        self.assertEqual([b["type"] for b in last], ["tool_result", "tool_result"])  # both results in ONE message
        self.assertEqual(last[0]["content"][1]["type"], "image")
        self.assertEqual(body["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertIn("input_schema", body["tools"][0])

    def test_cancel(self):
        provider = providers.make_provider("openai", base_url=f"http://127.0.0.1:{self.port}/v1",
                                           api_key="", model="m")
        a = agent_mod.Agent(provider, self.skills, "s", [], fake_blender, is_cancelled=lambda: True)
        with self.assertRaises(agent_mod.Cancelled):
            a.run([{"role": "user", "content": "hi"}])

    def test_http_error(self):
        provider = providers.make_provider("openai", base_url="http://127.0.0.1:1/v1", api_key="", model="m",
                                           timeout=3)
        with self.assertRaises(providers.ProviderError):
            provider.chat("s", [{"role": "user", "content": "hi"}], [])


class SkillTests(unittest.TestCase):
    def setUp(self):
        self.lib = skills_mod.SkillLibrary(os.path.join(PKG, "skills"))

    def test_catalog(self):
        self.assertGreaterEqual(len(self.lib), 90)
        self.assertIn("blender-director", self.lib.skills)
        self.assertTrue(all(s.description for s in self.lib.skills.values()))

    def test_load_and_refs(self):
        body = self.lib.load_skill("lighting")
        self.assertIn("lighting/references/", body)
        self.assertIn("Naming", self.lib.read_file("../references/naming-conventions.md"))
        with self.assertRaises(KeyError):
            self.lib.load_skill("nope")

    def test_path_traversal(self):
        for bad in ("../../__init__.py", "..\\..\\agent.py", "/etc/passwd", "lighting/../../../x.md"):
            with self.assertRaises((PermissionError, FileNotFoundError)):
                self.lib.read_file(bad)


if __name__ == "__main__":
    unittest.main()
