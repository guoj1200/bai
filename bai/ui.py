"""Sidebar chat panel and operators."""

import textwrap
import threading

import bpy
from bpy.props import BoolProperty, IntProperty, StringProperty

from . import blender_tools, mdparse
from .prefs import build_provider, get_prefs, get_skills, run_config
from .providers import ProviderError
from .session import SESSION, group_log, redraw, summarize_group

ROLE_ICON = {"user": "USER", "assistant": "LIGHT", "tool": "TOOL_SETTINGS", "error": "ERROR", "info": "INFO"}
MAX_DETAIL_LINES = 80


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _wrap_width(context):
    region = context.region
    scale = context.preferences.view.ui_scale
    px = region.width if region else 300
    return max(20, int((px - 50) / (7.0 * scale)))


def _draw_wrapped(layout, text, width, icon=None, max_lines=None):
    col = layout.column(align=True)
    col.scale_y = 0.85
    lines = []
    for para in str(text).splitlines() or [""]:
        lines.extend(textwrap.wrap(para, width, replace_whitespace=False, drop_whitespace=True) or [""])
    truncated = max_lines is not None and len(lines) > max_lines
    if truncated:
        lines = lines[:max_lines]
    for i, line in enumerate(lines):
        if i == 0 and icon:
            col.label(text=line, icon=icon)
        else:
            col.label(text=line)
    if truncated:
        col.label(text="… (open in Text Editor for the rest)")


PROMPT_TEXT = "AI_prompt.md"


def _separator_line(layout):
    try:
        layout.separator(type="LINE")  # Blender 4.2+
    except TypeError:
        layout.separator()


def _hanging(layout, marker, text, width, avail_px, depth=0):
    """List item: marker in a narrow column, wrapped text beside it."""
    indent_px = 18 + 16 * depth
    factor = min(0.5, max(0.04, indent_px / max(avail_px, 60)))
    split = layout.split(factor=factor, align=True)
    left = split.column(align=True)
    left.alignment = "RIGHT"
    left.scale_y = 0.85
    left.label(text=marker)
    chars = max(10, width - int(indent_px / 7))
    _draw_wrapped(split, text, chars)


def _draw_markdown(layout, text, width, avail_px, entry_index):
    """Lay out Markdown with Blender UI primitives (labels, boxes, splits)."""
    col = layout.column(align=False)
    code_idx = 0
    for block in mdparse.parse(text):
        kind = block[0]
        if kind == "para":
            _draw_wrapped(col, block[1], width)
        elif kind == "heading":
            level, title = block[1], block[2]
            col.separator(factor=0.4)
            row = col.row()
            if level <= 2:
                row.scale_y = 1.15
                row.label(text=title, icon="RIGHTARROW_THIN" if level == 1 else "NONE")
                _separator_line(col)
            else:
                row.label(text=title)
        elif kind == "item":
            _hanging(col, block[2], block[3], width, avail_px, block[1])
        elif kind == "quote":
            box = col.box()
            _hanging(box, "\u2502", block[1], width - 4, avail_px)
        elif kind == "code":
            box = col.box()
            head = box.row(align=True)
            head.label(text=block[1] or "code", icon="SCRIPT")
            op = head.operator("bai.open_text", text="", icon="COPYDOWN", emboss=False)
            op.index = entry_index
            op.block = code_idx
            code_idx += 1
            body = box.column(align=True)
            body.scale_y = 0.8
            shown = 0
            for line in block[2].splitlines() or [""]:
                for chunk in textwrap.wrap(line, max(10, width - 3), drop_whitespace=False) or [""]:
                    if shown < 40:
                        body.label(text=chunk.replace("    ", "  "))
                    shown += 1
            if shown > 40:
                body.label(text="... (click the copy icon to see all)")
        elif kind == "table":
            header, rows = block[1], block[2]
            ncols = max([len(header)] + [len(r) for r in rows])
            box = col.box()
            grid = box.grid_flow(row_major=True, columns=ncols, even_columns=True, align=True)
            for c in range(ncols):
                grid.label(text=header[c] if c < len(header) else "")
            _separator_line(box)
            grid = box.grid_flow(row_major=True, columns=ncols, even_columns=True, align=True)
            grid.scale_y = 0.85
            for r in rows:
                for c in range(ncols):
                    grid.label(text=r[c] if c < len(r) else "")
        elif kind == "rule":
            _separator_line(col)
    links = mdparse.links_in(text)
    if links:
        lcol = col.column(align=True)
        for label, url in links[:8]:
            lcol.operator("wm.url_open", text=label[:60], icon="URL").url = url


