# BAI – Blender AI Chat

A Blender add-on that puts a chat panel in the 3D Viewport sidebar. You talk to **your own LLM provider**, and the model works in your scene by running `bpy` Python. It follows the 94 technical-art skills from [arjun988/blender-skills](https://github.com/arjun988/blender-skills).

```
3D Viewport ▸ Sidebar (N) ▸ AI Chat
┌──────────────────────────────┐
│ claude-opus-5-5 · 94 skills  │
│ 👤 Make a low-poly campfire   │
│ 💡 Plan: load prop-artist…    │
│ ▸ 🔧 load skill: prop-artist  │
│ ▸ 🔧 inspect scene            │
│ ▾ 🔧 run code (42 lines)      │
│     import bpy …              │
│ ┌ Run this code? ──────────┐ │
│ │ [✓ Run]   [✗ Reject]     │ │
│ └──────────────────────────┘ │
│ [ message…            ] [▶]  │
└──────────────────────────────┘
```

## Features

- **Any provider.** Supports two API formats:
  - **OpenAI-compatible** `/chat/completions`. This covers OpenAI, Azure OpenAI, OpenRouter, DeepSeek, Together, LiteLLM and in-house gateways, plus local servers such as Ollama, LM Studio, vLLM and llama.cpp.
  - **Anthropic Messages** `/v1/messages`. The system prompt is sent with prompt caching.
- **Skills by progressive disclosure.** The system prompt holds only a short catalog: each skill's name and description. The model calls `load_skill` to read a full `SKILL.md` and `read_skill_file` to read its `references/*.md`. The skills were written for "Blender MCP" agents, and the prompt maps their tool names onto this add-on's tools.
- **Tools available to the model:** `get_scene_info`, `execute_blender_code`, `get_viewport_screenshot` (for vision models), `load_skill`, `read_skill_file`.
- **Safe by default.** You approve each code snippet before it runs (**Run / Reject**). Each run adds an undo step, so **Ctrl+Z** reverts it. **Stop** cancels a turn that is in progress.
- **Non-blocking.** LLM calls run on a background thread, and every `bpy` call goes back to the main thread through `bpy.app.timers`.
- **No dependencies.** It uses only the Python standard library, so nothing is installed with pip.

## Install

Requires Blender 4.2 or newer. Tested on Blender 5.2.

1. Build the zip, or use `dist/bai-0.3.0.zip`:
   ```powershell
   & "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --factory-startup --command extension build --source-dir bai --output-dir dist
   ```
2. In Blender, open **Edit ▸ Preferences ▸ Get Extensions ▸ ⌄ ▸ Install from Disk…** and pick the zip. Dragging the zip into Blender also works.
3. Open the add-on preferences and set up the provider:
   - **Preset** fills in the base URL and model for OpenAI, Anthropic, OpenRouter, Ollama or LM Studio. You can also enter any custom endpoint.
   - **API Key**: enter it directly, or name an environment variable in **API Key Env Var**.
   - **Extra Headers (JSON)**: for gateways that need other headers, e.g. `{"api-key": "…"}` for Azure.
   - Turn off **Verify SSL** only for internal gateways that use self-signed certificates.
   - Click **Test Connection**. The result appears in the chat panel.

For development, you can link the source folder instead of installing the zip. Add `<repo>` as an extension repository, or symlink `bai/` into `%APPDATA%\Blender Foundation\Blender\<ver>\extensions\user_default\bai`.

## Using it

Press **N** in the 3D Viewport and open the **AI Chat** tab. Type a message, then press **Enter** or click **Send**.

The tab has two panels:
- **BAI**, at the top: the code approval box, the status line, and the input. They stay in reach without scrolling.
- **Conversation**: the chat history. Consecutive tool steps collapse into one row, such as `16 steps · 10 code · 6 screenshots`; click ▸ to see each step. Only the most recent items are shown (**Preferences ▸ Visible History Items**, default 20), with a **Show earlier** button for the rest.

To put the input below the history instead, drag the panel headers by their ⠿ grip.

Approval buttons: **Run** runs this code once. **Run All** runs this code and every later snippet in the chat without asking, until you click **Ask Again** or start a **New Chat**. **Reject** tells the model to try something else.

- **Longer messages:** the ⤢ button next to the input opens a wide composer popup. Once your text is longer than the field, a wrapped preview appears under it.
- **Multi-line messages:** the 📄 button opens an `AI_prompt.md` text in any Text Editor. Write there, then click **Send 'AI_prompt.md'** in the panel.
- **Markdown replies:** the assistant's replies are drawn as Markdown. Headings, bullet, numbered and task lists, tables, quotes and rules are laid out. Code blocks get a copy-to-Text-Editor button, and links become buttons that open in your browser. Blender labels can't show bold or italic text, so that formatting is removed. You can turn this off with **Preferences ▸ Render Markdown Replies**.
- The 🖼 toggle attaches a viewport screenshot to your next message. Use it for "make it look more like this" requests.
- Click ▸ on a tool row to see the code and its output. **Open in Text Editor** copies the code into a Text datablock.
- The header icons: export the transcript, start a new chat, open the add-on preferences.

### Model recommendations

This is an agentic tool-use workload: many steps, with code generation. Use a model with strong function calling, such as Claude, GPT-4.1/5-class, Qwen3 or DeepSeek-V3-class. Small local models often fail at multi-step tool calls. Turn off **Model Supports Images** for text-only models. That disables the screenshot tool.

## Skills

A copy of the blender-skills `.claude/skills` folder (v1.3.0, MIT License) is bundled in `bai/skills/`. To use another version or your own skills, point **Preferences ▸ Skills Folder** to any folder laid out as `<skill-name>/SKILL.md`, for example a fresh `git clone` of blender-skills at `blender-skills/.claude/skills`. Then click **Reload Skills**.

To update the bundled copy:

```powershell
git clone --depth 1 https://github.com/arjun988/blender-skills $env:TEMP\bs
```
```powershell
Remove-Item -Recurse -Force bai\skills; Copy-Item -Recurse $env:TEMP\bs\.claude\skills bai\skills; Copy-Item $env:TEMP\bs\LICENSE bai\skills\LICENSE.txt
```

## Layout

| File | Purpose |
|---|---|
| `bai/__init__.py` | Registration |
| `bai/blender_manifest.toml` | Extension manifest (4.2+) |
| `bai/prefs.py` | Add-on preferences, provider presets, skill library cache |
| `bai/providers.py` | OpenAI-compatible and Anthropic HTTP adapters, built on urllib (no bpy) |
| `bai/agent.py` | Tool schemas, system prompt, agent loop, skill tools (no bpy) |
| `bai/mdparse.py` | Markdown to layout blocks for the chat panel (no bpy) |
| `bai/skills.py` | `SKILL.md` parser, catalog, path-safe reference reader (no bpy) |
| `bai/blender_tools.py` | Scene info, code execution, viewport screenshot (main thread) |
| `bai/session.py` | Chat state, worker thread, main-thread job queue, approvals |
| `bai/ui.py` | Sidebar panel and operators |
| `tests/test_agent.py` | Offline tests against a mock LLM server, for both API formats |
| `tests/test_mdparse.py` | Markdown parser tests |

Run the tests. They do not need Blender:

```powershell
python -m unittest discover -s tests
```

## Security note

`execute_blender_code` runs whatever Python the model writes, with your user's permissions. That Python can read and write files and access the network. Keep **Confirm Code Before Running** on unless you trust the model and provider. Scene data and screenshots are sent to the provider you configure.

## License

BAI is licensed under the [GNU General Public License v3.0 or later](LICENSE), as Blender add-ons must be (and as the Blender Extensions platform requires).

The bundled skill library in `bai/skills/` comes from [arjun988/blender-skills](https://github.com/arjun988/blender-skills). It keeps its own MIT License; see [bai/skills/LICENSE.txt](bai/skills/LICENSE.txt).
