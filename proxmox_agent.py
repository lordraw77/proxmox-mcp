"""
proxmox_agent.py — Proxmox AI Agent (standalone, no external llm.py dependency)
=================================================================================
Single-file agent: all provider logic is embedded.
Requires only: mcp, openai, httpx, python-dotenv  (pip install mcp openai httpx python-dotenv)

Environment variables
---------------------
  PROXMOX_MCP_HOST             — Proxmox host
  PROXMOX_MCP_PORT             — Proxmox port (default: 8006)
  PROXMOX_MCP_USER             — Proxmox user (e.g. root@pam)
  PROXMOX_MCP_TOKEN_ID         — API token id   (token auth)
  PROXMOX_MCP_TOKEN_SECRET     — API token secret (token auth)
  PROXMOX_MCP_PASSWORD         — password (password auth)
  PROXMOX_MCP_VERIFY_SSL       — verify TLS cert (default: false)

  PROXMOX_MCP_PROVIDER         — provider: openrouter|groq|gemini|cloudflare|cerebras|mistral|ollama
  PROXMOX_MCP_OPENROUTER_API_KEY / _MODEL
  PROXMOX_MCP_GROQ_API_KEY / _MODEL
  PROXMOX_MCP_GEMINI_API_KEY / _MODEL
  PROXMOX_MCP_CLOUDFLARE_API_KEY / _ACCOUNT_ID / _MODEL
  PROXMOX_MCP_CEREBRAS_API_KEY / _MODEL
  PROXMOX_MCP_MISTRAL_API_KEY / _MODEL
  PROXMOX_MCP_OLLAMA_HOST              — Ollama base URL (default: http://localhost:11434)
  PROXMOX_MCP_OLLAMA_MODEL             — model (default: qwen2.5:7b-instruct)
  PROXMOX_MCP_OLLAMA_TIMEOUT           — LLM response timeout in seconds (default: 120)
  PROXMOX_MCP_OLLAMA_TOOL_TIMEOUT      — MCP tool call timeout in seconds (default: 30)
  PROXMOX_MCP_OLLAMA_MAX_ITERATIONS    — max tool-call rounds per question (default: 10)

  PROXMOX_MCP_USE_DOCKER       — "true" to spawn MCP server in Docker
  PROXMOX_MCP_DOCKER_IMAGE     — Docker image (default: lordraw/proxmox-mcp:latest)
  PROXMOX_MCP_ENV_FILE         — .env path forwarded to the container (default: .env)

Usage
-----
  python proxmox_agent.py
  PROXMOX_MCP_PROVIDER=groq python proxmox_agent.py
  PROXMOX_MCP_PROVIDER=ollama python proxmox_agent.py
"""

import asyncio
import json
import os
import sys
from collections.abc import Callable
from typing import Any

import dotenv
import httpx
import openai as _openai_module
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from openai import AsyncOpenAI

dotenv.load_dotenv()

# ── Provider registry ─────────────────────────────────────────────────────────

PROVIDERS: dict[str, tuple[str, str, str, str]] = {
    "openrouter": (
        "https://openrouter.ai/api/v1",
        "PROXMOX_MCP_OPENROUTER_API_KEY",
        "PROXMOX_MCP_OPENROUTER_MODEL",
        "openrouter/auto",
    ),
    "groq": (
        "https://api.groq.com/openai/v1",
        "PROXMOX_MCP_GROQ_API_KEY",
        "PROXMOX_MCP_GROQ_MODEL",
        "llama-3.3-70b-versatile",
    ),
    "gemini": (
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        "PROXMOX_MCP_GEMINI_API_KEY",
        "PROXMOX_MCP_GEMINI_MODEL",
        "gemini-2.0-flash",
    ),
    "cloudflare": (
        "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
        "PROXMOX_MCP_CLOUDFLARE_API_KEY",
        "PROXMOX_MCP_CLOUDFLARE_MODEL",
        "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
    ),
    "cerebras": (
        "https://api.cerebras.ai/v1",
        "PROXMOX_MCP_CEREBRAS_API_KEY",
        "PROXMOX_MCP_CEREBRAS_MODEL",
        "llama-3.3-70b",
    ),
    "mistral": (
        "https://api.mistral.ai/v1",
        "PROXMOX_MCP_MISTRAL_API_KEY",
        "PROXMOX_MCP_MISTRAL_MODEL",
        "mistral-large-latest",
    ),
    "ollama": (
        "{ollama_host}/v1",
        "",
        "PROXMOX_MCP_OLLAMA_MODEL",
        "qwen2.5:7b-instruct",
    ),
}


