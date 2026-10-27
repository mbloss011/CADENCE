# Kestrel

Async, autonomous, local-first coder agent. Claude-Code-style tool use, but built for long-running,
goal-driven work: an MCP client, Agent Skills, a verify → reflect → retry loop, a heartbeat daemon,
semantic memory, image understanding, hooks/plugins, sandboxed shell, and Telegram/WhatsApp channels.

| Role | Default model | Does |
|---|---|---|
| **big** | `ornith-1.5-9b` | planning, coding, tool use, reflection verdicts, idea generation |
| **small** | `liquid/lfm2-1.2b` | context compaction, tool-output distillation, lesson consolidation |
| **vision** | big model (override with `vision:`) | `view_image`, image attachments |
| **embed** | `text-embedding-nomic-embed-text-v1.5` | semantic memory retrieval |

Both are served through any OpenAI-compatible endpoint (LM Studio default: `http://localhost:1234/v1`).

## Quick start

```bash
pip install -e ".[dev]"
kestrel doctor                      # checks both models, MCP servers, skills
kestrel                             # interactive chat (Rich UI)
kestrel run "add tests for parser.py" --verify "pytest -q"
kestrel daemon                      # heartbeat + event-driven autonomous mode
kestrel tasks add "fix flaky auth test" --verify "pytest tests/test_auth.py"
kestrel goals add "get coverage above 80%" --success "pytest --cov reports >80%"
kestrel run "match this mockup" --image mockup.png
kestrel memory search "how do we deploy"   # also: stats | add | reindex
kestrel inspect plugins                    # also: hooks | tools | skills | mcp
```

In chat: `/image <path> [prompt]`, `/memory <query>`, `/plugins`, `/hooks`, `/help` for the rest.

Config: copy `examples/config.yaml` to `~/.kestrel/config.yaml`. Env overrides: `LMSTUDIO_URL`,
`KESTREL_BIG_MODEL`, `KESTREL_SMALL_MODEL`, `KESTREL_BIG_URL`, `KESTREL_SMALL_URL`, `KESTREL_HOME`.

## Web GUI

```bash
kestrel web            # http://127.0.0.1:8765 — also runs the daemon + channels
kestrel web --open     # and open the browser    ·  --no-daemon / --no-channels / --port N
```

- **Projects**: every chat, task, goal, idea, canvas document and browser profile belongs to a project:
  a named folder the agent's tools, shell and sandbox are confined to. Leave the name empty and the
  small model names it from the folder's files and README. Switch projects from the top of the sidebar.
- **Chat**: streaming replies, tool cards, thinking, steering (send while it works), `Esc` to stop,
  images. A resizable dock beside the chat holds:
  - **Plan**: the agent's task list, context meter and session health.
  - **Canvas**: documents the agent writes with `canvas_write` (plans, reports, specs, HTML mockups).
    Edit them yourself; the agent re-reads your edits before revising. Version history, save as a project file.
  - **Browser**: one real Chromium per project that you and the agent drive together. It streams live; your
    clicks, typing and scrolling go to the page, so you can log in or solve a captcha and hand back. The
    agent uses `browser_open/snapshot/click/type/scroll/back/key/look`. Logins persist per project.
- **Files**: explorer + editor for the project. Files the agent writes are marked and refresh live;
  saving over an agent edit asks first. Upload by drag-and-drop; HTML/Markdown/image previews.
- **Goals**: long-horizon objectives with a vision and success criteria. The planner reviews progress,
  queues the next 1-3 tasks, re-plans after each one, asks you when it is stuck, and marks the goal
  achieved when every criterion is met. Journal, progress, pause/resume, notes that steer the planner.
- **Tasks**: concrete, verifiable work (act → verify → reflect → retry), from you, ideas or the planner.
  Edit, delete, steer, stop, retry, open the transcript.
- **Ideas**: an inbox the daemon fills during its heartbeat (or **Brainstorm** on demand). Edit, dismiss,
  delete, or promote to a task or a goal. Nothing runs until promoted (unless `auto_approve_ideas`).
