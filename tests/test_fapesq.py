"""FAPESQ-PB corrigida: pastas do ano pela home, prazo da versão mais recente, PPSUS com parceiros.

As fixtures em `tests/fixtures/fapesq/` são trechos reduzidos das páginas públicas lidas em
14/10/2026 (home, página de prazos abertos, capa de 2026, busca do PPSUS, página do edital 70/2026)
e trechos de texto dos PDFs (PPSUS 02/2025 retificado e edital 70/2026 retificado). A página de
prazos abertos da fixture não traz o 70/2026, para o teste passar pelo caminho da capa: página do
edital e PDF da última retificação.
"""
import re
import unittest
import urllib.error
from datetime import date
from pathlib import Path
from unittest import mock

from funding_intelligence.adapters import fapesq
from funding_intelligence.adapters.fapesq import FapesqAdapter, prazo_de_submissao

FIXTURES = Path(__file__).parent / "fixtures" / "fapesq"
HOJE = date(2026, 10, 14)
# Os ids da fonte no catálogo publicado em 14/10/2026 (public/data/funding-opportunities-v2.json).
IDS_DO_CATALOGO = {"fapesq-66-2026", "fapesq-67-2026", "fapesq-68-2026", "fapesq-69-2026", "fapesq-70-2026"}
FORMATO_ID = re.compile(r"^fapesq-[1-9]\d{0,2}-20\d{2}$")


def coletar(historical=False):
    adapter = FapesqAdapter(fixture=str(FIXTURES), today=HOJE)
    adapter.historical = historical
    itens = {item["id"]: item for item in adapter.collect()}
    return adapter, itens


class IdentidadeTests(unittest.TestCase):
    def test_ids_do_catalogo_continuam_iguais(self):
        _, itens = coletar()
        self.assertLessEqual(IDS_DO_CATALOGO, set(itens))

    def test_formato_nn_aaaa_sem_zero_a_esquerda(self):
        _, itens = coletar()
        for item_id in itens:
            self.assertRegex(item_id, FORMATO_ID)
        self.assertIn("fapesq-2-2025", itens)  # "Edital Nº 02/2025 – CHAMADA PPSUS"

    def test_numero_do_edital_nunca_do_resto_do_titulo(self):
        casos = [
            ("EDITAL Nº 70/2026 – PROCESSO SELETIVO … VAGAS REMANESCENTES 2025/2028", "", "70-2026"),
            ("EDITAL Nº 44 /2026 - VAGAS REMANESCENTES", "", "44-2026"),
            ("Edital nº 67 de 2026", "", "67-2026"),
            ("N° 02/2025 - CHAMADA PPSUS - PROGRAMA PESQUISA PARA O SUS", "", "2-2025"),
            ("Documento", "https://fapesq.rpp.br/editais/editais-2025/x-edital-n0-02_2025-chamada-ppsus.pdf", "2-2025"),
            # A data de retificação não é número de edital.
            ("CHAMADA NEXBIO AMAZÔNIA - Critérios específicos (Retificado em 17.03.2025 - Item 3)", "", None),
            ("Chamada Conjunta RAMP 2026 - Critérios adotados pela FAPESQ", "", None),
        ]
        for rotulo, href, esperado in casos:
            with self.subTest(rotulo=rotulo):
                self.assertEqual(esperado, FapesqAdapter._external_id(rotulo, href))

    def test_titulo_inteiro_e_estavel_entre_as_paginas(self):
        _, itens = coletar()
        # Antes o título era cortado em "RESULTADO" ("… COMITÊ ESTADUAL DE GESTÃO DE").
        self.assertIn("GESTÃO DE RESULTADO DE APRENDIZAGEM", itens["fapesq-69-2026"]["title"])
        # A página de prazos escreve "SEE/FAPESQ/PB" depois do número; a sigla vai para parceiros.
        self.assertNotIn("SEE/FAPESQ/PB", itens["fapesq-69-2026"]["title"])
        self.assertEqual(["SEE"], itens["fapesq-69-2026"]["partners"])


class DescobertaTests(unittest.TestCase):
    def test_pastas_do_ano_pela_home(self):
        home = (
            '<a href="https://fapesq.rpp.br/editais/editais-2025">2025</a>'
            '<a href="https://fapesq.rpp.br/editais/2026/editais-2026">2026</a>'
            '<a href="/editais/2027/editais-2027">2027</a>'
            '<a href="https://fapesq.rpp.br/noticias/editais-com-prazo-de-submissao-de-propostas-aberto">prazos</a>'
            '<a href="https://outro.site/editais/2028/editais-2028">fora</a>'
        )
        anos, abertos = FapesqAdapter(today=HOJE)._descobrir(home)
        self.assertEqual({2025, 2026, 2027}, set(anos))
        self.assertEqual("https://fapesq.rpp.br/editais/2027/editais-2027", anos[2027])
        self.assertTrue(abertos.endswith("editais-com-prazo-de-submissao-de-propostas-aberto"))

    def test_le_a_capa_do_ano_corrente_achada_na_home(self):
        adapter, itens = coletar()
        self.assertIn("fapesq-70-2026", itens)  # só está na capa de 2026
        self.assertLessEqual(adapter.pedidos, fapesq.ORCAMENTO_DIA)


