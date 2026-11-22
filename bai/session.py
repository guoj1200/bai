"""Chat session state and the worker-thread <-> main-thread bridge.

The HTTP calls to the LLM run on a background thread so Blender's UI stays
responsive. Anything touching ``bpy`` is queued to the main thread and run by
a ``bpy.app.timers`` callback; the worker blocks until the result is back.
"""

import queue
import threading
import time

import bpy

from . import agent as agent_mod
from . import blender_tools
from .providers import ProviderError


class Entry:
    __slots__ = ("role", "text", "detail", "is_error", "expanded")

    def __init__(self, role, text, detail="", is_error=False):
        self.role = role          # user | assistant | tool | error | info
        self.text = text
        self.detail = detail      # e.g. code or tool output, shown when expanded
        self.is_error = is_error
        self.expanded = False


class PendingApproval:
    def __init__(self, code):
        self.code = code
        self.event = threading.Event()
        self.approved = False


class Session:
    def __init__(self):
        self.history = []          # provider-neutral messages sent to the LLM
        self.log = []              # Entry objects shown in the panel
        self.lock = threading.Lock()
        self.busy = False
        self.status = ""
        self.cancel = threading.Event()
        self.pending = None        # PendingApproval
        self.auto_approve = False  # "Run All" pressed for this chat
        self.group_open = set()    # first log index of expanded tool groups
        self.show_all = False      # show history beyond the configured limit
        self._jobs = queue.Queue()
        self._thread = None
        self._dirty = False

    # -- log helpers (thread safe) ------------------------------------------
    def add(self, role, text, detail="", is_error=False):
        with self.lock:
            self.log.append(Entry(role, text, detail, is_error))
            self._dirty = True

    def set_status(self, text):
        self.status = text
        self._dirty = True

    def clear(self):
        if self.busy:
            return False
        with self.lock:
            self.history.clear()
            self.log.clear()
        self.status = ""
        self.auto_approve = False
        self.group_open.clear()
        self.show_all = False
        redraw()
        return True

    # -- main-thread bridge ---------------------------------------------------
    def _call_on_main(self, name, args):
        done = threading.Event()
        box = {}
        self._jobs.put((name, args, done, box))
        while not done.wait(0.1):
            if self.cancel.is_set():
                raise agent_mod.Cancelled()
        return box["result"]

    def _execute_blender_tool(self, name, args, confirm_code):
        if name == "execute_blender_code" and confirm_code and not self.auto_approve:
            p = PendingApproval(str(args.get("code", "")))
            self.pending = p
            self.set_status("Waiting for your approval…")
            while not p.event.wait(0.1):
                if self.cancel.is_set():
                    self.pending = None
                    raise agent_mod.Cancelled()
            self.pending = None
            if not p.approved:
                return "The user rejected this code. Ask what they want instead or try another approach.", [], True
            self.set_status("Running code…")
        return self._call_on_main(name, args)

    def tick(self):
        """Timer callback on the main thread."""
        try:
            while True:
                name, args, done, box = self._jobs.get_nowait()
                box["result"] = blender_tools.run(name, args)
                done.set()
        except queue.Empty:
            pass
        if self._dirty:
            self._dirty = False
            redraw()
        if self.busy or not self._jobs.empty():
            return 0.1
        redraw()
        return None

    # -- running a turn --------------------------------------------------------
    def send(self, text, config, provider, skills, images=None):
        if self.busy:
            return False
        msg = {"role": "user", "content": text}
        if images:
            msg["images"] = images
        self.history.append(msg)
        self.add("user", text + ("  [+ viewport image]" if images else ""))
        self.busy = True
        self.cancel.clear()
        self.set_status("Thinking…")

        vision = config["vision"]
        system = agent_mod.build_system_prompt(bpy.app.version_string, skills, vision, config["extra_prompt"])
        tools = agent_mod.tool_specs(skills, vision)

        def on_event(kind, data):
            if kind == "assistant":
                self.add("assistant", data)
            elif kind == "tool_call":
                detail = data["arguments"].get("code", "") if data["name"] == "execute_blender_code" else ""
                self.add("tool", agent_mod.summarize_args(data["name"], data["arguments"]), detail)
                self.set_status(f"Running {data['name']}…")
            elif kind == "tool_result":
                with self.lock:
                    if self.log and self.log[-1].role == "tool":
                        e = self.log[-1]
                        e.is_error = data["is_error"]
                        out = data["content"]
                        if data["name"] in ("load_skill", "read_skill_file"):
                            out = out[:600] + ("…" if len(out) > 600 else "")
                        e.detail = (e.detail + "\n\n# --- output ---\n" if e.detail else "") + out
                        self._dirty = True
            elif kind == "status":
                self.set_status(data)
            elif kind == "error":
                self.add("error", data, is_error=True)

        runner = agent_mod.Agent(
            provider, skills, system, tools,
            execute_blender_tool=lambda n, a: self._execute_blender_tool(n, a, config["confirm_code"]),
            on_event=on_event, max_steps=config["max_steps"],
            max_tool_output=config["max_tool_output"], is_cancelled=self.cancel.is_set,
        )

        def work():
            started = time.time()
            try:
                runner.run(self.history)
                self.set_status(f"Done in {time.time() - started:.0f}s")
            except agent_mod.Cancelled:
                self._repair_history()
                self.add("info", "Stopped.")
                self.set_status("Stopped")
            except ProviderError as e:
                self._repair_history()
                self.add("error", str(e), is_error=True)
                self.set_status("Error")
            except Exception as e:  # never leave the session stuck
                self._repair_history()
                self.add("error", f"{type(e).__name__}: {e}", is_error=True)
                self.set_status("Error")
            finally:
                self.busy = False
                self._dirty = True

        self._thread = threading.Thread(target=work, name="BAI-agent", daemon=True)
        self._thread.start()
        if not bpy.app.timers.is_registered(_tick):
            bpy.app.timers.register(_tick, first_interval=0.05)
        return True

    def _repair_history(self):
        """Ensure every tool call has a result so the next request is valid."""
        answered = {m["tool_call_id"] for m in self.history if m["role"] == "tool"}
        for m in list(self.history):
            if m["role"] == "assistant":
                for tc in m.get("tool_calls") or []:
                    if tc["id"] not in answered:
                        idx = self.history.index(m) + 1
                        while idx < len(self.history) and self.history[idx]["role"] == "tool":
                            idx += 1
                        self.history.insert(idx, {"role": "tool", "tool_call_id": tc["id"], "name": tc["name"],
                                                  "content": "Cancelled.", "images": [], "is_error": True})
                        answered.add(tc["id"])

    def stop(self):
        self.cancel.set()
        p = self.pending
        if p:
            p.approved = False
            p.event.set()

    def transcript(self):
        lines = []
        for e in self.log:
            lines.append(f"## {e.role.upper()}\n{e.text}")
            if e.detail:
                lines.append(e.detail)
            lines.append("")
        return "\n".join(lines)


