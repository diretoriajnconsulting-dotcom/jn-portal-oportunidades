"""PDIL (Ministério da Saúde): RSS do Plone, datas por extenso e prorrogação.

As fixtures em `tests/fixtures/pdil/` são trechos reduzidos das páginas lidas em
14/10/2026 (RSS das pastas, busca do site e o texto dos dois PDFs da janela de 2024,
sem nomes de pessoas). Os cenários futuros (anúncio, nova janela, regime da Lei
15.471/2026, bloqueio) trocam respostas por cima delas, sem rede.
"""
import json
from datetime import date
from pathlib import Path
import unittest
from unittest import mock

from funding_intelligence.adapters import pdil
from funding_intelligence.adapters.pdil import (
    BASE, FEEDS, ORCAMENTO_PEDIDOS, RAIZ_RSS, Candidato, Entrada, PdilAdapter, Resposta,
    ato_do_endereco, atos_do_texto, data_do_ato, data_por_extenso, periodo_por_extenso,
)
from funding_intelligence.pipeline import aggregate, validate_catalog


ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "pdil"
SCHEMA = str(ROOT / "schema/funding-opportunity-2.2.schema.json")
HOJE = date(2026, 10, 14)
INFORMES, NOTAS, LEGISLACAO, DECEIIS, BUSCA, BUSCA_PRORROGA = FEEDS[1:7]
INFORME_2024 = BASE + "/informes-tecnicos/informe-no-02-2024-sectics-ms"
NOTA_2024_PDF = (
    "https://www.gov.br/saude/pt-br/centrais-de-conteudo/publicacoes/estudos-e-notas-informativas/2024/"
    "nota-informativa-no-5-2024-deceiis-sectics-ms.pdf"
)
NOTA_2024_LINK = (
    "https://www.gov.br/saude/pt-br/composicao/sectics/deceiis/publicacoes/"
    "nota-informativa-no-5-2024-deceiis-sectics-ms"
)
PORTARIA_10154_LINK = BASE + "/legislacao/portaria-gm-ms-no-10-154-de-14-de-janeiro-de-2026"


def rss(*itens):
    """RSS 1.0 do Plone com os itens (título, link, descrição, tipo, data)."""
    corpo = "".join(
        f'<item rdf:about="{link}"><title>{titulo}</title><link>{link}</link>'
        f"<description>{descricao}</description><dc:date>{data}</dc:date><dc:type>{tipo}</dc:type></item>"
        for titulo, link, descricao, tipo, data in itens
    )
    return Resposta(200, (
        '<?xml version="1.0" encoding="utf-8" ?>'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns="http://purl.org/rss/1.0/">'
        f'<channel rdf:about="x"><title>teste</title></channel>{corpo}</rdf:RDF>'
    ).encode("utf-8"), None, "application/atom+xml")


def texto(conteudo):
    return Resposta(200, conteudo.encode("utf-8"), None, "text/plain; charset=utf-8")


def fixture_rss(nome):
    return Resposta(200, (FIXTURES / nome).read_bytes(), None, "application/atom+xml")


class Adaptador(PdilAdapter):
    """As fixtures do estudo, com respostas trocadas pelo teste por cima."""

    def __init__(self, respostas=None, today=HOJE):
        super().__init__(fixture=str(FIXTURES), today=today)
        self.respostas = respostas or {}

    def _transporte(self, url):
        if url in self.respostas:
            return self.respostas[url]
        return super()._transporte(url)


def por_id(itens):
    return {item["id"]: item for item in itens}


