"""LLM provider adapters using only the Python standard library.

Blender ships its own Python without pip, so the adapters speak HTTP via
``urllib``. Two wire formats are supported:

* ``openai``    – any OpenAI-compatible ``/chat/completions`` endpoint
                  (OpenAI, Azure/OpenRouter/DeepSeek/Together, Ollama,
                  LM Studio, vLLM, llama.cpp server, LiteLLM, in-house gateways…)
* ``anthropic`` – the Anthropic Messages API (``/v1/messages``)

Conversation history is kept in a provider-neutral format::

    {"role": "user", "content": str, "images": [img, ...]}
    {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "arguments": dict}]}
    {"role": "tool", "tool_call_id": str, "name": str, "content": str,
     "images": [img, ...], "is_error": bool}

where ``img = {"media_type": "image/png", "data": <base64 str>}``.
"""

import json
import ssl
import urllib.error
import urllib.request


class ProviderError(Exception):
    pass


class Reply:
    """Normalised assistant reply."""

    def __init__(self, text="", tool_calls=None, stop_reason="", usage=None):
        self.text = text or ""
        self.tool_calls = tool_calls or []
        self.stop_reason = stop_reason
        self.usage = usage or {}


def _post_json(url, body, headers, timeout, verify_ssl):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        if v:
            req.add_header(k, v)
    ctx = None
    if url.startswith("https") and not verify_ssl:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:2000]
        raise ProviderError(f"HTTP {e.code} from {url}: {detail}") from None
    except urllib.error.URLError as e:
        raise ProviderError(f"Cannot reach {url}: {e.reason}") from None
    except TimeoutError:
        raise ProviderError(f"Request to {url} timed out after {timeout}s") from None
    try:
        return json.loads(raw)
    except ValueError:
        raise ProviderError(f"Non-JSON response from {url}: {raw[:500]}") from None


def _parse_args(raw):
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {"value": val}
    except ValueError:
        return {"__invalid_json__": raw}


class BaseProvider:
    def __init__(self, base_url, api_key, model, max_tokens=16000, temperature=None,
                 timeout=180, verify_ssl=True, extra_headers=None):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature  # None = provider default
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.extra_headers = extra_headers or {}

    def chat(self, system, messages, tools):  # pragma: no cover - interface
        raise NotImplementedError


