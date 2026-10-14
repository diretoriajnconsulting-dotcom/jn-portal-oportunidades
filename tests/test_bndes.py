"""Adaptador do BNDES: produtos contínuos do catálogo e chamadas de seleção de fundos.

As fixtures (`tests/fixtures/bndes/`) são trechos reduzidos das páginas lidas em 14/10/2026: o
recorte da API do catálogo, a listagem de chamadas de fundos e o detalhe de sete chamadas.
"""
import copy
import json
import urllib.error
from datetime import date
from pathlib import Path
import unittest

from funding_intelligence.adapters.bndes import (
    API_PRODUTOS, FORA, LISTAGEM_FUNDOS, PRODUTOS, BndesAdapter, FonteIndisponivel,
    ler_janela, ler_parceiros, ler_publicacao, normalizar, permitido,
)
from funding_intelligence.models import ORGANIZATION_TYPES
from funding_intelligence.pipeline import aggregate, validate_catalog


ROOT = Path(__file__).parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "bndes"
SCHEMA = str(ROOT / "schema/funding-opportunity-2.2.schema.json")
PORTFOLIOS = json.loads((ROOT / "config/portfolios.json").read_text(encoding="utf-8"))["portfolios"]
HOJE = date(2026, 10, 14)


def adaptador(historico=False, hoje=HOJE):
    adapter = BndesAdapter(fixture=str(FIXTURES), today=hoje)
    adapter.historical = historico
    return adapter


def registros():
    return json.loads((FIXTURES / "linhas_programas.json").read_text(encoding="utf-8"))


def por_id(itens):
    return {item["id"]: item for item in itens}


def rede_falsa(falha_em, codigo=403, corpo=None):
    """Serve as fixtures como se fosse a rede, e falha numa URL."""
    adapter = BndesAdapter(today=HOJE)
    adapter.historical = False
    adapter.pausa = 0
    pedidos = []

    def fetch_bytes(url):
        pedidos.append(url)
        if url == falha_em:
            if corpo is not None:
                return corpo
            raise urllib.error.HTTPError(url, codigo, "bloqueado", {}, None)
        if url == API_PRODUTOS:
            return (FIXTURES / "linhas_programas.json").read_bytes()
        if url == LISTAGEM_FUNDOS:
            return (FIXTURES / "chamadas_fundos.html").read_bytes()
        slug = normalizar(url)[0]
        return (FIXTURES / f"chamada_{slug}.html").read_bytes()

    adapter.fetch_bytes = fetch_bytes
    return adapter, pedidos


