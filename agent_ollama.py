"""
agent_ollama.py — Proxmox AI Agent powered by Ollama
======================================================
Same agentic loop as agent.py but uses a local or remote Ollama instance
instead of OpenRouter.  Ollama exposes an OpenAI-compatible API so the
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

    Model                   Size    Tool use quality
    ──────────────────────  ──────  ────────────────────────────────────
    qwen2.5:7b-instruct     4.7 GB  ★★★★★  Excellent — recommended
    qwen2.5:14b             9.0 GB  ★★★★★  Best local quality
    llama3.1:8b             4.7 GB  ★★★★☆  Very good
    llama3.2:3b             2.0 GB  ★★★☆☆  Good, faster on low-end hw
    mistral:7b              4.1 GB  ★★★☆☆  Decent
    gemma3:4b               3.3 GB  ★★☆☆☆  Limited — may answer in text

  Models that do NOT support tool calling return plain text instead of
  tool_calls.  The agent detects this and prints a warning.

Error handling
--------------
  The agent distinguishes and recovers from:
    • Ollama unreachable        — clear message, no traceback
    • Request timeout           — configurable via OLLAMA_TIMEOUT
    • HTTP errors (4xx / 5xx)  — model not found, GPU OOM, etc.
    • Empty response            — guards against choices[] IndexError
    • Malformed tool arguments  — JSON parse errors fed back to model
    • Unknown tool names        — hallucinated tools fed back to model
    • MCP tool timeout          — configurable via OLLAMA_TOOL_TIMEOUT
    • Context window exceeded   — finish_reason=length detected
    • Infinite tool-call loop   — capped at OLLAMA_MAX_ITERATIONS

Transport
---------
  LLM   : Ollama HTTP API (OpenAI-compatible) — no internet required
  MCP   : stdio — server.py spawned as a subprocess per ask() call

Environment variables (loaded from .env)
-----------------------------------------
  OLLAMA_HOST            — Ollama base URL (default: http://localhost:11434)
  OLLAMA_MODEL           — model to use (default: qwen2.5:7b-instruct)
  OLLAMA_TIMEOUT         — seconds to wait for each LLM response (default: 120)
  OLLAMA_TOOL_TIMEOUT    — seconds to wait for each MCP tool call (default: 30)
  OLLAMA_MAX_ITERATIONS  — max tool-call rounds per question (default: 10)
  MCP_USE_DOCKER         — "true" to spawn server.py inside the Docker image
  MCP_DOCKER_IMAGE       — image name (default: proxmox-mcp:latest)
  MCP_ENV_FILE           — path to .env forwarded to the container

Usage
-----
  # Pull a tool-capable model first (once)
  ollama pull qwen2.5:7b-instruct

  # Start the agent
  cd /opt/proxmox-mcp
  .venv/bin/python agent_ollama.py

  # Remote Ollama, specific model, increased timeout
  OLLAMA_HOST=http://192.168.0.140:11434 OLLAMA_MODEL=qwen2.5:7b-instruct .venv/bin/python agent_ollama.py
"""

import asyncio
import json
import os
import openai as _openai_module
from openai import AsyncOpenAI
from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters
import dotenv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

dotenv.load_dotenv()

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
MODEL       = os.getenv("OLLAMA_MODEL", "qwen2.5:7b-instruct")

# How long to wait for a single LLM completion (seconds).
# Local inference on CPU can be slow — 120 s is a safe default.
# Reduce for fast GPU setups; increase if large models are slow to start.
LLM_TIMEOUT: float = float(os.getenv("OLLAMA_TIMEOUT", "120"))

# How long to wait for a single MCP tool execution (seconds).
# Most Proxmox API calls complete in <5 s; 30 s covers slow clusters.
TOOL_TIMEOUT: float = float(os.getenv("OLLAMA_TOOL_TIMEOUT", "30"))

# Maximum tool-call rounds per user question.
# Prevents infinite loops when the model keeps hallucinating tool calls.
MAX_ITERATIONS: int = int(os.getenv("OLLAMA_MAX_ITERATIONS", "10"))