def _set_input(wm, value):
    _on_input_update._guard = True
    try:
        wm.bai_input = value
    finally:
        _on_input_update._guard = False


def _send(context, text):
    text = text.strip()
    prefs = get_prefs(context)
    if not text or prefs is None:
        return False, "Nothing to send"
    if SESSION.busy:
        return False, "The assistant is still working"
    try:
        provider = build_provider(prefs)
    except ProviderError as e:
        return False, str(e)
    if not prefs.model.strip():
        return False, "Set a model in the add-on preferences"
    images = None
    wm = context.window_manager
    if wm.bai_attach_screenshot and prefs.vision:
        _msg, imgs, err = blender_tools.get_viewport_screenshot({"max_size": 1024})
        if not err:
            images = imgs
    SESSION.send(text, run_config(prefs), provider, get_skills(prefs), images)
    return True, ""


def _on_input_update(self, context):
    wm = context.window_manager
    if getattr(_on_input_update, "_guard", False):
        return
    prefs = get_prefs(context)
    if not prefs or not prefs.send_on_enter or not wm.bai_input.strip():
        return
    ok, _err = _send(context, wm.bai_input)
    if ok:
        _set_input(wm, "")


def _text_block(name, body):
    txt = bpy.data.texts.get(name) or bpy.data.texts.new(name)
    txt.clear()
    txt.write(body)
    # Show it in an open Text Editor if there is one.
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == "TEXT_EDITOR":
                area.spaces.active.text = txt
                return txt, True
    return txt, False


# --------------------------------------------------------------------------
# operators
# --------------------------------------------------------------------------
class BAI_OT_send(bpy.types.Operator):
    bl_idname = "bai.send"
    bl_label = "Send"
    bl_description = "Send the message to the assistant"

    def execute(self, context):
        wm = context.window_manager
        ok, err = _send(context, wm.bai_input)
        if not ok:
            self.report({"WARNING"}, err)
            return {"CANCELLED"}
        _set_input(wm, "")
        return {"FINISHED"}


class BAI_OT_stop(bpy.types.Operator):
    bl_idname = "bai.stop"
    bl_label = "Stop"
    bl_description = "Stop the assistant after the current step"

    def execute(self, context):
        SESSION.stop()
        return {"FINISHED"}


class BAI_OT_clear(bpy.types.Operator):
    bl_idname = "bai.clear"
    bl_label = "New Chat"
    bl_description = "Clear the conversation"

    def execute(self, context):
        if not SESSION.clear():
            self.report({"WARNING"}, "Stop the assistant first")
            return {"CANCELLED"}
        return {"FINISHED"}


class BAI_OT_approve(bpy.types.Operator):
    bl_idname = "bai.approve"
    bl_label = "Approve"
    bl_description = "Approve or reject the code proposed by the assistant"

    approve: BoolProperty(default=True)
    all_chat: BoolProperty(default=False, description="Also run all further code in this chat without asking")

    @classmethod
    def description(cls, context, props):
        if props.all_chat:
            return "Run this code and all further code in this chat without asking (until New Chat)"
        return "Run this code" if props.approve else "Reject this code; the assistant will try something else"

    def execute(self, context):
        p = SESSION.pending
        if p is None:
            return {"CANCELLED"}
        if self.all_chat and self.approve:
            SESSION.auto_approve = True
        p.approved = self.approve
        p.event.set()
        return {"FINISHED"}


class BAI_OT_auto_approve(bpy.types.Operator):
    bl_idname = "bai.auto_approve"
    bl_label = "Ask Before Running Code"
    bl_description = "Ask again before each code run in this chat"

    def execute(self, context):
        SESSION.auto_approve = False
        redraw()
        return {"FINISHED"}


class BAI_OT_toggle_group(bpy.types.Operator):
    bl_idname = "bai.toggle_group"
    bl_label = "Tool Steps"
    bl_description = "Show or hide the individual tool steps"

    index: IntProperty()

    def execute(self, context):
        SESSION.group_open ^= {self.index}
        redraw()
        return {"FINISHED"}


class BAI_OT_show_all(bpy.types.Operator):
    bl_idname = "bai.show_all"
    bl_label = "Show Earlier"
    bl_description = "Show or hide older messages"

    def execute(self, context):
        SESSION.show_all = not SESSION.show_all
        redraw()
        return {"FINISHED"}