class PrazoTests(unittest.TestCase):
    def test_pagina_de_prazos_abertos(self):
        _, itens = coletar()
        self.assertEqual(("open", "2026-10-16"), (itens["fapesq-69-2026"]["status"], itens["fapesq-69-2026"]["dates"]["deadline"]))
        # Linha "Inscrições" no parágrafo seguinte ao link.
        self.assertEqual("2026-11-09", itens["fapesq-40-2026"]["dates"]["deadline"])
        # Data por extenso.
        self.assertEqual("2026-11-09", itens["fapesq-42-2026"]["dates"]["deadline"])
        # "Até 06/11 (17h)", sem ano: vale o ano do edital.
        self.assertEqual(("open", "2026-11-06"), (itens["fapesq-32-2026"]["status"], itens["fapesq-32-2026"]["dates"]["deadline"]))

    def test_data_sem_ano_que_ja_passou_fica_unknown(self):
        adapter, _ = coletar()
        self.assertEqual(("2026-01-06", True), adapter._prazo_da_linha("Inscrições: Até 06/01 (17h)", 2026))
        grupo = adapter._grupo({}, "5-2026")
        grupo["linha_inscricoes"] = "Inscrições: Até 06/01 (17h)"
        grupo["titulos"]["abertos"] = "EDITAL Nº 05/2026 - SELEÇÃO DE BOLSISTAS"
        item = adapter._montar(grupo)
        self.assertEqual(("unknown", None), (item["status"], item["dates"]["deadline"]))

    def test_versao_mais_recente_pela_pagina_do_edital(self):
        _, itens = coletar()
        item = itens["fapesq-70-2026"]
        # Edital de 07/10 e retificação de 13/10: vale o cronograma da retificação, na linha da
        # inscrição, e não o maior valor (contratação até 20/11, início em 01/12).
        self.assertEqual("2026-10-16", item["dates"]["deadline"])
        self.assertEqual("2026-10-07", item["dates"]["published"])
        self.assertEqual("open", item["status"])
        self.assertEqual(["CODATA"], item["partners"])
        self.assertEqual(3, len(item["documents"]))  # página, edital e retificação

    def test_cronograma_com_tabelas_antigas_e_recurso_depois(self):
        texto = (FIXTURES / "ppsus_02_2025.txt").read_text(encoding="utf-8")
        self.assertEqual("2025-04-10", prazo_de_submissao(texto))
        self.assertEqual("2025-02-24", fapesq.lancamento(texto))
        texto = (FIXTURES / "edital_70_2026_retificado.txt").read_text(encoding="utf-8")
        self.assertEqual("2026-10-16", prazo_de_submissao(texto))

    def test_nao_pega_o_maior_valor_das_datas(self):
        texto = (
            "Inscrições até 31/08/2026. Prazo para recurso até 30/09/2026. "
            "Divulgação do resultado das inscrições em 15/10/2026."
        )
        self.assertEqual("2026-08-31", prazo_de_submissao(texto))

    def test_todas_as_retificacoes_e_a_mais_recente(self):
        adapter = FapesqAdapter(today=HOJE)
        grupo = adapter._grupo({}, "2-2025")
        for rotulo, data_doc in (
            ("Edital 02/2025 - CHAMADA PPSUS", "2025-02-24"),
            ("Nº 02/2025 - CHAMADA PPSUS (Retificado em 26.03.2025)", "2025-03-26"),
            ("Nº 02/2025 - CHAMADA PPSUS (Retificado em 27/05/25)", "2025-05-27"),
            ("Nº 02/2025 - CHAMADA PPSUS (Retificado em 10/06/25)", "2025-06-10"),
            ("Nº 02/2025 - CHAMADA PPSUS (Retificado em 08/07/25, Item 4 - Cronograma)", "2025-07-08"),
            ("Edital 02/2025 - CHAMADA PPSUS (Resultado final em 22/07/25)", "2025-07-22"),
        ):
            adapter._documento(grupo, rotulo, f"https://fapesq.rpp.br/editais/editais-2025/{data_doc}.pdf", data_doc)
        self.assertEqual(6, len(grupo["documentos"]))
        self.assertEqual("2025-07-08", adapter._versao_mais_recente(grupo["documentos"])["published"])


