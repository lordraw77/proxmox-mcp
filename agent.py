"""
agent.py — Proxmox AI Agent (interactive CLI)
===============================================
An interactive command-line agent that lets a user ask natural-language
questions about a Proxmox VE cluster and trigger management operations.

Architecture
------------
  User prompt
      │
      ▼
  OpenRouter API  (LLM inference — any model the user chooses)
      │  tool_calls
      ▼
  MCP stdio client  (spawns server.py as a child process)
      │  JSON-RPC calls
      ▼
  server.py  (MCP server — wraps the Proxmox REST API)
      │  HTTPS
      ▼
  Proxmox VE cluster

The agent implements a standard tool-use agentic loop:
  1. Send the user message + available tools to the LLM.
  2. If the LLM returns tool_calls, execute each tool via the MCP client.
  3. Append every tool result to the conversation and call the LLM again.
  4. Repeat until the LLM produces a plain text response with no tool calls.

Each call to ask() opens a fresh MCP session (spawning server.py) and closes
it when done.  The conversation history within a single ask() call is preserved
so the LLM can correlate multiple tool results, but history is NOT carried
across separate ask() calls (stateless between user prompts).

Transport
---------
  LLM   : OpenRouter HTTPS API (OpenAI-compatible format)
  MCP   : stdio — server.py is launched as a subprocess for every ask() call.
          Two subprocess modes are supported:
            local  (default) — spawns .venv/bin/python server.py
            docker           — spawns docker run proxmox-mcp:latest

Environment variables (loaded from .env)
-----------------------------------------
  OPENROUTER_API_KEY  — your OpenRouter API key (required)
  OPENROUTER_MODEL    — model string to use, e.g. "anthropic/claude-opus-4.5"
                        Defaults to "openrouter/free" (free-tier routing)
  MCP_USE_DOCKER      — set to "true" to spawn server.py inside a Docker
                        container instead of the local virtualenv.
                        Requires the proxmox-mcp:latest image to be built.
  MCP_DOCKER_IMAGE    — Docker image to use (default: proxmox-mcp:latest)
  MCP_ENV_FILE        — path to the .env file forwarded to the container
                        via --env-file (default: .env in the working directory)

Usage — local virtualenv
-------------------------
  cd /opt/proxmox-mcp
  .venv/bin/python agent.py

Usage — Docker backend
-----------------------
  docker build -t proxmox-mcp:latest .
  MCP_USE_DOCKER=true .venv/bin/python agent.py

  >>> How many VMs are running on node pve?
  >>> Show me the config of VM 100
  >>> exit
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

# Load .env before reading any environment variable.
dotenv.load_dotenv()

# Model identifier in OpenRouter format: "{provider}/{model-name}".
# The default "openrouter/free" uses OpenRouter's free-tier routing which
# may select any available free model.  Set OPENROUTER_MODEL in .env to pin
# a specific model (e.g. "anthropic/claude-opus-4.5", "google/gemini-2.5-pro").
MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free")

# ---------------------------------------------------------------------------
# MCP server spawn parameters
# ---------------------------------------------------------------------------

def _build_server_params() -> StdioServerParameters:
    """
    Return the StdioServerParameters used to spawn the MCP server process.

    Two modes are supported:

    Local mode (default)
        Spawns .venv/bin/python server.py directly.  Fastest — no container
        overhead.  Requires the virtualenv to be set up on the host.

    Docker mode  (MCP_USE_DOCKER=true)
        Spawns a Docker container from the proxmox-mcp:latest image.
        Credentials are forwarded via --env-file so they are never baked
        into the image.  Requires:
          1. Docker daemon running on the host
          2. proxmox-mcp:latest image built:  docker build -t proxmox-mcp .

    Returns:
        StdioServerParameters compatible with mcp.client.stdio.stdio_client()
    """
    use_docker = os.getenv("MCP_USE_DOCKER", "false").strip().lower() in ("true", "1", "yes")

    if use_docker:
        image = os.getenv("MCP_DOCKER_IMAGE", "proxmox-mcp:latest")
        env_file = os.path.abspath(os.getenv("MCP_ENV_FILE", ".env"))
        return StdioServerParameters(
            command="docker",
            args=[
                "run",
                "--rm",          # remove the container when the process exits
                "-i",            # keep stdin open — required for stdio JSON-RPC
                "--env-file", env_file,   # forward PROXMOX_* credentials
                image,
            ],
            # The container reads credentials from --env-file, so the host
            # environment is not forwarded (avoids leaking unrelated vars).
            env={},
        )

    # Local mode — spawn the venv Python interpreter with server.py.
    # os.environ is forwarded so the child process inherits PROXMOX_*
    # credentials already loaded by dotenv.load_dotenv() above.
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
    Convert an MCP Tool descriptor into the OpenAI function-calling format.

    The MCP spec and the OpenAI Chat Completions API use slightly different
    schemas for tool definitions:

      MCP Tool object fields:
        .name        str  — unique tool identifier
        .description str  — natural-language description for the LLM
        .inputSchema dict — JSON Schema for the tool's arguments

      OpenAI function tool format:
        {"type": "function", "function": {"name", "description", "parameters"}}

    Args:
        tool : mcp.types.Tool instance returned by ClientSession.list_tools()

    Returns:
        dict compatible with the OpenAI tools[] parameter
    """
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",   # guard against None descriptions
            "parameters": tool.inputSchema,           # JSON Schema — already in the right format
        },
    }

# ---------------------------------------------------------------------------
# Core agentic loop
# ---------------------------------------------------------------------------

