# Local System Bridge — MCP Server

An MCP server that runs on YOUR computer and answers questions straight from it.
When an AI agent asks "what's in this folder?" or "run this command", these tools
execute on **your machine** and send the answer back — no more copy-paste middleman.

## Tools exposed

| Tool | What it does |
|---|---|
| `read_file_content` | Reads any text file on this PC and returns its contents |
| `list_directory` | Lists files/folders in any directory |
| `get_system_info` | OS version, CPU cores, RAM, disk space, uptime |
| `run_command` | Runs any shell command and returns stdout/stderr |

## 1. Install

```bash
pip install fastmcp
# optional, for RAM stats on Windows/Mac:
pip install psutil
```

## 2. Test it standalone

```bash
fastmcp dev server.py
```

This opens the MCP Inspector in your browser so you can click each tool and try it.

## 3. Hook it into an AI client

### Claude Desktop
**Easy way** (auto-edits the config for you):

```bash
fastmcp install claude-desktop server.py
```

**Manual way** — edit the config file:

- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "local-system-bridge": {
      "command": "fastmcp",
      "args": ["run", "C:\\full\\path\\to\\local-mcp-bridge\\server.py"]
    }
  }
}
```

Restart Claude Desktop → you'll see the 🔨 tools icon with your 4 tools.

### Cursor
Settings → MCP → "Add new global MCP server", or edit `%USERPROFILE%\.cursor\mcp.json`
(`~/.cursor/mcp.json` on Mac/Linux) with the same JSON as above.

## 4. Use it

Just talk normally to the agent:

- "What's on my Desktop?" → calls `list_directory`
- "Read my notes.txt" → calls `read_file_content`
- "How much RAM do I have?" → calls `get_system_info`
- "Run `ipconfig` and tell me my IP" → calls `run_command`

## Notes & safety knobs (edit top of server.py)

- `MAX_CHARS` — caps file/output size so the agent isn't flooded.
- `CMD_TIMEOUT` — kills any command after 60s.
- Since your real data lives on another PC, `run_command` is fully open.
  If that ever changes, add a simple allowlist inside `run_command` before the
  `subprocess.run` call, e.g.:

```python
ALLOWED = ("dir", "ipconfig", "ping", "python", "echo")
if not command.strip().lower().startswith(ALLOWED):
    return "Blocked: command not in allowlist"
```
