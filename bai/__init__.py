"""BAI – Blender AI Chat.

Chat with any LLM (OpenAI-compatible or Anthropic API) from a 3D Viewport
sidebar panel. The model drives Blender with Python and follows the
blender-skills library (https://github.com/arjun988/blender-skills).
"""

bl_info = {
    "name": "BAI - Blender AI Chat",
    "author": "BAI contributors",
    "version": (0, 3, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar (N) > AI Chat",
    "description": "Chat with a custom LLM that uses blender-skills to work in your scene",
    "category": "Interface",
}

import bpy

from . import prefs, session, ui

_classes = (prefs.BAI_AddonPreferences,) + ui.classes


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    ui.register_props()


def unregister():
    session.SESSION.stop()
    if bpy.app.timers.is_registered(session._tick):
        bpy.app.timers.unregister(session._tick)
    ui.unregister_props()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
