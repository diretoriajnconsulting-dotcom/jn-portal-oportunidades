"""Troca de identidade entre execuções.

A correção de 12/09/2026 tirou o prazo de dentro do id do TransfereGov. Na
primeira execução depois dela, TODOS os ids da fonte mudam de uma vez. Sem
tratamento, o radar reinseria os antigos como `closed` e marcava os novos como
`new` — um catálogo com o dobro de entradas e janelas fantasmas encerradas.
"""
import json
import unittest
from pathlib import Path

from funding_intelligence.models import opportunity
from funding_intelligence.pipeline import aggregate, validate_catalog

ROOT = Path(__file__).parents[1]
ANTES = "2026-09-11T12:00:00+00:00"
DEPOIS = "2026-09-12T12:00:00+00:00"


def janela(external_id, deadline="2026-10-30", title="Programa"):
    return opportunity(
        source_id="transferegov", source_name="TransfereGov",
        source_url="https://repositorio.dados.gov.br/seges/detru/",
        checked_at=DEPOIS, external_id=external_id, title=title,
        funder="Ministério", status="open", deadline=deadline,
        organization_types=["municipio"], geography=["PB"], themes=[],
        instrument_type="convenio", repayable=False,
    )


def snap(itens):
    return {
        "source": {"id": "transferegov", "name": "TransfereGov", "url": "https://repositorio.dados.gov.br/seges/detru/"},
        "checked_at": DEPOIS, "status": "healthy", "error": None, "opportunities": itens,
    }


class IdentityResetTests(unittest.TestCase):
    def setUp(self):
        self.portfolios = json.loads((ROOT / "config/portfolios.json").read_text(encoding="utf-8"))["portfolios"]

    def _anterior(self, ids):
        return aggregate([snap([janela(i) for i in ids])], None, self.portfolios, generated_at=ANTES)

    def test_troca_total_de_ids_nao_gera_fantasmas(self):
        anterior = self._anterior(["velho-1-proposta", "velho-2-proposta", "velho-3-emenda"])
        atual = aggregate(
            [snap([janela("novo-1-proposta"), janela("novo-2-proposta"), janela("novo-3-emenda")])],
            anterior, self.portfolios, generated_at=DEPOIS,
        )
        ops = atual["opportunities"]
        self.assertEqual(3, len(ops), "sem reinserir os ids antigos como closed")
        self.assertEqual({"baseline"}, {o["change_status"] for o in ops})
        self.assertEqual(0, atual["summary"]["new"], "nada de 'novas' falsas")
        self.assertEqual(0, atual["summary"]["closed"], "nada de encerradas fantasmas")
        self.assertEqual(3, atual["summary"]["monitored"])
        # O catálogo continua válido contra o schema oficial.
        validate_catalog(atual, str(ROOT / "schema/funding-opportunity-2.0.schema.json"))

    def test_encerramento_real_continua_sendo_publicado(self):
        # O controle: a regra é disjunção TOTAL, não queda de volume. Uma janela
        # que sai enquanto as outras ficam é encerramento de verdade.
        anterior = self._anterior(["a-proposta", "b-proposta", "c-proposta"])
        atual = aggregate(
            [snap([janela("a-proposta"), janela("b-proposta")])],
            anterior, self.portfolios, generated_at=DEPOIS,
        )
        fechadas = [o for o in atual["opportunities"] if o["change_status"] == "closed"]
        self.assertEqual(["transferegov-c-proposta"], [o["id"] for o in fechadas])
        self.assertEqual(1, atual["summary"]["closed"])

    def test_janela_nova_ao_lado_das_antigas_continua_nova(self):
        anterior = self._anterior(["a-proposta"])
        atual = aggregate(
            [snap([janela("a-proposta"), janela("b-proposta")])],
            anterior, self.portfolios, generated_at=DEPOIS,
        )
        status = {o["id"]: o["change_status"] for o in atual["opportunities"]}
        self.assertEqual("unchanged", status["transferegov-a-proposta"])
        self.assertEqual("new", status["transferegov-b-proposta"])

    def test_fonte_que_comeca_do_zero_nao_e_troca_de_identidade(self):
        # Sem ids anteriores não há o que trocar: tudo é novo mesmo.
        vazio = aggregate([snap([])], None, self.portfolios, generated_at=ANTES)
        atual = aggregate([snap([janela("a-proposta")])], vazio, self.portfolios, generated_at=DEPOIS)
        self.assertEqual("new", atual["opportunities"][0]["change_status"])


if __name__ == "__main__":
    unittest.main()
