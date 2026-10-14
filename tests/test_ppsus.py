"""PPSUS: sentinela no SISC&T e leitura por UF nas FAPs (estudo de 14/10/2026).

Os testes rodam sem rede. Cada cenário é um JSON em `tests/fixtures/ppsus/` que
aponta cada URL para um trecho reduzido da página pública (ou do texto do PDF),
medido no estudo. O texto do PDF do Amapá é sintético e diz isso no cenário: o
`robots.txt` da FAPEAP responde 403, e o PDF não foi lido.
"""
import json
import re
from datetime import date
from pathlib import Path
import unittest

from funding_intelligence.adapters.ppsus import (
    LIMITE_PEDIDOS_DIA, Documento, FalhaUF, PpsusAdapter, agrupar, aceita_servico_saude,
    classificar, numero_do_cabecalho, numero_do_rotulo, prazo_submissao, valor_global,
)
from funding_intelligence.adapters.ppsus_ufs import SIGLAS_UF, UFS, UFS_FORA
from funding_intelligence.pipeline import aggregate, validate_catalog


ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "ppsus"
SCHEMA = str(ROOT / "schema/funding-opportunity-2.2.schema.json")
PORTFOLIOS = json.loads((ROOT / "config/portfolios.json").read_text(encoding="utf-8"))["portfolios"]
HOJE = date(2026, 10, 14)


def texto(nome):
    return (FIXTURES / nome).read_text(encoding="utf-8")


def adaptador(cenario, *, hoje=HOJE, historico=False, ufs=None, extra=None):
    a = PpsusAdapter(fixture=str(FIXTURES / cenario), today=hoje, historical=historico, ufs=ufs)
    if extra:
        a.mapa.update(extra)
    return a


def por_id(itens):
    return {item["id"]: item for item in itens}


class PrazoDeSubmissao(unittest.TestCase):
    """A regra é uma só para as 26 FAPs: a linha de submissão do cronograma, na versão mais nova."""

    def test_cronograma_do_edital(self):
        self.assertEqual("2025-03-21", prazo_submissao(texto("ma_02_2025.txt")))  # "De 17/02/2025 até 21/03/2025"
        self.assertEqual("2025-06-13", prazo_submissao(texto("rr_004_2025.txt")))
        self.assertEqual("2025-03-28", prazo_submissao(texto("pe_07_2025.txt")))

    def test_retificacao_vale_o_leia_se(self):
        # "onde se lê … Até 13/06/2025 … leia-se … Até 30/06/2025"
        self.assertEqual("2025-06-30", prazo_submissao(texto("rr_ret1.txt")))
        self.assertEqual("2025-08-22", prazo_submissao(texto("to_ret001.txt")))

    def test_retificacao_que_so_mexe_no_resultado_nao_da_prazo(self):
        self.assertIsNone(prazo_submissao(texto("to_ret002.txt")))
        self.assertIsNone(prazo_submissao(texto("rs_aditivo1.txt")))

    def test_linha_de_documentos_nao_e_prazo_de_submissao(self):
        # FAPESB: "Data Limite para submissão … 23/05/2025 Data Limite para envio da proposta e
        # documentos complementares … 27/05/2025". Vale 23/05.
        self.assertEqual("2025-05-23", prazo_submissao(texto("ba_errata1.txt")))

    def test_numero_de_secao_nao_vira_data(self):
        # FAPERN: "conforme item 5.6.7.2. 19/12/2025 a 27/02/2026" não pode virar 07/02/2019.
        self.assertEqual("2026-02-27", prazo_submissao(texto("rn_25_2025.txt")))

    def test_intervalo_e_texto_colado_do_pdf(self):
        # FAPERO: "40 dias após Lançamento (29/04/2025 a 09/06/2025)"
        self.assertEqual("2025-06-09", prazo_submissao(texto("ro_1_2025.txt")))
        # Fundação Araucária: "Submissão eletrônica daspropostas … Até às 23:59h do dia 30/06/2025
        # 4.3 Conferência … Até dia 07/07/2025". Vale a primeira.
        self.assertEqual("2025-06-30", prazo_submissao(texto("pr_ato115.txt")))

    def test_item_de_secao_e_cronograma_do_projeto_nao_enganam(self):
        # FAPESC 09/2025: "à data de submissão … 3.1.12." não é 03/01/2012, e o "cronograma de
        # atividades aprovado" (do projeto) vem depois do quadro da chamada.
        self.assertEqual("2025-04-30", prazo_submissao(texto("sc_09_2025.txt")))

    def test_adendo_com_horario_entre_as_palavras(self):
        self.assertEqual("2025-01-31", prazo_submissao(texto("ce_759.txt")))

    def test_segunda_fase_da_fapesc_nao_reabre(self):
        trecho = ("CRONOGRAMA Etapas Datas Submissão das propostas aprovadas no SIGFAPESC (2ª fase) "
                  "25/08/2025 a 15/09/2025")
        self.assertIsNone(prazo_submissao(trecho))

    def test_resumo_da_fapemig_e_da_fapesp(self):
        self.assertEqual("2025-05-30", prazo_submissao("Limite para submissão: 30 de mai. de 2025"))
        self.assertEqual("2025-05-26", prazo_submissao("Data limite para submissão das propostas: 26/05/2025"))