def _build_client(
    provider: str | None = None,
    *,
    timeout: float | None = None,
) -> tuple[AsyncOpenAI, str]:
    if provider is None:
        provider = os.getenv("PROXMOX_MCP_PROVIDER", "openrouter").strip().lower()

    if provider not in PROVIDERS:
        sys.exit(
            f"ERROR: unknown provider '{provider}'. "
            f"Valid options: {', '.join(PROVIDERS)}"
        )

    base_url_tpl, key_env, model_env, default_model = PROVIDERS[provider]

    if provider == "ollama":
        ollama_host = os.getenv("PROXMOX_MCP_OLLAMA_HOST", "http://localhost:11434").rstrip("/")
        base_url    = base_url_tpl.format(ollama_host=ollama_host)
        api_key     = "ollama"
    elif provider == "cloudflare":
        api_key    = os.getenv(key_env) or sys.exit(f"ERROR: {key_env} is not set.")
        account_id = os.getenv("PROXMOX_MCP_CLOUDFLARE_ACCOUNT_ID") or sys.exit(
            "ERROR: PROXMOX_MCP_CLOUDFLARE_ACCOUNT_ID is not set."
        )
        base_url = base_url_tpl.format(account_id=account_id)
    else:
        api_key  = os.getenv(key_env) or sys.exit(f"ERROR: {key_env} is not set.")
        base_url = base_url_tpl

    model = os.getenv(model_env, default_model)
    kwargs: dict[str, Any] = {"base_url": base_url, "api_key": api_key}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return AsyncOpenAI(**kwargs), model


def _build_server_params() -> StdioServerParameters:
    use_docker = os.getenv("PROXMOX_MCP_USE_DOCKER", "false").strip().lower() in ("true", "1", "yes")
    if use_docker:
        image    = os.getenv("PROXMOX_MCP_DOCKER_IMAGE", "lordraw/proxmox-mcp:latest")
        env_file = os.path.abspath(os.getenv("PROXMOX_MCP_ENV_FILE", ".env"))
        return StdioServerParameters(
            command="docker",
            args=["run", "--rm", "-i", "--env-file", env_file, image],
            env={**os.environ},
        )
    return StdioServerParameters(
        command=str(os.path.abspath(".venv/bin/python")),
        args=[str(os.path.abspath("server.py"))],
        env={**os.environ},
    )


def _mcp_tool_to_openai(tool) -> dict:
    return {
        "type": "function",
        "function": {
            "name":        tool.name,
            "description": tool.description or "",
            "parameters":  tool.inputSchema,
        },
    }


