"""Programas das APIs novas do TransfereGov: janela, filtro de programa antigo, canal, público e contrato."""

import json
import re
import urllib.error
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from funding_intelligence.adapters import ADAPTERS
from funding_intelligence.adapters import transferegov_apis as apis
from funding_intelligence.adapters.transferegov_apis import (
    EspeciaisAdapter, FundoAFundoAdapter, LeituraIncompleta, ParceriasAdapter, janela,
)
from funding_intelligence.pipeline import aggregate, validate_catalog

RAIZ = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"


def por_id(itens):
    return {item["id"]: item for item in itens}


class JanelaTests(unittest.TestCase):
    def test_aberta_agendada_encerrada_e_sem_data(self):
        hoje = date(2026, 9, 15)
        self.assertEqual("open", janela("2026-09-01", "2026-09-15", hoje))  # fim é inclusivo
        self.assertEqual("open", janela(None, "2026-12-31T00:00:00", hoje))
        self.assertEqual("scheduled", janela("2026-09-16", "2026-10-01", hoje))
        self.assertIsNone(janela("2026-09-01", "2026-09-14", hoje))
        self.assertIsNone(janela("2026-09-01", None, hoje))


class EspeciaisTests(unittest.TestCase):
    def test_so_janela_de_ciencia_aberta_ou_agendada(self):
        itens = por_id(EspeciaisAdapter(fixture=str(FIXTURES / "transferegov_especiais.json"), today=date(2026, 5, 10)).collect())
        self.assertEqual({"transferegov-especiais-programa-25", "transferegov-especiais-programa-26"}, set(itens))
        aberto = itens["transferegov-especiais-programa-25"]
        self.assertEqual("open", aberto["status"])
        self.assertEqual({"published": "2026-03-30", "deadline": "2026-05-15"}, aberto["dates"])
        self.assertEqual("emenda_parlamentar", aberto["channel"])
        self.assertEqual(["municipio", "estado"], aberto["eligibility"]["organization_types"])
        self.assertEqual("Transferências especiais 2026 · 1º ciclo · ciência e plano de ação", aberto["title"])
        self.assertEqual(6779708728.64, aberto["funding"]["program_budget"])
        agendado = itens["transferegov-especiais-programa-26"]
        self.assertEqual("scheduled", agendado["status"])
        self.assertIn("2º ciclo", agendado["title"])
        self.assertIsNone(agendado["funding"]["program_budget"])


class FundoAFundoTests(unittest.TestCase):
    def setUp(self):
        self.itens = por_id(FundoAFundoAdapter(
            fixture=str(FIXTURES / "transferegov_fundoafundo.json"), today=date(2026, 9, 15)).collect())

    def test_uma_oportunidade_por_janela_aberta_de_programa_recente_e_disponibilizado(self):
        self.assertEqual({
            "transferegov-fundoafundo-126-beneficiario-especifico",
            "transferegov-fundoafundo-130-voluntaria",
            "transferegov-fundoafundo-130-emenda-parlamentar",
        }, set(self.itens))

    def test_canal_status_valor_e_temas(self):
        especifico = self.itens["transferegov-fundoafundo-126-beneficiario-especifico"]
        self.assertEqual(("beneficiario_especifico", "open", "2028-12-31"),
                         (especifico["channel"], especifico["status"], especifico["dates"]["deadline"]))
        self.assertEqual("Ministério da Justiça e Segurança Pública", especifico["funder"])
        self.assertEqual("Apoiar ações de enfrentamento.", especifico["description"])
        voluntaria = self.itens["transferegov-fundoafundo-130-voluntaria"]
        self.assertEqual(("voluntaria", "open", 1200.5), (voluntaria["channel"], voluntaria["status"], voluntaria["funding"]["program_budget"]))
        self.assertEqual(["seguranca_hidrica", "saneamento", "cultura"], voluntaria["themes"])
        self.assertEqual("scheduled", self.itens["transferegov-fundoafundo-130-emenda-parlamentar"]["status"])


class ParceriasTests(unittest.TestCase):
    def setUp(self):
        self.itens = por_id(ParceriasAdapter(
            fixture=str(FIXTURES / "transferegov_parcerias.json"), today=date(2026, 9, 15)).collect())

    def test_filtra_situacao_e_ano_e_abre_uma_por_janela(self):
        self.assertEqual({
            "transferegov-parcerias-111-emenda-parlamentar",
            "transferegov-parcerias-150-voluntaria",
            "transferegov-parcerias-150-beneficiario-especifico",
            "transferegov-parcerias-151-voluntaria",
        }, set(self.itens))

    def test_publico_pelo_programa_ou_pelo_instrumento_e_territorio_pelas_ufs(self):
        suas = self.itens["transferegov-parcerias-111-emenda-parlamentar"]
        self.assertEqual((["municipio", "estado"], ["BR"]), (suas["eligibility"]["organization_types"], suas["eligibility"]["geography"]))
        pronon = self.itens["transferegov-parcerias-150-voluntaria"]
        self.assertEqual((["osc"], ["BR"]), (pronon["eligibility"]["organization_types"], pronon["eligibility"]["geography"]))
        self.assertIn("saude", pronon["themes"])
        consorcio = self.itens["transferegov-parcerias-151-voluntaria"]
        self.assertEqual((["consorcio_publico", "municipio"], ["PB"]),
                         (consorcio["eligibility"]["organization_types"], consorcio["eligibility"]["geography"]))
        self.assertEqual("scheduled", consorcio["status"])

    def test_abertura_com_erro_de_digitacao_na_fonte_fica_vazia(self):
        self.assertIsNone(self.itens["transferegov-parcerias-111-emenda-parlamentar"]["dates"]["published"])
        self.assertEqual("2026-08-01", self.itens["transferegov-parcerias-150-voluntaria"]["dates"]["published"])