class PpsusTests(unittest.TestCase):
    def test_ppsus_da_pb(self):
        _, itens = coletar()
        item = itens["fapesq-2-2025"]
        self.assertEqual("closed", item["status"])
        self.assertEqual("2025-04-10", item["dates"]["deadline"])
        self.assertEqual("FAPESQ-PB", item["funder"])
        self.assertEqual(["Decit/MS", "CNPq", "SES-PB"], item["partners"])
        self.assertIn("pesquisa_sus", item["themes"])
        self.assertIn("saude", item["themes"])
        self.assertEqual(3_000_000, item["funding"]["program_budget"])
        self.assertEqual("auxilio_pesquisa", item["instrument"]["type"])  # auxílio a projeto, não bolsa
        tipos = item["eligibility"]["organization_types"]
        for tipo in ("pesquisador", "universidade", "ict", "ict_privada"):
            self.assertIn(tipo, tipos)
        # "empresa pública" é executora e "participação de empresa" é parceira: empresa privada
        # não é proponente.
        self.assertNotIn("empresa", tipos)
        self.assertEqual(["empresa"], item["eligibility"]["partner_types"])
        self.assertTrue(item["title"].startswith("Edital Nº 02/2025 - CHAMADA PPSUS"))

    def test_carga_historica_segue_a_busca(self):
        adapter, itens = coletar(historical=True)
        self.assertIn("fapesq-2-2025", itens)
        self.assertLessEqual(adapter.pedidos, fapesq.ORCAMENTO_HISTORICO)


class PublicoEInstrumentoTests(unittest.TestCase):
    def test_palavra_empresa_solta_nao_faz_empresa_elegivel(self):
        tipos, parceiras = FapesqAdapter._audience(
            "EDITAL Nº 53/2026 - SELEÇÃO DE EQUIPES DE EMPRESAS JUNIORES DO ESTADO DA PARAÍBA",
            "… empresa … empresas parceiras …", False,
        )
        self.assertNotIn("empresa", tipos)
        self.assertEqual([], parceiras)
        tipos, _ = FapesqAdapter._audience("EDITAL Nº 58/2024 – PROGRAMA TECNOVA III - SUBVENÇÃO ECONÔMICA", "", False)
        self.assertIn("empresa", tipos)

    def test_instrumento(self):
        self.assertEqual("bolsa", FapesqAdapter._instrument("EDITAL Nº 42/2026 CONCESSÃO DE BOLSAS DE PÓS-DOUTORADO", "", False))
        self.assertEqual(
            "bolsa",
            FapesqAdapter._instrument("EDITAL Nº 65/2026 – SELEÇÃO DE ESTUDANTES PARA O PROJETO DE PESQUISA LABORATÓRIO", "", False),
        )
        self.assertEqual("auxilio_pesquisa", FapesqAdapter._instrument("Edital Nº 02/2025 – CHAMADA PPSUS", "bolsas de IC", True))
        self.assertEqual("subvencao_economica", FapesqAdapter._instrument("Edital Nº 58/2024 – TECNOVA III", "", False))

    def test_temas_do_titulo_e_nao_do_menu_do_site(self):
        _, itens = coletar()
        self.assertNotIn("saude", itens["fapesq-70-2026"]["themes"])
        self.assertNotIn("agroindustria", itens["fapesq-70-2026"]["themes"])


class EducacaoComOServidorTests(unittest.TestCase):
    def test_orcamento_curto_para_sem_derrubar_a_rodada(self):
        adapter = FapesqAdapter(fixture=str(FIXTURES), today=HOJE)
        with mock.patch.object(fapesq, "ORCAMENTO_DIA", 4):
            snap = adapter.snapshot()
        self.assertEqual(4, adapter.pedidos)
        self.assertEqual("healthy", snap["status"])
        self.assertIn("orçamento", snap["error"])
        self.assertIn("fapesq-69-2026", {item["id"] for item in snap["opportunities"]})

    def test_403_para_no_host(self):
        adapter = FapesqAdapter(today=HOJE)
        erro = urllib.error.HTTPError(fapesq.HOME, 429, "Too Many Requests", None, None)
        with mock.patch.object(adapter, "fetch_bytes", side_effect=erro) as pedir, \
             mock.patch.object(fapesq.time, "sleep"):
            with self.assertRaises(fapesq.Bloqueio):
                adapter.collect()
            with self.assertRaises(fapesq.Bloqueio):
                adapter._html(fapesq.BUSCA_PPSUS)
        self.assertEqual(1, pedir.call_count)

    def test_sem_listagem_nenhuma_a_rodada_falha(self):
        adapter = FapesqAdapter(today=HOJE)
        home = (FIXTURES / "home.html").read_bytes()

        def responder(url):
            if url == fapesq.HOME:
                return home
            raise urllib.error.URLError("fora do ar")

        with mock.patch.object(adapter, "fetch_bytes", side_effect=responder), \
             mock.patch.object(fapesq.time, "sleep"):
            with self.assertRaises(RuntimeError):
                adapter.collect()


if __name__ == "__main__":
    unittest.main()