class NumeroDoEdital(unittest.TestCase):
    def test_cabecalho(self):
        self.assertEqual((2025, 4), numero_do_cabecalho(texto("rr_004_2025.txt"), HOJE).chave)
        self.assertEqual((2025, 1), numero_do_cabecalho(texto("ro_1_2025.txt"), HOJE).chave)
        self.assertEqual((2024, 23869), numero_do_cabecalho(
            "CHAMADA PPSUS - PROGRAMA PESQUISA PARA O SUS: Gestão Compartilhada em Saúde/FAPESP Nº 023869/2024", HOJE,
        ).chave)
        self.assertEqual((2025, 2), numero_do_cabecalho("Edital N⁰ 02/2025 - CHAMADA PPSUS", HOJE).chave)

    def test_numero_da_retificacao_nao_e_o_do_edital(self):
        # "… FAPT 01/2025 RETIFICAÇÃO N. 002/2025": o edital é o 01/2025.
        self.assertEqual((2025, 1), numero_do_cabecalho(texto("to_ret002.txt"), HOJE).chave)
        # "FAPT 01/2025-8º EDIÇÃO … Convênio … TRANSFEREGOV nº 967717/2024"
        self.assertEqual((2025, 1), numero_do_cabecalho(texto("to_01_2025.txt"), HOJE).chave)

    def test_rotulo(self):
        self.assertEqual((2025, 2), numero_do_rotulo("Termo_Aditivo_001_Edital_002_2025_PPSUS", HOJE).chave)
        self.assertEqual((2025, 8), numero_do_rotulo("CP 08/25. PPSUS", HOJE).chave)
        self.assertIsNone(numero_do_rotulo("Ato DEFA 115/2025: Prorrogação no Cronograma", HOJE))
        self.assertIsNone(numero_do_rotulo("DO11817_30_04_2025-PPSUS-enq-final", HOJE))


