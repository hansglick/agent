"""Consultation d'un processus judiciaire par la API pública oficial du CNJ (DataJud) — alternative officielle quand le
portail du tribunal est inaccessible ou protégé par captcha.

    python -m kyc_agent.datajud <tribunal> <numero_processo> <saida.json>
    ex. : python -m kyc_agent.datajud tjrs 0000000-00.0000.8.21.0000 cases/<ID>/work/web/datajud_<numero>.json

<tribunal> : sigle du tribunal (tjrs, tjsp, trf4, stj…) → index api_publica_<tribunal>.
La réponse JSON brute est enregistrée telle quelle (preuve ; lue par le build via state["consultas_processuais"]),
avec à côté <saida>.meta.json (URL, requête, date de consultation). Limite : DataJud ne donne pas les parties ni le
contenu des pièces, seulement les métadonnées et les mouvements ; la recherche se fait par numéro de processus.
Clé : clé publique publiée par le CNJ (datajud-wiki.cnj.jus.br), remplaçable par la variable DATAJUD_APIKEY.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

CHAVE_PUBLICA = "cDZHYzlZa0JadVREZDJCendQbXY6SkJlTzNjLV9TRENyQk1RdnFKZGRQdw=="  # publiée par le CNJ (wiki DataJud)
URL = "https://api-publica.datajud.cnj.jus.br/api_publica_{trib}/_search"


def consultar(tribunal: str, numero: str, saida: Path, timeout: int = 60) -> dict:
    trib = re.sub(r"[^a-z0-9]", "", tribunal.lower())
    num = re.sub(r"\D", "", numero)
    if not trib or len(num) != 20:
        raise SystemExit("Tribunal ou numéro invalide (le numéro CNJ compte 20 chiffres).")
    url = URL.format(trib=trib)
    corpo = {"query": {"match": {"numeroProcesso": num}}}
    cab = {"Authorization": "APIKey " + (os.environ.get("DATAJUD_APIKEY") or CHAVE_PUBLICA), "Content-Type": "application/json"}
    for tentativa in range(4):  # limite de débit (429) ou indisponibilité passagère : nouvelles tentatives espacées
        try:
            r = requests.post(url, json=corpo, timeout=timeout, headers=cab)
        except requests.RequestException as e:
            if tentativa == 3:
                raise SystemExit(f"DataJud inaccessible : {e} — contrôle « não consultado », jamais négatif.")
            time.sleep(15 * (tentativa + 1))
            continue
        if r.status_code in (429, 500, 502, 503, 504) and tentativa < 3:
            time.sleep(int(r.headers.get("Retry-After") or 0) or 20 * (tentativa + 1))
            continue
        break
    if r.status_code != 200:
        raise SystemExit(f"DataJud a répondu HTTP {r.status_code} — contrôle « não consultado », jamais négatif.")
    d = r.json()
    saida.parent.mkdir(parents=True, exist_ok=True)
    saida.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    meta = {"fonte": "CNJ — DataJud, API pública oficial", "url": url, "consulta": corpo, "numero_formatado": numero,
            "consultado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "resultados": (d.get("hits") or {}).get("total", {}).get("value")}
    saida.with_suffix(".meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def main(argv):
    if len(argv) != 3:
        print(__doc__)
        return 2
    meta = consultar(argv[0], argv[1], Path(argv[2]))
    print(json.dumps(meta, ensure_ascii=False))
    if not meta["resultados"]:
        print("Aucun processus trouvé avec ce numéro dans ce tribunal (vérifier le tribunal ou le numéro) : "
              "à consigner tel quel, ce n'est pas un résultat négatif sur la partie.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
