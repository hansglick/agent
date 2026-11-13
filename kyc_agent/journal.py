"""Journal complet de l'agent (audit) : tout ce que l'agent a reçu, pensé, demandé, obtenu et ce qui lui a été refusé.

Fichier : cases/<ID>/runs/<run>/journal.jsonl — une ligne JSON par événement, écrite au fil de l'eau.
En cas de succès, une copie figée accompagne la version produite : versions/vN/journal_agente.jsonl.

Chaque ligne porte `h` = SHA-256(h de la ligne précédente + contenu de la ligne) : une ligne modifiée, supprimée ou
insérée après coup casse la chaîne, ce que `verificar()` détecte (l'application l'affiche).

Types d'événements :
  inicio            consignes données à l'agent (prompt système, demande, modèle, outils, limites)
  etapa             étape résumée (ce qu'affiche le cerveau du tableau de bord)
  texto             texte rédigé par l'agent
  raciocinio        raisonnement de l'agent (quand le modèle le fournit)
  ferramenta        outil demandé par l'agent (nom + paramètres complets)
  resultado_ferramenta  ce que l'outil a renvoyé à l'agent (texte, tronqué au-delà de LIMITE avec taille + empreinte)
  recusa            action refusée par les règles du dossier (outil, paramètres, motif)
  subagente_inicio / subagente_fim
  resultado         fin de l'agent (résumé, coût exact, tours, durée)
  fim               fin de l'exécution (état : concluido, erro, parado, simulado, revertido)
Le champ `agente` vaut « principal » ou la mission du sous-agent concerné.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

LIMITE = 20_000          # caractères conservés par texte (au-delà : début conservé + taille et empreinte du texte complet)
LIMITE_CONSIGNES = 400_000
NOME = "journal.jsonl"
NOME_VERSAO = "journal_agente.jsonl"


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _hash(prev: str, corpo: str) -> str:
    return hashlib.sha256((prev + corpo).encode("utf-8")).hexdigest()


def _corpo(linha: dict) -> str:
    return json.dumps({k: v for k, v in linha.items() if k != "h"}, ensure_ascii=False, sort_keys=True)


def reduzir(v, limite: int = LIMITE):
    """Tronque les longs textes (récursif) en gardant la trace du texte complet."""
    if isinstance(v, str):
        if len(v) <= limite:
            return v
        return {"inicio": v[:limite], "truncado": True, "tamanho": len(v),
                "sha256": hashlib.sha256(v.encode("utf-8")).hexdigest()}
    if isinstance(v, dict):
        return {str(k): reduzir(x, limite) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [reduzir(x, limite) for x in v]
    if v is None or isinstance(v, (bool, int, float)):
        return v
    return reduzir(str(v), limite)


def texto_de_resultado(content) -> str:
    """Contenu d'un résultat d'outil (texte ou liste de blocs) → texte lisible."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    partes = []
    for b in content if isinstance(content, list) else [content]:
        if isinstance(b, dict):
            if b.get("type") == "text":
                partes.append(b.get("text", ""))
            elif b.get("type") == "image":
                partes.append("[image]")
            else:
                partes.append(json.dumps(b, ensure_ascii=False)[:2000])
        else:
            partes.append(str(b))
    return "\n".join(partes)


class Journal:
    def __init__(self, run_dir: Path):
        self.p = Path(run_dir) / NOME
        self.n, self.h = 0, ""
        if self.p.exists():  # reprise (ne devrait pas arriver) : on continue la chaîne
            for linha in ler(self.p):
                self.n, self.h = linha.get("n", self.n) + 1, linha.get("h", self.h)

    def ev(self, tipo: str, agente: str | None = None, limite: int = LIMITE, **dados):
        try:
            linha = {"n": self.n, "ts": _agora(), "tipo": tipo, "agente": agente or "principal", **reduzir(dados, limite)}
            linha["h"] = _hash(self.h, _corpo(linha))
            with self.p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(linha, ensure_ascii=False) + "\n")
            self.n, self.h = self.n + 1, linha["h"]
        except OSError:
            pass  # le journal ne doit jamais faire échouer l'agent


def ler(p: Path) -> list[dict]:
    out = []
    try:
        with Path(p).open(encoding="utf-8") as f:
            for l in f:
                try:
                    out.append(json.loads(l))
                except ValueError:
                    out.append({"tipo": "linha_ilegivel", "bruto": l[:500]})
    except OSError:
        pass
    return out


def verificar(p: Path) -> dict:
    """Contrôle de la chaîne d'empreintes : {"ok", "n", "primeira_invalida"}."""
    prev, n = "", 0
    for i, linha in enumerate(ler(p)):
        if linha.get("n") != i or linha.get("h") != _hash(prev, _corpo(linha)):
            return {"ok": False, "n": i, "primeira_invalida": i}
        prev, n = linha["h"], i + 1
    return {"ok": True, "n": n, "primeira_invalida": None}