class BAI_OT_toggle_entry(bpy.types.Operator):
    bl_idname = "bai.toggle_entry"
    bl_label = "Details"
    bl_description = "Show or hide details"

    index: IntProperty()

    def execute(self, context):
        if 0 <= self.index < len(SESSION.log):
            e = SESSION.log[self.index]
            e.expanded = not e.expanded
        redraw()
        return {"FINISHED"}


class BAI_OT_open_text(bpy.types.Operator):
    bl_idname = "bai.open_text"
    bl_label = "Open in Text Editor"
    bl_description = "Copy this content into a Text datablock"

    index: IntProperty(default=-1, description="-1 = pending code")
    block: IntProperty(default=-1, description="Index of a fenced code block in the message, -1 = whole entry")

    def execute(self, context):
        if self.index < 0:
            if SESSION.pending is None:
                return {"CANCELLED"}
            name, body = "AI_pending_code.py", SESSION.pending.code
        else:
            if not (0 <= self.index < len(SESSION.log)):
                return {"CANCELLED"}
            e = SESSION.log[self.index]
            name, body = "AI_output.py", (e.detail or e.text)
            if self.block >= 0:
                codes = [b for b in mdparse.parse(e.text) if b[0] == "code"]
                if self.block < len(codes):
                    lang = codes[self.block][1] or "txt"
                    ext = "py" if lang in ("python", "py") else lang
                    name, body = f"AI_snippet.{ext}", codes[self.block][2]
        txt, shown = _text_block(name, body)
        if not shown:
            self.report({"INFO"}, f"Saved to text '{txt.name}' — open it in a Text Editor")
        return {"FINISHED"}


class BAI_OT_export_transcript(bpy.types.Operator):
    bl_idname = "bai.export_transcript"
    bl_label = "Export Transcript"
    bl_description = "Write the conversation into a Text datablock"

    def execute(self, context):
        txt, shown = _text_block("AI_chat_transcript.md", SESSION.transcript())
        if not shown:
            self.report({"INFO"}, f"Saved to text '{txt.name}'")
        return {"FINISHED"}


class BAI_OT_reload_skills(bpy.types.Operator):
    bl_idname = "bai.reload_skills"
    bl_label = "Reload Skills"

    def execute(self, context):
        prefs = get_prefs(context)
        lib = get_skills(prefs, force=True)
        self.report({"INFO"}, f"{len(lib) if lib else 0} skills loaded")
        return {"FINISHED"}


class BAI_OT_test_connection(bpy.types.Operator):
    bl_idname = "bai.test_connection"
    bl_label = "Test Connection"
    bl_description = "Send a short message to the configured model (result appears in the chat panel)"

    def execute(self, context):
        prefs = get_prefs(context)
        try:
            provider = build_provider(prefs)
        except ProviderError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}

        def work():
            try:
                r = provider.chat("Reply with the single word: pong", [{"role": "user", "content": "ping"}], [])
                SESSION.add("info", f"Connection OK ({provider.model}): {r.text.strip()[:200]}")
            except Exception as e:
                SESSION.add("error", f"Connection test failed: {e}", is_error=True)
            bpy.app.timers.register(lambda: (redraw(), None)[1], first_interval=0.01)

        threading.Thread(target=work, daemon=True).start()
        self.report({"INFO"}, "Testing connection… see the AI Chat panel")
        return {"FINISHED"}


class BAI_OT_compose(bpy.types.Operator):
    bl_idname = "bai.compose"
    bl_label = "Compose Message"
    bl_description = "Write the message in a wide popup"

    message: StringProperty(name="Message", default="")
    attach: BoolProperty(name="Attach Viewport Screenshot", default=False)

    def invoke(self, context, event):
        wm = context.window_manager
        self.message = wm.bai_input
        self.attach = wm.bai_attach_screenshot
        return wm.invoke_props_dialog(self, width=760, title="Message the AI", confirm_text="Send")

    def draw(self, context):
        layout = self.layout
        row = layout.row()
        row.scale_y = 2.0
        row.activate_init = True
        row.prop(self, "message", text="")
        if len(self.message) > 100:
            box = layout.box()
            box.active = False
            _draw_wrapped(box, self.message, 110)
        prefs = get_prefs(context)
        row = layout.row()
        row.enabled = bool(prefs and prefs.vision)
        row.prop(self, "attach")
        layout.label(text="Need several lines? Use the Text Editor button in the panel.", icon="INFO")

    def execute(self, context):
        wm = context.window_manager
        wm.bai_attach_screenshot = self.attach
        ok, err = _send(context, self.message)
        if not ok:
            _set_input(wm, self.message)
            self.report({"WARNING"}, err)
            return {"CANCELLED"}
        _set_input(wm, "")
        return {"FINISHED"}


