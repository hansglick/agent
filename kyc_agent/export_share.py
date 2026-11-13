"""Export statique partageable d'une version de dossier (page HTML autonome, données pseudonymisées).

Usage : python -m kyc_agent.export_share <case_dir> <version> <out.html>
- Lit les livrables de versions/v<N>/ (state, XLSX, messages, index du ZIP, note FR).
- Masque CPF (format CGU ***.xxx.xxx-**), numéros de RG, compte bancaire et téléphone (guide v2 §10.4 :
  pas de lien partageable vers pièces d'identité ou informations bancaires). Aucun document client inclus.
"""
from __future__ import annotations

import csv
import io
import json
import re
import sys
import zipfile
from pathlib import Path

from openpyxl import load_workbook

from kyc_agent.tarefas import tarefas


def mask(text: str) -> str:
    text = re.sub(r"\b(\d{3})\.(\d{3})\.(\d{3})-(\d{2})\b", r"***.\2.\3-**", text)          # CPF formaté
    text = re.sub(r"(?<![\d.])(\d{3})(\d{6})(\d{2})(?![\d.])", lambda m: f"***{m.group(2)}**", text)  # CPF 11 chiffres
    text = re.sub(r"(RG\s*(?:n[ºo°.]*\s*)?)(\d{6,})", lambda m: m.group(1) + "*" * (len(m.group(2)) - 3) + m.group(2)[-3:], text)
    text = re.sub(r"(c/c\s*)(\d+)", lambda m: m.group(1) + "****" + m.group(2)[-3:], text)
    text = re.sub(r"\b51-9\d{4}-\d{4}\b", "51-9****-****", text)
    text = re.sub(r"(?<![\d.\-/])\(?(\d{2})\)?\s?9?\d{4}-?\d{4}(?![\d.\-/])",
                  lambda m: m.group(0) if len(re.sub(r"\D", "", m.group(0))) < 10 else f"({m.group(1)}) ****-****", text)  # téléphones 10-11 chiffres
    text = re.sub(r"(ag\.\s*)(\d{3,5})", lambda m: m.group(1) + "****", text)                # agence bancaire
    text = re.sub(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@(?!gfgroupus\.com)([A-Za-z0-9.-]+\.[a-z]{2,})\b",
                  r"\1***@\2", text)                                                          # e-mails externes
    return text


def main(case_dir, v, out):
    case = Path(case_dir)
    vd = case / "versions" / f"v{v}"
    cid = case.name
    state = json.loads((vd / f"state_v{v}.json").read_text(encoding="utf-8"))
    wb = load_workbook(vd / f"{cid}_Matriz_v{v}.xlsx", read_only=True, data_only=True)
    sheets = []
    for ws in wb.worksheets:
        rows = [["" if c is None else str(c) for c in r] for r in ws.iter_rows(values_only=True)]
        if rows:
            sheets.append({"nome": ws.title, "cab": rows[0], "linhas": rows[1:]})
    with zipfile.ZipFile(vd / f"{cid}_Evidencias_v{v}.zip") as z:
        idx_name = next(n for n in z.namelist() if n.endswith("/INDICE.csv"))
        idx = list(csv.DictReader(io.StringIO(z.read(idx_name).decode("utf-8-sig")), delimiter=";"))
        for r in idx:
            r["tamanho"] = z.getinfo(r["arquivo_no_zip"]).file_size
            r.pop("fonte_original", None)
    nota = (vd / f"Nota_FR_v{v}.md").read_text(encoding="utf-8") if (vd / f"Nota_FR_v{v}.md").exists() else ""
    data = {"state": state, "tarefas": tarefas(state), "sheets": sheets, "indice": idx, "nota": nota, "versao": v,
            "zip_mb": round((vd / f"{cid}_Evidencias_v{v}.zip").stat().st_size / 1048576, 1)}
    payload = mask(json.dumps(data, ensure_ascii=False))
    leaks = re.findall(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b", payload)
    assert not leaks, leaks
    tpl = (Path(__file__).parent / "share_template.html").read_text(encoding="utf-8")
    html = tpl.replace("__TITLE__", f"Dossiê {cid}").replace("/*__DATA__*/null", payload.replace("</", "<\\/"))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(html, encoding="utf-8")
    print(f"OK {out} ({len(html)//1024} KB)")


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), sys.argv[3])
