"""Agent loop: LLM <-> tools, with skill tools handled here (no bpy dependency).

Blender-side tools are executed through an injected ``execute_blender_tool``
callback so this module can be unit-tested outside Blender and run in a
background thread inside it.
"""

import json

SKILL_TOOLS = [
    {
        "name": "load_skill",
        "description": (
            "Load the full instructions of a Blender skill from the skill library. "
            "Call this BEFORE doing work the skill covers; follow its workflow."
        ),
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Skill name from the catalog, e.g. 'blender-director'."}},
            "required": ["name"],
        },
    },
    {
        "name": "read_skill_file",
        "description": (
            "Read a reference markdown file from the skill library, e.g. 'lighting/references/cinematic-light-rigs.md' "
            "or shared files like 'references/naming-conventions.md'. Use the paths skills mention."
        ),
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path relative to the skills folder."}},
            "required": ["path"],
        },
    },
]

BLENDER_TOOLS = [
    {
        "name": "get_scene_info",
        "description": (
            "Return a JSON summary of the current Blender scene: objects (name, type, location, dimensions, "
            "polycount, materials, collection, modifiers), collections, active object, selection, mode, "
            "render engine, frame range and Blender version. Call before editing and after major steps."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "execute_blender_code",
        "description": (
            "Execute Python code inside the user's running Blender (full bpy API). Pre-imported: bpy, bmesh, "
            "mathutils, math, C (bpy.context), D (bpy.data). Operators run with a 3D Viewport context override "
            "when one exists. Returns captured stdout/stderr, the value of a variable named `result` if you set "
            "one, or the traceback on failure. Work in small, verifiable steps and print what you changed."
        ),
        "parameters": {
            "type": "object",
            "properties": {"code": {"type": "string", "description": "Python source to execute."}},
            "required": ["code"],
        },
    },
    {
        "name": "get_viewport_screenshot",
        "description": (
            "Capture the 3D viewport as an image so you can see your work (silhouette, materials, lighting, "
            "camera framing). Use after camera, material and lighting phases."
        ),
        "parameters": {
            "type": "object",
            "properties": {"max_size": {"type": "integer", "description": "Longest edge in pixels (default 800)."}},
        },
    },
]

SYSTEM_TEMPLATE = """You are an AI technical artist embedded in Blender {blender_version}. You chat with the user in a side panel and act on their scene directly with tools.

# Tools
- get_scene_info – inspect the scene. Never assume scene state; query it first.
- execute_blender_code – run bpy Python in the live session. This is how you do everything: modeling, modifiers, materials, lighting, cameras, rendering, export.
- get_viewport_screenshot – look at the result.{vision_note}
- load_skill / read_skill_file – the skill library below. It encodes senior technical-art workflows.

# Skills
{skill_rules}

The skills were written for an agent driving Blender through "Blender MCP". In this add-on you ARE inside Blender, so map their tool names like this:
- `execute_blender_code` → execute_blender_code (same name)
- `get_viewport_screenshot` → get_viewport_screenshot (same name)
- scene queries / `get_scene_info` / "query via MCP" → get_scene_info, or execute_blender_code for details
- Poly Haven, Sketchfab, Hyper3D/Hunyuan3D generation tools → NOT available; build with bpy instead, or tell the user.
- "check MCP connection / read MCP schemas" steps → skip; your tools are always connected.

# Writing robust bpy code
- Prefer bpy.data API over bpy.ops where possible; ops depend on context and selection.
- Find shader nodes by type, not by name (names are localized): next(n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED').
- Socket names differ between Blender versions (e.g. 'Specular' vs 'Specular IOR Level'); check `node.inputs.keys()` when unsure.
- Don't hardcode enum values you are unsure of; read them from bl_rna, and wrap assignments like scene.render.engine in try/except.
- Material colors go on shader node inputs; material.diffuse_color only affects the viewport.
- Keep work non-destructive (modifiers, duplicates) unless asked otherwise. Never delete user data or save/overwrite files unless the user asked.
- If code fails, read the traceback, fix, and retry with a smaller step. Never silently skip a failed step.

# Style
Be concise in chat (the panel is narrow). Briefly state your plan, do the work with tools, then summarise what changed (object names, counts) and any next steps. Ask a short clarifying question only when the request is genuinely ambiguous.
{extra}"""

SKILL_RULES_WITH_LIBRARY = """You have {count} skills. For any non-trivial request:
1. Pick the relevant skill(s) from the catalog. For complex or multi-discipline asset work, load `blender-director` first; it routes to specialists.
2. load_skill each one you use and follow its workflow; fetch the reference files it points to with read_skill_file when needed.
3. Shared references live under `references/` (asset-pipeline.md, naming-conventions.md, polycount-budgets.md, validation-checklist.md, …).
Don't load skills for trivial one-line requests (e.g. "add a cube").

## Skill catalog
{catalog}"""

NO_LIBRARY_RULES = "No skill library is loaded. Rely on your own Blender knowledge."


def build_system_prompt(blender_version, skills, vision=True, extra=""):
    if skills is not None and len(skills):
        rules = SKILL_RULES_WITH_LIBRARY.format(count=len(skills), catalog=skills.catalog())
    else:
        rules = NO_LIBRARY_RULES
    vision_note = "" if vision else " (Your model is configured without vision; the screenshot tool is disabled.)"
    extra = ("\n# Additional instructions\n" + extra.strip()) if extra and extra.strip() else ""
    return SYSTEM_TEMPLATE.format(blender_version=blender_version, skill_rules=rules,
                                  vision_note=vision_note, extra=extra)


def tool_specs(skills, vision=True):
    specs = list(BLENDER_TOOLS)
    if not vision:
        specs = [t for t in specs if t["name"] != "get_viewport_screenshot"]
    if skills is not None and len(skills):
        specs = SKILL_TOOLS + specs
    return specs


def _truncate(text, limit):
    if limit and len(text) > limit:
        return text[:limit] + f"\n…[truncated {len(text) - limit} characters]"
    return text


class Cancelled(Exception):
    pass


class Agent:
    """Runs one user turn to completion (possibly many LLM/tool round-trips)."""

    def __init__(self, provider, skills, system_prompt, tools, execute_blender_tool,
                 on_event=lambda kind, data: None, max_steps=30, max_tool_output=20000,
                 is_cancelled=lambda: False):
        self.provider = provider
        self.skills = skills
        self.system_prompt = system_prompt
        self.tools = tools
        self.execute_blender_tool = execute_blender_tool
        self.on_event = on_event
        self.max_steps = max_steps
        self.max_tool_output = max_tool_output
        self.is_cancelled = is_cancelled

    def _check_cancel(self):
        if self.is_cancelled():
            raise Cancelled()

    def _run_tool(self, name, args):
        """Return (text, images, is_error)."""
        if "__invalid_json__" in args:
            return "Tool arguments were not valid JSON: " + args["__invalid_json__"][:500], [], True
        try:
            if name == "load_skill":
                return self.skills.load_skill(str(args.get("name", ""))), [], False
            if name == "read_skill_file":
                return self.skills.read_file(str(args.get("path", ""))), [], False
            if name in {t["name"] for t in BLENDER_TOOLS}:
                return self.execute_blender_tool(name, args)
            return f"Unknown tool '{name}'.", [], True
        except Cancelled:
            raise
        except Exception as e:  # report to the model, let it recover
            return f"{type(e).__name__}: {e}", [], True

    def run(self, history):
        """Mutates ``history`` (neutral format). Returns the final assistant text."""
        for _step in range(self.max_steps):
            self._check_cancel()
            self.on_event("status", "Thinking…")
            reply = self.provider.chat(self.system_prompt, history, self.tools)
            self._check_cancel()
            history.append({"role": "assistant", "content": reply.text, "tool_calls": reply.tool_calls})
            if reply.text:
                self.on_event("assistant", reply.text)
            if not reply.tool_calls:
                if reply.stop_reason in ("max_tokens", "length"):
                    self.on_event("error", "Reply was cut off by the max-tokens limit; raise it in preferences.")
                return reply.text
            for tc in reply.tool_calls:
                self.on_event("tool_call", tc)
                if self.is_cancelled():
                    text, images, is_error = "Cancelled by the user.", [], True
                else:
                    text, images, is_error = self._run_tool(tc["name"], tc["arguments"])
                text = _truncate(text, self.max_tool_output)
                history.append({"role": "tool", "tool_call_id": tc["id"], "name": tc["name"],
                                "content": text, "images": images, "is_error": is_error})
                self.on_event("tool_result", {"name": tc["name"], "content": text,
                                              "is_error": is_error, "has_image": bool(images)})
            self._check_cancel()
        self.on_event("error", f"Stopped after {self.max_steps} steps (limit set in preferences).")
        return ""


def summarize_args(name, args):
    """Short one-line description of a tool call for the chat log."""
    if name == "load_skill":
        return f"load skill: {args.get('name')}"
    if name == "read_skill_file":
        return f"read: {args.get('path')}"
    if name == "execute_blender_code":
        code = str(args.get("code", "")).strip().splitlines()
        return f"run code ({len(code)} lines)"
    if name == "get_viewport_screenshot":
        return "viewport screenshot"
    if name == "get_scene_info":
        return "inspect scene"
    return f"{name} {json.dumps(args)[:80]}"