class ClassificacaoEValores(unittest.TestCase):
    def test_classifica_documentos(self):
        self.assertEqual("edital", classificar("Edital_004_-_Chamada_Publica_PPSUS_8_Edicao_assinado"))
        self.assertEqual("retificacao", classificar("2ª Retificação Edital PPSUS Cronograma"))
        self.assertEqual("outro", classificar("RESULTADO FINAL DO JULGAMENTO DAS PROPOSTAS – CHAMADA PPSUS"))
        self.assertEqual("outro", classificar("Retificação dos Resultados Preliminares I e II – PPSUS"))
        # A chamada nacional CNPq 10/2025 (PPSUS Inovação) é do adaptador do CNPq.
        self.assertEqual("excluir", classificar("CHAMADA CNPq Nº 10/2025 - PPSUS INOVAÇÃO"))
        self.assertEqual("excluir", classificar("Edital PPSUS Inovação_Secom-RR (1)[1]"))
        self.assertEqual("excluir", classificar(
            "EDITAL FAPITEC/SE/FUNTEC Nº 02/2025 –CONCESSÃO DE BOLSAS DE INICIAÇÃO CIENTÍFICA … PPSUS"))
        self.assertEqual("excluir", classificar("Anexos do Edital.docx"))

    def test_valor_global(self):
        self.assertEqual(1_800_000, valor_global(texto("ma_02_2025.txt")))
        self.assertEqual(11_800_000, valor_global("Valor da chamada: R$ 11,8 milhões"))
        self.assertEqual(5_796_000, valor_global(
            "O total de recursos financeiros destinados a esta Chamada é de R$ 5.796.000,00"))

    def test_servico_de_saude_como_executora(self):
        self.assertTrue(aceita_servico_saude(texto("ba_03_2025.txt")))
        self.assertFalse(aceita_servico_saude(texto("ma_02_2025.txt")))  # "serviços de saúde local em geral"


class SentinelaSisct(unittest.TestCase):
    def test_vazio(self):
        self.assertEqual({}, PpsusAdapter.editais_sisct(texto("sisct_vazio.html"), "https://sisct.saude.gov.br/sisct/"))

    def test_lista_por_uf(self):
        editais = PpsusAdapter.editais_sisct(texto("sisct_2025_07_09.html"), "https://sisct.saude.gov.br/sisct/")
        self.assertEqual({"AP", "RJ", "TO"}, set(editais))
        self.assertIn("linkEdital", editais["AP"][0][1])

    def test_numero_do_sisct_nao_e_identidade(self):
        # O SISC&T dizia "EDITAL FAPERJ Nº 10/2025"; o edital da FAPERJ é o 11/2025.
        itens = adaptador("cenario_sisct_2025.json", hoje=date(2025, 7, 10), ufs=["RJ", "TO", "AP"]).collect()
        ids = {item["id"] for item in itens}
        self.assertIn("ppsus-rj-2025-11", ids)
        self.assertNotIn("ppsus-rj-2025-10", ids)


class Agrupamento(unittest.TestCase):
    def test_chamada_de_excedentes_fica_em_outra_edicao(self):
        linhas = json.loads(texto("ma_media.json"))
        docs = []
        for linha in linhas:
            arquivo = linha["source_url"].rsplit("/", 1)[-1]
            docs.append(Documento(
                titulo=linha["title"]["rendered"], url=linha["source_url"], data=linha["date"][:10],
                tipo=classificar(f"{linha['title']['rendered']} {arquivo}"),
                numero=numero_do_rotulo(f"{linha['title']['rendered']} {arquivo}", HOJE),
            ))
        grupos = agrupar("MA", docs)
        self.assertEqual(2, len(grupos))
        principal = next(g for g in grupos if g.numero and g.numero.chave == (2025, 2))
        self.assertTrue(any("RETIFICA" in d.titulo.upper() for d in principal.documentos))


class DiaDeHoje(unittest.TestCase):
    """14/10/2026: nada aberto em nenhuma UF, FAPERN fora do ar (eleição)."""

    @classmethod
    def setUpClass(cls):
        cls.a = adaptador("cenario_hoje.json")
        cls.snap = cls.a.snapshot()

    def test_nada_aberto_e_nada_especulativo(self):
        self.assertEqual([], self.snap["opportunities"])

    def test_fapern_503_vira_falha_so_do_rn(self):
        self.assertEqual(["RN"], self.snap["failed_scopes"])
        self.assertEqual("healthy", self.snap["status"])

    def test_orcamento_do_dia(self):
        # Sem robots.txt na fixture: no ar, soma-se um pedido por host (cerca de 25).
        self.assertLessEqual(self.snap["requests"], LIMITE_PEDIDOS_DIA - 25)
        por_uf = {uf: n for uf, n in self.a.pedidos_por_uf.items() if uf != "SISCT"}
        self.assertLessEqual(max(por_uf.values()), 2)
        self.assertNotIn("PB", por_uf)