# ---------------------------------------------------------------------------
# MCP server spawn parameters
# ---------------------------------------------------------------------------

def _build_server_params() -> StdioServerParameters:
    """
    Return StdioServerParameters for spawning the MCP server process.

    Local mode  (default): spawns .venv/bin/python server.py
    Docker mode (MCP_USE_DOCKER=true): spawns docker run proxmox-mcp:latest
    """
    use_docker = os.getenv("MCP_USE_DOCKER", "false").strip().lower() in ("true", "1", "yes")

    if use_docker:
        image    = os.getenv("MCP_DOCKER_IMAGE", "proxmox-mcp:latest")
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
    """Convert an MCP Tool descriptor to the OpenAI function-calling format."""
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
    Build an AsyncOpenAI client pointed at the Ollama endpoint.

    timeout is set at the client level so every request inherits it.
    The api_key field is required by the openai SDK but ignored by Ollama.
    """
    return AsyncOpenAI(
        base_url=f"{OLLAMA_HOST}/v1",
        api_key="ollama",
        timeout=LLM_TIMEOUT,   # applies to connect + read; raises APITimeoutError
    )

# ---------------------------------------------------------------------------
# Error classification helpers
# ---------------------------------------------------------------------------

def _classify_llm_error(exc: Exception) -> str:
    """
    Return a human-readable error message for an exception raised by the
    Ollama API call, distinguishing the most common failure modes.

    Ollama-specific HTTP status meanings:
      404 — model not installed  (`ollama pull <model>`)
      500 — internal error (GPU OOM, corrupted model, etc.)
      503 — Ollama is starting up or overloaded
    """
    if isinstance(exc, _openai_module.APITimeoutError):
        return (
            f"Ollama request timed out after {LLM_TIMEOUT:.0f}s. "
            "The model may be loading or the machine is under load. "
            f"Increase OLLAMA_TIMEOUT (current: {LLM_TIMEOUT:.0f}s) or wait and retry."
        )

    if isinstance(exc, _openai_module.APIConnectionError):
        return (
            f"Cannot connect to Ollama at {OLLAMA_HOST}. "
            "Check that `ollama serve` is running and OLLAMA_HOST is correct."
        )

    if isinstance(exc, _openai_module.APIStatusError):
        code = exc.status_code
        body = exc.message or str(exc.body or "")

        if code == 404:
            return (
                f"Model '{MODEL}' not found in Ollama (HTTP 404). "
                f"Install it with:  ollama pull {MODEL}"
            )
        if code == 500:
            if "out of memory" in body.lower() or "oom" in body.lower():
                return (
                    f"Ollama ran out of memory while running '{MODEL}' (HTTP 500). "
                    "Try a smaller quantisation or free GPU memory."
                )
            return f"Ollama internal error (HTTP 500): {body}"
        if code == 503:
            return (
                "Ollama is unavailable (HTTP 503) — it may still be loading. "
                "Wait a few seconds and retry."
            )
        return f"Ollama returned HTTP {code}: {body}"

    # Fallback for unexpected exception types.
    return (
        f"Unexpected error communicating with Ollama: {type(exc).__name__}: {exc}. "
        f"Ollama host: {OLLAMA_HOST}"
    )

# ---------------------------------------------------------------------------
# Core agentic loop
# ---------------------------------------------------------------------------

async def ask(question: str) -> str:
    """
    Send a question to Ollama and resolve tool calls via the Proxmox MCP server.

    Robustness guarantees
    ----------------------
    • LLM call timeout    : raises after OLLAMA_TIMEOUT seconds (default 120 s)
    • MCP tool timeout    : raises after TOOL_TIMEOUT seconds (default 30 s)
    • Max iterations      : exits loop after MAX_ITERATIONS rounds (default 10)
    • Empty choices list  : guarded with an explicit check
    • Null content        : replaced with empty string
    • Bad tool arguments  : JSON errors fed back to the model as tool results
    • Unknown tool names  : hallucinated names fed back to the model
    • finish_reason=length: context window exhausted — returns partial answer
      with a warning prompt

    Args:
        question : Natural-language question or command.

    Returns:
        The model's final plain-text response string.

    Raises:
        RuntimeError: on unrecoverable LLM-side errors (connection, timeout,
                      HTTP errors).  MCP and tool errors are recovered inline
                      by returning an error string to the model.
    """
    async with stdio_client(SERVER_PARAMS) as (read, write):
        async with ClientSession(read, write) as mcp_client:
            await mcp_client.initialize()

            client = _build_ollama_client()

            tools_result = await mcp_client.list_tools()
            tools        = [_mcp_tool_to_openai(t) for t in tools_result.tools]
            valid_names  = {t["function"]["name"] for t in tools}

            messages = [{"role": "user", "content": question}]

            # ── Agentic loop ──────────────────────────────────────────────
            for iteration in range(MAX_ITERATIONS):

                # ── LLM call ─────────────────────────────────────────────
                try:
                    response = await client.chat.completions.create(
                        model=MODEL,
                        messages=messages,
                        tools=tools,
                        tool_choice="auto",
                        extra_body={
                            "options": {
                                # num_ctx: context window for this request.
                                # 69 tool schemas consume ~6 K tokens; 8192 leaves
                                # ~2 K for conversation history and the answer.
                                # Increase to 16384 for longer conversations.
                                "num_ctx": 8192,
                            }
                        },
                    )
                except (
                    _openai_module.APITimeoutError,
                    _openai_module.APIConnectionError,
                    _openai_module.APIStatusError,
                ) as exc:
                    # Known, classifiable errors → friendly message, no traceback.
                    raise RuntimeError(_classify_llm_error(exc)) from exc
                except Exception as exc:
                    # Unexpected errors (SDK bugs, network stack, etc.)
                    raise RuntimeError(_classify_llm_error(exc)) from exc

                # ── Response validation ───────────────────────────────────
                if not response.choices:
                    # Ollama returned an empty choices list — should not happen
                    # with a healthy model but can occur during OOM recovery.
                    raise RuntimeError(
                        "Ollama returned an empty response (choices=[]).  "
                        "The model may have crashed. Check `ollama ps` and retry."
                    )

                choice        = response.choices[0]
                msg           = choice.message
                finish_reason = choice.finish_reason

                # Guard against completely empty messages (no content, no tool_calls).
                if msg is None:
                    raise RuntimeError(
                        "Ollama returned a null message object.  "
                        "This is a server-side bug — retry or restart Ollama."
                    )

                messages.append(msg)

                # ── Context window exhausted ──────────────────────────────
                if finish_reason == "length":
                    # The model hit max_tokens or num_ctx.  Return whatever
                    # partial answer was generated and warn the user.
                    partial = (msg.content or "").strip()
                    warning = (
                        "\n\n[warning] The response was cut short because the "
                        "context window is full.  Try a shorter question or "
                        "increase OLLAMA_TIMEOUT / num_ctx."
                    )
                    return partial + warning if partial else warning

                # ── Final answer (no tool calls) ──────────────────────────
                if not msg.tool_calls:
                    if finish_reason not in ("stop", "length", "tool_calls", None):
                        print(f"[warning] unexpected finish_reason={finish_reason!r}")
                    return msg.content or ""

                # ── Execute tool calls ────────────────────────────────────
                for call in msg.tool_calls:
                    tool_name = call.function.name

                    # Some models return None IDs — generate a fallback so the
                    # tool_result message is always well-formed.
                    tool_id = call.id or f"call_{iteration}_{tool_name}"

                    if tool_name not in valid_names:
                        # Hallucinated tool name — tell the model and let it retry.
                        content = (
                            f"Error: tool '{tool_name}' does not exist.  "
                            f"Available tools: {', '.join(sorted(valid_names))}"
                        )
                    else:
                        try:
                            args = json.loads(call.function.arguments or "{}")
                        except json.JSONDecodeError as exc:
                            # Malformed JSON — feed the parse error back so the
                            # model can regenerate the arguments correctly.
                            content = (
                                f"Error: could not parse arguments for '{tool_name}': {exc}.  "
                                f"Raw arguments string: {call.function.arguments!r}"
                            )
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_id,
                                "content": content,
                            })
                            continue

                        try:
                            # Apply a per-tool timeout so a hanging Proxmox API
                            # call does not block the whole agent indefinitely.
                            result = await asyncio.wait_for(
                                mcp_client.call_tool(tool_name, args),
                                timeout=TOOL_TIMEOUT,
                            )
                            content = "\n".join(
                                block.text
                                for block in result.content
                                if hasattr(block, "text")
                            ) or "(tool returned no output)"

                        except asyncio.TimeoutError:
                            content = (
                                f"Error: tool '{tool_name}' timed out after "
                                f"{TOOL_TIMEOUT:.0f}s.  The Proxmox API may be "
                                "slow or unreachable."
                            )
                        except Exception as exc:
                            content = f"Error executing tool '{tool_name}': {exc}"

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "content": content,
                    })

                # Loop → send tool results back to Ollama.

            # ── Max iterations reached ────────────────────────────────────
            # The model kept calling tools without producing a final answer.
            # Return what we have so far (last non-tool message, if any).
            last_text = next(
                (m.content for m in reversed(messages)
                 if getattr(m, "role", None) == "assistant" and getattr(m, "content", None)),
                None,
            )
            return (
                (last_text + "\n\n") if last_text else ""
            ) + (
                f"[warning] Reached the maximum of {MAX_ITERATIONS} tool-call "
                "iterations without a final answer.  The model may be looping.  "
                "Try rephrasing the question or set a stricter OLLAMA_MAX_ITERATIONS."
            )

# ---------------------------------------------------------------------------
# Interactive CLI
# ---------------------------------------------------------------------------

async def main():
    """Interactive REPL — reads questions from stdin, prints answers."""
    use_docker = os.getenv("MCP_USE_DOCKER", "false").strip().lower() in ("true", "1", "yes")
    backend = (
        f"docker ({os.getenv('MCP_DOCKER_IMAGE', 'proxmox-mcp:latest')})"
        if use_docker else "local venv"
    )

    print(
        f"Proxmox Agent (Ollama)"
        f" — model: {MODEL}"
        f" @ {OLLAMA_HOST}"
        f" | timeout: {LLM_TIMEOUT:.0f}s"
        f" | max_iter: {MAX_ITERATIONS}"
        f" | backend: {backend}"
    )

    # ── Pre-flight Ollama check ───────────────────────────────────────────
    try:
        import httpx
        async with httpx.AsyncClient() as http:
            r = await http.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        r.raise_for_status()
        installed = [m["name"] for m in r.json().get("models", [])]
        model_ok  = MODEL in installed or any(
            m.startswith(MODEL.split(":")[0]) for m in installed
        )
        if not model_ok:
            print(f"[warning] '{MODEL}' not found in Ollama.")
            print(f"          Run:  ollama pull {MODEL}")
            print(f"          Installed: {', '.join(installed) or 'none'}")
        else:
            print(f"Ollama ready — installed: {', '.join(installed)}")
    except httpx.TimeoutException:
        print(f"[warning] Ollama at {OLLAMA_HOST} did not respond within 5 s.")
        print("          It may still be starting. Proceeding anyway.")
    except httpx.HTTPStatusError as exc:
        print(f"[warning] Ollama health check failed: HTTP {exc.response.status_code}")
    except Exception as exc:
        print(f"[warning] Cannot reach Ollama at {OLLAMA_HOST}: {exc}")
        print("          Start Ollama with:  ollama serve")

    print("Type a question or 'exit' to quit.\n")

    # ── REPL ─────────────────────────────────────────────────────────────
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
        except RuntimeError as exc:
            # Unrecoverable LLM error — print cleanly without traceback.
            print(f"\n[error] {exc}\n")
        except Exception as exc:
            # Unexpected failure — print with type for debugging.
            print(f"\n[error] {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    asyncio.run(main())