def group_log(log):
    """Collapse runs of consecutive tool entries.

    Returns a list of ("entry", index) and ("tools", [indices]) items.
    """
    items = []
    for i, e in enumerate(log):
        if e.role == "tool":
            if items and items[-1][0] == "tools":
                items[-1][1].append(i)
            else:
                items.append(("tools", [i]))
        else:
            items.append(("entry", i))
    return items


def summarize_group(entries):
    """'12 steps · 8 code · 3 screenshots · 1 failed' for a tool group."""
    kinds = {"code": 0, "screenshot": 0, "skill": 0, "scene": 0}
    failed = 0
    for e in entries:
        t = e.text
        if t.startswith("run code"):
            kinds["code"] += 1
        elif t.startswith("viewport"):
            kinds["screenshot"] += 1
        elif t.startswith(("load skill", "read:")):
            kinds["skill"] += 1
        elif t.startswith("inspect"):
            kinds["scene"] += 1
        failed += e.is_error
    n = len(entries)
    parts = [f"{n} step{'s' if n != 1 else ''}"]
    labels = {"code": "code", "screenshot": "screenshot", "skill": "skill", "scene": "scene check"}
    for k, v in kinds.items():
        if v:
            parts.append(f"{v} {labels[k]}{'s' if v != 1 and k != 'code' else ''}")
    if failed:
        parts.append(f"{failed} failed")
    return " · ".join(parts)


SESSION = Session()


def _tick():
    return SESSION.tick()


def redraw():
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None:
        return
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