async def ask(question: str) -> str:
    """
    Send a natural-language question to the LLM and resolve any tool calls
    by executing them against the Proxmox MCP server.

    This function manages the full lifecycle of one user interaction:
      - Opening an MCP session (spawning server.py)
      - Initialising the MCP handshake and fetching the tool catalogue
      - Creating an OpenRouter client with the current API key
      - Running the agentic loop until the LLM produces a final text response
      - Closing the MCP session and returning the answer

    The conversation messages list grows on each iteration:
      user message → LLM response (possibly with tool_calls) → tool results
      → LLM response → … → final LLM text response

    Args:
        question : The user's natural-language question or command.

    Returns:
        The LLM's final plain-text response string.
    """
    # Open the stdio transport to the MCP server.  stdio_client() spawns
    # server.py as a subprocess and gives us asyncio-compatible read/write
    # stream pairs for the JSON-RPC channel.
    async with stdio_client(SERVER_PARAMS) as (read, write):

        # Wrap the raw streams in a ClientSession which handles the MCP
        # protocol framing, request IDs and response correlation.
        async with ClientSession(read, write) as mcp_client:

            # Perform the MCP initialize handshake.  This must complete before
            # any other MCP method (list_tools, call_tool, etc.) can be sent.
            await mcp_client.initialize()

            # Build the OpenAI-compatible client pointing at OpenRouter.
            # We instantiate it here (inside ask) so the API key is read from
            # the environment at call time — this avoids a module-level
            # instantiation error when the key is not yet set.
            client = AsyncOpenAI(
                base_url="https://openrouter.ai/api/v1",
                api_key=os.getenv("OPENROUTER_API_KEY"),
            )

            # Fetch the tool catalogue from the MCP server and convert each
            # tool descriptor to the OpenAI function-calling format.
            tools_result = await mcp_client.list_tools()
            tools = [_mcp_tool_to_openai(t) for t in tools_result.tools]

            # Seed the conversation with the user's question.
            # The messages list will grow with each LLM turn and each batch
            # of tool results until the LLM signals it is done.
            messages = [{"role": "user", "content": question}]

            # ── Agentic loop ──────────────────────────────────────────────
            while True:

                # Call the LLM.  tool_choice="auto" lets the model decide
                # whether to use a tool or respond directly.
                response = await client.chat.completions.create(
                    model=MODEL,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                )

                # Extract the assistant's message from the first (and only)
                # choice.  The message object may contain:
                #   .content    — text response (present when no tool calls)
                #   .tool_calls — list of tool invocation requests (may be None)
                msg = response.choices[0].message

                # Append the raw assistant message object to the conversation.
                # The OpenAI SDK message objects are accepted directly by the
                # next messages.create() call — no manual serialisation needed.
                messages.append(msg)

                # If the LLM did not request any tool calls, it has produced
                # its final answer.  Return the text content and exit the loop.
                if not msg.tool_calls:
                    return msg.content or ""

                # ── Execute tool calls ────────────────────────────────────
                # The LLM may request multiple tool calls in a single response.
                # We execute all of them before sending results back, which
                # allows the model to reason over a complete batch of results
                # on the next turn.
                for call in msg.tool_calls:

                    # Deserialise the arguments JSON string that the LLM
                    # produced.  The LLM follows the tool's inputSchema but
                    # always serialises arguments as a JSON string, not a dict.
                    args = json.loads(call.function.arguments)

                    # Forward the tool call to the MCP server.  This triggers
                    # the corresponding handler in server.py which calls the
                    # Proxmox REST API and returns formatted text.
                    result = await mcp_client.call_tool(call.function.name, args)

                    # The MCP response content is a list of content blocks
                    # (TextContent, ImageContent, etc.).  We join all text
                    # blocks into a single string for the LLM.
                    content = "\n".join(
                        block.text
                        for block in result.content
                        if hasattr(block, "text")   # skip non-text blocks
                    )

                    # Append the tool result as a "tool" role message.
                    # tool_call_id links this result to the specific call the
                    # LLM made — the model requires this to correlate results
                    # when multiple tools were called in the same turn.
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": content,
                    })

                # Loop back to the LLM with the updated messages list.
                # The model will now see the tool results and either call more
                # tools or produce its final text response.

# ---------------------------------------------------------------------------
# Interactive CLI
# ---------------------------------------------------------------------------

async def main():
    """
    Run an interactive read-eval-print loop (REPL) in the terminal.

    Each iteration:
      1. Reads a line from stdin.
      2. Calls ask() to send the question through the agentic loop.
      3. Prints the LLM's response.

    The loop exits cleanly on EOF (Ctrl-D), KeyboardInterrupt (Ctrl-C),
    or when the user types 'exit', 'quit' or 'q'.
    """
    use_docker = os.getenv("MCP_USE_DOCKER", "false").strip().lower() in ("true", "1", "yes")
    backend = f"docker ({os.getenv('MCP_DOCKER_IMAGE', 'proxmox-mcp:latest')})" if use_docker else "local venv"
    print(f"Proxmox Agent — model: {MODEL} | backend: {backend}")
    print("Type a question or 'exit' to quit.\n")

    while True:
        try:
            # input() blocks synchronously.  For a production agent you might
            # replace this with asyncio.get_event_loop().run_in_executor() to
            # keep the event loop responsive during input, but for a CLI tool
            # blocking is perfectly acceptable.
            question = input(">>> ").strip()
        except (EOFError, KeyboardInterrupt):
            # EOFError  : stdin was closed (e.g. piped input exhausted)
            # KeyboardInterrupt : user pressed Ctrl-C
            print()   # move to a new line so the shell prompt looks clean
            break

        if not question:
            # Skip blank lines — don't waste an API call on empty input.
            continue

        if question.lower() in ("exit", "quit", "q"):
            break

        # Run the agentic loop and print the result.
        answer = await ask(question)
        print(f"\n{answer}\n")


if __name__ == "__main__":
    asyncio.run(main())