class LeitorDeDatas(unittest.TestCase):
    def test_intervalo_por_extenso_herda_o_ano_da_segunda_data(self):
        frase = "o período de submissão de propostas de projeto de PDIL é de 90 (noventa) dias, " \
                "entre 24 de junho e 23 de setembro de 2024."
        self.assertEqual(("2024-06-24", "2024-09-23"), periodo_por_extenso(frase))

    def test_quebras_de_linha_do_pdf(self):
        trecho = "período de submissão de propostas\n... é de \n90 (noventa) dias\n, entre \n" \
                 "24 de junho e 23 de setembro\nde 2024\n."
        self.assertEqual(("2024-06-24", "2024-09-23"), periodo_por_extenso(trecho))

    def test_prorrogacao_da_nota_informativa_5_2024(self):
        nota = (FIXTURES / "nota-informativa-5-2024-deceiis-sectics-ms.txt").read_text(encoding="utf-8")
        self.assertEqual((None, "2024-09-30"), periodo_por_extenso(nota))
        self.assertEqual("2024-09-20", data_do_ato(nota))

    def test_informe_02_2024_inteiro(self):
        informe = (FIXTURES / "informe-02-2024-sectics-ms.txt").read_text(encoding="utf-8")
        # O texto também cita o Decreto 10.543 (13 de novembro de 2020) e a Portaria 900
        # (31 de março de 2017): só vale o período de submissão.
        self.assertEqual(("2024-06-24", "2024-09-23"), periodo_por_extenso(informe))
        self.assertEqual("2024-06-25", data_do_ato(informe))

    def test_variantes(self):
        casos = {
            "Propostas serão recebidas de 1º de março a 30 de abril de 2027.": ("2027-03-01", "2027-04-30"),
            "Período de submissão: entre os dias 15 de novembro e 15 de janeiro de 2027.":
                ("2026-11-15", "2027-01-15"),
            "Prazo de submissão no período de 24/06/2024 a 23/09/2024.": ("2024-06-24", "2024-09-23"),
            "Período de submissão de 2 de Março de 2026 a 1º de Junho de 2026.": ("2026-03-02", "2026-06-01"),
            "As propostas poderão ser enviadas até o dia 30 de abril de 2027.": (None, "2027-04-30"),
            "Propostas até 31 de fevereiro de 2027.": (None, None),
            "Vigência de 1º de janeiro a 31 de dezembro de 2026.": (None, None),
        }
        for frase, esperado in casos.items():
            with self.subTest(frase=frase):
                self.assertEqual(esperado, periodo_por_extenso(frase))

    def test_prorrogacao_no_mesmo_texto_estende_o_prazo(self):
        frase = "Período de submissão entre 1º de março e 30 de abril de 2027. " \
                "O prazo foi prorrogado até 15 de maio de 2027."
        self.assertEqual(("2027-03-01", "2027-05-15"), periodo_por_extenso(frase))

    def test_data_solta(self):
        self.assertEqual("2027-03-01", data_por_extenso("1º de Março de 2027"))
        self.assertEqual("2024-09-30", data_por_extenso("30/09/2024"))
        self.assertIsNone(data_por_extenso("30 de setembro"))


class IdentidadeDoAto(unittest.TestCase):
    def test_numero_do_ato_pelo_endereco(self):
        casos = {
            INFORME_2024 + "/view": "informe-02-2024",
            BASE + "/publicacoes/informe-tecnico-no-02-2024": "informe-02-2024",
            NOTA_2024_PDF + "/view": "nota-informativa-05-2024",
            "https://www.gov.br/saude/x/nota-informativa-n-5-2025-deceiis-sectics-ms.pdf": "nota-informativa-05-2025",
            BASE + "/notas-tecnicas-e-informativas/nota-informativa-no-5-2026-decis-sctie-ms":
                "nota-informativa-05-2026",
            PORTARIA_10154_LINK: "portaria-gm-ms-10154-2026",
            "https://www.gov.br/saude/x/resolucao-sectics-ms-no-1-de-9-de-dezembro-de-2024":
                "resolucao-sectics-ms-1-2024",
            "https://www.gov.br/saude/x/edital-sctie-ms-no-2-2026": "edital-02-2026",
        }
        for url, esperado in casos.items():
            with self.subTest(url=url):
                self.assertEqual(esperado, ato_do_endereco(url).id)
                self.assertFalse(ato_do_endereco(url).id.startswith("pdil"))
        self.assertIsNone(ato_do_endereco(
            "https://www.gov.br/saude/pt-br/assuntos/noticias-ms/2026/outubro/brasil-avanca-no-desenvolvimento"))

    def test_numero_do_ato_pelo_texto(self):
        ids = [a.id for a in atos_do_texto(
            "O Informe Técnico nº 03/2027 abre a rodada, nos termos da Portaria GM/MS nº 4.473, de 20 de junho "
            "de 2024, e da Nota Informativa nº 5/2024-DECEIIS/SECTICS/MS.")]
        self.assertEqual(["informe-03-2027", "nota-informativa-05-2024", "portaria-gm-ms-4473-2024"], sorted(ids))


