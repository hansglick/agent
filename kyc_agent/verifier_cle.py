"""Vérifie que la clé API est installée et fonctionne, par le même chemin que l'agent (Claude Agent SDK).

    .venv\\Scripts\\python -m kyc_agent.verifier_cle

Un seul échange très court avec le modèle de l'agent (coût : une fraction de centime), sans outil, sans aucune donnée de
dossier. La clé n'est jamais affichée : seuls son début (« sk-ant-… ») et ses 4 derniers caractères le sont.
"""
from __future__ import annotations

import asyncio
import os
import sys

from kyc_agent.worker import MODEL, chave_api


def masque(k: str) -> str:
    return f"{k[:7]}…{k[-4:]}" if len(k) > 12 else "(trop courte)"


async def essai():
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query
    opts = ClaudeAgentOptions(model=MODEL, setting_sources=[], allowed_tools=[], max_turns=1, env={"ANTHROPIC_API_KEY": chave_api()},
                              system_prompt="Tu réponds uniquement par le mot demandé.")
    texto, res = "", None
    async for m in query(prompt="Réponds exactement : OK", options=opts):
        if isinstance(m, AssistantMessage):
            texto += "".join(b.text for b in m.content if isinstance(b, TextBlock))
        elif isinstance(m, ResultMessage):
            res = m
    return texto.strip(), res


def main() -> int:
    k = chave_api()
    if not k:
        print("✗ Aucune clé : la variable KYC_ANTHROPIC_API_KEY n'est pas visible dans cette fenêtre.\n"
              "  → Créez-la (Variables d'environnement Windows), puis ouvrez une NOUVELLE fenêtre PowerShell.")
        return 1
    source = "KYC_ANTHROPIC_API_KEY" if os.environ.get("KYC_ANTHROPIC_API_KEY") else "ANTHROPIC_API_KEY"
    print(f"Clé trouvée ({source}) : {masque(k)}")
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("⚠ ANTHROPIC_API_KEY est définie dans Windows : Claude Code l'utilisera aussi à la place de votre connexion "
              "claude.ai.\n  → Supprimez-la et gardez uniquement KYC_ANTHROPIC_API_KEY.")
    if not k.startswith("sk-ant-"):
        print("⚠ La clé ne commence pas par « sk-ant- » : vérifiez le copier-coller (espace ou guillemet en trop ?).")
    if os.environ.get("KYC_AGENT_SIMULATE") == "1":
        print("⚠ KYC_AGENT_SIMULATE=1 est défini : l'application resterait en simulation malgré la clé.")
    print(f"Essai d'un échange très court avec {MODEL}…")
    try:
        texto, res = asyncio.run(essai())
    except Exception as e:
        msg = str(e)
        print(f"✗ Échec : {msg[:400]}")
        low = msg.lower()
        if "401" in msg or "authentication" in low or "invalid x-api-key" in low or "invalid api key" in low:
            print("  → Clé refusée : clé mal copiée, supprimée ou expirée. Une clé API commence par « sk-ant- » ;"
                  " si elle n'a pas été copiée à sa création, recréez-en une dans la console.")
        elif "credit" in low or "billing" in low or "402" in msg:
            print("  → Crédits insuffisants : ajoutez des crédits (Billing) dans la console.")
        elif "429" in msg or "rate" in low:
            print("  → Limite de débit atteinte : réessayez dans une minute.")
        elif "model" in low and ("not found" in low or "404" in msg):
            print(f"  → Le modèle {MODEL} n'est pas accessible avec cette clé (workspace / organisation).")
        return 2
    if "Invalid API key" in texto or (res is not None and "Invalid API key" in (res.result or "")):
        print("✗ Clé refusée par Anthropic (Invalid API key). Une clé API commence par « sk-ant- » ; recréez-en une si besoin.")
        return 2
    if res is None or res.is_error:
        print(f"✗ Réponse en erreur : {(res.result if res else '')[:400]}")
        return 2
    print(f"✓ La clé fonctionne. Réponse du modèle : « {texto[:40]} » · coût de l'essai : {res.total_cost_usd or 0:.4f} $")
    print("  L'application passera en mode RÉEL si le serveur est lancé depuis une fenêtre qui voit cette clé.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
