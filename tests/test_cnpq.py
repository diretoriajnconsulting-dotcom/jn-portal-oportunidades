"""CNPq ampliado: as três coleções, o Ministério da Saúde (Decit/SECTICS) e a carga de 24 meses.

As fixtures em `tests/fixtures/cnpq/` são trechos reduzidos das páginas públicas lidas em
14/10/2026 (coleções, busca, detalhe) e trechos de texto dos editais em PDF. `urls.json` liga cada
URL a um arquivo; URL sem arquivo responde 404, como um anexo quebrado.
"""
import re
import unittest
import urllib.error
from datetime import date
from pathlib import Path
from unittest import mock

from funding_intelligence.adapters import cnpq
from funding_intelligence.adapters.cnpq import CnpqAdapter, _cronograma, _elegibilidade

FIXTURES = Path(__file__).parent / "fixtures" / "cnpq"
HOJE = date(2026, 10, 14)
# Os ids da fonte no catálogo publicado em 14/10/2026 (public/data/funding-opportunities-v2.json).
IDS_DO_CATALOGO = {"cnpq-30-2026", "cnpq-33-2026", "cnpq-32-2026", "cnpq-29-2026"}
FORMATO_ID = re.compile(r"^cnpq-(?:chamamento-)?[1-9]\d{0,2}-20\d{2}$")


def coletar(historical=False):
    adapter = CnpqAdapter(fixture=str(FIXTURES), today=HOJE)
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
        # "Nº 02/2026" (PROSUL, em análise) vira 2-2026, não 02-2026.
        self.assertIn("cnpq-2-2026", itens)

    def test_id_vem_da_pasta_e_nunca_do_titulo(self):
        casos = [
            # A pasta manda, mesmo quando o título escreve o número de outro jeito.
            ("https://www.gov.br/cnpq/pt-br/chamadas/todas-as-chamadas/chamadas-2025/chamada-no-08-2025/x",
             "Chamada Nº 008/2025 Apoio a Eventos Técnico-Científicos em Saúde", "8-2025"),
            ("https://www.gov.br/cnpq/pt-br/chamadas/todas-as-chamadas/chamadas-2024/chamada-ndeg-30-2024-1/x",
             "Chamada CNPq Nº 30/2024", "30-2024"),
            ("https://www.gov.br/cnpq/pt-br/chamadas/todas-as-chamadas/chamadas-2026/chamamento-no-01-2026/x",
             "Chamamento Público CNPq/FNDCT/MCTI Nº 01/2026", "chamamento-1-2026"),
            # Pasta sem número ("sdf"): o número do título.
            ("https://www.gov.br/cnpq/pt-br/chamadas/todas-as-chamadas/chamadas-2024/sdf/chamada-publica-cnpq-ndeg-49-2024",
             "Chamada pública CNPq Nº 49/2024 - Bolsas no País", "49-2024"),
            # Título sem a palavra "chamada" não some mais.
            (None, "MCTI/CNPq/DFG Nº 14/2025", "14-2025"),
            (None, "CNPq/CAPES/IRD Nº 27/2025 Programa de apoio ao Centro Franco-Brasileiro", "27-2025"),
            (None, "Chamada Nº 008/2025 - Eventos", "8-2025"),
        ]
        for url, titulo, esperado in casos:
            with self.subTest(titulo=titulo):
                self.assertEqual(esperado, CnpqAdapter._external_id(url, titulo))

    def test_titulos_dificeis_das_colecoes(self):
        _, itens = coletar()
        self.assertIn("cnpq-14-2025", itens)  # "MCTI/CNPq/DFG Nº 14/2025", sem "chamada"
        self.assertIn("cnpq-27-2025", itens)  # "CNPq/CAPES/IRD Nº 27/2025"
        self.assertIn("cnpq-chamamento-1-2026", itens)


