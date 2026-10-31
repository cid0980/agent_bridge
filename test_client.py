"""
test_client.py — quick test for the local-system-bridge MCP server.
Run AFTER activating the venv:   python test_client.py
It launches server.py exactly like an AI client would and calls every tool.
"""

import asyncio
from fastmcp import Client


async def main() -> None:
    async with Client("server.py") as client:   # spawns the server over stdio
        tools = await client.list_tools()
        print("Tools the server exposes:", [t.name for t in tools])
        print("=" * 60)

        checks = [
            ("get_system_info", {}),
            ("list_directory", {"path": "~"}),
            ("run_command", {"command": "whoami && pwd"}),
        ]
        for tool_name, args in checks:
            print(f">>> calling {tool_name}({args})")
            result = await client.call_tool(tool_name, args)
            text = result.content[0].text if result.content else str(result.data)
            print(text[:800])
            print("=" * 60)

        print("ALL GOOD — your MCP server works 🎉")


if __name__ == "__main__":
    asyncio.run(main())