class ProdutosContinuos(unittest.TestCase):
    def test_regra_de_selecao_publica_so_a_lista(self):
        itens = adaptador().collect()
        produtos = {i["external_id"] for i in itens if i["channel"] == "fluxo_continuo"}
        self.assertEqual({f"produto-{slug}" for slug in PRODUTOS}, produtos)
        self.assertEqual(12, len(produtos))

    def test_setor_saude_e_finalidade_inovacao_nao_bastam(self):
        # O FUST (telecomunicações) vem com setor "Saúde" e finalidade "Inovação" na API.
        fust = next(r for r in registros() if r["nome"] == "fust-bndes")
        self.assertIn("Saúde", fust["setores"])
        self.assertIn("Inovação", fust["finalidades"])
        ids = por_id(adaptador().collect())
        for slug in ("fust-bndes", "fiis-apoio-saude", "programa-bndes-mais-inovacao", "bndes-finame-todos"):
            self.assertNotIn(f"bndes-produto-{slug}", ids)
        self.assertTrue({"fust-bndes", "fiis-apoio-saude", "programa-bndes-mais-inovacao"} <= set(FORA))

    def test_expirado_e_aviso_de_expiracao_nao_entram(self):
        adapter = adaptador()
        base = registros()
        cartao = next(r for r in base if r["nome"] == "cartao-bndes")
        # `expirado: false` com aviso de expiração (há 18 assim no catálogo) também sai.
        cartao["aviso"] = "<p>Este programa se encontra expirado. Caso ele seja prorrogado, as informações desta página serão atualizadas.</p>"
        pme = next(r for r in base if r["nome"] == "bndes-credito-pequenas-e-medias-empresas")
        pme["expirado"] = True
        ids = por_id(adapter.produtos(base))
        self.assertNotIn("bndes-produto-cartao-bndes", ids)
        self.assertNotIn("bndes-produto-bndes-credito-pequenas-e-medias-empresas", ids)
        self.assertEqual(10, len(ids))

    def test_produto_da_lista_com_data_de_encerramento_entra_encerrado(self):
        base = registros()
        funtec = next(r for r in base if r["nome"] == "bndes-funtec")
        funtec["expirado"] = True
        funtec["aviso"] = "<p>Vigência do programa encerrada em\xa031.12.2025.</p>"
        item = por_id(adaptador().produtos(base))["bndes-produto-bndes-funtec"]
        self.assertEqual("closed", item["status"])
        self.assertEqual("2025-12-31", item["dates"]["closed"])
        self.assertIsNone(item["dates"]["deadline"])

    def test_campos_do_produto(self):
        ids = por_id(adaptador().collect())
        funtec = ids["bndes-produto-bndes-funtec"]
        self.assertEqual("fluxo_continuo", funtec["channel"])
        self.assertEqual("open", funtec["status"])
        self.assertIsNone(funtec["dates"]["deadline"])
        self.assertEqual({"type": "subvencao_economica", "repayable": False}, funtec["instrument"])
        self.assertEqual(["ict", "ict_privada", "universidade"], funtec["eligibility"]["organization_types"])
        self.assertEqual(["empresa"], funtec["eligibility"]["partner_types"])
        self.assertEqual(
            "https://www.bndes.gov.br/wps/portal/site/home/financiamento/produto/bndes-funtec",
            funtec["source"]["url"],
        )
        self.assertIn("Instituições Tecnológicas", funtec["description"])

        farmacos = ids["bndes-produto-bndes-producao-farmacos"]
        self.assertEqual({"type": "credito", "repayable": True}, farmacos["instrument"])
        self.assertIn("saude", farmacos["themes"])

        subscricao = ids["bndes-produto-bndes-subscricao-de-valores-mobiliarios"]
        self.assertEqual({"type": "investimento", "repayable": None}, subscricao["instrument"])
        self.assertEqual("BNDESPAR", subscricao["funder"])

        cartao = ids["bndes-produto-cartao-bndes"]
        self.assertIn("startup", cartao["eligibility"]["organization_types"])
        socio = ids["bndes-produto-bndes-fundo-socioambiental"]
        self.assertEqual({"type": "outros", "repayable": False}, socio["instrument"])
        self.assertTrue(set(socio["eligibility"]["organization_types"]) <= {"osc", "municipio", "estado"})

    def test_fichas_usam_o_vocabulario_do_contrato(self):
        for slug, ficha in PRODUTOS.items():
            with self.subTest(slug=slug):
                self.assertTrue(set(ficha.proponentes) <= ORGANIZATION_TYPES)
                self.assertTrue(set(ficha.parceiras) <= ORGANIZATION_TYPES)
                self.assertNotIn(slug, FORA)
                self.assertIn(ficha.instrumento, {"credito", "investimento", "subvencao_economica", "outros"})

    def test_produto_novo_de_saude_vira_aviso_e_nao_e_publicado(self):
        adapter = adaptador()
        base = registros()
        novo = copy.deepcopy(base[0])
        novo.update(nome="bndes-saude-digital", titulo="BNDES Saúde Digital", aviso="", expirado=False,
                    infoBasicas="Financiamento a soluções digitais para hospitais.")
        itens = adapter.produtos(base + [novo])
        self.assertNotIn("bndes-produto-bndes-saude-digital", por_id(itens))
        self.assertTrue(any("bndes-saude-digital" in aviso for aviso in adapter.avisos))

    def test_catalogo_vazio_ou_sem_a_lista_e_falha_nao_zero_produtos(self):
        adapter = adaptador()
        with self.assertRaises(FonteIndisponivel):
            adapter.produtos([])
        with self.assertRaises(FonteIndisponivel):
            adapter.produtos([{"nome": "outro-produto", "titulo": "Outro", "expirado": False}])
        with self.assertRaises(FonteIndisponivel):
            adapter.produtos({"erro": "manutenção"})