class ColetaComAsFixtures(unittest.TestCase):
    def test_janela_de_2024_encerrada_com_a_prorrogacao(self):
        adaptador = Adaptador()
        snapshot = adaptador.snapshot()
        self.assertEqual("healthy", snapshot["status"])
        self.assertNotIn("failed_scopes", snapshot)
        self.assertEqual(["pdil-informe-02-2024"], [i["id"] for i in snapshot["opportunities"]])
        item = snapshot["opportunities"][0]
        self.assertEqual("informe-02-2024", item["external_id"])
        self.assertEqual("closed", item["status"])
        self.assertEqual(
            {"published": "2024-06-25", "deadline": "2024-09-30", "closed": "2024-09-30"}, item["dates"])
        self.assertEqual(
            ["ict", "ict_privada", "universidade", "osc", "hospital", "fundacao_apoio", "estado", "municipio"],
            item["eligibility"]["organization_types"])
        self.assertEqual(["empresa", "startup"], item["eligibility"]["partner_types"])
        self.assertEqual(["SECTICS/MS"], item["partners"])
        self.assertEqual({"type": "outros", "repayable": False}, item["instrument"])
        self.assertEqual("chamada_publica", item["channel"])
        for tema in ("saude", "complexo_economico_industrial_saude", "terapias_avancadas"):
            self.assertIn(tema, item["themes"])
        self.assertEqual([INFORME_2024, NOTA_2024_PDF], [d["url"] for d in item["documents"]])
        self.assertIn("24/06/2024 a 23/09/2024", item["description"])
        self.assertIn("Prorrogado até 30/09/2024", item["description"])
        # 7 RSS e os 2 PDFs; nada no DOU.
        self.assertLessEqual(adaptador.pedidos, ORCAMENTO_PEDIDOS)
        self.assertEqual(9, adaptador.pedidos)
        self.assertFalse(any("in.gov.br" in url for url in adaptador.urls_pedidas))

    def test_resultado_e_ranking_nao_viram_janela(self):
        adaptador = Adaptador()
        adaptador.collect()
        lidos = set(adaptador.urls_pedidas[len(FEEDS):])
        # Informe 07/2025 (com o "Inelegível" da Ltda.), Portaria 10.154/2026 (recursos)
        # e Nota 5/2026 (ranking) estão nos RSS, mas não são abertura de janela.
        for url in (BASE + "/informes-tecnicos/informe-no-07-2025-sectics-ms", PORTARIA_10154_LINK,
                    BASE + "/notas-tecnicas-e-informativas/nota-informativa-no-5-2026-decis-sctie-ms"):
            self.assertNotIn(url, lidos)

    def test_aberta_antes_do_prazo_prorrogado(self):
        item = Adaptador(today=date(2024, 9, 25)).collect()[0]
        self.assertEqual("open", item["status"])
        self.assertEqual("2024-09-30", item["dates"]["deadline"])
        self.assertNotIn("closed", item["dates"])

    def test_sem_a_nota_vale_o_prazo_original(self):
        adaptador = Adaptador({BUSCA_PRORROGA: rss()}, today=date(2024, 9, 25))
        item = adaptador.collect()[0]
        self.assertEqual("closed", item["status"])
        self.assertEqual("2024-09-23", item["dates"]["deadline"])

    def test_link_da_prorrogacao_segue_o_302_dentro_do_gov_br(self):
        # Sem o item "Arquivo" da busca, a Nota vem pelo Link do DECEIIS: 302 para o PDF.
        so_link = rss(("Nota Informativa nº 5/2024-DECEIIS/SECTICS/MS", NOTA_2024_LINK,
                       "Prorrogação de prazo para submissão de proposta de projetos no âmbito do PDIL e PDP",
                       "Link", ""))
        adaptador = Adaptador({BUSCA_PRORROGA: so_link})
        item = adaptador.collect()[0]
        self.assertEqual("2024-09-30", item["dates"]["deadline"])
        self.assertIn(NOTA_2024_LINK, adaptador.urls_pedidas)
        self.assertIn(NOTA_2024_PDF, adaptador.urls_pedidas)
        self.assertEqual(10, adaptador.pedidos)

    def test_contrato_22_e_corte_dos_24_meses_no_pipeline(self):
        portfolios = json.loads((ROOT / "config/portfolios.json").read_text(encoding="utf-8"))["portfolios"]
        snapshot = Adaptador(today=date(2024, 10, 1)).snapshot()
        catalogo = aggregate([snapshot], None, portfolios, generated_at="2024-10-01T12:00:00+00:00")
        validate_catalog(catalogo, SCHEMA)
        self.assertEqual(["pdil-informe-02-2024"], [i["id"] for i in catalogo["opportunities"]])
        # Hoje (14/10/2026) o prazo de 30/09/2024 já passou dos 24 meses: o pipeline corta.
        hoje = aggregate([Adaptador().snapshot()], None, portfolios, generated_at="2026-10-14T12:00:00+00:00")
        validate_catalog(hoje, SCHEMA)
        self.assertEqual([], hoje["opportunities"])


