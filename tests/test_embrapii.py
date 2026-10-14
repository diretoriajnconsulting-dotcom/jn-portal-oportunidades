"""EMBRAPII: chamadas de credenciamento pelo RSS e as quatro portas contínuas da empresa.

As fixtures são trechos reduzidos das páginas públicas lidas em 14/10/2026 (RSS, sete
detalhes de chamada e as quatro páginas das portas), sem script, menu, rodapé nem dado
pessoal. Os prazos esperados são os do estudo (`radar-fontes-saude/estudo/embrapii.md`).
"""
import copy
import json
import shutil
import tempfile
import unittest
import urllib.error
from datetime import date
from pathlib import Path
from unittest import mock

from funding_intelligence.adapters.embrapii import NORTE, Bloqueio, EmbrapiiAdapter
from funding_intelligence.pipeline import aggregate, validate_catalog


ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "embrapii"
SCHEMA = str(ROOT / "schema/funding-opportunity-2.2.schema.json")
PORTFOLIOS = json.loads((ROOT / "config/portfolios.json").read_text(encoding="utf-8"))["portfolios"]
HOJE = date(2026, 10, 14)

CHAMADAS = {"embrapii-25591", "embrapii-24889", "embrapii-24658", "embrapii-22810", "embrapii-21977", "embrapii-21970"}
PORTAS = {"embrapii-modelo-continuo", "embrapii-sebrae", "embrapii-bndes-saude", "embrapii-alto-impacto-saude"}


def adaptador(today=HOJE, historical=False, fixture=FIXTURES, catalogo=None):
    adapter = EmbrapiiAdapter(fixture=str(fixture), today=today, catalogo=catalogo)
    adapter.historical = historical
    return adapter


def por_id(items):
    return {item["id"]: item for item in items}


def carga_historica(today=HOJE):
    return adaptador(today=today, historical=True).snapshot()


class FeedTests(unittest.TestCase):
    def test_rss_da_o_id_do_post_e_a_migracao_nao_conta_como_publicacao(self):
        itens = {item["post"]: item for item in
                 adaptador()._ler_feed((FIXTURES / "feed.xml").read_bytes())}
        self.assertEqual({"25591", "24889", "24658", "22810", "21977", "21970", "2352", "2489"}, set(itens))
        self.assertEqual(date(2026, 6, 8), itens["25591"]["pub"])
        self.assertFalse(itens["25591"]["migrada"])
        # 02 e 03/10/2024 é a data da migração do site, não a da chamada (CCTA é de 2022).
        self.assertTrue(itens["2352"]["migrada"])
        self.assertTrue(itens["2489"]["migrada"])

    def test_datas_da_celula_do_cronograma(self):
        self.assertEqual(["2026-06-08", "2026-06-26"], EmbrapiiAdapter._datas("08/06 a 26/06/2026"))
        self.assertEqual(["2026-06-03"], EmbrapiiAdapter._datas("03/06/2026, às 15h"))
        self.assertEqual(["2025-10-20", "2025-10-31"], EmbrapiiAdapter._datas("20/10/2025  a  31/10/2025"))
        self.assertEqual([], EmbrapiiAdapter._datas("A definir*"))
        self.assertEqual([], EmbrapiiAdapter._datas("12/2025"))


