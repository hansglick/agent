"""Sauvegarde automatique des dossiers.

Emplacement : KYC_BACKUP_DIR (à placer de préférence sur un AUTRE disque ou un partage sauvegardé),
par défaut <DATA_DIR>/sauvegardes. Pour chaque dossier :

    sauvegardes/<ID>/<ID>_<AAAAMMJJ-HHMMSS>.zip   une copie du dossier (état, versions, livrables, réponses, journaux…)
                                                  SANS les pièces, plus _pecas.json (chemin → empreinte SHA-256)
    sauvegardes/<ID>/pecas/<sha256>                chaque pièce stockée une seule fois, quel que soit le nombre de copies
    sauvegardes/<ID>/estado.json                   empreinte du dossier à la dernière copie (pas de copie si rien n'a changé)
    sauvegardes/estado.json                        dernier cycle (affiché sur la page d'accueil)

Règles : copie seulement si le dossier a changé ; jamais pendant une exécution de l'agent (état incohérent) ;
les KYC_BACKUP_GARDER (20) dernières copies sont gardées par dossier, les pièces qui ne sont plus utilisées par
aucune copie sont effacées. La suppression d'un dossier dans l'application efface aussi ses sauvegardes
(la fenêtre de suppression annonce une perte définitive).

Restauration : toujours vers un NOUVEAU dossier (rien n'est écrasé), avec contrôle de l'empreinte de chaque pièce.

    python -m kyc_agent.sauvegarde ciclo                         sauvegarde maintenant les dossiers modifiés
    python -m kyc_agent.sauvegarde listar [<ID>]                 copies disponibles
    python -m kyc_agent.sauvegarde restaurar <ID> <copie|ultima> [<NOUVEL_ID>]
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAFE = re.compile(r"^[A-Za-z0-9._-]+$")
COPIA = re.compile(r"^(?P<cid>.+)_(?P<ts>\d{8}-\d{6})\.zip$")


def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", ROOT)).resolve()


def backup_dir() -> Path:
    return Path(os.environ.get("KYC_BACKUP_DIR") or (data_dir() / "sauvegardes")).resolve()


def garder() -> int:
    try:
        return max(1, int(os.environ.get("KYC_BACKUP_GARDER", "20")))
    except ValueError:
        return 20


def agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _ler(p: Path, defaut):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defaut


def _gravar(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


# ------------------------------------------------------------------ ce qui est copié
def base_pecas(case: Path) -> Path:
    inv = _ler(case / "documentos.json", {})
    base = inv.get("base") or "documentos"
    return (case / "documentos") if base == "documentos" else (ROOT / base)


def _excluido(rel: Path) -> bool:
    partes = rel.parts
    return (len(partes) >= 3 and partes[0] == "runs" and partes[2] == "reversao") or rel.suffix in (".tmp", ".lock")


def arquivos_do_dossie(case: Path) -> tuple[list[Path], list[tuple[str, Path]]]:
    """(fichiers du dossier hors pièces, pièces [(chemin relatif dans la base des pièces, fichier)])."""
    pecas_dir = base_pecas(case).resolve()
    dentro = case.resolve() in pecas_dir.parents or pecas_dir == case.resolve()
    outros, pecas = [], []
    for p in sorted(case.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(case)
        if dentro and pecas_dir in p.resolve().parents:
            pecas.append((p.resolve().relative_to(pecas_dir).as_posix(), p))
        elif not _excluido(rel):
            outros.append(p)
    if not dentro and pecas_dir.is_dir():  # pièces rangées hors du dossier (base indiquée dans documentos.json)
        inv = _ler(case / "documentos.json", {})
        for a in inv.get("arquivos", []):
            p = pecas_dir / a["caminho"]
            if p.is_file():
                pecas.append((a["caminho"], p))
    return outros, pecas


def impressao(case: Path) -> str:
    """Empreinte rapide de l'état du dossier (chemins, tailles, dates de modification)."""
    outros, pecas = arquivos_do_dossie(case)
    h = hashlib.sha256()
    for p in outros + [p for _, p in pecas]:
        st = p.stat()
        h.update(f"{p.as_posix()}|{st.st_size}|{st.st_mtime_ns}\n".encode("utf-8"))
    return h.hexdigest()


