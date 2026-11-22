"""Skill library loader (no bpy dependency).

Reads Claude-style skill folders (``<root>/<name>/SKILL.md`` with YAML-ish
front matter) such as https://github.com/arjun988/blender-skills and exposes
them to the LLM through progressive disclosure: a compact catalog goes into
the system prompt, full skill bodies and reference files are fetched on
demand through tools.
"""

import os
import re

_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


def _parse_front_matter(text):
    """Return (meta, body). Only the flat ``key: value`` lines we need."""
    m = _FRONT_MATTER.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        if not line or line.startswith((" ", "\t", "#")):
            continue  # nested keys (metadata:) are ignored
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, text[m.end():]


class Skill:
    __slots__ = ("name", "description", "path")

    def __init__(self, name, description, path):
        self.name = name
        self.description = description
        self.path = path  # folder containing SKILL.md


class SkillLibrary:
    def __init__(self, root):
        self.root = os.path.abspath(root) if root else ""
        self.skills = {}
        self.reload()

    # -- loading -----------------------------------------------------------
    def reload(self):
        self.skills = {}
        if not self.root or not os.path.isdir(self.root):
            return
        for entry in sorted(os.listdir(self.root)):
            folder = os.path.join(self.root, entry)
            skill_md = os.path.join(folder, "SKILL.md")
            if not os.path.isfile(skill_md):
                continue
            try:
                with open(skill_md, encoding="utf-8") as f:
                    meta, _ = _parse_front_matter(f.read())
            except OSError:
                continue
            name = meta.get("name") or entry
            self.skills[name] = Skill(name, meta.get("description", ""), folder)

    def __len__(self):
        return len(self.skills)

    # -- prompt material ---------------------------------------------------
    def catalog(self, max_desc=240):
        lines = []
        for s in self.skills.values():
            desc = s.description
            if len(desc) > max_desc:
                desc = desc[: max_desc - 1].rstrip() + "…"
            lines.append(f"- {s.name}: {desc}")
        return "\n".join(lines)

    # -- tool backends -----------------------------------------------------
    def load_skill(self, name):
        skill = self.skills.get(name)
        if skill is None:
            close = [n for n in self.skills if name.lower() in n.lower()]
            hint = f" Did you mean: {', '.join(close[:5])}?" if close else ""
            raise KeyError(f"Unknown skill '{name}'.{hint}")
        with open(os.path.join(skill.path, "SKILL.md"), encoding="utf-8") as f:
            body = f.read()
        refs = []
        ref_dir = os.path.join(skill.path, "references")
        if os.path.isdir(ref_dir):
            refs = sorted(f"{name}/references/{r}" for r in os.listdir(ref_dir) if r.endswith(".md"))
        footer = (
            "\n\n---\n[Skill folder: " + name + "/  — relative links like `references/x.md` resolve to `"
            + name + "/references/x.md`, and `../references/x.md` resolves to `references/x.md`. "
            "Fetch them with read_skill_file.]"
        )
        if refs:
            footer += "\n[Reference files: " + ", ".join(refs) + "]"
        return body + footer

    def read_file(self, rel_path):
        """Read a markdown file under the library root (path-traversal safe)."""
        rel_path = rel_path.replace("\\", "/").lstrip("/")
        while rel_path.startswith("../"):
            rel_path = rel_path[3:]  # skills refer to shared files as ../references/...
        full = os.path.abspath(os.path.join(self.root, rel_path))
        if os.path.commonpath([full, self.root]) != self.root:
            raise PermissionError("Path escapes the skills folder.")
        if not full.endswith(".md"):
            raise PermissionError("Only .md files can be read.")
        if not os.path.isfile(full):
            raise FileNotFoundError(f"No such skill file: {rel_path}")
        with open(full, encoding="utf-8") as f:
            return f.read()

    def list_files(self):
        out = []
        for dirpath, _dirs, files in os.walk(self.root):
            for fn in files:
                if fn.endswith(".md"):
                    out.append(os.path.relpath(os.path.join(dirpath, fn), self.root).replace("\\", "/"))
        return sorted(out)
