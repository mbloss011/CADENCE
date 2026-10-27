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
kestrel goals add "fix flaky auth test" --verify "pytest tests/test_auth.py"
kestrel run "match this mockup" --image mockup.png
kestrel memory search "how do we deploy"   # also: stats | add | reindex
kestrel inspect plugins                    # also: hooks | tools | skills | mcp
```

In chat: `/image <path> [prompt]`, `/memory <query>`, `/plugins`, `/hooks`, `/help` for the rest.

Config: copy `examples/config.yaml` to `~/.kestrel/config.yaml`. Env overrides: `LMSTUDIO_URL`,
`KESTREL_BIG_MODEL`, `KESTREL_SMALL_MODEL`, `KESTREL_BIG_URL`, `KESTREL_SMALL_URL`, `KESTREL_HOME`.

## Web GUI

```bash
kestrel web            # http://127.0.0.1:8765 — chat, steer, goals, memory, system; also runs the daemon + channels
kestrel web --open     # and open the browser    ·  --no-daemon / --no-channels / --port N
```

- **Chat**: streaming replies, collapsible tool cards (exit codes, hook blocks), thinking, the agent's
  task list and context meter. While a turn runs, anything you send **steers** it; `Esc` / Stop interrupts.
  Paste, drop or attach images.
- **Goals**: queue goals with a verify command, approve/reject daemon ideas, stop or steer running goals,
  read reflections. Pause/resume the daemon.
- **Memory**: semantic search, filter by kind, add memories. **System**: model/memory/sandbox health,
  tools, plugins, hooks, MCP, skills.

Loopback-only by default; requests must carry a loopback `Host` (anti DNS-rebinding) and a same-site
`Origin`. Binding elsewhere (`--host 0.0.0.0`) requires a token, auto-generated and printed.
Frontend source is in `web/` (React + Vite + Tailwind): `cd web && npm install && npm run build`
writes to `src/kestrel/web/static/`; `npm run dev` hot-reloads against a running `kestrel web`.

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
goals.py (act → verify → reflect) ◄── autonomy.py (heartbeat, events, parallel goals)
   └─ sandbox.py (none | bwrap | docker for bash + verify)      channels/ (telegram, whatsapp) ──► agents
plugins.py (register(api): tools + hooks)
```

- **Dynamic compaction** (`context.py`): at 70% of the window the small model runs three escalating
  stages — truncate old tool output, distill large outputs, fold oldest turns into a rolling summary
  (chunked to what a 1.2B model handles). Tool-call/result pairs are never split; if the small model is
  down, a deterministic summary takes over.
- **Reflection loop** (`goals.py`): after each attempt an optional `--verify` command runs; objective
  exit codes outrank the model's opinion. Lessons go to procedural memory, outcomes to episodic.
- **Daemon** (`autonomy.py`): wakes immediately on new goals, otherwise heartbeats every
  `24h / reflection_cycles`, proposing ideas (needing `approve` unless `auto_approve_ideas: true`) and
  consolidating lessons. Goals interrupted by a crash are re-queued on restart.
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
- **Hooks** (`hooks.py`): `session_start`, `user_prompt`, `pre_tool`, `post_tool`, `stop`, `goal_done`.
  Shell hooks get JSON on stdin; exit 2 blocks with stderr as the reason, stdout on exit 0 becomes context.
- **Plugins** (`plugins.py`): `register(api)` modules in `~/.kestrel/plugins` / `.kestrel/plugins` or
  `kestrel.plugins` entry points; `@api.tool(...)` and `@api.on(event, matcher)`. See `examples/plugins/`.
  Plugins and workspace config run with your privileges — only use ones you trust.
- **Sandbox** (`sandbox.py`): `bwrap` gives a read-only host, writable workspace, private `/tmp`, hidden
  `$HOME` and optional network cut-off; `docker` runs a throwaway container as your uid. Applies to
  `bash` and goal verify commands. `kestrel doctor` runs a probe command through it.
- **Channels** (`channels/`): `kestrel daemon` also starts configured channels. Each chat gets its own
  persistent agent session; messages during a running turn steer it; `/goal`, `/goals`, `/approve`,
  `/stop`, `/new`, `/status` work in chat; photos go to the vision model. Telegram uses long polling
  (no public URL); WhatsApp uses the Meta Cloud API webhook (HMAC-verified) behind a tunnel. An allowlist
  is required for both.

## Tests

```bash
pytest -q        # 57 tests: context, agent loop, goals, MCP, SSE, daemon, vector memory (local + Qdrant),
                 # images, hooks, plugins, bwrap sandbox, Telegram, WhatsApp webhook, web API + websocket
```

## Limits

- `bash` is unsandboxed unless `sandbox.mode` is set; file tools are always workspace-confined.
- The `bwrap` sandbox hides `$HOME`, so tools installed there (pyenv, nvm, cargo) need `ro_binds`.
- Snap-packaged Docker cannot mount workspaces under `/tmp`; use a workspace under `$HOME` or `bwrap`.
- WhatsApp needs a Meta Business app, a phone number id and a public HTTPS URL (e.g. `cloudflared`).
- Browser/computer use are expected to come from MCP servers (pair them with `view_image` for screenshots).
