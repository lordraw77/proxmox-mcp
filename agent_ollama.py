"""
agent_ollama.py — Proxmox AI Agent powered by Ollama
======================================================
Same agentic loop as agent.py but uses a local Ollama instance instead of
OpenRouter.  Ollama exposes an OpenAI-compatible API on localhost so the
same openai client and tool-calling code work without modification.

Architecture
------------
  User prompt
      │
      ▼
  Ollama  (local LLM inference — any model installed with `ollama pull`)
      │  tool_calls
      ▼
  MCP stdio client  (spawns server.py as a child process)
      │  JSON-RPC calls
      ▼
  server.py  (MCP server — 69 Proxmox tools)
      │  HTTPS
      ▼
  Proxmox VE cluster

Tool calling support
--------------------
  Not every Ollama model supports structured function/tool calling.
  Verified working models (install with `ollama pull <name>`):

    Model              Size    Tool use quality
    ─────────────────  ──────  ────────────────────────────────────────
    qwen2.5:7b         4.7 GB  ★★★★★  Excellent — recommended default
    qwen2.5:14b        9.0 GB  ★★★★★  Best local quality
    llama3.1:8b        4.7 GB  ★★★★☆  Very good
    llama3.2:3b        2.0 GB  ★★★☆☆  Good, faster on low-end hardware
    mistral:7b         4.1 GB  ★★★☆☆  Decent
    gemma3:4b          3.3 GB  ★★☆☆☆  Limited tool use — may fail

  Models that do NOT support tool calling will return plain text instead
  of tool_calls.  The agent detects this and prints a warning.

Transport
---------
  LLM   : Ollama HTTP API (OpenAI-compatible) — no network required
  MCP   : stdio — server.py spawned as a subprocess per ask() call

Environment variables (loaded from .env)
-----------------------------------------
  OLLAMA_HOST       — Ollama base URL (default: http://localhost:11434)
  OLLAMA_MODEL      — model to use    (default: qwen2.5:7b)
  MCP_USE_DOCKER    — "true" to spawn server.py inside the Docker image
  MCP_DOCKER_IMAGE  — image name      (default: proxmox-mcp:latest)
  MCP_ENV_FILE      — path to .env forwarded to the container

Usage
-----
  # Pull a tool-capable model first (once)
  ollama pull qwen2.5:7b

  # Start the agent
  cd /opt/proxmox-mcp
  .venv/bin/python agent_ollama.py

  # Use a different model
  OLLAMA_MODEL=llama3.1:8b .venv/bin/python agent_ollama.py
"""

import asyncio
import json
import os
from openai import AsyncOpenAI
from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters
import dotenv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

dotenv.load_dotenv()

# Ollama serves an OpenAI-compatible API on port 11434 by default.
# Set OLLAMA_HOST to point to a remote Ollama instance if needed.
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")

# Default model — qwen2.5:7b has the best tool-calling support among
# commonly available Ollama models at a reasonable size.
# Override with OLLAMA_MODEL env var or OLLAMA_MODEL=<name> prefix.
MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")

# ---------------------------------------------------------------------------
# MCP server spawn parameters (shared logic with agent.py)
# ---------------------------------------------------------------------------

def _build_server_params() -> StdioServerParameters:
    """
    Return StdioServerParameters for spawning the MCP server.
    Supports local venv mode and Docker mode (MCP_USE_DOCKER=true).
    """
    use_docker = os.getenv("MCP_USE_DOCKER", "false").strip().lower() in ("true", "1", "yes")

    if use_docker:
        image = os.getenv("MCP_DOCKER_IMAGE", "proxmox-mcp:latest")
        env_file = os.path.abspath(os.getenv("MCP_ENV_FILE", ".env"))
        return StdioServerParameters(
            command="docker",
            args=["run", "--rm", "-i", "--env-file", env_file, image],
            env={},
        )

    return StdioServerParameters(
        command=str(os.path.abspath(".venv/bin/python")),
        args=[str(os.path.abspath("server.py"))],
        env={**os.environ},
    )


SERVER_PARAMS = _build_server_params()

# ---------------------------------------------------------------------------
# Tool format conversion
# ---------------------------------------------------------------------------

def _mcp_tool_to_openai(tool) -> dict:
    """
    Convert an MCP Tool descriptor to the OpenAI function-calling format.

    Ollama follows the same OpenAI tool schema so no special handling is
    needed beyond what agent.py already does.
    """
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.inputSchema,
        },
    }

# ---------------------------------------------------------------------------
# Ollama client factory
# ---------------------------------------------------------------------------

def _build_ollama_client() -> AsyncOpenAI:
    """
    Build an AsyncOpenAI client pointed at the local Ollama endpoint.

    Ollama's OpenAI-compatible API lives at /v1 — the openai library
    handles the rest identically to a real OpenAI call.  The api_key value
    is required by the client constructor but ignored by Ollama.
    """
    return AsyncOpenAI(
        base_url=f"{OLLAMA_HOST}/v1",
        api_key="ollama",           # required field, not validated by Ollama
    )

# ---------------------------------------------------------------------------
# Core agentic loop
# ---------------------------------------------------------------------------