class ChamadasTests(unittest.TestCase):
    def test_carga_historica_le_as_chamadas_dos_24_meses_e_as_portas(self):
        snap = carga_historica()
        ops = por_id(snap["opportunities"])
        self.assertEqual(CHAMADAS | PORTAS, set(ops))
        # RSS + 6 detalhes + 4 portas; as duas da migração nem são pedidas.
        self.assertEqual(11, snap["requests"])
        self.assertEqual("healthy", snap["status"])
        self.assertIsNone(snap["error"])
        for item_id in CHAMADAS:
            self.assertEqual("closed", ops[item_id]["status"])
            self.assertEqual("chamada_publica", ops[item_id]["channel"])

    def test_prazo_e_a_primeira_entrega_publica(self):
        ops = por_id(carga_historica()["opportunities"])
        esperado = {
            # "Novos Prazos Limites": a Etapa 1 (30/01), não a Etapa 2 (17/04).
            "embrapii-24658": ("2025-12-18", "2026-01-30"),
            # "Período de inscrições 08/06 a 26/06/2026"; a live de 03/06 não é a publicação.
            "embrapii-25591": ("2026-06-08", "2026-06-26"),
            # "Envio de Carta de Manifestação ... - Etapa 1".
            "embrapii-21977": ("2025-07-25", "2025-08-26"),
            "embrapii-21970": ("2025-07-25", "2025-08-15"),
            # As linhas "A definir*" não viram prazo.
            "embrapii-24889": ("2026-01-15", "2026-02-20"),
            "embrapii-22810": ("2025-09-02", "2025-09-19"),
        }
        for item_id, (publicada, prazo) in esperado.items():
            self.assertEqual(publicada, ops[item_id]["dates"]["published"], item_id)
            self.assertEqual(prazo, ops[item_id]["dates"]["deadline"], item_id)

    def test_aberta_so_com_prazo_lido_e_futuro(self):
        ops = por_id(carga_historica(today=date(2026, 1, 20))["opportunities"])
        self.assertEqual("open", ops["embrapii-24658"]["status"])
        self.assertEqual("open", ops["embrapii-24889"]["status"])
        # Abertura em 08/06/2026, depois de "hoje": anunciada, ainda não aberta.
        self.assertEqual("scheduled", ops["embrapii-25591"]["status"])
        self.assertEqual("closed", ops["embrapii-21970"]["status"])
        no_prazo = por_id(carga_historica(today=date(2026, 1, 30))["opportunities"])
        self.assertEqual("open", no_prazo["embrapii-24658"]["status"])
        depois = por_id(carga_historica(today=date(2026, 1, 31))["opportunities"])
        self.assertEqual("closed", depois["embrapii-24658"]["status"])

    def test_id_e_o_post_e_nao_o_numero_da_chamada(self):
        ops = por_id(carga_historica()["opportunities"])
        # "Unidades 01/2025" e "CC 01/2025" têm o mesmo número e são chamadas diferentes.
        self.assertEqual("21970", ops["embrapii-21970"]["external_id"])
        self.assertEqual("21977", ops["embrapii-21977"]["external_id"])
        self.assertIn("01/2025", ops["embrapii-21970"]["title"])
        self.assertIn("01/2025", ops["embrapii-21977"]["title"])

    def test_credenciamento_e_de_ict_e_a_empresa_nao_propoe(self):
        ops = por_id(carga_historica()["opportunities"])
        for item_id in CHAMADAS:
            self.assertNotIn("empresa", ops[item_id]["eligibility"]["organization_types"], item_id)
            self.assertEqual("credenciamento", ops[item_id]["instrument"]["type"])
        self.assertEqual(["ict", "ict_privada", "universidade"],
                         ops["embrapii-24658"]["eligibility"]["organization_types"])
        # Unidades 01/2025 (MS) selecionou Unidades de base hospitalar: o edital aceitava hospital.
        self.assertIn("hospital", ops["embrapii-21970"]["eligibility"]["organization_types"])
        self.assertNotIn("hospital", ops["embrapii-21977"]["eligibility"]["organization_types"])
        # Unidades 02/2025 foi só para Institutos Federais (ICT pública).
        self.assertEqual(["ict"], ops["embrapii-22810"]["eligibility"]["organization_types"])
        self.assertEqual(NORTE, ops["embrapii-24889"]["eligibility"]["geography"])
        self.assertEqual(["BR"], ops["embrapii-21970"]["eligibility"]["geography"])

    def test_parceiros_e_temas_vem_da_apresentacao(self):
        ops = por_id(carga_historica()["opportunities"])
        self.assertEqual(["MS"], ops["embrapii-21970"]["partners"])
        self.assertEqual("EMBRAPII / MS", ops["embrapii-21970"]["funder"])
        self.assertEqual(["MCTI"], ops["embrapii-24658"]["partners"])
        self.assertEqual(["MEC"], ops["embrapii-22810"]["partners"])
        self.assertEqual(["CNPq"], ops["embrapii-25591"]["partners"])
        self.assertNotIn("partners", ops["embrapii-24889"])
        self.assertIn("saude", ops["embrapii-21970"]["themes"])
        # "RNA Mensageiro" só está no título do edital, na lista de arquivos.
        self.assertIn("biotecnologia", ops["embrapii-21977"]["themes"])
        self.assertIn("capacitacao", ops["embrapii-24889"]["themes"])
        # O tema da IFPE selecionada (descarbonização) não é o da chamada, que é de tema livre.
        self.assertNotIn("descarbonizacao", ops["embrapii-22810"]["themes"])
        edital = [doc for doc in ops["embrapii-21970"]["documents"] if "rev1" in doc["title"]]
        self.assertTrue(edital[0]["url"].startswith("https://storage.googleapis.com/uploads-site-embrapii/"))

    def test_chamada_encerrada_sem_prazo(self):
        # Basic Funding Alliance (2022): fora da janela, mas é o modelo antigo de página, com
        # "CHAMADA ENCERRADA" e uma linha "Início de submissão" que não é prazo.
        adapter = adaptador()
        html = (FIXTURES / "post-2489.html").read_text(encoding="utf-8")
        item = adapter._chamada({
            "post": "2489", "link": "https://embrapii.org.br/chamadas-publicas/basic-funding-alliance-em-saude/",
            "title": "BASIC FUNDING ALLIANCE em Saúde", "pub": date(2024, 10, 2), "migrada": True,
        }, html)
        self.assertEqual("closed", item["status"])
        self.assertIsNone(item["dates"]["deadline"])
        self.assertEqual("2022-12-27", item["dates"]["published"])


