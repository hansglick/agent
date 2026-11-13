"""Import d'un fichier ZIP (webapp/app.py) : analyse avant création, décompression sûre, empreinte seule conservée."""
import hashlib
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def cli(dados):
    from webapp import app as webapp
    c = TestClient(webapp.app)
    c.cookies.set("lang", "fr")
    return c


def fazer_zip(membros: dict, infos=()) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for nome, data in membros.items():
            z.writestr(nome, data)
        for info, data in infos:
            z.writestr(info, data)
    return buf.getvalue()


def enviar_zip(cli, data: bytes, nome="pecas_cliente.zip"):
    tok = cli.post("/envios", params={"tipo": "zip"}).json()["envio"]
    r = cli.post(f"/envios/{tok}/lote", data={"caminhos": json.dumps([nome])}, files=[("arquivos", (nome, data))])
    assert r.status_code == 200, r.text
    return tok


ZIP_OK = {
    "Pasta_Cliente/contrato.pdf": b"%PDF contrato",
    "Pasta_Cliente/identidade/rg.pdf": b"%PDF rg",
    "Pasta_Cliente/identidade/Thumbs.db": b"x",
    "__MACOSX/Pasta_Cliente/._contrato.pdf": b"meta",
    "Pasta_Cliente/anexos/outro.zip": b"PK zip interno",
}


def test_analyse_puis_creation(cli, dados):
    data = fazer_zip(ZIP_OK)
    tok = enviar_zip(cli, data)
    r = cli.get(f"/envios/{tok}/zip").json()
    assert (r["n"], r["subpastas"], r["ignorados"]) == (3, 2, 2) and r["zips_internos"] == ["anexos/outro.zip"]
    assert not (dados / "cases" / "EX-010").exists()  # rien n'est créé par l'analyse
    r = cli.post("/dossies", data={"id": "EX-010", "nome": "Cliente", "envio": tok})
    assert r.status_code == 200, r.text
    case = dados / "cases" / "EX-010"
    inv = json.loads((case / "documentos.json").read_text(encoding="utf-8"))
    # le répertoire racine unique du ZIP n'est pas un sous-dossier ; les sous-dossiers sont conservés
    assert [a["caminho"] for a in inv["arquivos"]] == ["anexos/outro.zip", "contrato.pdf", "identidade/rg.pdf"]
    assert (case / "documentos" / "identidade" / "rg.pdf").read_bytes() == b"%PDF rg"
    z = inv["importado_de_zip"]
    assert z["arquivo"] == "pecas_cliente.zip" and z["sha256"] == hashlib.sha256(data).hexdigest() and z["tamanho"] == len(data)
    assert z["zips_internos_nao_descompactados"] == ["anexos/outro.zip"]
    # seule l'empreinte du ZIP est conservée : le ZIP lui-même n'est nulle part dans le dossier
    assert not list(case.rglob("pecas_cliente.zip"))
    assert not (dados / ".envios" / tok).exists()
    meta = json.loads((case / "dossie.json").read_text(encoding="utf-8"))
    assert "ZIP" in meta["fonte"]


def test_zip_sans_racine_unique(cli, dados):
    tok = enviar_zip(cli, fazer_zip({"a.pdf": b"A", "sub/b.pdf": b"B"}))
    assert cli.post("/dossies", data={"id": "EX-011", "envio": tok}).status_code == 200
    inv = json.loads((dados / "cases" / "EX-011" / "documentos.json").read_text(encoding="utf-8"))
    assert [a["caminho"] for a in inv["arquivos"]] == ["a.pdf", "sub/b.pdf"]


def _recusado(cli, dados, data, trecho, cid="EX-012"):
    tok = enviar_zip(cli, data)
    r = cli.get(f"/envios/{tok}/zip")
    assert r.status_code in (400, 413) and trecho in r.json()["detail"], r.text
    assert cli.post("/dossies", data={"id": cid, "envio": tok}).status_code in (400, 413)
    assert not (dados / "cases" / cid).exists()


