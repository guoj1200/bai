"""Blender-side tool implementations. Must run on Blender's main thread."""

import base64
import contextlib
import io
import json
import math
import os
import tempfile
import traceback

import bmesh
import bpy
import mathutils

MAX_OBJECTS = 200


def view3d_override():
    """Context members for the first 3D Viewport, so bpy.ops work from a timer."""
    wm = bpy.context.window_manager
    for win in wm.windows:
        screen = win.screen
        for area in screen.areas:
            if area.type == "VIEW_3D":
                region = next((r for r in area.regions if r.type == "WINDOW"), None)
                if region is not None:
                    return {"window": win, "screen": screen, "area": area, "region": region}
    if wm.windows:
        return {"window": wm.windows[0], "screen": wm.windows[0].screen}
    return {}


def _vec(v, nd=3):
    return [round(float(x), nd) for x in v]


def get_scene_info(_args=None):
    scene = bpy.context.scene
    view_layer = bpy.context.view_layer
    objects = []
    for obj in list(scene.objects)[:MAX_OBJECTS]:
        info = {
            "name": obj.name,
            "type": obj.type,
            "location": _vec(obj.location),
            "rotation_euler_deg": [round(math.degrees(a), 1) for a in obj.rotation_euler],
            "scale": _vec(obj.scale),
            "dimensions": _vec(obj.dimensions),
            "visible": obj.visible_get(view_layer=view_layer),
            "collections": [c.name for c in obj.users_collection],
        }
        if obj.parent:
            info["parent"] = obj.parent.name
        if obj.type == "MESH" and obj.data:
            me = obj.data
            info["mesh"] = {"verts": len(me.vertices), "faces": len(me.polygons),
                            "tris": sum(len(p.vertices) - 2 for p in me.polygons)}
        if getattr(obj, "material_slots", None):
            info["materials"] = [s.material.name if s.material else None for s in obj.material_slots]
        if obj.modifiers:
            info["modifiers"] = [f"{m.name} ({m.type})" for m in obj.modifiers]
        if obj.type == "LIGHT":
            info["light"] = {"type": obj.data.type, "energy": round(obj.data.energy, 3),
                             "color": _vec(obj.data.color)}
        if obj.type == "CAMERA":
            info["camera"] = {"lens_mm": round(obj.data.lens, 2), "type": obj.data.type}
        objects.append(info)

    def walk(col, depth=0):
        rows = [{"name": col.name, "depth": depth, "objects": len(col.objects)}]
        for child in col.children:
            rows += walk(child, depth + 1)
        return rows

    active = view_layer.objects.active
    data = {
        "blender_version": bpy.app.version_string,
        "file": bpy.data.filepath or "(unsaved)",
        "scene": scene.name,
        "mode": bpy.context.mode,
        "render_engine": scene.render.engine,
        "resolution": [scene.render.resolution_x, scene.render.resolution_y],
        "frame_range": [scene.frame_start, scene.frame_end, scene.frame_current],
        "unit_system": scene.unit_settings.system,
        "active_object": active.name if active else None,
        "selected": [o.name for o in bpy.context.selected_objects] if hasattr(bpy.context, "selected_objects") else [],
        "camera": scene.camera.name if scene.camera else None,
        "world": scene.world.name if scene.world else None,
        "object_count": len(scene.objects),
        "objects": objects,
        "collections": walk(scene.collection),
        "materials": [m.name for m in bpy.data.materials][:100],
    }
    if len(scene.objects) > MAX_OBJECTS:
        data["note"] = f"Only the first {MAX_OBJECTS} objects are listed."
    return json.dumps(data, indent=1), [], False


def execute_blender_code(args):
    code = str(args.get("code", ""))
    namespace = {
        "__name__": "__bai__",
        "bpy": bpy, "bmesh": bmesh, "mathutils": mathutils, "math": math,
        "C": bpy.context, "D": bpy.data,
    }
    out = io.StringIO()
    override = view3d_override()
    error = None
    try:
        compiled = compile(code, "<ai_code>", "exec")
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            with bpy.context.temp_override(**override):
                exec(compiled, namespace)
    except Exception:
        tb = traceback.format_exc()
        # Drop the frames that belong to this add-on.
        marker = 'File "<ai_code>"'
        idx = tb.find(marker)
        error = ("Traceback (most recent call last):\n  " + tb[idx:]) if idx != -1 else tb

    try:
        with bpy.context.temp_override(**override):
            bpy.ops.ed.undo_push(message="AI Chat: run code")
    except Exception:
        pass

    parts = []
    printed = out.getvalue()
    if printed.strip():
        parts.append("stdout:\n" + printed.rstrip())
    if "result" in namespace and error is None:
        try:
            parts.append("result = " + json.dumps(namespace["result"], default=str, indent=1))
        except Exception:
            parts.append("result = " + repr(namespace["result"]))
    if error:
        parts.append("ERROR:\n" + error.rstrip())
    elif not parts:
        parts.append("Code ran successfully (no output).")
    return "\n\n".join(parts), [], error is not None


def get_viewport_screenshot(args):
    max_size = int(args.get("max_size") or 800)
    max_size = max(128, min(max_size, 2048))
    if bpy.app.background:
        return "No viewport in background mode.", [], True
    override = view3d_override()
    if "area" not in override:
        return "No 3D Viewport is open.", [], True
    path = os.path.join(tempfile.gettempdir(), "bai_viewport.png")
    with bpy.context.temp_override(**override):
        bpy.ops.screen.screenshot_area(filepath=path)
    img = bpy.data.images.load(path, check_existing=False)
    try:
        w, h = img.size
        if max(w, h) > max_size:
            s = max_size / max(w, h)
            img.scale(max(1, int(w * s)), max(1, int(h * s)))
            img.filepath_raw = path
            img.file_format = "PNG"
            img.save()
        w2, h2 = img.size
    finally:
        bpy.data.images.remove(img)
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    try:
        os.remove(path)
    except OSError:
        pass
    return (f"Viewport screenshot ({w2}x{h2}) attached.",
            [{"media_type": "image/png", "data": b64}], False)


TOOLS = {
    "get_scene_info": get_scene_info,
    "execute_blender_code": execute_blender_code,
    "get_viewport_screenshot": get_viewport_screenshot,
}


def run(name, args):
    fn = TOOLS.get(name)
    if fn is None:
        return f"Unknown Blender tool '{name}'.", [], True
    try:
        return fn(args or {})
    except Exception:
        return "Tool failed:\n" + traceback.format_exc(), [], True