class SituacaoTests(unittest.TestCase):
    def test_tres_colecoes_e_situacao(self):
        _, itens = coletar()
        self.assertEqual("open", itens["cnpq-30-2026"]["status"])
        self.assertEqual("2026-12-11", itens["cnpq-30-2026"]["dates"]["deadline"])
        self.assertEqual("2026-08-19", itens["cnpq-30-2026"]["dates"]["published"])
        # Em análise: encerrada, com o prazo lido (a CT-Biotec deixou de sumir do radar).
        self.assertEqual("closed", itens["cnpq-24-2026"]["status"])
        self.assertEqual("2026-09-18", itens["cnpq-24-2026"]["dates"]["deadline"])
        self.assertEqual("closed", itens["cnpq-29-2026"]["status"])
        # Resultados: encerrada.
        self.assertEqual("closed", itens["cnpq-30-2025"]["status"])
        self.assertEqual({"open"}, {i["status"] for i in itens.values() if i["status"] != "closed"})

    def test_linha_de_inscricoes_em_formatos_variados(self):
        _, itens = coletar()
        self.assertEqual("2026-09-18", itens["cnpq-13-2026"]["dates"]["deadline"])  # "18/09 /2026"
        self.assertEqual("2026-09-30", itens["cnpq-20-2026"]["dates"]["deadline"])  # "30/09/26"
        self.assertEqual("2026-08-12", itens["cnpq-25-2026"]["dates"]["deadline"])  # "INSCRIÇÕES:"

    def test_publicado_em_nao_e_lancamento(self):
        _, itens = coletar()
        # "Publicado em 21/01/2026" na página; lançada em 31/12/2025 (linha "Inscrições").
        self.assertEqual("2025-12-31", itens["cnpq-30-2025"]["dates"]["published"])

    def test_aberta_sem_prazo_lido_fica_unknown(self):
        adapter = CnpqAdapter(fixture=str(FIXTURES), today=HOJE)
        chamada = adapter._chamada(
            "Chamada CNPq Nº 40/2026 - Teste", None, None, "abertas", "Sem linha de inscrições", None, [],
        )
        self.assertEqual("unknown", adapter._montar(chamada)["status"])

    def test_varias_rodadas_vale_o_proximo_prazo(self):
        adapter = CnpqAdapter(fixture=str(FIXTURES), today=HOJE)
        texto = "Inscrições 1ª Rodada: 01/08/2026 a 30/09/2026 Inscrições 2ª Rodada: 01/10/2026 a 30/11/2026"
        self.assertEqual(("2026-08-01", "2026-11-30"), adapter._janela(texto))

    def test_juntar_pelo_uid_fica_a_fase_mais_adiantada(self):
        adapter = CnpqAdapter(fixture=str(FIXTURES), today=HOJE)
        chamadas = {}
        base = dict(title="Chamada Nº 9/2026", url=None, texto="", resumo=None, documentos=[], external_id="9-2026", ano=2026)
        adapter._juntar(chamadas, {**base, "uid": "a" * 32, "fase": "abertas"})
        adapter._juntar(chamadas, {**base, "uid": "a" * 32, "fase": "analise"})
        adapter._juntar(chamadas, {**base, "uid": "a" * 32, "fase": "abertas"})  # página repetida
        self.assertEqual(["analise"], [c["fase"] for c in chamadas.values()])