def test_zip_avec_chemin_dangereux(cli, dados):
    _recusado(cli, dados, fazer_zip({"ok.pdf": b"1", "../../evil.txt": b"x"}), "caminho perigoso")


def test_zip_avec_chemin_absolu(cli, dados):
    _recusado(cli, dados, fazer_zip({"/etc/evil.txt": b"x"}), "caminho perigoso")


def test_zip_avec_lien_symbolique(cli, dados):
    info = zipfile.ZipInfo("lien")
    info.external_attr = (0o120777 << 16)
    _recusado(cli, dados, fazer_zip({"ok.pdf": b"1"}, [(info, "/etc/passwd")]), "link simbólico")


def test_zip_protege_par_mot_de_passe(cli, dados):
    data = bytearray(fazer_zip({"secreto.pdf": b"123"}))
    # marque le membre comme chiffré (bit 0 des drapeaux) dans l'en-tête local et le répertoire central
    for sig, off in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        i = data.find(sig)
        data[i + off] |= 1
    _recusado(cli, dados, bytes(data), "mot de passe")


def test_bombe_de_decompression(cli, dados):
    _recusado(cli, dados, fazer_zip({"zeros.bin": b"\0" * (11 * 1024 * 1024)}), "compressão anormal")


def test_fichier_qui_n_est_pas_un_zip(cli, dados):
    _recusado(cli, dados, b"ceci n'est pas un zip", "invalide")


def test_envoi_zip_avec_deux_fichiers_refuse(cli):
    tok = cli.post("/envios", params={"tipo": "zip"}).json()["envio"]
    cli.post(f"/envios/{tok}/lote", data={"caminhos": json.dumps(["a.zip", "b.zip"])},
             files=[("arquivos", ("a.zip", fazer_zip({"a": b"1"}))), ("arquivos", ("b.zip", fazer_zip({"b": b"2"})))])
    assert cli.get(f"/envios/{tok}/zip").status_code == 400


def test_ajout_d_un_zip_a_un_dossier(cli, caso):
    tok = enviar_zip(cli, fazer_zip({"Lote2/contrato_social.pdf": b"nova versao", "Lote2/procuracao.pdf": b"P"}), "lote2.zip")
    r = cli.post("/c/EX-001/documentos", data={"envio": tok})
    assert r.status_code == 200 and r.json()["n"] == 2
    inv = json.loads((caso / "documentos.json").read_text(encoding="utf-8"))
    novos = sorted(a["caminho"] for a in inv["arquivos"] if a.get("zip_origem") == "lote2.zip")
    assert novos == ["contrato_social (2).pdf", "procuracao.pdf"]  # homonyme d'une pièce existante : renommé, rien n'est écrasé
    assert inv["zips_importados"][-1]["arquivo"] == "lote2.zip"
    assert (caso / "documentos" / "contrato_social.pdf").read_bytes() == b"%PDF-1.4 ficticio"


def test_gros_zip_par_morceaux_et_nouvel_essai_de_creation(cli, dados, monkeypatch):
    from webapp import app as webapp
    monkeypatch.setattr(webapp, "PEDACO_BYTES", 64)
    data = fazer_zip({"R/a.pdf": b"A" * 300, "R/b.pdf": b"B" * 300})
    tok = cli.post("/envios", params={"tipo": "zip"}).json()["envio"]
    for i in range(0, len(data), 64):
        r = cli.post(f"/envios/{tok}/pedaco", params={"caminho": "gros.zip", "inicio": i, "total": len(data)}, content=data[i:i + 64])
        assert r.status_code == 200, r.text
    assert cli.get(f"/envios/{tok}/zip").json()["n"] == 2
    # identifiant déjà pris : refus, puis nouvel essai avec un autre identifiant sans renvoyer le ZIP
    assert cli.post("/dossies", data={"id": "EX-001", "envio": tok}).status_code == 409
    assert cli.post("/dossies", data={"id": "EX-013", "envio": tok}).status_code == 200
    assert (dados / "cases" / "EX-013" / "documentos" / "b.pdf").read_bytes() == b"B" * 300
