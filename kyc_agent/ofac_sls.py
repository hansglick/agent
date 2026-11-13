"""Recherche dans l'outil officiel OFAC Sanctions List Search (S06) avec capture datée.

Usage : python -m kyc_agent.ofac_sls <subjects.json> <out_dir> [min_score]
Pour chaque sujet (nom principal + variantes) : saisie du nom, Minimum Name Score fixé AVANT exécution,
toutes listes / tous programmes, capture PNG + PDF + TXT + JSON. Aucune protection n'est contournée :
si un CAPTCHA ou une erreur apparaît, le résultat est « não consultado » avec l'erreur conservée.
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = "https://sanctionssearch.ofac.treas.gov/"


def slug(s):
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").upper()


def run(subjects_path, out_dir, min_score="85"):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    subjects = json.loads(Path(subjects_path).read_text(encoding="utf-8"))
    log = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1280, "height": 1400})
        for s in subjects:
            for nome in [s["nome"]] + s.get("variantes", []):
                ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
                rec = {"id": s["id"], "termo": nome, "min_name_score": int(min_score), "listas": "All", "programas": "All",
                       "executado_em_utc": ts, "url": URL}
                try:
                    pg.goto(URL, wait_until="domcontentloaded", timeout=60000)
                    pg.wait_for_timeout(1500)
                    if pg.locator("iframe[src*='captcha'], .g-recaptcha, .h-captcha").count():
                        raise RuntimeError("CAPTCHA presente — não contornado")
                    pg.fill("#ctl00_MainContent_txtLastName", nome)
                    # Minimum Name Score : champ texte associé au slider
                    pg.evaluate("v => {const e=document.getElementById('ctl00_MainContent_Slider1_Boundcontrol'); if(e){e.value=v;}"
                                " const h=document.getElementById('ctl00_MainContent_Slider1'); if(h){h.value=v;}}", str(min_score))
                    pg.click("#ctl00_MainContent_btnSearch")
                    pg.wait_for_load_state("domcontentloaded")
                    pg.wait_for_timeout(2500)
                    txt = pg.inner_text("body")
                    m = re.search(r"Lookup Results:\s*(\d+)\s*Found", txt)
                    rec["resultados"] = int(m.group(1)) if m else None
                    rows = pg.locator("#gvSearchResults tr").all_inner_texts() if pg.locator("#gvSearchResults").count() else []
                    rec["linhas"] = [r.strip() for r in rows][:30]
                    rec["score_usado_na_pagina"] = pg.evaluate(
                        "() => {const e=document.getElementById('ctl00_MainContent_Slider1_Boundcontrol'); return e? e.value : null}")
                    rec["status"] = "executado"
                except Exception as e:  # incident conservé
                    rec["status"] = "não consultado"
                    rec["erro"] = str(e)[:500]
                    txt = pg.inner_text("body") if pg else ""
                ts2 = datetime.now(timezone.utc).isoformat(timespec="seconds")
                pg.evaluate("""([u,t,q,sc]) => {const d=document.createElement('div');
                  d.style='font:12px monospace;background:#ffd;border:1px solid #990;padding:4px;margin:4px';
                  d.textContent='GF CAPITAL – captura de evidência | '+u+' | termo: '+q+' | Minimum Name Score: '+sc+' | UTC: '+t;
                  document.body.prepend(d);}""", [URL, ts2, nome, str(min_score)])
                pref = out / f"OFAC-SLS_{s['id']}_{slug(nome)}"
                pg.screenshot(path=str(pref) + ".png", full_page=True)
                try:
                    pg.pdf(path=str(pref) + ".pdf", format="A4", print_background=True)
                except Exception:
                    pass
                Path(str(pref) + ".txt").write_text(txt, encoding="utf-8")
                rec["arquivos"] = [pref.name + ext for ext in (".png", ".pdf", ".txt")]
                log.append(rec)
                print(rec["id"], nome, rec["status"], "resultados:", rec.get("resultados"), "score:", rec.get("score_usado_na_pagina"), rec.get("erro", ""))
        b.close()
    (out / "ofac_sls_log.json").write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    return log


if __name__ == "__main__":
    run(*sys.argv[1:4])