class MinisterioDaSaudeTests(unittest.TestCase):
    def test_chamada_30_2025_terapias_avancadas(self):
        _, itens = coletar()
        item = itens["cnpq-30-2025"]
        self.assertEqual("CNPq", item["funder"])
        self.assertEqual(["Decit/MS"], item["partners"])
        tipos = item["eligibility"]["organization_types"]
        for tipo in ("pesquisador", "ict", "ict_privada", "osc"):
            self.assertIn(tipo, tipos)
        self.assertNotIn("empresa", tipos)
        # O edital admite "instituições participantes" sem dizer o tipo (item 6.5.4). Isso não basta para
        # dizer à empresa que ela entra como parceira: sem a empresa nomeada, não há `partner_types`.
        self.assertNotIn("partner_types", item["eligibility"])
        self.assertIn("terapias_avancadas", item["themes"])
        self.assertIn("saude", item["themes"])
        self.assertEqual(30_000_000, item["funding"]["program_budget"])
        self.assertEqual("auxilio_pesquisa", item["instrument"]["type"])

    def test_31_2025_aceitou_empresa_executora(self):
        _, itens = coletar()
        self.assertIn("empresa", itens["cnpq-31-2025"]["eligibility"]["organization_types"])
        self.assertNotIn("partner_types", itens["cnpq-31-2025"]["eligibility"])
        self.assertEqual(12_000_000, itens["cnpq-31-2025"]["funding"]["program_budget"])

    def test_parceiros_pelo_bloco_de_siglas(self):
        _, itens = coletar()
        self.assertEqual(["Daevs/SVSA/MS"], itens["cnpq-33-2025"]["partners"])
        self.assertEqual(["Decit/MS"], itens["cnpq-18-2026"]["partners"])
        self.assertEqual(["SECTICS/MS", "CAPES", "FAPs"], itens["cnpq-46-2024"]["partners"])
        self.assertEqual(["MCTI", "ANR"], itens["cnpq-32-2026"]["partners"])
        # O financiador continua sendo o CNPq.
        self.assertEqual({"CNPq"}, {i["funder"] for i in itens.values()})

    def test_ms_tambem_pela_primeira_frase_e_pelo_ppsus(self):
        self.assertEqual(["Decit/MS"], CnpqAdapter._parceiros(
            "CHAMADA Nº 10/2025 - PPSUS - INOVAÇÃO",
        ))
        self.assertEqual(["Decit/MS"], CnpqAdapter._parceiros(
            "Chamada Nº 008/2025 Apoio a Eventos Técnico-Científicos em Saúde",
            "Com financiamento do Departamento de Ciência e Tecnologia (Decit/SECTICS/MS), o CNPq…",
        ))

    def test_mai_dai_empresa_como_instituicao_parceira(self):
        _, itens = coletar()
        item = itens["cnpq-30-2026"]
        self.assertIn("empresa", item["eligibility"]["partner_types"])
        self.assertNotIn("empresa", item["eligibility"]["organization_types"])
        self.assertEqual(["CAPES"], item["partners"])
        self.assertEqual("bolsa", item["instrument"]["type"])
        self.assertEqual(62_440_000, item["funding"]["program_budget"])

    def test_sem_edital_lido_mantem_o_comportamento_anterior(self):
        _, itens = coletar()
        # O PDF da 29/2026 não está na fixture (404): fica a regra antiga, sem inventar.
        self.assertEqual(["empresa", "startup", "pesquisador"], itens["cnpq-29-2026"]["eligibility"]["organization_types"])
        self.assertNotIn("partner_types", itens["cnpq-29-2026"]["eligibility"])

    def test_elegibilidade_sem_o_item_do_edital(self):
        self.assertIsNone(_elegibilidade("Texto sem a seção da instituição de execução."))


class CronogramaTests(unittest.TestCase):
    def test_prazo_e_lancamento_do_pdf(self):
        texto = (FIXTURES / "chamada_40_2024.txt").read_text(encoding="utf-8")
        self.assertEqual(("2024-08-30", "2024-10-18"), _cronograma(texto))

    def test_retificacao_vale_a_ultima_tabela_e_nao_o_maior_valor(self):
        texto = (
            "2 - Cronograma FASES DATA Lançamento da Chamada 01/02/2026 "
            "Data limite para submissão das propostas 10/03/2026 "
            "Prazo final para interposição de recurso administrativo 30/05/2026 "
            "Cronograma retificado FASES DATA Lançamento da Chamada 01/02/2026 "
            "Data limite para submissão das propostas 20/03/2026 Divulgação 30/06/2026"
        )
        self.assertEqual(("2026-02-01", "2026-03-20"), _cronograma(texto))