class ChamadasDeFundos(unittest.TestCase):
    def test_em_andamento_com_prazo_vencido_sai_encerrada(self):
        adapter = adaptador()
        ids = por_id(adapter.collect())
        ia = ids["bndes-fundos-fip-ia-2026"]
        self.assertEqual("closed", ia["status"])
        self.assertEqual("2026-05-28", ia["dates"]["deadline"])
        self.assertEqual("2026-05-28", ia["dates"]["closed"])
        conexoes = ids["bndes-fundos-fip-conexoes-startups-2026"]
        self.assertEqual("closed", conexoes["status"])
        self.assertEqual("2026-08-12", conexoes["dates"]["deadline"])
        # No dia a dia, as encerradas da listagem não são lidas nem emitidas.
        self.assertNotIn("bndes-fundos-chamada-fundo-investimento-complexo-da-saude", ids)
        self.assertEqual(4, adapter.pedidos)
        self.assertLessEqual(adapter.pedidos, adapter.orcamento_diario)

    def test_aberta_so_com_prazo_lido_e_futuro(self):
        conexoes = por_id(adaptador(hoje=date(2026, 7, 1)).collect())["bndes-fundos-fip-conexoes-startups-2026"]
        self.assertEqual("open", conexoes["status"])
        self.assertEqual("2026-06-18", conexoes["dates"]["published"])
        self.assertNotIn("closed", conexoes["dates"])
        no_dia = por_id(adaptador(hoje=date(2026, 8, 12)).collect())["bndes-fundos-fip-conexoes-startups-2026"]
        self.assertEqual("open", no_dia["status"])
        antes = por_id(adaptador(hoje=date(2026, 6, 1)).collect())["bndes-fundos-fip-conexoes-startups-2026"]
        self.assertEqual("scheduled", antes["status"])

    def test_campos_da_chamada(self):
        conexoes = por_id(adaptador().collect())["bndes-fundos-fip-conexoes-startups-2026"]
        self.assertEqual("chamada_publica", conexoes["channel"])
        self.assertEqual({"type": "investimento", "repayable": None}, conexoes["instrument"])
        self.assertEqual(["outros"], conexoes["eligibility"]["organization_types"])
        self.assertEqual("BNDESPAR", conexoes["funder"])
        self.assertEqual(["Finep"], conexoes["partners"])
        self.assertEqual(160_000_000, conexoes["funding"]["program_budget"])
        self.assertIn("startups", conexoes["themes"])
        self.assertIn("gestora", conexoes["description"])
        self.assertEqual(
            "https://www.bndes.gov.br/wps/portal/site/home/mercado-de-capitais/fundos-de-investimentos/"
            "chamadas-publicas-para-selecao-de-fundos/fip-conexoes-startups-2026",
            conexoes["source"]["url"],
        )
        for documento in conexoes["documents"]:
            self.assertNotIn("CVID", documento["url"])
            self.assertIn("/wps/wcm/connect/site/", documento["url"])
        self.assertIn("Edital", [d["title"] for d in conexoes["documents"]])

    def test_carga_historica_le_encerradas_de_24_meses(self):
        adapter = adaptador(historico=True)
        ids = por_id(adapter.collect())
        saude = ids["bndes-fundos-chamada-fundo-investimento-complexo-da-saude"]
        self.assertEqual("closed", saude["status"])
        self.assertEqual("2025-04-07", saude["dates"]["published"])
        self.assertEqual("2025-05-22", saude["dates"]["deadline"])
        self.assertEqual("2025-05-22", saude["dates"]["closed"])
        self.assertEqual(["Finep", "Fundação Butantan"], saude["partners"])
        self.assertEqual(200_000_000, saude["funding"]["program_budget"])
        self.assertIn("saude", saude["themes"])

        transicao = ids["bndes-fundos-fip-transicao-energetica-2025"]
        self.assertEqual(("2025-06-04", "2025-07-31"), (transicao["dates"]["published"], transicao["dates"]["deadline"]))
        self.assertEqual(["Petrobras", "Finep"], transicao["partners"])
        # Clima: o prazo foi prorrogado para 21/10/2025; a apresentação ao comitê (15 a 19/12) não é prazo.
        clima = ids["bndes-fundos-chamada-publica-selecao-fundos-mitigacao-climatica"]
        self.assertEqual("2025-10-21", clima["dates"]["deadline"])
        self.assertEqual("2025-12-05", ids["bndes-fundos-chamada-publica-selecao-fundos-indice-de-mercado-etf"]["dates"]["deadline"])
        # Mineração encerrou em 23/07/2024, antes do corte de 24 meses (14/10/2024).
        self.assertNotIn("bndes-fundos-chamada-investimento-em-participacoes-mineracao", ids)
        # API, listagem, 2 em andamento e 5 encerradas; depois de três antigas seguidas, para.
        self.assertEqual(9, adapter.pedidos)
        self.assertLessEqual(adapter.pedidos, adapter.orcamento_historico)

    def test_orcamento_esgotado_nao_vira_pedido_extra(self):
        adapter = adaptador(historico=True)
        adapter.orcamento_historico = 5
        ids = por_id(adapter.collect())
        self.assertEqual(5, adapter.pedidos)
        self.assertIn("bndes-fundos-chamada-publica-selecao-fundos-indice-de-mercado-etf", ids)
        self.assertNotIn("bndes-fundos-chamada-publica-selecao-fundos-mitigacao-climatica", ids)
        self.assertTrue(any("orçamento" in aviso for aviso in adapter.avisos))


