"""Contrato 2.2: encerradas por 24 meses, porta de fluxo contínuo e parceiras.

Decisões do titular de 14/10/2026: a encerrada fica no catálogo por 24 meses; o
produto sem edital (EMBRAPII, BNDES) entra como `fluxo_continuo`; e quem só
participa como parceira (a empresa no PPSUS, no PDIL de 2024 e na Chamada CNPq
30/2025) vai em `eligibility.partner_types`, para o filtro não esconder a porta.
"""
import json
from pathlib import Path
import unittest

from funding_intelligence.models import opportunity
from funding_intelligence.pipeline import aggregate, validate_catalog


ROOT = Path(__file__).parents[1]
SCHEMA = str(ROOT / "schema/funding-opportunity-2.2.schema.json")


def item(external_id="1", status="open", deadline="2026-08-31", **extra):
    return opportunity(
        source_id="finep", source_name="Finep", source_url=f"https://finep.gov.br/chamada/{external_id}",
        checked_at="2026-08-19T12:00:00+00:00", external_id=external_id, title=f"Chamada {external_id}",
        funder="Finep", status=status, deadline=deadline, organization_types=["empresa"],
        geography=["BR"], themes=["saude"], instrument_type="subvencao_economica", repayable=False,
        **extra,
    )


def snapshot(items, checked_at):
    return {
        "source": {"id": "finep", "name": "Finep", "url": "https://finep.gov.br/chamadas"},
        "checked_at": checked_at, "status": "healthy", "error": None, "opportunities": items,
    }


def rodada(items, previous, quando):
    return aggregate([snapshot(items, quando)], previous, PORTFOLIOS, generated_at=quando)


PORTFOLIOS = json.loads((ROOT / "config/portfolios.json").read_text(encoding="utf-8"))["portfolios"]


def por_id(catalog):
    return {op["id"]: op for op in catalog["opportunities"]}


class EncerradasPor24Meses(unittest.TestCase):
    def test_encerrada_fica_e_sai_depois_de_24_meses(self):
        r1 = rodada([item("1"), item("2")], None, "2026-08-18T12:00:00+00:00")
        r2 = rodada([item("1")], r1, "2026-08-19T12:00:00+00:00")
        encerrada = por_id(r2)["finep-2"]
        self.assertEqual("closed", encerrada["status"])
        self.assertEqual("closed", encerrada["change_status"])
        self.assertEqual("2026-08-19", encerrada["dates"]["closed"])

        # Antes de 14/10/2026 ela sumia aqui. Agora segue, sem virar "nova" nem "mudou".
        r3 = rodada([item("1")], r2, "2026-08-20T12:00:00+00:00")
        self.assertIn("finep-2", por_id(r3))
        self.assertEqual("unchanged", por_id(r3)["finep-2"]["change_status"])
        self.assertEqual("2026-08-19", por_id(r3)["finep-2"]["dates"]["closed"])

        # 730 dias depois de encerrar, ainda fica; no dia seguinte, sai.
        r4 = rodada([item("1")], r3, "2028-08-18T12:00:00+00:00")
        self.assertIn("finep-2", por_id(r4))
        r5 = rodada([item("1")], r4, "2028-08-19T12:00:00+00:00")
        self.assertNotIn("finep-2", por_id(r5))
        for catalog in (r2, r3, r4, r5):
            validate_catalog(catalog, SCHEMA)

    def test_encerrada_publicada_pela_fonte_conta_pelo_prazo(self):
        quando = "2026-10-14T12:00:00+00:00"
        recente = item("30-2025", status="closed", deadline="2026-02-23")
        antiga = item("9-2023", status="closed", deadline="2023-05-10")
        catalog = rodada([recente, antiga], None, quando)
        ops = por_id(catalog)
        self.assertIn("finep-30-2025", ops)
        self.assertEqual("2026-02-23", ops["finep-30-2025"]["dates"]["closed"])
        self.assertNotIn("finep-9-2023", ops)
        validate_catalog(catalog, SCHEMA)

    def test_encerrada_sem_prazo_nao_ganha_a_data_da_rodada(self):
        quando = "2026-10-14T12:00:00+00:00"
        sem_prazo = item("eventos-2025", status="closed", deadline=None)
        sem_prazo["dates"]["published"] = "2025-03-01"
        velha = item("eventos-2023", status="closed", deadline=None)
        velha["dates"]["published"] = "2023-03-01"
        catalog = rodada([sem_prazo, velha], None, quando)
        ops = por_id(catalog)
        self.assertNotIn("closed", ops["finep-eventos-2025"]["dates"])
        self.assertNotIn("finep-eventos-2023", ops, "sem prazo, a publicação conta para os 24 meses")
        validate_catalog(catalog, SCHEMA)

    def test_reaberta_perde_a_data_de_encerramento(self):
        r1 = rodada([item("1"), item("2")], None, "2026-08-18T12:00:00+00:00")
        r2 = rodada([item("1")], r1, "2026-08-19T12:00:00+00:00")
        r3 = rodada([item("1"), item("2", deadline="2026-12-31")], r2, "2026-08-20T12:00:00+00:00")
        reaberta = por_id(r3)["finep-2"]
        self.assertEqual("open", reaberta["status"])
        self.assertNotIn("closed", reaberta["dates"])
        self.assertEqual("changed", reaberta["change_status"])