class CargaHistoricaTests(unittest.TestCase):
    def test_busca_por_ano_e_buraco_na_numeracao(self):
        adapter, itens = coletar(historical=True)
        # A 40/2024 não está nas coleções nem na busca do ano: veio da busca pontual "40/2024",
        # da página de detalhe (sem linha de inscrições) e do cronograma do PDF.
        item = itens["cnpq-40-2024"]
        self.assertEqual("closed", item["status"])
        self.assertEqual("2024-10-18", item["dates"]["deadline"])
        self.assertEqual("2024-08-30", item["dates"]["published"])
        self.assertEqual(["Fiocruz"], item["partners"])
        self.assertLessEqual(adapter.pedidos, cnpq.ORCAMENTO_HISTORICO)
        # Nenhuma encerrada de mais de 24 meses.
        piso = "2024-10-14"
        for registro in itens.values():
            if registro["dates"]["deadline"]:
                self.assertGreaterEqual(registro["dates"]["deadline"], piso)

    def test_descida_para_apos_tres_antigas_seguidas(self):
        adapter = CnpqAdapter(fixture=str(FIXTURES), today=HOJE)
        adapter.historical = True
        lidas = []
        prazos = {n: ("2024-06-01" if n <= 45 else "2024-11-30") for n in range(1, 50)}

        def busca(numero, ano):
            return {"url": f"x/{numero}", "title": f"Chamada Nº {numero}/{ano}", "fase": "resultados"} if ano == 2024 else None

        def ler(achado, uids, novos):
            numero = int(achado["url"].rsplit("/", 1)[1])
            lidas.append(numero)
            return prazos[numero]

        with mock.patch.object(adapter, "_html", side_effect=urllib.error.URLError("sem rede")), \
             mock.patch.object(adapter, "_busca_pontual", side_effect=busca), \
             mock.patch.object(adapter, "_ler_historica", side_effect=ler):
            chamadas = {"49-2024": {"external_id": "49-2024", "uid": None, "texto": ""}}
            try:
                adapter._historico(chamadas, [])
            except urllib.error.URLError:
                pass
        # Sem a busca do ano (falhou), a descida parte da 49 conhecida e para depois de 45, 44 e 43.
        self.assertEqual([48, 47, 46, 45, 44, 43], lidas)


class EducacaoComOServidorTests(unittest.TestCase):
    def test_orcamento_do_dia(self):
        adapter, _ = coletar()
        self.assertLessEqual(adapter.pedidos, cnpq.ORCAMENTO_DIA)
        self.assertLessEqual(adapter.pedidos_pdf, cnpq.ORCAMENTO_PDF_DIA)

    def test_orcamento_curto_para_sem_derrubar_a_rodada(self):
        adapter = CnpqAdapter(fixture=str(FIXTURES), today=HOJE)
        with mock.patch.object(cnpq, "ORCAMENTO_DIA", 4):
            snap = adapter.snapshot()
        self.assertEqual(4, adapter.pedidos)
        self.assertEqual("healthy", snap["status"])
        self.assertIn("orçamento", snap["error"])
        self.assertIn("cnpq-30-2025", {item["id"] for item in snap["opportunities"]})

    def test_403_para_no_host(self):
        adapter = CnpqAdapter(today=HOJE)
        erro = urllib.error.HTTPError(cnpq.COLECOES[0][1], 403, "Forbidden", None, None)
        with mock.patch.object(adapter, "fetch_bytes", side_effect=erro) as pedir, \
             mock.patch.object(cnpq.time, "sleep"):
            with self.assertRaises(cnpq.Bloqueio):
                adapter.collect()
            with self.assertRaises(cnpq.Bloqueio):
                adapter._html(cnpq.COLECOES[1][1])
        self.assertEqual(1, pedir.call_count)

    def test_pagina_de_desafio_e_bloqueio(self):
        adapter = CnpqAdapter(today=HOJE)
        desafio = b"<html><head><title>Just a moment...</title></head><body></body></html>"
        with mock.patch.object(adapter, "fetch_bytes", return_value=desafio), \
             mock.patch.object(cnpq.time, "sleep"):
            with self.assertRaises(cnpq.Bloqueio):
                adapter.collect()

    def test_uma_pausa_por_pedido_ao_mesmo_host(self):
        adapter = CnpqAdapter(today=HOJE)
        pagina = (FIXTURES / "abertas.html").read_bytes()
        with mock.patch.object(adapter, "fetch_bytes", return_value=pagina), \
             mock.patch.object(cnpq.time, "sleep") as dormir:
            adapter._html(cnpq.COLECOES[0][1])
            adapter._html(cnpq.COLECOES[1][1])
        self.assertEqual(1, dormir.call_count)


if __name__ == "__main__":
    unittest.main()