class BAI_OT_edit_prompt(bpy.types.Operator):
    bl_idname = "bai.edit_prompt"
    bl_label = "Write in Text Editor"
    bl_description = "Write a multi-line message in the 'AI_prompt.md' text, then click Send Text"

    def execute(self, context):
        wm = context.window_manager
        txt = bpy.data.texts.get(PROMPT_TEXT)
        if txt is None:
            txt = bpy.data.texts.new(PROMPT_TEXT)
            if wm.bai_input.strip():
                txt.write(wm.bai_input)
                _set_input(wm, "")
        for win in wm.windows:
            for area in win.screen.areas:
                if area.type == "TEXT_EDITOR":
                    area.spaces.active.text = txt
                    return {"FINISHED"}
        self.report({"INFO"}, f"Open a Text Editor (Shift+F11) and select '{PROMPT_TEXT}'")
        return {"FINISHED"}


class BAI_OT_send_text(bpy.types.Operator):
    bl_idname = "bai.send_text"
    bl_label = "Send Text"
    bl_description = "Send the contents of the 'AI_prompt.md' text and clear it"

    def execute(self, context):
        txt = bpy.data.texts.get(PROMPT_TEXT)
        body = txt.as_string().strip() if txt else ""
        ok, err = _send(context, body)
        if not ok:
            self.report({"WARNING"}, err)
            return {"CANCELLED"}
        txt.clear()
        return {"FINISHED"}


# --------------------------------------------------------------------------
# panel
# --------------------------------------------------------------------------
def _draw_tool_row(layout, e, i, width):
    row = layout.row(align=True)
    row.alert = e.is_error
    op = row.operator("bai.toggle_entry", text="", emboss=False,
                      icon="DISCLOSURE_TRI_DOWN" if e.expanded else "DISCLOSURE_TRI_RIGHT")
    op.index = i
    row.label(text=e.text, icon="ERROR" if e.is_error else "DOT")
    if e.expanded and e.detail:
        _draw_wrapped(layout, e.detail, width, max_lines=MAX_DETAIL_LINES)
        layout.operator("bai.open_text", icon="TEXT").index = i


class BAI_PT_chat(bpy.types.Panel):
    """Controls: approval, status and input. Always at the top of the tab."""

    bl_idname = "BAI_PT_chat"
    bl_label = "BAI"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "AI Chat"
    bl_order = 0

    def draw_header_preset(self, context):
        row = self.layout.row(align=True)
        row.operator("bai.export_transcript", text="", icon="TEXT", emboss=False)
        row.operator("bai.clear", text="", icon="FILE_NEW", emboss=False)
        op = row.operator("preferences.addon_show", text="", icon="PREFERENCES", emboss=False)
        op.module = __package__

    def draw(self, context):
        layout = self.layout
        prefs = get_prefs(context)
        wm = context.window_manager
        width = _wrap_width(context)

        if prefs is not None:
            row = layout.row()
            row.alignment = "LEFT"
            lib = get_skills(prefs)
            skills_txt = f" · {len(lib)} skills" if lib else ""
            row.label(text=f"{prefs.model or '(no model)'}{skills_txt}", icon="WORLD")

        p = SESSION.pending
        if p is not None:
            box = layout.box()
            box.alert = True
            lines = p.code.strip().splitlines()
            box.label(text=f"Run this code? ({len(lines)} lines)", icon="QUESTION")
            _draw_wrapped(box, p.code.strip(), width, max_lines=8)
            row = box.row(align=True)
            row.scale_y = 1.4
            op = row.operator("bai.approve", text="Run", icon="CHECKMARK")
            op.approve, op.all_chat = True, False
            op = row.operator("bai.approve", text="Run All", icon="FF")
            op.approve, op.all_chat = True, True
            op = row.operator("bai.approve", text="Reject", icon="X")
            op.approve, op.all_chat = False, False
            box.operator("bai.open_text", text="View Full Code", icon="TEXT").index = -1

        if SESSION.auto_approve:
            row = layout.row(align=True)
            row.label(text="Running code without asking", icon="FF")
            row.operator("bai.auto_approve", text="Ask Again", icon="LOCKED")

        if SESSION.busy or SESSION.status:
            row = layout.row()
            row.label(text=SESSION.status or "Working…", icon="SORTTIME" if SESSION.busy else "CHECKMARK")
            if SESSION.busy:
                row.operator("bai.stop", text="Stop", icon="CANCEL")

        col = layout.column(align=True)
        col.enabled = not SESSION.busy
        row = col.row(align=True)
        row.scale_y = 1.6
        row.prop(wm, "bai_input", text="", icon="GREASEPENCIL")
        row.operator("bai.compose", text="", icon="FULLSCREEN_ENTER")
        if len(wm.bai_input) > width - 6:
            preview = col.box()
            preview.active = False
            _draw_wrapped(preview, wm.bai_input, width - 2, max_lines=12)
        row = col.row(align=True)
        row.scale_y = 1.3
        sub = row.row(align=True)
        sub.enabled = bool(prefs and prefs.vision)
        sub.prop(wm, "bai_attach_screenshot", text="", icon="IMAGE_DATA", toggle=True)
        row.operator("bai.edit_prompt", text="", icon="TEXT")
        row.operator("bai.send", icon="PLAY")
        txt = bpy.data.texts.get(PROMPT_TEXT)
        if txt is not None and txt.as_string().strip():
            row = col.row(align=True)
            row.operator("bai.send_text", text=f"Send '{PROMPT_TEXT}'", icon="EXPORT")