def run_em_curso(case: Path) -> bool:
    d = case / "runs"
    for rd in d.iterdir() if d.is_dir() else []:
        st = _ler(rd / "status.json", {})
        if st.get("estado") == "em_curso":
            return True
    return False


# ------------------------------------------------------------------ sauvegarde
def copias(cid: str) -> list[dict]:
    d = backup_dir() / cid
    out = []
    for z in sorted(d.glob(f"{cid}_*.zip"), reverse=True) if d.is_dir() else []:
        m = COPIA.match(z.name)
        if m and m["cid"] == cid:
            ts = datetime.strptime(m["ts"], "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
            out.append({"nome": z.name, "em": ts.isoformat(timespec="seconds"), "tamanho": z.stat().st_size, "path": z})
    return out


def salvar(case: Path, motivo: str = "automática", forcar: bool = False) -> dict:
    """Copie du dossier si nécessaire. Retourne {"cid", "feita": bool, "motivo"/"copia"}."""
    cid = case.name
    dest = backup_dir() / cid
    est_p = dest / "estado.json"
    est = _ler(est_p, {})
    if run_em_curso(case):
        return {"cid": cid, "feita": False, "motivo": "execução do agente em curso"}
    imp = impressao(case)
    if not forcar and est.get("impressao") == imp:
        return {"cid": cid, "feita": False, "motivo": "sem alteração"}
    outros, pecas = arquivos_do_dossie(case)
    cache = est.get("cache_pecas", {})  # "chemin|taille|mtime" -> sha (évite de relire les grosses pièces)
    novo_cache, mapa = {}, []
    (dest / "pecas").mkdir(parents=True, exist_ok=True)
    for rel, p in pecas:
        st = p.stat()
        chave = f"{p.as_posix()}|{st.st_size}|{st.st_mtime_ns}"
        sha = cache.get(chave) or _sha(p)
        novo_cache[chave] = sha
        alvo = dest / "pecas" / sha
        if not alvo.exists():
            tmp = alvo.with_name(sha + ".tmp")
            shutil.copyfile(p, tmp)
            if _sha(tmp) != sha:  # pièce modifiée pendant la copie : on la reprendra au prochain cycle
                tmp.unlink()
                raise RuntimeError(f"pièce modifiée pendant la sauvegarde : {rel}")
            os.replace(tmp, alvo)
        mapa.append({"caminho": rel, "sha256": sha, "tamanho": st.st_size})
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    z_p = dest / f"{cid}_{ts}.zip"
    tmp = dest / f".{cid}_{ts}.zip.tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for p in outros:
            z.write(p, p.relative_to(case).as_posix())
        z.writestr("_pecas.json", json.dumps({"dossie": cid, "copia_em": agora(), "motivo": motivo,
                                              "base_pecas": _ler(case / "documentos.json", {}).get("base", "documentos"),
                                              "pecas": mapa}, ensure_ascii=False, indent=2))
    with zipfile.ZipFile(tmp) as z:
        if z.testzip() is not None:
            raise RuntimeError("copie illisible après écriture")
    os.replace(tmp, z_p)
    _gravar(est_p, {"impressao": imp, "ultima": z_p.name, "em": agora(), "cache_pecas": novo_cache})
    limpar(cid)
    return {"cid": cid, "feita": True, "copia": z_p.name}


def limpar(cid: str):
    """Garde les N dernières copies ; efface les pièces que plus aucune copie n'utilise."""
    cs = copias(cid)
    for c in cs[garder():]:
        c["path"].unlink(missing_ok=True)
    usados = set()
    for c in copias(cid):
        try:
            with zipfile.ZipFile(c["path"]) as z:
                usados |= {x["sha256"] for x in json.loads(z.read("_pecas.json"))["pecas"]}
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            return  # dans le doute on n'efface aucune pièce
    for p in (backup_dir() / cid / "pecas").glob("*"):
        if p.name not in usados:
            p.unlink(missing_ok=True)


def ciclo(cases: Path | None = None) -> dict:
    """Un passage sur tous les dossiers ; résumé écrit dans sauvegardes/estado.json."""
    cases = cases or (data_dir() / "cases")
    res = {"em": agora(), "destino": str(backup_dir()), "feitas": [], "sem_alteracao": [], "adiadas": [], "erros": {}}
    for case in sorted(cases.iterdir()) if cases.is_dir() else []:
        if not case.is_dir() or case.name.startswith(".") or not SAFE.match(case.name):
            continue
        if not ((case / "dossie.json").exists() or (case / "versions").is_dir()):
            continue
        try:
            r = salvar(case)
            (res["feitas"] if r["feita"] else res["sem_alteracao"] if r["motivo"] == "sem alteração" else res["adiadas"]).append(case.name)
        except Exception as e:  # une erreur sur un dossier n'empêche pas les autres
            res["erros"][case.name] = str(e)[:300]
    _gravar(backup_dir() / "estado.json", res)
    return res


def estado() -> dict:
    return _ler(backup_dir() / "estado.json", {})


def excluir(cid: str):
    if SAFE.match(cid):
        shutil.rmtree(backup_dir() / cid, ignore_errors=True)


# ------------------------------------------------------------------ restauration
def restaurar(cid: str, copia: str = "ultima", novo: str | None = None, cases: Path | None = None) -> Path:
    cases = cases or (data_dir() / "cases")
    cs = copias(cid)
    if not cs:
        raise ValueError(f"aucune sauvegarde pour {cid}")
    c = cs[0] if copia in ("ultima", "", None) else next((x for x in cs if x["nome"] == copia), None)
    if not c:
        raise ValueError(f"copie inconnue : {copia}")
    novo = novo or f"{cid}-restaurado-{COPIA.match(c['nome'])['ts']}"
    if not SAFE.match(novo) or len(novo) > 60:
        raise ValueError("identifiant invalide")
    dest = cases / novo
    if dest.exists():
        raise ValueError(f"le dossier {novo} existe déjà : rien n'est écrasé")
    tmp = cases / f".rest_{novo}"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        with zipfile.ZipFile(c["path"]) as z:
            meta = json.loads(z.read("_pecas.json"))
            for n in z.namelist():
                if n == "_pecas.json":
                    continue
                alvo = (tmp / n).resolve()
                if tmp.resolve() not in alvo.parents:
                    raise ValueError(f"chemin invalide dans la copie : {n}")
                alvo.parent.mkdir(parents=True, exist_ok=True)
                alvo.write_bytes(z.read(n))
        for x in meta["pecas"]:
            src = backup_dir() / cid / "pecas" / x["sha256"]
            alvo = (tmp / "documentos" / x["caminho"]).resolve()
            if (tmp / "documentos").resolve() not in alvo.parents:
                raise ValueError(f"chemin de pièce invalide : {x['caminho']}")
            alvo.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, alvo)
            if _sha(alvo) != x["sha256"]:
                raise ValueError(f"pièce altérée dans la sauvegarde : {x['caminho']}")
        inv_p = tmp / "documentos.json"
        if meta.get("base_pecas", "documentos") != "documentos" and inv_p.exists():  # pièces désormais dans le dossier
            inv = _ler(inv_p, {})
            inv["base"], inv["nota_restauracao"] = "documentos", f"peças copiadas de {meta['base_pecas']} na restauração"
            _gravar(inv_p, inv)
        dm_p = tmp / "dossie.json"
        dm = _ler(dm_p, {})
        dm.update(restaurado_de={"dossie": cid, "copia": c["nome"], "em": agora()},
                  nome=(dm.get("nome") or cid) + " (restaurado)")
        dm.pop("tratado_em", None)
        _gravar(dm_p, dm)
        tmp.rename(dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return dest


def main(argv):
    if not argv or argv[0] == "ciclo":
        print(json.dumps(ciclo(), ensure_ascii=False, indent=2))
    elif argv[0] == "listar":
        cids = argv[1:] or sorted(p.name for p in backup_dir().iterdir() if p.is_dir()) if backup_dir().is_dir() else []
        for cid in cids:
            for c in copias(cid):
                print(f"{cid}\t{c['nome']}\t{c['em']}\t{c['tamanho']:,} o")
    elif argv[0] == "restaurar" and len(argv) >= 2:
        print("Restauré dans", restaurar(argv[1], argv[2] if len(argv) > 2 else "ultima", argv[3] if len(argv) > 3 else None))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