class LeituraDoTexto(unittest.TestCase):
    def test_janelas_do_portal(self):
        casos = {
            "deverá ser encaminhada ao BNDES, a partir de 07.04.2025 até as 18h00min do dia 22.05.2025.":
                ("2025-04-07", "2025-05-22"),
            "O prazo de envio vai de 18.06.2026 até 12.08.2026, às 18h (horário de Brasília).":
                ("2026-06-18", "2026-08-12"),
            "Prazo para recebimento das propostas De 01.07.2025 até às 18h de 31.07.2025 (horário de Brasília/DF)":
                ("2025-07-01", "2025-07-31"),
            "As Propostas deverão ser encaminhadas em versão eletrônica até às 18h do dia 17/12/2021, para o endereço":
                (None, "2021-12-17"),
        }
        for texto, esperado in casos.items():
            with self.subTest(texto=texto):
                self.assertEqual(esperado, ler_janela(texto))

    def test_outras_etapas_nao_viram_prazo(self):
        texto = (
            "A versão eletrônica das propostas deverá ser enviada a partir de 01.09.2025 até as 18h do dia "
            "21.10.2025. Calendário: Apresentação das propostas ao Comitê de Seleção: de 15.12.2025 a 19.12.2025. "
            "Divulgação das propostas qualificadas para apresentação presencial * Até 19.09.2025."
        )
        self.assertEqual(("2025-09-01", "2025-10-21"), ler_janela(texto))
        self.assertEqual((None, None), ler_janela("Dúvidas de 01.07.2025 até 10.07.2025 pelo e-mail."))

    def test_prorrogacao_vale_o_prazo_novo(self):
        texto = (
            "A documentação deverá ser encaminhada a partir de 01.09.2025 até as 18h do dia 14.10.2025. "
            "Comunica-se que o prazo para recebimento de propostas foi postergado para até as 18h do dia 21.10.2025."
        )
        self.assertEqual(("2025-09-01", "2025-10-21"), ler_janela(texto))

    def test_publicacao_e_parceiros(self):
        self.assertEqual("2025-10-29", ler_publicacao("A BNDESPAR lançou, em 29.10.2025, Edital de Chamada"))
        self.assertEqual("2025-06-04", ler_publicacao("Energia - 2025 Em 04.06.2025 , a BNDESPAR, em parceria"))
        self.assertEqual(
            ["Finep"],
            ler_parceiros("em parceria com a Financiadora de Estudos e Projetos (“Finep”), lançou Edital"),
        )

    def test_urls_do_portal_viram_slug_estavel(self):
        wcm = ("?1dmy&urile=wcm%3apath%3a%2Fbndes_institucional%2Fhome%2Fmercado-de-capitais%2F"
               "fundos-de-investimentos%2Fchamadas-publicas-para-fundos%2Ffip-ia-2026")
        amigavel = f"{LISTAGEM_FUNDOS}/fip-ia-2026"
        self.assertEqual(("fip-ia-2026", amigavel), normalizar(wcm))
        estado = f"{amigavel}/!ut/p/z1/tVNdb4IwFP0tPvjYtCBfPqJRmV9kTIf0xRQo2m20CBW3_fpVY5Zli5jF2Jf2tueee09uD8RwBTEnNdsQyQQnbyqOsLWedicDz_DRdGQHBnLNuT9coEDzXR2GJwC6sFwEcUO-b8FniCEuEpbCKEUWpQSZQEMEAYPEDogzMwUkNqluUqpnmnFEJ1wWcgujmKe0WjNeSSb3yanfNtqKnLZRTsuEpAKkFCSkYJKwqo2yPU9FdbxjvKYqK6dcCvWQlIxImnwfAKvPuhqE42bZ4bHTywxIt34DfMt2FSAYO0PP0iY94w_goTNArtefmsZgoo26nTOgoYtIqbAvq-jAsGb0AJdclLma9tM_h-Fdq_Co31jhCr15X3r7rvSWcSP9uNFc6o8q97KX3Q67yjSCS_ou4equrlEF9XLWn22UDiK3KiUTcPUDUOTL3Ol8gNfA--zNwagfO4dFlp-30KncVusLyb7l2Q!!/dz/d5/L2dBISEvZ0FBIS9nQSEh/"
        self.assertEqual(("fip-ia-2026", amigavel), normalizar(estado))
        # Chamada fora da pasta comum: fica a forma WCM, sem estado de portlet.
        slug, url = normalizar("?1dmy&urile=wcm%3apath%3a%2Fbndes_institucional%2Fhome%2Fmercado-de-capitais"
                               "%2Ffundos-de-investimentos%2Fcriatec%2Fcriatec-iv")
        self.assertEqual("criatec-iv", slug)
        self.assertTrue(url.startswith(f"{LISTAGEM_FUNDOS}?1dmy&urile=wcm"))
        self.assertNotIn("!ut", url)
        self.assertIsNone(normalizar("https://www.worldlabs.org/opportunity/inteligencia-dados-saude"))

    def test_ids_estaveis(self):
        ids = set(por_id(adaptador().collect()))
        self.assertIn("bndes-produto-bndes-funtec", ids)
        self.assertIn("bndes-fundos-fip-ia-2026", ids)
        # O título nunca entra no id.
        self.assertTrue(all(i.startswith(("bndes-produto-", "bndes-fundos-")) for i in ids))


