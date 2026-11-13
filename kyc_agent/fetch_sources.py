"""Télécharge les listes officielles utilisées par kyc_agent.screening dans <dest> (en général cases/<ID>/work/sources).

Usage : python -m kyc_agent.fetch_sources <dest>

  OFAC  SDN.CSV, ALT.CSV, CONS_PRIM.CSV, CONS_ALT.CSV  (sanctionslistservice.ofac.treas.gov)
  ONU   consolidated.xml                               (scsanctions.un.org)
  CGU   PEP (dernière compétence mensuelle publiée), CEIS et CNEP (dernier fichier quotidien) — Portal da Transparência

Seules des requêtes GET publiques sont faites. Un téléchargement impossible est signalé (code de sortie 2, fichier
manquant listé dans <dest>/fetch_log.json) : le contrôle correspondant devient « não consultado », jamais négatif.
"""
from __future__ import annotations

import json
import sys
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

UA = {"User-Agent": "Mozilla/5.0 (compliance screening; official public lists)"}
OFAC = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/"
UN = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"
CGU = "https://portaldatransparencia.gov.br/download-de-dados/"


def baixar(url: str, dest: Path, timeout=180) -> bool:
    try:
        r = requests.get(url, headers=UA, timeout=timeout, allow_redirects=True)
    except requests.RequestException:
        return False
    if r.status_code != 200 or len(r.content) < 200 or r.content[:15].lower().startswith((b"<!doctype html", b"<html")):
        return False
    dest.write_bytes(r.content)
    return True


def meses_recentes(n=6):
    d = date.today().replace(day=1)
    for _ in range(n):
        yield d.strftime("%Y%m")
        d = (d - timedelta(days=1)).replace(day=1)


def main(dest_dir: str) -> int:
    dest = Path(dest_dir)
    unz = dest / "unz"
    unz.mkdir(parents=True, exist_ok=True)
    log = {"executado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"), "ok": {}, "falhas": {}}

    for nome, alvo in (("SDN.CSV", "ofac_sdn.csv"), ("ALT.CSV", "ofac_alt.csv"), ("CONS_PRIM.CSV", "ofac_cons_prim.csv"), ("CONS_ALT.CSV", "ofac_cons_alt.csv")):
        (log["ok"] if baixar(OFAC + nome, dest / alvo) else log["falhas"])[alvo] = OFAC + nome
    (log["ok"] if baixar(UN, dest / "un_consolidated.xml") else log["falhas"])["un_consolidated.xml"] = UN

    def cgu(base, candidatos, sufixo):
        for c in candidatos:
            z = dest / f"{base}_{c}.zip"
            if baixar(f"{CGU}{base}/{c}", z):
                try:
                    with zipfile.ZipFile(z) as zf:
                        for m in zf.namelist():
                            if m.upper().endswith(f"_{sufixo}.CSV"):
                                (unz / Path(m).name).write_bytes(zf.read(m))
                    log["ok"][z.name] = f"{CGU}{base}/{c}"
                    return
                except zipfile.BadZipFile:
                    z.unlink(missing_ok=True)
        log["falhas"][base] = f"{CGU}{base}/ (tentado: {', '.join(candidatos)})"

    cgu("pep", list(meses_recentes()), "PEP")
    dias = [(date.today() - timedelta(days=i)).strftime("%Y%m%d") for i in range(10)]
    cgu("ceis", dias, "CEIS")
    cgu("cnep", dias, "CNEP")

    (dest / "fetch_log.json").write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(log, ensure_ascii=False, indent=2))
    return 2 if log["falhas"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