class PortasTests(unittest.TestCase):
    def test_quatro_portas_em_fluxo_continuo(self):
        ops = por_id(carga_historica()["opportunities"])
        for item_id in PORTAS:
            porta = ops[item_id]
            self.assertEqual("open", porta["status"])
            self.assertEqual("fluxo_continuo", porta["channel"])
            self.assertIsNone(porta["dates"]["deadline"])
            self.assertEqual(["empresa", "startup"], porta["eligibility"]["organization_types"])
            self.assertEqual("contratacao_pdi", porta["instrument"]["type"])
            self.assertFalse(porta["instrument"]["repayable"])
            self.assertIn("Fonte: EMBRAPII", porta["description"])
        self.assertEqual(["BNDES"], ops["embrapii-bndes-saude"]["partners"])
        self.assertIn("pesquisa_clinica", ops["embrapii-bndes-saude"]["themes"])
        self.assertEqual(["Sebrae"], ops["embrapii-sebrae"]["partners"])
        alto = ops["embrapii-alto-impacto-saude"]
        self.assertEqual(["MS"], alto["partners"])
        self.assertEqual("2025-07-25", alto["dates"]["published"])
        self.assertEqual(60_000_000, alto["funding"]["program_budget"])

    def _copia(self, destino, trocas):
        shutil.copytree(FIXTURES, destino, dirs_exist_ok=True)
        for nome, (antes, depois) in trocas.items():
            arquivo = Path(destino) / nome
            texto = arquivo.read_text(encoding="utf-8")
            self.assertIn(antes, texto)
            arquivo.write_text(texto.replace(antes, depois), encoding="utf-8")

    def test_porta_sem_frase_ancora_nao_sai_e_o_relatorio_registra(self):
        with tempfile.TemporaryDirectory() as pasta:
            self._copia(pasta, {"porta-bndes-saude.html": ("Funtec", "Fundo")})
            snap = adaptador(fixture=pasta, historical=True).snapshot()
        ids = {item["id"] for item in snap["opportunities"]}
        self.assertNotIn("embrapii-bndes-saude", ids)
        self.assertIn("embrapii-alto-impacto-saude", ids)
        self.assertEqual("healthy", snap["status"])
        self.assertIn("bndes-saude", snap["error"])
        self.assertIn("Funtec", snap["error"])

    def test_porta_que_fala_em_fim_nao_sai(self):
        # "até que se esgotem os recursos" é o texto normal; "foram esgotados" é o fim.
        with tempfile.TemporaryDirectory() as pasta:
            self._copia(pasta, {"porta-alto-impacto-saude.html": (
                "Recursos Financeiros", "Recursos Financeiros Os recursos do programa foram esgotados.",
            )})
            snap = adaptador(fixture=pasta, historical=True).snapshot()
        self.assertNotIn("embrapii-alto-impacto-saude", {item["id"] for item in snap["opportunities"]})
        self.assertIn("alto-impacto-saude", snap["error"])


