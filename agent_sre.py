"""
agent_sre.py — Proxmox SRE AI Agent (ReAct pattern, multi-provider)
====================================================================
SRE agent con loop ReAct esplicito (Thought / Action / Observation).
Logica provider e loop agentico centralizzati in llm.py.

ReAct display
-------------
  THOUGHT ›   ragionamento del modello prima di ogni tool call
  ACTION  ›   tool invocato + argomenti
  OBSERVE ›   risultato del tool

Persona SRE
-----------
  • Solo lettura — non esegue operazioni di scrittura.
  • Raccoglie prima i fatti, poi ragiona sulla causa.
  • Descrive cosa farebbe (🔧 WOULD DO:) senza eseguirlo.
  • Chiude sempre con una VALUTAZIONE FINALE.

Environment variables
---------------------
  SRE_PROVIDER        — provider attivo (default: openrouter)
                        openrouter | groq | gemini | cloudflare | cerebras | mistral
  SRE_MAX_ITERATIONS  — max iterazioni ReAct (default: 20)
  + variabili del provider selezionato (vedi llm.py e .env.example)
  MCP_USE_DOCKER      — "true" per usare server.py in container
  MCP_DOCKER_IMAGE    — immagine Docker (default: proxmox-mcp:latest)
  MCP_ENV_FILE        — path .env forwarded al container

Usage
-----
  cd /opt/proxmox-mcp
  .venv/bin/python agent_sre.py

  sre >>> Il cluster è sano?
  sre >>> La VM 105 non risponde, indaga
  sre >>> exit
"""

import asyncio
import json
import os
import sys

import dotenv
from mcp import ClientSession
from mcp.client.stdio import stdio_client

import llm

dotenv.load_dotenv()

MAX_ITERATIONS = int(os.getenv("SRE_MAX_ITERATIONS", "20"))

# ── ANSI colours (disabled when not a tty) ───────────────────────────────────

_USE_COLOUR = sys.stdout.isatty()

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOUR else text

CYAN   = lambda t: _c("36", t)
YELLOW = lambda t: _c("33", t)
GREEN  = lambda t: _c("32", t)
DIM    = lambda t: _c("2",  t)
BOLD   = lambda t: _c("1",  t)

# ── SRE system prompt ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Sei un Site Reliability Engineer (SRE) esperto specializzato in \
infrastrutture Proxmox VE. Operi in modalità READ-ONLY consultiva.

## Lingua

Rispondi SEMPRE in italiano, inclusi ragionamenti, osservazioni e valutazioni finali.

## Vincolo assoluto — nessuna scrittura

PUOI chiamare qualsiasi tool di sola lettura (list_*, *_status, *_config, *_rrddata,
*_syslog, *_network, cluster_resources, cluster_tasks, ha_*, ceph_*, …).

NON DEVI chiamare tool che modificano lo stato: vm_start, vm_stop, vm_shutdown,
vm_reboot, vm_migrate, create_snapshot, delete_snapshot, rollback_snapshot,
create_backup, restore_backup, vm_clone, vm_create, vm_delete, vm_resize_disk,
vm_move_disk, vm_unlink_disk, vm_template, create_firewall_rule,
delete_firewall_rule, create_replication, delete_replication,
node_reboot, node_shutdown, node_apt_upgrade, vm_agent_exec, o qualsiasi altro
tool che cambia lo stato del cluster.

Se fosse necessaria un'azione di scrittura, descrivi esattamente cosa faresti e perché,
ma non chiamare il tool.

## Comportamento ReAct

Prima di ogni tool call DEVI emettere un breve Thought che spieghi:
  1. Cosa sai finora.
  2. Quale ipotesi stai verificando.
  3. Quale tool stai per chiamare e perché.

Dopo aver ricevuto un'Observation (risultato del tool), analizzala prima di procedere.

## Principi SRE

- Raccogli prima i fatti, poi ragiona sulla causa radice.
- Per ogni azione di scrittura raccomandata, indica: tool esatto + argomenti,
  risultato atteso, blast radius e piano di rollback.
- SCALA ESPLICITAMENTE se le condizioni superano l'envelope sicuro.
- CHIUDI SEMPRE CON UNA VALUTAZIONE FINALE: salute attuale, causa radice (se identificata),
  azioni raccomandate (non eseguite), follow-up suggerito.

## Stile di risposta

- Sii conciso. L'operatore è sotto pressione.
- Usa elenchi puntati per i risultati.
- Prefissa i problemi critici con ⚠️  e i componenti sani con ✅.
- Marca le azioni raccomandate ma non eseguite con 🔧 FAREI:.
- Non inventare output dei tool — afferma solo fatti dalle Observation.
"""

# ── ReAct callbacks ───────────────────────────────────────────────────────────

def _on_thought(text: str) -> None:
    print(CYAN("THOUGHT › ") + text.strip())
    print()

def _on_action(name: str, args: dict) -> None:
    print(YELLOW("ACTION  › ") + BOLD(name) + DIM(f"  {json.dumps(args, ensure_ascii=False)}"))

def _on_observe(name: str, result: str) -> None:
    truncated = len(result) > 600
    print(DIM("OBSERVE › ") + result[:600] + (DIM("  [troncato]") if truncated else ""))
    print()

# ── MCP server params ─────────────────────────────────────────────────────────

SERVER_PARAMS = llm.build_mcp_server_params()

# ── Agentic loop ──────────────────────────────────────────────────────────────

async def ask(question: str, client, model: str) -> str:
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
                    system_prompt=SYSTEM_PROMPT,
                    max_iterations=MAX_ITERATIONS,
                    on_thought=_on_thought,
                    on_action=_on_action,
                    on_observe=_on_observe,
                )
            except Exception as exc:
                _error = exc
    if _error is not None:
        raise _error
    return _result  # type: ignore[return-value]

# ── Interactive REPL ──────────────────────────────────────────────────────────

async def main() -> None:
    client, model = llm.build_client()
    provider = os.getenv("SRE_PROVIDER", "openrouter").strip().lower()

    print(BOLD("Proxmox SRE Agent") +
          f"  provider: {provider}  model: {model}  max-iter: {MAX_ITERATIONS}")
    print(DIM("ReAct pattern — Thought / Action / Observation loop"))
    print(DIM("Digita una domanda o 'exit' per uscire.\n"))

    while True:
        try:
            question = input(GREEN("sre >>> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not question:
            continue
        if question.lower() in ("exit", "quit", "q"):
            break

        print()
        try:
            answer = await ask(question, client, model)
        except Exception as exc:
            print(f"[errore] {exc}\n")
            continue
        print()
        print(BOLD("─" * 60))
        print(BOLD("VALUTAZIONE FINALE"))
        print(BOLD("─" * 60))
        print(answer)
        print()


if __name__ == "__main__":
    asyncio.run(main())