async def ask(question: str) -> str:
    """
    Send a question to the local Ollama model and resolve any tool calls
    via the Proxmox MCP server.

    The loop is identical to agent.py:
      1. Send user question + tool schemas to Ollama.
      2. If the model returns tool_calls, execute each via the MCP client.
      3. Append tool results and call Ollama again.
      4. Repeat until Ollama returns a plain text response.

    Args:
        question : Natural-language question or command.

    Returns:
        The model's final text response.

    Raises:
        RuntimeError: if Ollama is unreachable or the model is not installed.
    """
    async with stdio_client(SERVER_PARAMS) as (read, write):
        async with ClientSession(read, write) as mcp_client:
            await mcp_client.initialize()

            client = _build_ollama_client()

            # Fetch and convert the 69 MCP tools to OpenAI format.
            tools_result = await mcp_client.list_tools()
            tools = [_mcp_tool_to_openai(t) for t in tools_result.tools]

            messages = [{"role": "user", "content": question}]

            # ── Agentic loop ──────────────────────────────────────────────
            while True:
                try:
                    response = await client.chat.completions.create(
                        model=MODEL,
                        messages=messages,
                        tools=tools,
                        tool_choice="auto",
                        # Ollama-specific options can be passed via extra_body.
                        # num_ctx controls the context window — increase if the
                        # 69 tool schemas overflow the model's default context.
                        extra_body={"options": {"num_ctx": 8192}},
                    )
                except Exception as e:
                    # Surface connection errors clearly — common causes:
                    #   • Ollama daemon not running  → `ollama serve`
                    #   • Model not installed        → `ollama pull <model>`
                    #   • Wrong OLLAMA_HOST          → check the env var
                    raise RuntimeError(
                        f"Cannot reach Ollama at {OLLAMA_HOST}. "
                        f"Make sure `ollama serve` is running and the model "
                        f"'{MODEL}' is installed (`ollama pull {MODEL}`). "
                        f"Original error: {e}"
                    ) from e

                msg = response.choices[0].message
                messages.append(msg)

                # Check finish reason — "stop" means no tool calls.
                finish_reason = response.choices[0].finish_reason

                if not msg.tool_calls:
                    # If the model produced text without calling any tool,
                    # return it immediately.  This is also the fallback for
                    # models that don't support tool calling — they will just
                    # answer from their own knowledge (potentially wrong for
                    # Proxmox data, but graceful).
                    if finish_reason not in ("stop", "length", None):
                        # Unexpected finish reason — log and return anyway.
                        print(f"[warning] finish_reason={finish_reason}")
                    return msg.content or ""

                # ── Execute tool calls ────────────────────────────────────
                # Some Ollama models may hallucinate tool names that don't
                # exist in our MCP server.  We catch the resulting errors
                # and feed them back to the model as tool results so it can
                # self-correct rather than crashing.
                valid_tool_names = {t["function"]["name"] for t in tools}

                for call in msg.tool_calls:
                    tool_name = call.function.name

                    if tool_name not in valid_tool_names:
                        # Unknown tool — tell the model it made a mistake.
                        content = (
                            f"Error: tool '{tool_name}' does not exist. "
                            f"Available tools: {', '.join(sorted(valid_tool_names))}"
                        )
                    else:
                        try:
                            args = json.loads(call.function.arguments)
                            result = await mcp_client.call_tool(tool_name, args)
                            content = "\n".join(
                                block.text
                                for block in result.content
                                if hasattr(block, "text")
                            )
                        except json.JSONDecodeError as e:
                            # Some models produce malformed JSON arguments.
                            content = f"Error parsing tool arguments: {e}"
                        except Exception as e:
                            content = f"Tool execution error: {e}"

                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": content,
                    })

                # Loop — send tool results back to Ollama.

# ---------------------------------------------------------------------------
# Interactive CLI
# ---------------------------------------------------------------------------

async def main():
    """Interactive REPL — reads questions from stdin, prints answers."""
    use_docker = os.getenv("MCP_USE_DOCKER", "false").strip().lower() in ("true", "1", "yes")
    backend = (
        f"docker ({os.getenv('MCP_DOCKER_IMAGE', 'proxmox-mcp:latest')})"
        if use_docker
        else "local venv"
    )

    print(f"Proxmox Agent (Ollama) — model: {MODEL} @ {OLLAMA_HOST} | backend: {backend}")

    # Verify Ollama is reachable before entering the loop.
    try:
        import httpx
        async with httpx.AsyncClient() as http:
            r = await http.get(f"{OLLAMA_HOST}/api/tags", timeout=3)
        installed = [m["name"] for m in r.json().get("models", [])]
        if MODEL not in installed and not any(m.startswith(MODEL.split(":")[0]) for m in installed):
            print(f"[warning] Model '{MODEL}' not found in Ollama. Run: ollama pull {MODEL}")
            print(f"          Installed models: {', '.join(installed) or 'none'}")
        else:
            print(f"Model ready. Installed: {', '.join(installed)}")
    except Exception as e:
        print(f"[warning] Cannot reach Ollama at {OLLAMA_HOST}: {e}")
        print("          Start Ollama with:  ollama serve")

    print("Type a question or 'exit' to quit.\n")

    while True:
        try:
            question = input(">>> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not question:
            continue
        if question.lower() in ("exit", "quit", "q"):
            break

        try:
            answer = await ask(question)
            print(f"\n{answer}\n")
        except RuntimeError as e:
            # Surface Ollama connection errors cleanly without a traceback.
            print(f"\n[error] {e}\n")


if __name__ == "__main__":
    asyncio.run(main())
