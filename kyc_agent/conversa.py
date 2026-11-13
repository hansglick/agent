"""Conversation avec l'agent (bulle en bas à droite) : questions du superviseur sur le dossier, sans modification.

    python -m kyc_agent.conversa <case_dir>

Répond au dernier message du superviseur de cases/<ID>/conversa/conversa.json. L'agent est **en lecture seule**
(permissions) : il lit le dossier, ses pièces, ses versions et le guide, peut consulter le web, mais n'écrit rien,
ne lance aucune commande et ne produit aucune version. La conversation se poursuit d'un message à l'autre (session
du SDK reprise). Plafond de dépense par message : KYC_CHAT_MAX_USD (5 $ par défaut). Sans clé API : simulation.
"""
import asyncio
import json
import os
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from kyc_agent.worker import (CODIGO_DE_DOSSIE, LEITURA_LOCAL, MODEL, PROMPTS, ROOT, TAMPAO_MENSAGEM, _caminho, _dentro,
                              chave_api, modo_simulacao, sessao_disponivel, texto_guia, uso_de, versoes, zonas_leitura)

PASTA = "conversa"
LIVRES_CHAT = {"WebSearch", "WebFetch"}
MAX_USD = float(os.environ.get("KYC_CHAT_MAX_USD") or 5)


def agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def arquivo(case: Path) -> Path:
    return case / PASTA / "conversa.json"


def ler(case: Path) -> dict:
    try:
        return json.loads(arquivo(case).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"mensagens": [], "estado": "pronto", "sessao": None}


def gravar(case: Path, c: dict):
    p = arquivo(case)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def atividade(case: Path, texto: str):
    c = ler(case)
    c["atividade"] = texto[:160]
    gravar(case, c)


def criar_permissao(case: Path):
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    zonas = zonas_leitura(case.resolve())

    async def permissao(tool, inp, ctx):
        if tool in LIVRES_CHAT:
            return PermissionResultAllow()
        if tool in LEITURA_LOCAL:
            alvo = _caminho(inp, "file_path", "path", "notebook_path")
            if alvo is None:
                return PermissionResultDeny(message=f"Indique le chemin (paramètre path) dans {case.resolve()}.")
            if not _dentro(alvo, zonas) or CODIGO_DE_DOSSIE.search(alvo.name):
                return PermissionResultDeny(message=f"Lecture interdite hors du dossier {case.name}.")
            return PermissionResultAllow()
        return PermissionResultDeny(message="Conversation en lecture seule : aucune écriture, commande ni sous-agent. "
                                            "Pour modifier le dossier, propose au superviseur une requête de mise à jour.")
    return permissao


def prompt_sistema(case: Path) -> str:
    try:
        rel = case.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        rel = case.resolve().as_posix()
    vs = versoes(case)
    base = (PROMPTS / "conversa.md").read_text(encoding="utf-8").replace("<CASE>", rel).replace("<ID>", case.name)
    base = base.replace("<VN>", f"v{vs[-1]}" if vs else "aucune (première analyse pas encore faite)")
    return base + "\n\n" + texto_guia()


async def responder(case: Path, c: dict, pergunta: str) -> dict:
    from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, TextBlock,
                                  ToolUseBlock)

    anterior = c.get("sessao")
    retomar = bool(anterior and sessao_disponivel(anterior))
    arq_sistema = case / PASTA / "prompt_sistema.md"  # par fichier : trop long pour la ligne de commande Windows
    arq_sistema.write_text(prompt_sistema(case), encoding="utf-8")
    opts = ClaudeAgentOptions(
        system_prompt={"type": "file", "path": str(arq_sistema)}, model=MODEL, cwd=str(ROOT), setting_sources=[],
        can_use_tool=criar_permissao(case), allowed_tools=sorted(LIVRES_CHAT), max_budget_usd=MAX_USD,
        env={"PYTHONIOENCODING": "utf-8", "ANTHROPIC_API_KEY": chave_api()}, max_buffer_size=TAMPAO_MENSAGEM,
        **({"resume": anterior} if retomar else {"session_id": str(uuid.uuid4())}))
    resultado = None
    async with ClaudeSDKClient(options=opts) as client:
        await client.query(pergunta)
        async for m in client.receive_response():
            if isinstance(m, AssistantMessage):
                for b in m.content:
                    if isinstance(b, ToolUseBlock):
                        alvo = b.input.get("file_path") or b.input.get("path") or b.input.get("pattern") or b.input.get("url") or b.input.get("query") or ""
                        atividade(case, f"{b.name} · {Path(str(alvo)).name if b.name in LEITURA_LOCAL else alvo}")
                    elif isinstance(b, TextBlock) and b.text.strip():
                        atividade(case, b.text.strip().splitlines()[0])
            elif isinstance(m, ResultMessage):
                resultado = m
    if resultado is None:
        raise RuntimeError("L'agent s'est arrêté sans réponse.")
    if resultado.is_error and not (resultado.result or "").strip():
        raise RuntimeError(f"Réponse en erreur ({resultado.subtype}).")
    u = uso_de(resultado.usage) if resultado.usage else {}
    return {"texto": (resultado.result or "").strip() or "(réponse vide)", "custo_usd": resultado.total_cost_usd,
            "uso": u, "sessao": getattr(resultado, "session_id", None) or (anterior if retomar else opts.session_id),
            "limite": resultado.subtype == "error_max_budget_usd"}


def main(case_dir: str):
    case = Path(case_dir).resolve()
    c = ler(case)
    msgs = c.get("mensagens") or []
    if not msgs or msgs[-1]["papel"] != "usuario":
        c["estado"] = "pronto"
        gravar(case, c)
        return
    pergunta = msgs[-1]["texto"]
    t0 = time.time()
    try:
        if modo_simulacao():
            time.sleep(1)
            r = {"texto": "(simulation : aucune clé API) J'aurais répondu à : « " + pergunta[:200] + " »", "custo_usd": 0,
                 "uso": {}, "sessao": c.get("sessao")}
        else:
            r = asyncio.run(responder(case, c, pergunta))
        c = ler(case)
        c["sessao"] = r.get("sessao") or c.get("sessao")
        c["mensagens"].append({"papel": "agente", "texto": r["texto"], "em": agora(), "custo_usd": r.get("custo_usd"),
                               "uso": r.get("uso"), "duracao_s": round(time.time() - t0),
                               **({"aviso": "plafond de dépense atteint"} if r.get("limite") else {})})
    except BaseException as e:
        (case / PASTA / "erro.txt").write_text(traceback.format_exc(), encoding="utf-8")
        c = ler(case)
        c["mensagens"].append({"papel": "agente", "erro": True, "texto": f"Je n'ai pas pu répondre : {str(e)[:300]}",
                               "em": agora(), "duracao_s": round(time.time() - t0)})
        if not isinstance(e, Exception):
            c["estado"], c["atividade"] = "pronto", ""
            gravar(case, c)
            raise
    c["estado"], c["atividade"] = "pronto", ""
    gravar(case, c)


if __name__ == "__main__":
    main(sys.argv[1])
