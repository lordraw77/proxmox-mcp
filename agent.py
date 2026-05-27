"""
agent.py — Proxmox AI Agent (OpenRouter, interactive CLI)
==========================================================
Agentic loop via OpenRouter.  Provider logic lives in llm.py.

Environment variables
---------------------
  PROXMOX_MCP_OPENROUTER_API_KEY   — required
  PROXMOX_MCP_OPENROUTER_MODEL     — model string (default: openrouter/auto)
  PROXMOX_MCP_USE_DOCKER           — "true" to spawn server.py in a container
  PROXMOX_MCP_DOCKER_IMAGE         — Docker image (default: lordraw/proxmox-mcp:latest)
  PROXMOX_MCP_ENV_FILE             — .env path forwarded to the container

Usage
-----
  cd /opt/proxmox-mcp
  .venv/bin/python agent.py

  >>> How many VMs are running on node pve?
  >>> Show me the config of VM 100
  >>> exit
"""

import asyncio
import os

import dotenv
from mcp import ClientSession
from mcp.client.stdio import stdio_client

import llm

dotenv.load_dotenv()

SERVER_PARAMS = llm.build_mcp_server_params()


async def ask(question: str) -> str:
    client, model = llm.build_client("openrouter")
    _result: str | None = None
    _error:  Exception | None = None
    async with stdio_client(SERVER_PARAMS) as (read, write):
        async with ClientSession(read, write) as mcp:
            await mcp.initialize()
            try:
                _result = await llm.agentic_loop(
                    client=client, model=model, mcp=mcp, question=question,
                    on_action=_on_action,
                )
            except Exception as exc:
                _error = exc
    if _error is not None:
        raise _error
    return _result  # type: ignore[return-value]


def _on_action(name: str, args: dict) -> None:
    import json
    print(f"  [tool] {name}({json.dumps(args, ensure_ascii=False)})")


async def main():
    _, model = llm.build_client("openrouter")
    use_docker = os.getenv("PROXMOX_MCP_USE_DOCKER", "false").strip().lower() in ("true", "1", "yes")
    mcp_backend = f"docker({os.getenv('PROXMOX_MCP_DOCKER_IMAGE', 'lordraw/proxmox-mcp:latest')})" if use_docker else "local"
    print(f"Proxmox AI Agent  |  provider=openrouter  model={model}  mcp={mcp_backend}")
    print("Type your request, or 'exit' / Ctrl-C to quit.\n")

    while True:
        try:
            question = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not question:
            continue
        if question.lower() in ("exit", "quit", "q"):
            break

        answer = await ask(question)
        print(f"\n{answer}\n")


if __name__ == "__main__":
    asyncio.run(main())