class LeituraTests(unittest.TestCase):
    def paginas(self, *corpos):
        respostas = [json.dumps(c).encode() for c in corpos]
        adapter = ParceriasAdapter(today=date(2026, 9, 15))
        adapter.request_bytes = mock.Mock(side_effect=respostas)
        return adapter

    def test_le_todas_as_paginas(self):
        adapter = self.paginas({"data": [{"id_programa": 1}], "total_items": 2, "total_pages": 2},
                               {"data": [{"id_programa": 2}], "total_items": 2, "total_pages": 2})
        self.assertEqual([1, 2], [x["id_programa"] for x in adapter.ler_paginas()])
        self.assertIn("pagina=2", adapter.request_bytes.call_args_list[1].args[0])

    def test_total_divergente_ou_que_muda_falha(self):
        with self.assertRaisesRegex(LeituraIncompleta, "leu 1 de 3"):
            self.paginas({"data": [{"id_programa": 1}], "total_items": 3, "total_pages": 1}).ler_paginas()
        with self.assertRaisesRegex(LeituraIncompleta, "total mudou"):
            self.paginas({"data": [{"id_programa": 1}], "total_items": 2, "total_pages": 2},
                         {"data": [{"id_programa": 2}], "total_items": 3, "total_pages": 2}).ler_paginas()

    def test_espera_bloqueio_e_desiste_de_erro_de_chamada(self):
        adapter = ParceriasAdapter(today=date(2026, 9, 15))
        bloqueio = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
        adapter.request_bytes = mock.Mock(side_effect=[bloqueio, b'{"data": [], "total_items": 0, "total_pages": 0}'])
        with mock.patch.object(apis.time, "sleep") as dormir:
            self.assertEqual([], adapter.ler_paginas())
        dormir.assert_called_once_with(apis.ESPERAS_S[0])
        adapter.request_bytes = mock.Mock(side_effect=urllib.error.HTTPError("u", 422, "Unprocessable", {}, None))
        with mock.patch.object(apis.time, "sleep") as dormir, self.assertRaises(urllib.error.HTTPError):
            adapter.ler_paginas()
        dormir.assert_not_called()


class ContratoTests(unittest.TestCase):
    def test_catalogo_com_as_tres_fontes_valida_no_schema_2_1(self):
        snapshots = []
        for adapter, fixture, hoje in (
            (EspeciaisAdapter, "transferegov_especiais.json", date(2026, 5, 10)),
            (FundoAFundoAdapter, "transferegov_fundoafundo.json", date(2026, 9, 15)),
            (ParceriasAdapter, "transferegov_parcerias.json", date(2026, 9, 15)),
        ):
            snapshots.append(adapter(fixture=str(FIXTURES / fixture), today=hoje).snapshot())
        portfolios = json.loads((RAIZ / "config" / "portfolios.json").read_text(encoding="utf-8"))["portfolios"]
        catalog = aggregate(snapshots, None, portfolios, generated_at="2026-09-15T12:00:00+00:00")
        validate_catalog(catalog, str(RAIZ / "schema" / "funding-opportunity-2.1.schema.json"))
        ids = [item["id"] for item in catalog["opportunities"]]
        self.assertEqual(9, len(ids))
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(re.fullmatch(r"[a-z0-9-]+", i) for i in ids))

    def test_toda_fonte_registrada_esta_na_configuracao_e_no_workflow(self):
        fontes = {s["id"]: s for s in json.loads((RAIZ / "config" / "sources.json").read_text(encoding="utf-8"))["sources"]}
        workflow = (RAIZ / ".github" / "workflows" / "radar.yml").read_text(encoding="utf-8")
        matriz = re.search(r"source: \[([^\]]*)\]", workflow).group(1)
        coletadas = {x.strip() for x in matriz.split(",")} | {"transferegov"}
        for chave, classe in ADAPTERS.items():
            self.assertEqual(chave, classe.id)
            self.assertIn(chave, fontes)
            self.assertEqual((classe.name, classe.url, chave), (fontes[chave]["name"], fontes[chave]["url"], fontes[chave]["adapter"]))
            self.assertIn(chave, coletadas)


if __name__ == "__main__":
    unittest.main()