class FluxoContinuoEParceiras(unittest.TestCase):
    def test_fluxo_continuo_conta_a_parte(self):
        porta = opportunity(
            source_id="embrapii", source_name="EMBRAPII", source_url="https://embrapii.org.br/",
            checked_at="2026-10-14T12:00:00+00:00", external_id="modelo-embrapii",
            title="Projeto de PD&I com uma Unidade EMBRAPII", funder="EMBRAPII", status="open",
            organization_types=["empresa", "startup"], geography=["BR"], themes=["inovacao"],
            instrument_type="contratacao_pdi", repayable=False, channel="fluxo_continuo",
        )
        quando = "2026-10-14T12:00:00+00:00"
        catalog = aggregate(
            [snapshot([item("1", deadline="2026-12-31")], quando),
             {"source": {"id": "embrapii", "name": "EMBRAPII", "url": "https://embrapii.org.br/"},
              "checked_at": quando, "status": "healthy", "error": None, "opportunities": [porta]}],
            None, PORTFOLIOS, generated_at=quando,
        )
        self.assertEqual(1, catalog["summary"]["open"])
        self.assertEqual(1, catalog["summary"]["continuous"])
        self.assertEqual("2.2", catalog["version"])
        validate_catalog(catalog, SCHEMA)

    def test_parceira_nao_repete_proponente_e_valida(self):
        chamada = opportunity(
            source_id="cnpq", source_name="CNPq", source_url="https://www.gov.br/cnpq/",
            checked_at="2026-10-14T12:00:00+00:00", external_id="30-2025",
            title="Chamada CNPq/MS-SCTIE-Decit nº 30/2025", funder="CNPq", status="closed",
            deadline="2026-02-23", organization_types=["ict", "ict_privada", "osc", "pesquisador"],
            partner_types=["empresa", "ict", "hospital"], partners=["Decit/MS", "Decit/MS", "  "],
            geography=["BR"], themes=["terapia celular"], instrument_type="outros", repayable=False,
        )
        self.assertEqual(["empresa", "hospital"], chamada["eligibility"]["partner_types"])
        self.assertEqual(["Decit/MS"], chamada["partners"])
        self.assertIn("terapias_avancadas", chamada["themes"])
        catalog = rodada([chamada], None, "2026-10-14T12:00:00+00:00")
        validate_catalog(catalog, SCHEMA)

    def test_sem_campos_novos_o_item_fica_igual_ao_21(self):
        simples = item("1")
        self.assertNotIn("partner_types", simples["eligibility"])
        self.assertNotIn("partners", simples)
        self.assertNotIn("closed", simples["dates"])


class FalhaParcial(unittest.TestCase):
    """Uma FAP fora do ar num dia não encerra os editais dela."""

    @staticmethod
    def edital(uf, numero, status="open", deadline="2026-12-31"):
        return opportunity(
            source_id="ppsus", source_name="PPSUS", source_url=f"https://fap.{uf.lower()}.br/{numero}",
            checked_at="2026-10-14T12:00:00+00:00", external_id=f"ppsus-{uf.lower()}-2026-{numero}",
            title=f"PPSUS {uf} {numero}/2026", funder="Decit/MS", status=status, deadline=deadline,
            organization_types=["pesquisador", "ict"], partner_types=["empresa"], geography=[uf],
            themes=["saude"], instrument_type="outros", repayable=False,
        )

    @staticmethod
    def coleta(items, quando, failed=None):
        snap = {
            "source": {"id": "ppsus", "name": "PPSUS", "url": "https://sisct.saude.gov.br/sisct/"},
            "checked_at": quando, "status": "healthy", "error": None, "opportunities": items,
        }
        if failed:
            snap["failed_scopes"] = failed
        return snap

    def test_uf_que_falhou_mantem_os_itens_como_defasados(self):
        r1 = aggregate([self.coleta([self.edital("PB", "1"), self.edital("CE", "5")], "2026-10-13T12:00:00+00:00")],
                       None, PORTFOLIOS, generated_at="2026-10-13T12:00:00+00:00")
        r2 = aggregate([self.coleta([self.edital("PB", "1")], "2026-10-14T12:00:00+00:00", failed=["CE"])],
                       r1, PORTFOLIOS, generated_at="2026-10-14T12:00:00+00:00")
        ce = por_id(r2)["ppsus-ppsus-ce-2026-5"]
        self.assertEqual("open", ce["status"])
        self.assertTrue(ce["source"]["stale"])
        self.assertEqual("unchanged", ce["change_status"])
        saude = {s["id"]: s for s in r2["sources"]}["ppsus"]
        self.assertEqual("sem resposta de: CE", saude["error"])
        validate_catalog(r2, SCHEMA)

        # Sem falha, a que sumiu da UF que respondeu encerra normalmente.
        r3 = aggregate([self.coleta([self.edital("CE", "5")], "2026-10-15T12:00:00+00:00")],
                       r2, PORTFOLIOS, generated_at="2026-10-15T12:00:00+00:00")
        self.assertEqual("closed", por_id(r3)["ppsus-ppsus-pb-2026-1"]["status"])


if __name__ == "__main__":
    unittest.main()