class Redirecionamentos(unittest.TestCase):
    def test_link_para_o_dou_nao_e_seguido(self):
        adaptador = Adaptador()
        entrada = Entrada("Portaria GM/MS nº 10.154, de 14 de janeiro de 2026", PORTARIA_10154_LINK,
                          "Divulga os resultados finais ...", "Link")
        candidato = Candidato(entrada, "abertura", ato_do_endereco(PORTARIA_10154_LINK))
        with self.assertLogs("funding_intelligence.adapters.pdil", level="INFO"):
            adaptador._ler_documento(candidato)
        self.assertEqual([PORTARIA_10154_LINK], adaptador.urls_pedidas)
        self.assertEqual("permanente", candidato.falha)
        self.assertIn("www.in.gov.br", candidato.motivo)
        self.assertIsNone(candidato.texto)

    def test_ato_recente_so_no_dou_sai_como_desconhecido(self):
        link = BASE + "/legislacao/portaria-sctie-ms-no-12-de-2-de-outubro-de-2026"
        legislacao = rss(("Portaria SCTIE/MS nº 12, de 2 de outubro de 2026", link,
                          "Torna público o processo seletivo do Programa de Desenvolvimento e Inovação Local.",
                          "Link", "2026-10-03T10:00:00Z"))
        adaptador = Adaptador({
            LEGISLACAO: legislacao,
            link: Resposta(302, b"", "https://www.in.gov.br/en/web/dou/-/portaria-sctie-ms-n-12-2026", "text/plain"),
        })
        with self.assertLogs("funding_intelligence.adapters.pdil", level="INFO"):
            itens = por_id(adaptador.collect())
        item = itens["pdil-portaria-sctie-ms-12-2026"]
        self.assertEqual("unknown", item["status"])
        self.assertIsNone(item["dates"]["deadline"])
        self.assertEqual("2026-10-02", item["dates"]["published"])
        self.assertIn("www.in.gov.br", item["description"])
        self.assertFalse(any("in.gov.br" in url for url in adaptador.urls_pedidas))