class CargaHistorica(unittest.TestCase):
    """A 8ª edição inteira, encerrada, com o prazo lido do edital ou da retificação mais nova."""

    ESPERADO = {
        "ppsus-am-2025-2": "2025-05-12", "ppsus-ba-2025-3": "2025-05-23", "ppsus-ce-2024-5": "2025-01-31",
        "ppsus-df-2025-4": "2025-05-09", "ppsus-es-2024-17": "2024-12-05", "ppsus-ma-2025-2": "2025-03-26",
        "ppsus-mg-2025-6": "2025-05-30", "ppsus-mt-2025-4": "2025-03-28", "ppsus-pe-2025-7": "2025-04-17",
        "ppsus-pr-2025-8": "2025-06-30", "ppsus-rj-2025-11": "2025-07-15", "ppsus-rn-2025-25": "2026-02-27",
        "ppsus-ro-2025-1": "2025-06-09", "ppsus-rr-2025-4": "2025-06-30", "ppsus-rs-2025-1": "2025-05-19",
        "ppsus-se-2025-1": "2025-03-10", "ppsus-sp-2024-23869": "2025-05-26", "ppsus-to-2025-1": "2025-08-22",
    }

    @classmethod
    def setUpClass(cls):
        cls.a = adaptador("cenario_historico.json", historico=True)
        cls.snap = cls.a.snapshot()
        cls.itens = por_id(cls.snap["opportunities"])

    def test_ids_e_prazos(self):
        self.assertEqual(set(self.ESPERADO), set(self.itens))
        for item_id, prazo in self.ESPERADO.items():
            item = self.itens[item_id]
            self.assertEqual(prazo, item["dates"]["deadline"], item_id)
            self.assertEqual("closed", item["status"], item_id)
            self.assertEqual(prazo, item["dates"]["closed"], item_id)

    def test_identidade_estavel(self):
        for item in self.itens.values():
            self.assertRegex(item["id"], r"^ppsus-[a-z]{2}-20\d\d-\d+$")
            self.assertEqual(item["id"].removeprefix("ppsus-"), item["external_id"])
            self.assertNotIn("ppsus", item["external_id"])

    def test_desenho_do_item(self):
        ma = self.itens["ppsus-ma-2025-2"]
        self.assertEqual("ppsus", ma["source"]["id"])
        self.assertEqual("Decit/MS e FAPEMA", ma["funder"])
        self.assertEqual(["Decit/MS", "CNPq", "FAPEMA", "SES-MA"], ma["partners"])
        self.assertEqual(["MA"], ma["eligibility"]["geography"])
        self.assertEqual(["pesquisador", "ict", "ict_privada", "universidade"], ma["eligibility"]["organization_types"])
        self.assertEqual("auxilio_pesquisa", ma["instrument"]["type"])
        self.assertFalse(ma["instrument"]["repayable"])
        self.assertEqual(1_800_000, ma["funding"]["program_budget"])
        self.assertIn("pesquisa_sus", ma["themes"])
        self.assertIn("saude", ma["themes"])

    def test_hospital_so_onde_o_edital_aceita_servico_de_saude(self):
        self.assertIn("hospital", self.itens["ppsus-ba-2025-3"]["eligibility"]["organization_types"])
        self.assertNotIn("hospital", self.itens["ppsus-rr-2025-4"]["eligibility"]["organization_types"])

    def test_empresa_so_como_parceira(self):
        ba = self.itens["ppsus-ba-2025-3"]["eligibility"]
        self.assertEqual(["empresa"], ba["partner_types"])
        self.assertNotIn("empresa", ba["organization_types"])

    def test_tema_do_caso_de_terapia_celular(self):
        # FAPES 17/2024: "Estudos clínicos com utilização de células tronco…"
        self.assertIn("terapias_avancadas", self.itens["ppsus-es-2024-17"]["themes"])

    def test_chamada_de_excedentes_nao_vira_edital(self):
        self.assertEqual(1, sum(1 for i in self.itens if i.startswith("ppsus-ma-")))

    def test_sem_ppsus_inovacao_nem_pb(self):
        for item in self.itens.values():
            self.assertNotRegex(item["title"].lower(), r"inova[cç][aã]o")
            self.assertNotEqual(["PB"], item["eligibility"]["geography"])

    def test_rn_falha_mas_a_semente_entra(self):
        self.assertEqual(["RN"], self.snap["failed_scopes"])
        self.assertIn("ppsus-rn-2025-25", self.itens)

    def test_orcamento_historico(self):
        self.assertLessEqual(self.snap["requests"], 200 - 25)

    def test_catalogo_valida_no_contrato_22(self):
        catalogo = aggregate([self.snap], None, PORTFOLIOS, generated_at="2026-10-14T12:00:00+00:00")
        validate_catalog(catalogo, SCHEMA)
        self.assertEqual(18, catalogo["summary"]["monitored"])