async def _agentic_loop(
    *,
    client: AsyncOpenAI,
    model: str,
    mcp: ClientSession,
    question: str,
    max_iterations: int = 20,
    tool_timeout: float | None = None,
    extra_body: dict | None = None,
    on_action: Callable[[str, dict], None] | None = None,
) -> str:
    tools_result = await mcp.list_tools()
    tools        = [_mcp_tool_to_openai(t) for t in tools_result.tools]
    valid_names  = {t["function"]["name"] for t in tools}

    messages: list[Any] = [{"role": "user", "content": question}]

    call_kwargs: dict[str, Any] = dict(
        model=model, messages=messages, tools=tools, tool_choice="auto",
    )
    if extra_body:
        call_kwargs["extra_body"] = extra_body

    for iteration in range(max_iterations):
        response = await client.chat.completions.create(**call_kwargs)

        if not response.choices:
            raise RuntimeError("LLM returned empty choices.")

        msg           = response.choices[0].message
        finish_reason = response.choices[0].finish_reason

        if msg is None:
            raise RuntimeError("LLM returned null message.")

        messages.append(msg)

        if finish_reason == "length":
            partial = (msg.content or "").strip()
            suffix  = "\n\n⚠️  Risposta troncata: finestra di contesto esaurita."
            return (partial + suffix) if partial else suffix

        if not msg.tool_calls:
            return msg.content or ""

        for call in msg.tool_calls:
            tool_name = call.function.name
            tool_id   = call.id or f"call_{iteration}_{tool_name}"

            if tool_name not in valid_names:
                result_text = (
                    f"Errore: il tool '{tool_name}' non esiste. "
                    f"Tool disponibili: {', '.join(sorted(valid_names))}"
                )
                messages.append({"role": "tool", "tool_call_id": tool_id, "content": result_text})
                continue

            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError as exc:
                result_text = f"Errore: argomenti non validi per '{tool_name}': {exc}."
                messages.append({"role": "tool", "tool_call_id": tool_id, "content": result_text})
                continue

            if on_action:
                on_action(tool_name, args)

            try:
                if tool_timeout is not None:
                    result = await asyncio.wait_for(
                        mcp.call_tool(tool_name, args), timeout=tool_timeout,
                    )
                else:
                    result = await mcp.call_tool(tool_name, args)
                result_text = "\n".join(
                    b.text for b in result.content if hasattr(b, "text")
                ) or "(il tool non ha restituito output)"
            except asyncio.TimeoutError:
                result_text = f"Errore: '{tool_name}' ha superato il timeout di {tool_timeout:.0f}s."
            except Exception as exc:
                result_text = f"Errore nell'esecuzione di '{tool_name}': {exc}"

            messages.append({"role": "tool", "tool_call_id": tool_id, "content": result_text})

    last_text = next(
        (
            getattr(m, "content", None) or (m.get("content") if isinstance(m, dict) else None)
            for m in reversed(messages)
            if (getattr(m, "role", None) or (m.get("role") if isinstance(m, dict) else None)) == "assistant"
        ),
        None,
    )
    suffix = f"\n\n⚠️  Fermato dopo {max_iterations} iterazioni (limite raggiunto)."
    return (last_text + suffix) if last_text else suffix


# ── Runtime ───────────────────────────────────────────────────────────────────

SERVER_PARAMS = _build_server_params()

_OLLAMA_HOST    = os.getenv("PROXMOX_MCP_OLLAMA_HOST", "http://localhost:11434").rstrip("/")
_OLLAMA_TIMEOUT = float(os.getenv("PROXMOX_MCP_OLLAMA_TIMEOUT",      "120"))
_OLLAMA_TOOL_TO = float(os.getenv("PROXMOX_MCP_OLLAMA_TOOL_TIMEOUT", "30"))
_OLLAMA_MAX_IT  = int(os.getenv("PROXMOX_MCP_OLLAMA_MAX_ITERATIONS",  "10"))


def _classify_ollama_error(exc: Exception, model: str) -> str:
    if isinstance(exc, _openai_module.APITimeoutError):
        return (
            f"Ollama non ha risposto entro {_OLLAMA_TIMEOUT:.0f}s. "
            f"Aumenta PROXMOX_MCP_OLLAMA_TIMEOUT o riprova."
        )
    if isinstance(exc, _openai_module.APIConnectionError):
        return (
            f"Impossibile connettersi a Ollama su {_OLLAMA_HOST}. "
            "Verifica che `ollama serve` sia in esecuzione."
        )
    if isinstance(exc, _openai_module.APIStatusError):
        code = exc.status_code
        body = exc.message or str(exc.body or "")
        if code == 404:
            return f"Modello '{model}' non trovato (HTTP 404). Esegui: ollama pull {model}"
        if code == 500:
            if "out of memory" in body.lower() or "oom" in body.lower():
                return f"Ollama ha esaurito la memoria con '{model}'. Usa una quantizzazione minore."
            return f"Errore interno Ollama (HTTP 500): {body}"
        if code == 503:
            return "Ollama non disponibile (HTTP 503) — potrebbe ancora essere in avvio."
        return f"Ollama ha restituito HTTP {code}: {body}"
    return f"Errore imprevisto con Ollama: {type(exc).__name__}: {exc}"


