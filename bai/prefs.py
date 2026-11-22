"""Add-on preferences: LLM provider settings and behaviour."""

import json
import os

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty

from . import providers
from .skills import SkillLibrary

ADDON_ID = __package__
BUNDLED_SKILLS = os.path.join(os.path.dirname(__file__), "skills")

PRESETS = {
    # id: (label, provider_type, base_url, model)
    "OPENAI": ("OpenAI", "openai", "https://api.openai.com/v1", "gpt-4.1"),
    "ANTHROPIC": ("Anthropic (Claude)", "anthropic", "https://api.anthropic.com", "claude-opus-5-5"),
    "DEEPSEEK": ("DeepSeek", "openai", "https://api.deepseek.com", "deepseek-v4-flash"),
    "OPENROUTER": ("OpenRouter", "openai", "https://openrouter.ai/api/v1", "anthropic/claude-opus-5-5"),
    "OLLAMA": ("Ollama (local)", "openai", "http://localhost:11434/v1", "qwen3:14b"),
    "LMSTUDIO": ("LM Studio (local)", "openai", "http://localhost:1234/v1", "local-model"),
}


def _apply_preset(self, _context):
    if self.preset == "CUSTOM":
        return
    _label, kind, url, model = PRESETS[self.preset]
    self.provider_type = kind
    self.base_url = url
    self.model = model


_skills_cache = {"path": None, "lib": None}


def get_prefs(context=None):
    context = context or bpy.context
    addon = context.preferences.addons.get(ADDON_ID)
    return addon.preferences if addon else None


def skills_root(prefs):
    path = bpy.path.abspath(prefs.skills_path).strip() if prefs.skills_path else ""
    return path if path else BUNDLED_SKILLS


def get_skills(prefs, force=False):
    if not prefs.use_skills:
        return None
    root = skills_root(prefs)
    if force or _skills_cache["path"] != root or _skills_cache["lib"] is None:
        _skills_cache["path"] = root
        _skills_cache["lib"] = SkillLibrary(root)
    return _skills_cache["lib"]


def build_provider(prefs):
    headers = {}
    if prefs.extra_headers.strip():
        try:
            headers = json.loads(prefs.extra_headers)
            if not isinstance(headers, dict):
                raise ValueError
            headers = {str(k): str(v) for k, v in headers.items()}
        except ValueError:
            raise providers.ProviderError('"Extra headers" must be a JSON object, e.g. {"api-key": "..."}') from None
    api_key = prefs.api_key
    if not api_key and prefs.api_key_env.strip():
        api_key = os.environ.get(prefs.api_key_env.strip(), "")
    return providers.make_provider(
        prefs.provider_type,
        base_url=prefs.base_url,
        api_key=api_key,
        model=prefs.model,
        max_tokens=prefs.max_tokens,
        temperature=prefs.temperature if prefs.use_temperature else None,
        timeout=prefs.timeout,
        verify_ssl=prefs.verify_ssl,
        extra_headers=headers,
    )


def run_config(prefs):
    """Plain-Python snapshot of settings, safe to use from the worker thread."""
    return {
        "vision": prefs.vision,
        "confirm_code": prefs.confirm_code,
        "max_steps": prefs.max_steps,
        "max_tool_output": prefs.max_tool_output,
        "extra_prompt": prefs.extra_prompt,
    }