- **Extensions**: create, edit, enable/disable and delete **skills** (SKILL.md, or import a folder/URL),
  **plugins** (Python, reloaded on save), **MCP servers** (or paste a Claude Desktop `mcpServers` block) and
  **hooks** (with a Test button). All live, including in chats already running.
- **Settings**: model roles picked from what LM Studio / Ollama / any OpenAI-compatible server actually has
  (scanned live: type, load state, quantization), per-model parameters, agent and daemon limits, sandbox,
  memory, browser, projects, status. Saved to `~/.kestrel/settings.json` (same keys as `config.yaml`,
  layered on top of it), applied live, kept across restarts. Theme and active project sync across browsers.
- **Memory**: semantic search, add and forget memories.

Loopback-only by default; requests must carry a loopback `Host` (anti DNS-rebinding) and a same-site
`Origin`. Binding elsewhere (`--host 0.0.0.0`) requires a token, auto-generated and printed. Project files
are served with `Content-Security-Policy: sandbox`, so an HTML file the agent wrote cannot call the API.
The shared browser needs `pip install "kestrel-agent[browser]"` and Chrome/Chromium (or `playwright install chromium`);
it runs outside the bash sandbox, with network access.
Frontend source is in `web/` (React + Vite + Tailwind): `cd web && npm install && npm run build`
writes to `src/kestrel/web/static/`; `npm run dev` hot-reloads against a running `kestrel web`.

### CLI equivalents

```bash
kestrel goals add "Ship v1 of the billing API" --success "POST /v1/invoices works" --success "docs/api.md exists"
kestrel goals | goals show <id> | goals pause|resume|archive <id>
kestrel tasks add "fix the flaky auth test" --verify "pytest -q tests/test_auth.py"
kestrel ideas | ideas add "..." | ideas promote <id> | ideas dismiss <id>
```

## Architecture

```
cli.py / ui.py ──► runtime.py ──► agent.py ──► llm.py (big)
   Rich UI            wiring        loop          │
                                     │            └─ context.py ──► llm.py (small)
                                     ├─ tools.py   (built-ins, workspace-confined)
                                     ├─ mcp_host.py (MCP client, mcp__server__tool)
                                     ├─ skills.py   (SKILL.md, progressive disclosure)
                                     ├─ memory.py   (episodic / semantic / procedural, hybrid vector search)
                                     ├─ hooks.py    (lifecycle hooks: shell + plugin callbacks)
                                     └─ vision.py   (image parts, view_image)
tasks.py (act → verify → reflect) ◄── autonomy.py (heartbeat, events, parallel tasks) ──► goals.py (planner)
ideas.py (idea inbox)   projects.py (named folders → per-project scopes)   canvas.py · browser.py
   └─ sandbox.py (none | bwrap | docker for bash + verify)      channels/ (telegram, whatsapp) ──► agents
plugins.py (register(api): tools + hooks)
```

- **Dynamic compaction** (`context.py`): at 70% of the window the small model runs three escalating
  stages — truncate old tool output, distill large outputs, fold oldest turns into a rolling summary
  (chunked to what a 1.2B model handles). Tool-call/result pairs are never split; if the small model is
  down, a deterministic summary takes over.
- **Reflection loop** (`tasks.py`): after each attempt an optional `--verify` command runs; objective
  exit codes outrank the model's opinion. Lessons go to procedural memory, outcomes to episodic.
- **Long-horizon goals** (`goals.py`): the planner (big model) reads the goal's vision, success criteria,
  journal, finished tasks, lessons and the project tree, then assesses progress (0-100), queues the next
  1-3 verifiable tasks, asks the user (`needs_user` pauses the goal) or declares it achieved. Each finished
  task is journaled and triggers a re-plan; a per-goal daily task budget (`goal_daily_tasks`) caps spend.
- **Daemon** (`autonomy.py`): wakes immediately on new work; runs pending tasks in parallel, plans active
  goals one at a time, and every `24h / reflection_cycles` (when idle) fills the idea inbox for the next
  project in rotation and consolidates lessons. Tasks interrupted by a crash are re-queued on restart.