class OpenAIProvider(BaseProvider):
    def endpoint(self):
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return self.base_url + "/chat/completions"

    @staticmethod
    def convert_messages(system, messages):
        out = [{"role": "system", "content": system}] if system else []
        pending_images = []  # tool messages can't carry images in this API

        def flush_images():
            if pending_images:
                parts = [{"type": "text", "text": "Image(s) returned by the previous tool call(s):"}]
                for img in pending_images:
                    parts.append({"type": "image_url", "image_url": {
                        "url": f"data:{img['media_type']};base64,{img['data']}"}})
                out.append({"role": "user", "content": parts})
                pending_images.clear()

        for m in messages:
            role = m["role"]
            if role != "tool":
                flush_images()
            if role == "user":
                if m.get("images"):
                    parts = [{"type": "text", "text": m.get("content", "")}]
                    for img in m["images"]:
                        parts.append({"type": "image_url", "image_url": {
                            "url": f"data:{img['media_type']};base64,{img['data']}"}})
                    out.append({"role": "user", "content": parts})
                else:
                    out.append({"role": "user", "content": m.get("content", "")})
            elif role == "assistant":
                msg = {"role": "assistant", "content": m.get("content") or None}
                if m.get("tool_calls"):
                    msg["tool_calls"] = [{
                        "id": tc["id"], "type": "function",
                        "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])},
                    } for tc in m["tool_calls"]]
                    if msg["content"] is None:
                        msg["content"] = ""
                out.append(msg)
            elif role == "tool":
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"],
                            "content": m.get("content", "")})
                pending_images.extend(m.get("images") or [])
        flush_images()
        return out

    def chat(self, system, messages, tools):
        body = {"model": self.model, "messages": self.convert_messages(system, messages)}
        if tools:
            body["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t["description"], "parameters": t["parameters"],
            }} for t in tools]
        if self.max_tokens:
            body["max_tokens"] = self.max_tokens
        if self.temperature is not None:
            body["temperature"] = self.temperature
        headers = dict(self.extra_headers)
        if self.api_key:
            headers.setdefault("Authorization", f"Bearer {self.api_key}")
        data = _post_json(self.endpoint(), body, headers, self.timeout, self.verify_ssl)
        if "error" in data and not data.get("choices"):
            raise ProviderError(f"Provider error: {json.dumps(data['error'])[:1000]}")
        try:
            choice = data["choices"][0]
            msg = choice["message"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError(f"Unexpected response shape: {json.dumps(data)[:1000]}") from None
        calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            calls.append({"id": tc.get("id") or f"call_{i}", "name": fn.get("name", ""),
                          "arguments": _parse_args(fn.get("arguments"))})
        return Reply(msg.get("content") or "", calls, choice.get("finish_reason", ""), data.get("usage"))


class AnthropicProvider(BaseProvider):
    API_VERSION = "2023-06-01"

    def endpoint(self):
        b = self.base_url or "https://api.anthropic.com"
        if b.endswith("/messages"):
            return b
        if b.endswith("/v1"):
            return b + "/messages"
        return b + "/v1/messages"

    @staticmethod
    def _image_block(img):
        return {"type": "image", "source": {"type": "base64", "media_type": img["media_type"],
                                            "data": img["data"]}}

    @classmethod
    def convert_messages(cls, messages):
        out = []

        def push(role, blocks):
            if out and out[-1]["role"] == role:
                out[-1]["content"].extend(blocks)  # API needs alternating roles
            else:
                out.append({"role": role, "content": list(blocks)})

        for m in messages:
            role = m["role"]
            if role == "user":
                blocks = [cls._image_block(i) for i in m.get("images") or []]
                blocks.append({"type": "text", "text": m.get("content") or "(empty)"})
                push("user", blocks)
            elif role == "assistant":
                blocks = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for tc in m.get("tool_calls") or []:
                    blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["name"],
                                   "input": tc["arguments"]})
                push("assistant", blocks or [{"type": "text", "text": "(no content)"}])
            elif role == "tool":
                content = [{"type": "text", "text": m.get("content") or "(no output)"}]
                content += [cls._image_block(i) for i in m.get("images") or []]
                block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": content}
                if m.get("is_error"):
                    block["is_error"] = True
                push("user", [block])
        return out

    def chat(self, system, messages, tools):
        body = {"model": self.model, "max_tokens": self.max_tokens or 16000,
                "messages": self.convert_messages(messages)}
        if system:
            # The system prompt (skill catalog) is large and stable: cache it.
            body["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        if tools:
            body["tools"] = [{"name": t["name"], "description": t["description"],
                              "input_schema": t["parameters"]} for t in tools]
        if self.temperature is not None:
            body["temperature"] = self.temperature
        headers = dict(self.extra_headers)
        headers.setdefault("anthropic-version", self.API_VERSION)
        if self.api_key:
            headers.setdefault("x-api-key", self.api_key)
        data = _post_json(self.endpoint(), body, headers, self.timeout, self.verify_ssl)
        if data.get("type") == "error":
            raise ProviderError(f"Provider error: {json.dumps(data.get('error'))[:1000]}")
        texts, calls = [], []
        for block in data.get("content") or []:
            if block.get("type") == "text":
                texts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                calls.append({"id": block["id"], "name": block["name"],
                              "arguments": block.get("input") or {}})
        stop = data.get("stop_reason", "")
        if stop == "refusal" and not texts:
            texts.append("(The model declined this request.)")
        return Reply("\n".join(texts), calls, stop, data.get("usage"))


PROVIDERS = {"openai": OpenAIProvider, "anthropic": AnthropicProvider}


def make_provider(kind, **kwargs):
    try:
        return PROVIDERS[kind](**kwargs)
    except KeyError:
        raise ProviderError(f"Unknown provider type '{kind}'") from None