class SemItemEspeculativo(unittest.TestCase):
    NOTA_3 = BASE + "/notas-tecnicas-e-informativas/nota-informativa-no-3-2026-decis-sctie-ms/view"
    INFORME_5 = BASE + "/informes-tecnicos/informe-no-05-2026-sctie-ms/view"

    def cenario(self, today, informe=False):
        notas = rss(("Nota Informativa nº 3/2026-DECIS/SCTIE/MS", self.NOTA_3,
                     "Anuncia nova rodada de submissão de propostas ao PDIL.", "File", "2026-10-06T12:00:00Z"))
        respostas = {
            NOTAS: notas,
            self.NOTA_3.removesuffix("/view"): texto(
                "NOTA INFORMATIVA Nº 3/2026-DECIS/SCTIE/MS. Informamos que será aberto novo período de submissão "
                "de propostas de projeto de PDIL, em data a ser divulgada. Brasília, 5 de outubro de 2026."),
        }
        if informe:
            original = fixture_rss("informes-tecnicos.rss.xml").corpo.decode("utf-8")
            novo = (
                f'<item rdf:about="{self.INFORME_5}"><title>Informe nº 05/2026 - SCTIE/MS</title>'
                f"<link>{self.INFORME_5}</link><description>Submissão de Propostas de Projeto referentes ao "
                "Programa de Desenvolvimento e Inovação Local - PDIL</description>"
                "<dc:date>2026-11-21T12:00:00Z</dc:date><dc:type>File</dc:type></item></rdf:RDF>"
            )
            respostas[INFORMES] = Resposta(200, original.replace("</rdf:RDF>", novo).encode("utf-8"), None, "x")
            respostas[self.INFORME_5.removesuffix("/view")] = texto(
                "INFORME TÉCNICO Nº 05/2026. Informa-se que o período de submissão de propostas de projeto de "
                "PDIL, nos termos da Matriz de Desafios, é entre 1º de dezembro de 2026 e 28 de fevereiro de 2027. "
                "Documento assinado eletronicamente por [NOME], em 20/11/2026, às 18:00.")
        return Adaptador(respostas, today=today)

    def test_hoje_nao_ha_aberta_nem_prevista(self):
        status = [i["status"] for i in Adaptador().collect()]
        self.assertEqual(["closed"], status)

    def test_noticia_sem_numero_de_ato_nao_vira_item(self):
        noticia = "https://www.gov.br/saude/pt-br/assuntos/noticias-ms/2026/outubro/ms-abre-processo-seletivo-do-pdil"
        busca = rss(("Ministério da Saúde vai abrir processo seletivo do PDIL", noticia,
                     "Programa de Desenvolvimento e Inovação Local terá nova rodada", "Notícia",
                     "2026/10/10 14:48:52 GMT-3"))
        pagina = Resposta(200, "<html><body><div id='content'><p>O Ministério da Saúde prepara o processo "
                               "seletivo do PDIL no novo regime legal.</p></div></body></html>".encode("utf-8"),
                          None, "text/html; charset=utf-8")
        adaptador = Adaptador({BUSCA: busca, noticia: pagina})
        itens = adaptador.collect()
        self.assertEqual(["pdil-informe-02-2024"], [i["id"] for i in itens])
        self.assertTrue(any("sem número de ato" in linha for linha in adaptador.registro))

    def test_anuncio_oficial_sem_prazo_sai_previsto(self):
        itens = por_id(self.cenario(date(2026, 10, 14)).collect())
        item = itens["pdil-nota-informativa-03-2026"]
        self.assertEqual("scheduled", item["status"])
        self.assertIsNone(item["dates"]["deadline"])
        self.assertEqual("2026-10-05", item["dates"]["published"])

    def test_ato_com_datas_substitui_o_anuncio(self):
        adaptador = self.cenario(date(2026, 12, 1), informe=True)
        itens = por_id(adaptador.collect())
        self.assertNotIn("pdil-nota-informativa-03-2026", itens)
        nova = itens["pdil-informe-05-2026"]
        self.assertEqual("open", nova["status"])
        self.assertEqual("2027-02-28", nova["dates"]["deadline"])
        self.assertEqual("2026-11-20", nova["dates"]["published"])
        self.assertIn("terapias_avancadas", nova["themes"])
        # 7 RSS + 3 documentos: a Nota 5/2024 (ato de 2024) ficou fora do orçamento, e
        # a janela de 2024 não sai com o prazo sem a prorrogação.
        self.assertEqual(ORCAMENTO_PEDIDOS, adaptador.pedidos)
        self.assertNotIn("pdil-informe-02-2024", itens)
        self.assertNotIn("failed_scopes", adaptador.snapshot())

    def test_ato_que_cita_a_lei_15471_muda_a_elegibilidade(self):
        informe = BASE + "/informes-tecnicos/informe-no-01-2027-sctie-ms/view"
        adaptador = Adaptador({
            INFORMES: rss(("Informe nº 01/2027 - SCTIE/MS", informe,
                           "Submissão de propostas de projeto ao PDIL", "File", "2027-03-02T12:00:00Z")),
            informe.removesuffix("/view"): texto(
                "Nos termos da Lei nº 15.471, de 20 de julho de 2026, o período de submissão de propostas de "
                "projeto de PDIL, por aliança entre Empresa Estratégica de Saúde credenciada e ICT, é de "
                "2 de março a 30 de abril de 2027."),
        }, today=date(2027, 3, 10))
        item = por_id(adaptador.collect())["pdil-informe-01-2027"]
        self.assertEqual("open", item["status"])
        self.assertIn("empresa", item["eligibility"]["organization_types"])
        self.assertNotIn("partner_types", item["eligibility"])
        self.assertIn("Lei nº 15.471/2026", item["description"])


