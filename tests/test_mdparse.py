"""Tests for the panel's Markdown parser (no Blender needed)."""

import importlib.util
import os
import unittest

_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bai", "mdparse.py")
_spec = importlib.util.spec_from_file_location("bai_mdparse", _path)
md = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(md)

SAMPLE = r"""# Campfire done

I created **3 objects** in `COL_Props` using the *prop-artist* skill.
Second line of the same paragraph.

## Changes
- `SM_Log_01` – bevelled cylinder
- Fire light: **Point**, 120 W
  continues here
  - nested detail
1. First step
2) Second step
- [x] done task
- [ ] open task

| Object | Tris |
|---|---:|
| SM_Log_01 | 320 |
| SM_Rock_\|01 | 96 |

> Tip: press **Ctrl+Z** to undo.

```python
import bpy
print("hi")
```

---
See [Blender docs](https://docs.blender.org/api/current/) and <https://example.com>.
my_snake_case_name stays and 2*3*4 stays.
"""


class ParseTests(unittest.TestCase):
    def test_blocks(self):
        b = md.parse(SAMPLE)
        kinds = [x[0] for x in b]
        self.assertEqual(kinds, ["heading", "para", "heading", "item", "item", "item", "item", "item",
                                 "item", "item", "table", "quote", "code", "rule", "para"])
        self.assertEqual(b[0], ("heading", 1, "Campfire done"))
        self.assertEqual(b[1][1], "I created 3 objects in COL_Props using the prop-artist skill. "
                                  "Second line of the same paragraph.")
        self.assertEqual(b[3], ("item", 0, "•", "SM_Log_01 – bevelled cylinder"))
        self.assertEqual(b[4], ("item", 0, "•", "Fire light: Point, 120 W continues here"))
        self.assertEqual(b[5], ("item", 1, "◦", "nested detail"))
        self.assertEqual(b[6][2], "1.")
        self.assertEqual(b[7][2], "2.")
        self.assertEqual(b[8][2:], ("☑", "done task"))
        self.assertEqual(b[9][2:], ("☐", "open task"))
        self.assertEqual(b[10], ("table", ["Object", "Tris"], [["SM_Log_01", "320"], ["SM_Rock_|01", "96"]]))
        self.assertEqual(b[11], ("quote", "Tip: press Ctrl+Z to undo."))
        self.assertEqual(b[12], ("code", "python", 'import bpy\nprint("hi")'))
        self.assertIn("my_snake_case_name stays and 2*3*4 stays.", b[14][1])

    def test_links(self):
        links = md.links_in(SAMPLE)
        self.assertEqual(links, [("Blender docs", "https://docs.blender.org/api/current/"),
                                 ("https://example.com", "https://example.com")])
        text, _ = md.inline("[local](file.md) and ![img](https://x/y.png)")
        self.assertEqual(text, "local and img")

    def test_code_span_protected(self):
        self.assertEqual(md.inline("`**not bold**` and **bold**")[0], "**not bold** and bold")
        self.assertEqual(md.inline(r"\*literal\*")[0], "*literal*")

    def test_unclosed_fence_and_plain(self):
        self.assertEqual(md.parse("```\ncode")[0], ("code", "", "code"))
        self.assertEqual(md.parse("just text"), [("para", "just text")])
        self.assertEqual(md.parse("Title\n====="), [("heading", 1, "Title")])


if __name__ == "__main__":
    unittest.main()