class BAI_PT_history(bpy.types.Panel):
    """The conversation, with tool steps grouped."""

    bl_idname = "BAI_PT_history"
    bl_label = "Conversation"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "AI Chat"
    bl_order = 1

    def draw(self, context):
        layout = self.layout
        prefs = get_prefs(context)
        width = _wrap_width(context)
        avail_px = (context.region.width if context.region else 300) - 40
        log = SESSION.log

        if not log:
            box = layout.box()
            _draw_wrapped(box, "Ask me to build, edit, light or render something in this scene. "
                               "e.g. \"Make a low-poly campfire with warm lighting\".", width, icon="LIGHT")
            return

        items = group_log(log)
        limit = prefs.history_items if prefs is not None else 0
        hidden = 0
        if limit and len(items) > limit and not SESSION.show_all:
            hidden = len(items) - limit
            items = items[hidden:]
        if hidden or SESSION.show_all and limit and len(group_log(log)) > limit:
            text = f"Show {hidden} earlier item{'s' if hidden != 1 else ''}" if hidden else "Hide earlier items"
            layout.operator("bai.show_all", text=text, icon="TRIA_UP" if hidden else "TRIA_DOWN", emboss=False)

        last_pos = len(items) - 1
        for pos, (kind, ref) in enumerate(items):
            box = layout.box()
            if kind == "tools":
                entries = [log[i] for i in ref]
                first = ref[0]
                is_open = first in SESSION.group_open
                live = SESSION.busy and pos == last_pos
                row = box.row(align=True)
                row.alert = any(e.is_error for e in entries) and not live
                op = row.operator("bai.toggle_group", text="", emboss=False,
                                  icon="DISCLOSURE_TRI_DOWN" if is_open else "DISCLOSURE_TRI_RIGHT")
                op.index = first
                row.label(text=summarize_group(entries), icon="SORTTIME" if live else "TOOL_SETTINGS")
                if is_open:
                    col = box.column(align=True)
                    for i in ref:
                        _draw_tool_row(col, log[i], i, width)
                elif live:
                    sub = box.row()
                    sub.active = False
                    sub.label(text=entries[-1].text, icon="BLANK1")
                continue
            e = log[ref]
            if e.role == "assistant" and prefs is not None and prefs.render_markdown:
                _draw_markdown(box, e.text, width, avail_px, ref)
            else:
                col = box.column()
                col.alert = e.is_error
                _draw_wrapped(col, e.text, width, icon=ROLE_ICON.get(e.role, "DOT"))


classes = (
    BAI_OT_send,
    BAI_OT_stop,
    BAI_OT_clear,
    BAI_OT_approve,
    BAI_OT_auto_approve,
    BAI_OT_toggle_group,
    BAI_OT_show_all,
    BAI_OT_toggle_entry,
    BAI_OT_open_text,
    BAI_OT_export_transcript,
    BAI_OT_reload_skills,
    BAI_OT_test_connection,
    BAI_OT_compose,
    BAI_OT_edit_prompt,
    BAI_OT_send_text,
    BAI_PT_chat,
    BAI_PT_history,
)


def register_props():
    bpy.types.WindowManager.bai_input = StringProperty(
        name="Message", description="Message to the assistant (Enter to send)", update=_on_input_update)
    bpy.types.WindowManager.bai_attach_screenshot = BoolProperty(
        name="Attach Viewport", description="Attach a screenshot of the 3D viewport to the next message",
        default=False)


def unregister_props():
    del bpy.types.WindowManager.bai_input
    del bpy.types.WindowManager.bai_attach_screenshot