class EducacaoComOServidor(unittest.TestCase):
    def test_robots(self):
        self.assertTrue(permitido(API_PRODUTOS))
        self.assertTrue(permitido(LISTAGEM_FUNDOS))
        self.assertTrue(permitido(f"{LISTAGEM_FUNDOS}?1dmy&urile=wcm%3apath%3a%2Fx"))
        self.assertFalse(permitido("https://www.bndes.gov.br/wps/wcm/connect/site/416e/Edital.pdf?MOD=AJPERES"))
        self.assertFalse(permitido("https://www.bndes.gov.br/wps/myportal/x"))
        self.assertFalse(permitido("https://www.bndes.gov.br/wps/portal/site/home/no-index/x"))
        self.assertFalse(permitido("https://garagem.bndes.gov.br/wp-json/wp/v2/media"))

    def test_403_para_a_casa_e_mantem_o_resto(self):
        adapter, pedidos = rede_falsa(LISTAGEM_FUNDOS, 403)
        snapshot = adapter.snapshot()
        self.assertEqual("healthy", snapshot["status"])
        self.assertEqual(["BR"], snapshot["failed_scopes"])
        self.assertIn("403", snapshot["error"])
        self.assertEqual(12, len(snapshot["opportunities"]))
        self.assertEqual([API_PRODUTOS, LISTAGEM_FUNDOS], pedidos)

    def test_desafio_do_cloudflare_conta_como_bloqueio(self):
        corpo = b"<html><title>Just a moment...</title></html>"
        adapter, pedidos = rede_falsa(f"{LISTAGEM_FUNDOS}/fip-conexoes-startups-2026", corpo=corpo)
        snapshot = adapter.snapshot()
        self.assertIn("desafio", snapshot["error"])
        self.assertNotIn("bndes-fundos-fip-conexoes-startups-2026", por_id(snapshot["opportunities"]))
        # Depois do bloqueio, nenhum pedido a mais ao mesmo host.
        self.assertEqual(3, len(pedidos))

    def test_tudo_fora_do_ar_e_falha_da_fonte(self):
        adapter = BndesAdapter(today=HOJE)
        adapter.pausa = 0

        def fora_do_ar(url):
            raise urllib.error.URLError("sem rota")

        adapter.fetch_bytes = fora_do_ar
        with self.assertRaises(FonteIndisponivel):
            adapter.snapshot()


