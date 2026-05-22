"""
agent_ollama.py — Proxmox AI Agent (Ollama, interactive CLI)
=============================================================
Agentic loop via a local/remote Ollama instance.  Provider logic lives in llm.py.
Ollama-specific concerns kept here: timeout config, error classification, health check.

Tool calling support
--------------------
  Verified working models (install with `ollama pull <name>`):

    Model                   Size    Tool use quality
    ──────────────────────  ──────  ────────────────────────────────────
    qwen2.5:7b-instruct     4.7 GB  ★★★★★  Excellent — recommended
    qwen2.5:14b             9.0 GB  ★★★★★  Best local quality
    llama3.1:8b             4.7 GB  ★★★★☆  Very good
    llama3.2:3b             2.0 GB  ★★★☆☆  Good, faster on low-end hw
    mistral:7b              4.1 GB  ★★★☆☆  Decent
    gemma3:4b               3.3 GB  ★★☆☆☆  Limited — may answer in text

Environment variables
---------------------
  OLLAMA_HOST            — Ollama base URL (default: http://localhost:11434)
  OLLAMA_MODEL           — model to use (default: qwen2.5:7b-instruct)
  OLLAMA_TIMEOUT         — seconds to wait for each LLM response (default: 120)
  OLLAMA_TOOL_TIMEOUT    — seconds to wait for each MCP tool call (default: 30)
  OLLAMA_MAX_ITERATIONS  — max tool-call rounds per question (default: 10)
  MCP_USE_DOCKER         — "true" to spawn server.py inside Docker
  MCP_DOCKER_IMAGE       — image name (default: proxmox-mcp:latest)
  MCP_ENV_FILE           — path to .env forwarded to the container

Usage
-----
  ollama pull qwen2.5:7b-instruct
  cd /opt/proxmox-mcp
  .venv/bin/python agent_ollama.py
"""

import asyncio
import os

import dotenv
import openai as _openai_module
from mcp import ClientSession
from mcp.client.stdio import stdio_client

import llm

dotenv.load_dotenv()

OLLAMA_HOST    = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
LLM_TIMEOUT    = float(os.getenv("OLLAMA_TIMEOUT",       "120"))
TOOL_TIMEOUT   = float(os.getenv("OLLAMA_TOOL_TIMEOUT",  "30"))
MAX_ITERATIONS = int(os.getenv("OLLAMA_MAX_ITERATIONS",   "10"))

SERVER_PARAMS = llm.build_mcp_server_params()


def _classify_error(exc: Exception, model: str) -> str:
    """Return a human-readable message for Ollama API errors."""
    if isinstance(exc, _openai_module.APITimeoutError):
        return (
            f"Ollama non ha risposto entro {LLM_TIMEOUT:.0f}s. "
            "Il modello potrebbe essere in caricamento. "
            f"Aumenta OLLAMA_TIMEOUT (attuale: {LLM_TIMEOUT:.0f}s) o riprova."
        )
    if isinstance(exc, _openai_module.APIConnectionError):
        return (
            f"Impossibile connettersi a Ollama su {OLLAMA_HOST}. "
            "Verifica che `ollama serve` sia in esecuzione e che OLLAMA_HOST sia corretto."
        )
    if isinstance(exc, _openai_module.APIStatusError):
        code = exc.status_code
        body = exc.message or str(exc.body or "")
        if code == 404:
            return f"Modello '{model}' non trovato in Ollama (HTTP 404). Esegui: ollama pull {model}"
        if code == 500:
            if "out of memory" in body.lower() or "oom" in body.lower():
                return f"Ollama ha esaurito la memoria con '{model}' (HTTP 500). Usa una quantizzazione minore."
            return f"Errore interno Ollama (HTTP 500): {body}"
        if code == 503:
            return "Ollama non disponibile (HTTP 503) — potrebbe ancora essere in avvio. Riprova tra qualche secondo."
        return f"Ollama ha restituito HTTP {code}: {body}"
    return f"Errore imprevisto con Ollama: {type(exc).__name__}: {exc}"


async def ask(question: str) -> str:
    client, model = llm.build_client("ollama", timeout=LLM_TIMEOUT)
    _result: str | None = None
    _error:  Exception | None = None
    async with stdio_client(SERVER_PARAMS) as (read, write):
        async with ClientSession(read, write) as mcp:
            await mcp.initialize()
            try:
                _result = await llm.agentic_loop(
                    client=client,
                    model=model,
                    mcp=mcp,
                    question=question,
                    max_iterations=MAX_ITERATIONS,
                    tool_timeout=TOOL_TIMEOUT,
                    extra_body={"options": {"num_ctx": 8192}},
                )
            except (
                _openai_module.APITimeoutError,
                _openai_module.APIConnectionError,
                _openai_module.APIStatusError,
            ) as exc:
                _error = RuntimeError(_classify_error(exc, model))
            except Exception as exc:
                _error = exc
    if _error is not None:
        raise _error
    return _result  # type: ignore[return-value]


async def main():
    _, model = llm.build_client("ollama")

    print(
        f"Proxmox Agent (Ollama)"
        f" — model: {model}"
        f" @ {OLLAMA_HOST}"
        f" | timeout: {LLM_TIMEOUT:.0f}s"
        f" | max_iter: {MAX_ITERATIONS}"
    )

    # Pre-flight Ollama connectivity check
    try:
        import httpx
        async with httpx.AsyncClient() as http:
            r = await http.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        r.raise_for_status()
        installed = [m["name"] for m in r.json().get("models", [])]
        model_ok  = model in installed or any(m.startswith(model.split(":")[0]) for m in installed)
        if not model_ok:
            print(f"[warning] '{model}' non trovato in Ollama. Esegui: ollama pull {model}")
            print(f"          Installati: {', '.join(installed) or 'nessuno'}")
        else:
            print(f"Ollama pronto — installati: {', '.join(installed)}")
    except Exception as exc:
        print(f"[warning] Impossibile raggiungere Ollama su {OLLAMA_HOST}: {exc}")

    print("Digita una domanda o 'exit' per uscire.\n")

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
            print(f"\n[errore] {exc}\n")
        except Exception as exc:
            print(f"\n[errore] {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    asyncio.run(main())