class BAI_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = ADDON_ID

    preset: EnumProperty(
        name="Preset",
        description="Fill in endpoint and model for a common provider",
        items=[("CUSTOM", "Custom", "Keep the values below")]
        + [(k, v[0], f"{v[2]}  ·  {v[3]}") for k, v in PRESETS.items()],
        default="CUSTOM",
        update=_apply_preset,
    )
    provider_type: EnumProperty(
        name="API Format",
        items=[
            ("openai", "OpenAI-compatible", "POST {base}/chat/completions with function tools. Works with most providers, gateways and local servers"),
            ("anthropic", "Anthropic Messages", "POST {base}/v1/messages with tool_use blocks"),
        ],
        default="openai",
    )
    base_url: StringProperty(name="Base URL", default="https://api.openai.com/v1",
                             description="API root, e.g. https://api.openai.com/v1 or http://localhost:11434/v1")
    api_key: StringProperty(name="API Key", subtype="PASSWORD", default="",
                            description="Stored in Blender's user preferences. Leave empty for local servers or to use an environment variable")
    api_key_env: StringProperty(name="API Key Env Var", default="",
                                description="Optional: read the key from this environment variable when the field above is empty")
    model: StringProperty(name="Model", default="gpt-4.1")
    max_tokens: IntProperty(name="Max Output Tokens", default=8192, min=256, max=128000)
    use_temperature: BoolProperty(name="Set Temperature", default=False,
                                  description="Send a temperature value (some models reject it)")
    temperature: FloatProperty(name="Temperature", default=0.3, min=0.0, max=2.0)
    timeout: IntProperty(name="Timeout (s)", default=300, min=10, max=3600)
    verify_ssl: BoolProperty(name="Verify SSL Certificates", default=True,
                             description="Disable only for internal gateways with self-signed certificates")
    extra_headers: StringProperty(name="Extra Headers (JSON)", default="",
                                  description='Additional HTTP headers, e.g. {"api-key": "..."} for Azure')
    vision: BoolProperty(name="Model Supports Images", default=True,
                         description="Enable the viewport screenshot tool and image attachments")

    confirm_code: BoolProperty(name="Confirm Code Before Running", default=True,
                               description="Show each Python snippet from the model and wait for Approve/Reject")
    send_on_enter: BoolProperty(name="Send on Enter", default=True)
    history_items: IntProperty(name="Visible History Items", default=20, min=0, max=500,
                               description="Show only the most recent items in the Conversation panel (0 = all)")
    render_markdown: BoolProperty(name="Render Markdown Replies", default=True,
                                  description="Show headings, lists, tables, code blocks and links in assistant replies")
    max_steps: IntProperty(name="Max Tool Steps per Message", default=30, min=1, max=200)
    max_tool_output: IntProperty(name="Max Tool Output (chars)", default=20000, min=1000, max=500000)

    use_skills: BoolProperty(name="Use Skill Library", default=True)
    skills_path: StringProperty(name="Skills Folder", subtype="DIR_PATH", default="",
                                description="Folder of <skill>/SKILL.md (e.g. blender-skills/.claude/skills). Empty = bundled copy")
    extra_prompt: StringProperty(name="Extra Instructions", default="",
                                 description="Appended to the system prompt")

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text="LLM Provider", icon="WORLD")
        box.prop(self, "preset")
        box.prop(self, "provider_type")
        box.prop(self, "base_url")
        box.prop(self, "model")
        row = box.row()
        row.prop(self, "api_key")
        box.prop(self, "api_key_env")
        col = box.column(align=True)
        col.prop(self, "max_tokens")
        row = col.row(align=True)
        row.prop(self, "use_temperature")
        sub = row.row()
        sub.enabled = self.use_temperature
        sub.prop(self, "temperature")
        col.prop(self, "timeout")
        box.prop(self, "extra_headers")
        row = box.row()
        row.prop(self, "vision")
        row.prop(self, "verify_ssl")
        box.operator("bai.test_connection", icon="PLUGIN")

        box = layout.box()
        box.label(text="Behaviour", icon="PREFERENCES")
        box.prop(self, "confirm_code")
        box.prop(self, "send_on_enter")
        box.prop(self, "history_items")
        box.prop(self, "render_markdown")
        box.prop(self, "max_steps")
        box.prop(self, "max_tool_output")
        box.prop(self, "extra_prompt")

        box = layout.box()
        box.label(text="Skills", icon="BOOKMARKS")
        box.prop(self, "use_skills")
        col = box.column()
        col.enabled = self.use_skills
        col.prop(self, "skills_path")
        lib = get_skills(self)
        root = skills_root(self)
        col.label(text=f"{len(lib) if lib else 0} skills loaded from: {root}")
        col.operator("bai.reload_skills", icon="FILE_REFRESH")