class AbertoNoSisct(unittest.TestCase):
    """10/07/2025: o SISC&T listava RJ, TO e AP. AP só entra pelo PDF do próprio SISC&T."""

    @classmethod
    def setUpClass(cls):
        cls.itens = por_id(adaptador("cenario_sisct_2025.json", hoje=date(2025, 7, 10),
                                     ufs=["RJ", "TO", "AP"]).collect())

    def test_abertas_com_prazo_lido(self):
        self.assertEqual({"ppsus-rj-2025-11": "2025-07-15", "ppsus-to-2025-1": "2025-08-22",
                          "ppsus-ap-2025-4": "2025-07-25"},
                         {i: item["dates"]["deadline"] for i, item in self.itens.items()})
        self.assertTrue(all(item["status"] == "open" for item in self.itens.values()))

    def test_ap_aponta_o_sisct_e_nao_o_link_efemero(self):
        ap = self.itens["ppsus-ap-2025-4"]
        self.assertEqual("https://sisct.saude.gov.br/sisct/", ap["source"]["url"])
        self.assertTrue(all("linkEdital" not in d["url"] for d in ap["documents"]))
        self.assertEqual(["empresa"], ap["eligibility"]["partner_types"])


class FalhasIsoladas(unittest.TestCase):
    def test_bloqueio_para_so_aquela_casa(self):
        a = adaptador("cenario_historico.json", historico=True, ufs=["RR", "MA"],
                      extra={"https://faperr.rr.gov.br/robots.txt": {"status": 403}})
        snap = a.snapshot()
        self.assertEqual(["RR"], snap["failed_scopes"])
        self.assertEqual(["ppsus-ma-2025-2"], [i["id"] for i in snap["opportunities"]])

    def test_robots_que_proibe_vira_falha_sem_pedido(self):
        a = adaptador("cenario_historico.json", historico=True, ufs=["MA"],
                      extra={"https://www.fapema.br/robots.txt": {"texto": "User-agent: *\nDisallow: /\n"}})
        with self.assertRaises(FalhaUF):
            a.collect()  # a única UF pedida falhou: a coleta toda vira erro
        self.assertEqual(1, a.pedidos_por_uf["MA"])  # só o robots.txt

    def test_orcamento_esgotado_deixa_as_ufs_restantes_como_falha(self):
        a = adaptador("cenario_historico.json", historico=True, ufs=["MA", "RR", "CE"])
        a.limite = 6
        snap = a.snapshot()
        self.assertTrue(snap["failed_scopes"])
        self.assertLessEqual(snap["requests"], 6)

    def test_prazo_da_coleta_esgotado_publica_o_lido_e_marca_o_resto(self):
        # 1ª carga histórica no GitHub (14/10/2026): o job estourou os 15 min sem terminar. Com o
        # prazo, a coleta para no meio, o que já foi lido sai, e as UFs que faltam ficam como falha.
        a = adaptador("cenario_historico.json", historico=True, ufs=["MA", "RR", "CE"])
        relogio = iter([0.0] * 6)  # a coleta lê o relógio ~14 vezes: o prazo estoura no meio
        a._relogio = lambda: next(relogio, 10_000.0)
        a._inicio = 0.0
        snap = a.snapshot()
        self.assertTrue(snap["opportunities"], "o que foi lido antes do prazo é publicado")
        self.assertTrue(snap["failed_scopes"], "as UFs que faltaram ficam como falha parcial")
        self.assertTrue(any("prazo de" in aviso for aviso in snap["warnings"]))

    def test_retificacao_ilegivel_nao_promove_a_aberta(self):
        # PDF-imagem (a 1ª alteração da FAPESPA veio assim): com prazo futuro, fica "unknown".
        # Em 01/06/2025, com RR no SISC&T, o dia lê o edital e as duas retificações mais novas.
        sisct = texto("sisct_2025_07_09.html").replace("TO - CHAMADA PPSUS", "RR - CHAMADA PPSUS")
        a = adaptador("cenario_historico.json", hoje=date(2025, 6, 1), ufs=["RR"], extra={
            "https://sisct.saude.gov.br/sisct/": {"texto": sisct},
            "https://faperr.rr.gov.br/wp-json/wp/v2/media?search=ppsus&per_page=20"
            "&_fields=id,date,title,source_url,mime_type": {"arquivo": "rr_media.json"},
        })
        url = next(k for k in a.mapa if k.endswith("2a-Retificacao-Edital-PPSUS-Cronograma.pdf"))
        a.mapa[url] = {"texto": ""}
        item = a.collect()[0]
        self.assertEqual("ppsus-rr-2025-4", item["id"])
        self.assertEqual("unknown", item["status"])
        self.assertIsNone(item["dates"]["deadline"])

    def test_sisct_fora_do_ar_deixa_ap_e_mt_como_falha(self):
        a = adaptador("cenario_hoje.json", ufs=["AP", "MT", "RR"],
                      extra={"https://sisct.saude.gov.br/sisct/": {"status": 500}})
        snap = a.snapshot()
        self.assertEqual(["AP", "MT"], snap["failed_scopes"])
        self.assertFalse(a.sisct_ok)


