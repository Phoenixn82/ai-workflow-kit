"""Developer smoke client. Sends real MCP calls; saved PNGs are test artifacts, not source."""
import asyncio
import base64
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent


async def main():
    params = StdioServerParameters(command=str(ROOT / "launch"), args=["serve", sys.argv[1] if len(sys.argv) > 1 else "Test"], env=dict(os.environ))
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as client:
            await client.initialize()
            print(json.dumps({"tools": [t.name for t in (await client.list_tools()).tools]}), flush=True)
            while True:
                line = await asyncio.to_thread(sys.stdin.readline)
                if not line or line.strip() == "quit":
                    break
                try:
                    request = json.loads(line)
                    result = await client.call_tool(request["tool"], request.get("arguments", {}))
                    blocks = []
                    for block in result.content:
                        if block.type == "image":
                            directory = ROOT / "evidence"
                            directory.mkdir(exist_ok=True, mode=0o700)
                            path = directory / "latest.png"
                            path.write_bytes(base64.b64decode(block.data))
                            blocks.append({"image": str(path)})
                        elif block.type == "text":
                            blocks.append(block.text)
                    print(json.dumps({"error": result.isError, "content": blocks}), flush=True)
                except Exception as exc:
                    print(json.dumps({"client_error": str(exc)}), flush=True)


asyncio.run(main())
