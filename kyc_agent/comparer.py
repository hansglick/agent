"""Comparatif objectif de deux analyses d'un même dossier (ex. analyse de référence vs analyse de l'agent API).

    python -m kyc_agent.comparer <state_reference.json> <state_a_comparer.json>
    ex. : python -m kyc_agent.comparer cases/TC-187/versions/v2/state_v2.json cases/TC-187-TEST/versions/v1/state_v1.json

Compte et compare : recommandation, contrôles du catalogue C01–C42 couverts, parties, documents, preuves, risques,
pendances, destinataires des messages, tâches humaines. Ne juge pas le fond : il montre où regarder.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

CATALOGO = [f"C{i:02d}" for i in range(1, 43)]


def ler(p: str) -> dict:
    return json.loads(Path(p).read_text(encoding="utf-8"))


def codigos(st: dict) -> set[str]:
    out = set()
    for c in st.get("controles", []):
        for m in re.findall(r"\bC(\d{1,2})\b", f"{c.get('id', '')} {c.get('controle', '')}"):
            if 1 <= int(m) <= 42:
                out.add(f"C{int(m):02d}")
    return out


def humanas(st: dict) -> list[dict]:
    return [t for t in st.get("registro_tecnico", []) if t.get("acao_humana", bool(t.get("respostas")))]


def resumo(st: dict) -> dict:
    return {
        "recommandation": (st.get("recomendacao") or {}).get("status"),
        "contrôles": len(st.get("controles", [])),
        "contrôles du catalogue couverts": len(codigos(st)),
        "parties": len(st.get("partes", [])),
        "documents": len(st.get("documentos", [])),
        "preuves": len(st.get("evidencias", [])),
        "risques": len(st.get("riscos", [])),
        "risques bloquants": sum(1 for r in st.get("riscos", []) if r.get("bloqueia_kyc")),
        "pendances": len(st.get("pendencias", [])),
        "messages": len(st.get("mensagens", [])),
        "tâches humaines": len(humanas(st)),
        "documents attendus (tâches + messages)": len(humanas(st)) + len(st.get("mensagens", [])),
    }


def norm(s: str) -> str:
    return (s or "").strip().lower().replace("í", "i")


def main(argv) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    a, b = ler(argv[0]), ler(argv[1])
    ra, rb = resumo(a), resumo(b)
    print(f"{'':42} {'RÉFÉRENCE':>22}   {'À COMPARER':>22}")
    for k in ra:
        print(f"{k:42} {str(ra[k]):>22}   {str(rb[k]):>22}")
    da = {norm(m.get("destinatario")) for m in a.get("mensagens", [])}
    db = {norm(m.get("destinatario")) for m in b.get("mensagens", [])}
    print("\nDestinataires absents de l'analyse comparée :", sorted(da - db) or "aucun")
    print("Destinataires en plus                       :", sorted(db - da) or "aucun")
    ca, cb = codigos(a), codigos(b)
    print("Contrôles du catalogue présents dans la référence seulement :", sorted(ca - cb) or "aucun")
    print("Contrôles du catalogue absents des deux                     :", sorted(set(CATALOGO) - ca - cb) or "aucun")
    print("\nTâches humaines — référence :")
    for t in humanas(a):
        print("  ", t.get("id"), "|", str(t.get("controle"))[:100])
    print("Tâches humaines — à comparer :")
    for t in humanas(b):
        print("  ", t.get("id"), "|", str(t.get("controle"))[:100])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