class ParaibaFica(unittest.TestCase):
    """Decisão do supervisor (14/10/2026): o PPSUS da PB vem pelo adaptador fapesq."""

    def test_pb_fora_da_tabela(self):
        self.assertNotIn("PB", UFS)
        self.assertIn("PB", UFS_FORA)
        self.assertIn("PB", SIGLAS_UF)  # a sigla segue reconhecida no SISC&T

    def test_nenhum_item_da_pb(self):
        sisct = texto("sisct_2025_07_09.html").replace("RJ - EDITAL FAPERJ", "PB - EDITAL FAPESQ")
        a = adaptador("cenario_hoje.json", ufs=["PB", "MA"], extra={
            "https://sisct.saude.gov.br/sisct/": {"texto": sisct},
        })
        itens = a.collect()
        self.assertEqual(["MA"], a.ufs)
        self.assertFalse(any("PB" in i["eligibility"]["geography"] for i in itens))
        self.assertNotIn("PB", a.sisct_ufs.keys() & set(a.ufs))
        self.assertNotIn("PB", a.pedidos_por_uf)  # a FAPESQ não é lida


class Fixtures(unittest.TestCase):
    def test_sem_dado_pessoal_e_no_tamanho(self):
        for arquivo in FIXTURES.iterdir():
            conteudo = arquivo.read_text(encoding="utf-8")
            limite = 20_000 if arquivo.suffix == ".txt" else 60_000
            self.assertLessEqual(len(arquivo.read_bytes()), limite, arquivo.name)
            self.assertNotRegex(conteudo, r"\d{3}\.\d{3}\.\d{3}-\d{2}", arquivo.name)
            emails = [e for e in re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", conteudo)]
            self.assertEqual([], emails, arquivo.name)


if __name__ == "__main__":
    unittest.main()