class NoPipeline(unittest.TestCase):
    def test_catalogo_valido_no_contrato_22(self):
        adapter = adaptador(historico=True)
        snapshot = adapter.snapshot()
        catalog = aggregate([snapshot], None, PORTFOLIOS, generated_at="2026-10-14T12:00:00+00:00")
        validate_catalog(catalog, SCHEMA)
        self.assertEqual(12, catalog["summary"]["continuous"])
        self.assertEqual(0, catalog["summary"]["open"])
        ids = por_id(catalog["opportunities"])
        self.assertIn("bndes-fundos-chamada-fundo-investimento-complexo-da-saude", ids)

    def test_falha_parcial_mantem_a_chamada_anterior(self):
        # Rodada 1 (01/07/2026): a FIP Conexões está aberta. Rodada 2: o detalhe não responde.
        r1 = aggregate([adaptador(hoje=date(2026, 7, 1)).snapshot()], None, PORTFOLIOS,
                       generated_at="2026-07-01T12:00:00+00:00")
        self.assertEqual("open", por_id(r1["opportunities"])["bndes-fundos-fip-conexoes-startups-2026"]["status"])
        adapter, _ = rede_falsa(f"{LISTAGEM_FUNDOS}/fip-conexoes-startups-2026", 503)
        adapter.today = date(2026, 7, 2)
        r2 = aggregate([adapter.snapshot()], r1, PORTFOLIOS, generated_at="2026-07-02T12:00:00+00:00")
        conexoes = por_id(r2["opportunities"])["bndes-fundos-fip-conexoes-startups-2026"]
        self.assertEqual("open", conexoes["status"])
        self.assertTrue(conexoes["source"]["stale"])
        validate_catalog(r2, SCHEMA)


if __name__ == "__main__":
    unittest.main()