- **Projects** (`projects.py`, `runtime.py`): each project gets a scope: a config view with its own
  workspace, file/shell tools, sandbox, hook working directory, skills (global + the project's) and
  canvas/browser tools. Tool registries are layered (agent → project → global), and forks read their
  parent live, so MCP servers and plugins added at runtime reach agents that already exist.
- **Steering / interrupts**: `Ctrl+C` cancels the in-flight model call or kills the running command
  (whole process group) and keeps context valid; `Agent.steer(text)` injects guidance before the next step.
- **Skills**: `<dir>/<name>/SKILL.md` with `name`/`description` frontmatter. Only the index is in the
  prompt; `load_skill` pulls the body on demand. Also reads `AGENTS.md` and `SOUL.md` from the workspace.
- **Memory** (`memory.py`): JSONL items stay the source of truth; vectors are indexed lazily into a
  local JSONL sidecar or Qdrant (`deploy/docker-compose.yml`). Retrieval blends cosine similarity (above
  a `min_similarity` floor, since embedders score unrelated text ~0.45) with keyword overlap and recency.
  Embedder or Qdrant down → keyword fallback, never an error.
- **Images** (`vision.py`): attachments become OpenAI `image_url` parts for the big model; the
  `view_image` tool asks the vision model about any workspace image (screenshots from MCP browsers,
  diagrams). Compaction only ever sees `[image]` placeholders, never base64.
- **Hooks** (`hooks.py`): `session_start`, `user_prompt`, `pre_tool`, `post_tool`, `stop`, `task_done` (alias `goal_done`), `goal_achieved`.
  Shell hooks get JSON on stdin; exit 2 blocks with stderr as the reason, stdout on exit 0 becomes context.
- **Plugins** (`plugins.py`): `register(api)` modules in `~/.kestrel/plugins` / `.kestrel/plugins` or
  `kestrel.plugins` entry points; `@api.tool(...)` and `@api.on(event, matcher)`. See `examples/plugins/`.
  Plugins and workspace config run with your privileges — only use ones you trust.
- **Sandbox** (`sandbox.py`): `bwrap` gives a read-only host, writable workspace, private `/tmp`, hidden
  `$HOME` and optional network cut-off; `docker` runs a throwaway container as your uid. Applies to
  `bash` and goal verify commands. `kestrel doctor` runs a probe command through it.
- **Channels** (`channels/`): `kestrel daemon` also starts configured channels. Each chat gets its own
  persistent agent session; messages during a running turn steer it; `/task`, `/goal`, `/goals`,
  `/tasks`, `/ideas`, `/approve`, `/stop`, `/new`, `/status` work in chat; photos go to the vision model. Telegram uses long polling
  (no public URL); WhatsApp uses the Meta Cloud API webhook (HMAC-verified) behind a tunnel. An allowlist
  is required for both.

## Tests

```bash
pytest -q        # 69 tests: context, agent loop, tasks, goals + planner, ideas, projects, MCP (incl. hot add/remove),
                 # daemon, vector memory (local + Qdrant), images, hooks, plugins (live reload), skills, settings,
                 # bwrap sandbox, Telegram, WhatsApp, web API + websocket, files, canvas, shared browser (real Chrome)
```

## Limits

- `bash` is unsandboxed unless `sandbox.mode` is set; file tools are always workspace-confined.
- The `bwrap` sandbox hides `$HOME`, so tools installed there (pyenv, nvm, cargo) need `ro_binds`.
- Snap-packaged Docker cannot mount workspaces under `/tmp`; use a workspace under `$HOME` or `bwrap`.
- Plugins load once globally, so `api.workspace` is the launch folder, not the active project.
- Changing the memory backend or embedding model needs a restart (the UI says so).
- WhatsApp needs a Meta Business app, a phone number id and a public HTTPS URL (e.g. `cloudflared`).
- Browser/computer use are expected to come from MCP servers (pair them with `view_image` for screenshots).