class OrcamentoEMemoriaTests(unittest.TestCase):
    def test_dia_a_dia_sem_memoria_le_so_duas_chamadas_novas(self):
        snap = adaptador().snapshot()
        ids = {item["id"] for item in snap["opportunities"]}
        # As duas mais recentes; as outras quatro ficam para as próximas rodadas.
        self.assertEqual({"embrapii-25591", "embrapii-24889"} | PORTAS, ids)
        self.assertEqual(7, snap["requests"])
        self.assertIsNone(snap["error"])
        self.assertTrue(any(aviso.startswith("4 chamada(s) sem leitura") for aviso in snap["warnings"]))

    def test_com_memoria_nao_rele_encerrada_e_o_pipeline_a_mantem(self):
        historica = carga_historica()
        r1 = aggregate([historica], None, PORTFOLIOS, generated_at="2026-10-13T12:00:00+00:00")
        diaria = adaptador(catalogo=r1).snapshot()
        # Só RSS e portas: as seis encerradas já estão no catálogo.
        self.assertEqual(5, diaria["requests"])
        self.assertEqual(PORTAS, {item["id"] for item in diaria["opportunities"]})
        r2 = aggregate([diaria], r1, PORTFOLIOS, generated_at="2026-10-14T12:00:00+00:00")
        ops = por_id(r2["opportunities"])
        self.assertEqual(CHAMADAS | PORTAS, set(ops))
        for item_id in CHAMADAS:
            self.assertEqual("closed", ops[item_id]["status"])
            self.assertEqual("unchanged", ops[item_id]["change_status"])
        self.assertEqual(4, r2["summary"]["continuous"])
        self.assertEqual(0, r2["summary"]["open"])
        validate_catalog(r1, SCHEMA)
        validate_catalog(r2, SCHEMA)

    def test_rele_a_viva_e_a_recem_encerrada(self):
        memoria = {"opportunities": copy.deepcopy(carga_historica()["opportunities"])}
        ops = por_id(memoria["opportunities"])
        ops["embrapii-24658"]["status"] = "open"  # estava aberta na rodada anterior
        ops["embrapii-24889"]["dates"]["deadline"] = "2026-10-10"  # encerrou há 4 dias
        snap = adaptador(catalogo=memoria).snapshot()
        lidas = {item["id"] for item in snap["opportunities"]} - PORTAS
        self.assertEqual({"embrapii-24658", "embrapii-24889"}, lidas)
        self.assertEqual(7, snap["requests"])
        self.assertEqual("closed", por_id(snap["opportunities"])["embrapii-24658"]["status"])

    def test_detalhe_que_falha_repete_o_item_anterior_defasado(self):
        memoria = {"opportunities": copy.deepcopy(carga_historica(today=date(2026, 1, 20))["opportunities"])}
        with tempfile.TemporaryDirectory() as pasta:
            shutil.copytree(FIXTURES, pasta, dirs_exist_ok=True)
            (Path(pasta) / "post-24658.html").unlink()
            snap = adaptador(fixture=pasta, today=date(2026, 1, 21), catalogo=memoria).snapshot()
        repetida = por_id(snap["opportunities"])["embrapii-24658"]
        self.assertEqual("open", repetida["status"])
        self.assertTrue(repetida["source"]["stale"])
        self.assertIn("24658", snap["error"])


class EducacaoComOServidorTests(unittest.TestCase):
    @staticmethod
    def _sem_fixture(memoria=None):
        adapter = EmbrapiiAdapter(today=HOJE, catalogo=memoria)
        adapter.historical = True
        adapter.pausa = 0
        return adapter

    def test_403_para_a_coleta_no_host_e_guarda_o_anterior(self):
        memoria = {"opportunities": carga_historica()["opportunities"]}
        adapter = self._sem_fixture(memoria)
        respostas = [(FIXTURES / "feed.xml").read_bytes()]

        def fetch(url):
            if respostas:
                return respostas.pop()
            raise urllib.error.HTTPError(url, 403, "Forbidden", None, None)

        with mock.patch.object(adapter, "fetch_bytes", side_effect=fetch) as chamada:
            snap = adapter.snapshot()
        # O RSS e o primeiro detalhe (403); depois disso, nenhum pedido mais ao host.
        self.assertEqual(2, chamada.call_count)
        self.assertEqual(2, snap["requests"])
        self.assertIn("bloqueou", snap["error"])
        ops = por_id(snap["opportunities"])
        self.assertEqual(CHAMADAS | PORTAS, set(ops))
        self.assertTrue(all(item["source"]["stale"] for item in ops.values()))

    def test_rss_bloqueado_derruba_a_coleta(self):
        adapter = self._sem_fixture()
        with mock.patch.object(adapter, "fetch_bytes",
                               side_effect=urllib.error.HTTPError("u", 429, "Too Many", None, None)):
            with self.assertRaises(Bloqueio):
                adapter.snapshot()

    def test_pagina_de_desafio_conta_como_bloqueio(self):
        adapter = self._sem_fixture()
        with mock.patch.object(adapter, "fetch_bytes",
                               return_value=b"<html><head><title>Just a moment...</title></head></html>"):
            with self.assertRaises(Bloqueio):
                adapter.snapshot()

    def test_pausa_entre_pedidos_ao_mesmo_host(self):
        adapter = self._sem_fixture()
        adapter.pausa = 1.5
        with mock.patch.object(adapter, "fetch_bytes", return_value=(FIXTURES / "feed.xml").read_bytes()), \
                mock.patch("funding_intelligence.adapters.embrapii.time.sleep") as dorme:
            adapter._get("https://embrapii.org.br/a/", "x")
            adapter._get("https://embrapii.org.br/b/", "x")
        self.assertEqual(1, dorme.call_count)
        self.assertGreater(dorme.call_args[0][0], 1.0)


if __name__ == "__main__":
    unittest.main()