class EducacaoComOServidor(unittest.TestCase):
    def test_403_na_primeira_pasta_para_tudo(self):
        adaptador = Adaptador({RAIZ_RSS: Resposta(403, b"Forbidden", None, "text/html")})
        with self.assertLogs("funding_intelligence.adapters.pdil", level="WARNING"):
            with self.assertRaises(RuntimeError):
                adaptador.collect()
        self.assertEqual(1, adaptador.pedidos)

    def test_captcha_conta_como_bloqueio(self):
        pagina = Resposta(200, b"<html><div class='g-recaptcha'></div></html>", None, "text/html")
        adaptador = Adaptador({LEGISLACAO: pagina})
        with self.assertLogs("funding_intelligence.adapters.pdil", level="WARNING"):
            snapshot = adaptador.snapshot()
        # Depois do bloqueio, nada mais vai ao www.gov.br nesta rodada.
        self.assertEqual(4, adaptador.pedidos)
        self.assertEqual(["BR"], snapshot["failed_scopes"])
        self.assertIn("HTTP 200", snapshot["error"])
        self.assertEqual([], snapshot["opportunities"])

    def test_pasta_fora_do_ar_vira_leitura_parcial(self):
        adaptador = Adaptador({NOTAS: Resposta(500, b"", None, "text/html")})
        with self.assertLogs("funding_intelligence.adapters.pdil", level="WARNING"):
            snapshot = adaptador.snapshot()
        self.assertEqual("healthy", snapshot["status"])
        self.assertEqual(["BR"], snapshot["failed_scopes"])
        self.assertEqual(["pdil-informe-02-2024"], [i["id"] for i in snapshot["opportunities"]])

    def test_orcamento_de_10_pedidos(self):
        links = [BASE + f"/informes-tecnicos/informe-no-{n:02d}-2026-sctie-ms/view" for n in range(1, 9)]
        informes = rss(*[(f"Informe nº {n:02d}/2026 - SCTIE/MS", link, "Submissão de propostas do PDIL",
                          "File", "2026-10-01T12:00:00Z") for n, link in enumerate(links, 1)])
        adaptador = Adaptador({INFORMES: informes})
        with self.assertLogs("funding_intelligence.adapters.pdil", level="WARNING"):
            snapshot = adaptador.snapshot()
        self.assertEqual(ORCAMENTO_PEDIDOS, adaptador.pedidos)
        self.assertEqual(["BR"], snapshot["failed_scopes"])
        self.assertIn("orçamento", snapshot["error"])

    def test_pausa_de_um_segundo_por_host_sem_fixture(self):
        adaptador = Adaptador()
        adaptador.fixture = None  # rede de mentira: o transporte ainda lê a pasta das fixtures
        adaptador.intervalo_s = pdil.INTERVALO_S
        adaptador._transporte = lambda url: Adaptador()._transporte(url)
        with mock.patch.object(pdil.time, "sleep") as pausa:
            adaptador.collect()
        self.assertEqual(adaptador.pedidos - 1, pausa.call_count)
        self.assertTrue(all(0 < chamada.args[0] <= pdil.INTERVALO_S for chamada in pausa.call_args_list))

    def test_pasta_nova_com_cara_de_janela_e_lida_na_mesma_rodada(self):
        pasta = BASE + "/processo-seletivo-2027"
        edital = pasta + "/edital-sctie-ms-no-1-2027/view"
        raiz = fixture_rss("pdil.rss.xml").corpo.decode("utf-8").replace("</rdf:RDF>", (
            f'<item rdf:about="{pasta}"><title>Processo seletivo 2027</title><link>{pasta}</link>'
            "<description></description><dc:date>2027-02-01T12:00:00Z</dc:date><dc:type>Folder</dc:type></item>"
            "</rdf:RDF>"))
        adaptador = Adaptador({
            RAIZ_RSS: Resposta(200, raiz.encode("utf-8"), None, "application/atom+xml"),
            pasta + "/RSS": rss(("Edital SCTIE/MS nº 1/2027", edital, "Processo seletivo de propostas do PDIL",
                                 "File", "2027-02-01T12:00:00Z")),
            edital.removesuffix("/view"): texto(
                "EDITAL SCTIE/MS Nº 1/2027. O período de submissão de propostas de projeto de PDIL é entre "
                "1º de fevereiro e 31 de março de 2027. Brasília, 29 de janeiro de 2027."),
        }, today=date(2027, 2, 10))
        itens = por_id(adaptador.collect())
        self.assertEqual(pasta + "/RSS", adaptador.urls_pedidas[1])
        item = itens["pdil-edital-01-2027"]
        self.assertEqual("open", item["status"])
        self.assertEqual(("2027-01-29", "2027-03-31"), (item["dates"]["published"], item["dates"]["deadline"]))
        self.assertLessEqual(adaptador.pedidos, ORCAMENTO_PEDIDOS)

    def test_nunca_pede_fora_do_gov_br_saude(self):
        adaptador = Adaptador()
        adaptador.collect()
        self.assertTrue(all(PdilAdapter.permitido(url) for url in adaptador.urls_pedidas))
        self.assertFalse(PdilAdapter.permitido("https://www.in.gov.br/en/web/dou/-/portaria"))
        self.assertFalse(PdilAdapter.permitido("http://www.gov.br/saude/pt-br"))
        self.assertFalse(PdilAdapter.permitido("https://www.gov.br/ebserh/x"))


if __name__ == "__main__":
    unittest.main()
