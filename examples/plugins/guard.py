"""Example Kestrel plugin: copy to ~/.kestrel/plugins/ or <workspace>/.kestrel/plugins/."""

from kestrel.hooks import HookResult

DANGEROUS = ("git push --force", "git push -f", "rm -rf /", "mkfs", ":(){")


def register(api):
    @api.on("pre_tool", matcher="bash")
    def block_dangerous(payload):
        cmd = payload["arguments"].get("command", "")
        for bad in DANGEROUS:
            if bad in cmd:
                return HookResult(block=True, reason=f"'{bad}' is blocked by the guard plugin")

    @api.on("pre_tool", matcher="write_file")
    def protect_secrets(payload):
        if payload["arguments"].get("path", "").endswith((".env", ".pem", "id_rsa")):
            return HookResult(block=True, reason="writing secrets files is blocked by the guard plugin")

    @api.tool("todo_scan", "List TODO/FIXME comments in the workspace (path:line: text).",
              {"type": "object", "properties": {"limit": {"type": "integer"}}}, read_only=True)
    def todo_scan(args):
        hits = []
        for f in api.workspace.rglob("*.py"):
            if ".venv" in f.parts or "node_modules" in f.parts:
                continue
            for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                if "TODO" in line or "FIXME" in line:
                    hits.append(f"{f.relative_to(api.workspace)}:{i}: {line.strip()[:160]}")
        return "\n".join(hits[: int(args.get("limit") or 100)]) or "(no TODO/FIXME found)"