def _on_action(name: str, args: dict) -> None:
    print(f"  [tool] {name}({json.dumps(args, ensure_ascii=False)})")


async def ask(question: str) -> str:
    provider = os.getenv("PROXMOX_MCP_PROVIDER", "openrouter").strip().lower()
    is_ollama = provider == "ollama"

    if is_ollama:
        client, model = _build_client("ollama", timeout=_OLLAMA_TIMEOUT)
        loop_kwargs: dict[str, Any] = dict(
            max_iterations=_OLLAMA_MAX_IT,
            tool_timeout=_OLLAMA_TOOL_TO,
            extra_body={"options": {"num_ctx": 8192}},
        )
    else:
        client, model = _build_client(provider)
        loop_kwargs = {}

    result: str | None = None
    error:  Exception | None = None
    async with stdio_client(SERVER_PARAMS) as (read, write):
        async with ClientSession(read, write) as mcp:
            await mcp.initialize()
            try:
                result = await _agentic_loop(
                    client=client, model=model, mcp=mcp,
                    question=question, on_action=_on_action,
                    **loop_kwargs,
                )
            except (
                _openai_module.APITimeoutError,
                _openai_module.APIConnectionError,
                _openai_module.APIStatusError,
            ) as exc:
                error = RuntimeError(_classify_ollama_error(exc, model)) if is_ollama else exc
            except Exception as exc:
                error = exc
    if error is not None:
        raise error
    return result  # type: ignore[return-value]


async def main() -> None:
    provider = os.getenv("PROXMOX_MCP_PROVIDER", "openrouter").strip().lower()
    is_ollama = provider == "ollama"
    _, model = _build_client(provider)
    use_docker = os.getenv("PROXMOX_MCP_USE_DOCKER", "false").strip().lower() in ("true", "1", "yes")
    mcp_backend = f"docker({os.getenv('PROXMOX_MCP_DOCKER_IMAGE', 'lordraw/proxmox-mcp:latest')})" if use_docker else "local"
    print(f"Proxmox AI Agent  |  provider={provider}  model={model}  mcp={mcp_backend}")

    if is_ollama:
        print(f"host={_OLLAMA_HOST}  timeout={_OLLAMA_TIMEOUT:.0f}s  max_iter={_OLLAMA_MAX_IT}")
        try:
            async with httpx.AsyncClient() as http:
                r = await http.get(f"{_OLLAMA_HOST}/api/tags", timeout=5)
            r.raise_for_status()
            installed = [m["name"] for m in r.json().get("models", [])]
            model_ok  = model in installed or any(m.startswith(model.split(":")[0]) for m in installed)
            if not model_ok:
                print(f"[warning] '{model}' non trovato. Esegui: ollama pull {model}")
                print(f"          Installati: {', '.join(installed) or 'nessuno'}")
            else:
                print(f"Ollama pronto — installati: {', '.join(installed)}")
        except Exception as exc:
            print(f"[warning] Impossibile raggiungere Ollama su {_OLLAMA_HOST}: {exc}")

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

        try:
            answer = await ask(question)
            print(f"\n{answer}\n")
        except RuntimeError as exc:
            print(f"\n[errore] {exc}\n")
        except Exception as exc:
            print(f"\n[errore] {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    asyncio.run(main())
