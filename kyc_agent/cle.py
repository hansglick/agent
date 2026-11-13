"""Clé API de l'application : lecture, vérification et enregistrement sur l'ordinateur de l'utilisateur.

La clé est enregistrée comme variable d'environnement UTILISATEUR de Windows (KYC_ANTHROPIC_API_KEY, registre
HKCU\\Environment) : elle reste sur cette machine, n'est jamais affichée, n'est jamais écrite dans un fichier de
l'application. L'application la relit dans le registre au démarrage, même si elle a été lancée depuis une fenêtre
ouverte avant l'enregistrement.

KYC_CLE_REGISTRE=0 désactive la lecture du registre (tests).
"""
from __future__ import annotations

import asyncio
import os
import sys

NOME = "KYC_ANTHROPIC_API_KEY"


def cle_registre() -> str:
    if sys.platform != "win32" or os.environ.get("KYC_CLE_REGISTRE") == "0":
        return ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            v, _ = winreg.QueryValueEx(k, NOME)
        return str(v or "").strip()
    except OSError:
        return ""


def masque(k: str) -> str:
    return f"{k[:7]}…{k[-4:]}" if len(k) > 12 else "(trop courte)"


def format_valido(k: str) -> bool:
    return k.startswith("sk-ant-") and 30 <= len(k) <= 300 and not any(c.isspace() for c in k)


async def _essai(k: str, modelo: str):
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query
    opts = ClaudeAgentOptions(model=modelo, setting_sources=[], allowed_tools=[], max_turns=1, env={"ANTHROPIC_API_KEY": k},
                              system_prompt="Tu réponds uniquement par le mot demandé.")
    texto, res = "", None
    async for m in query(prompt="Réponds exactement : OK", options=opts):
        if isinstance(m, AssistantMessage):
            texto += "".join(b.text for b in m.content if isinstance(b, TextBlock))
        elif isinstance(m, ResultMessage):
            res = m
    return texto.strip(), res


def tester(k: str, modelo: str) -> tuple[bool, str]:
    """Un échange très court avec le modèle de l'agent (fraction de centime, aucune donnée de dossier).
    Retourne (accepté, motif). Une erreur réseau n'est pas un refus de la clé : (True, avertissement)."""
    try:
        texto, res = asyncio.run(_essai(k, modelo))
    except Exception as e:  # noqa: BLE001 — on traduit l'erreur pour l'utilisateur
        msg, low = str(e), str(e).lower()
        if "401" in msg or "authentication" in low or "invalid x-api-key" in low or "invalid api key" in low:
            return False, "recusada"
        if "credit" in low or "billing" in low or "402" in msg:
            return False, "creditos"
        if "model" in low and ("not found" in low or "404" in msg):
            return False, "modelo"
        return True, "nao_verificada"
    if "Invalid API key" in texto or (res is not None and "Invalid API key" in (res.result or "")):
        return False, "recusada"
    if res is None or res.is_error:
        txt = ((res.result if res else "") or "").lower()
        if "credit" in txt or "billing" in txt:
            return False, "creditos"
        return True, "nao_verificada"
    return True, "ok"


def enregistrer(k: str):
    """Variable d'environnement utilisateur Windows + processus courant (le worker lancé ensuite en hérite)."""
    os.environ[NOME] = k
    if sys.platform != "win32":
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as h:
        winreg.SetValueEx(h, NOME, 0, winreg.REG_SZ, k)
    try:  # prévient Windows : les fenêtres ouvertes ensuite voient la variable
        import ctypes
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 2000, ctypes.byref(ctypes.c_ulong()))
    except Exception:  # noqa: BLE001
        pass
