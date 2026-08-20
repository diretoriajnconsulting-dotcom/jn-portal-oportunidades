from datetime import date
from pathlib import Path
import unittest

from funding_intelligence.adapters.cnpq import CnpqAdapter
from funding_intelligence.adapters.fapesq import FapesqAdapter
from funding_intelligence.adapters.finep import FinepAdapter


FIXTURES = Path(__file__).parent / "fixtures"


class AdapterTests(unittest.TestCase):
    def test_finep_extracts_open_calls(self):
        items = FinepAdapter(fixture=str(FIXTURES / "finep.html"), today=date(2026, 8, 19)).collect()
        self.assertEqual(2, len(items))
        self.assertEqual("finep-779", items[0]["id"])
        self.assertEqual("2026-09-30", items[0]["dates"]["deadline"])
        self.assertEqual(["empresa"], items[0]["eligibility"]["organization_types"])
        self.assertEqual("open", items[0]["status"])

    def test_finep_maps_current_official_api(self):
        adapter = FinepAdapter(today=date(2026, 8, 19))
        items = adapter.parse_api_items([{
            "id": 1019381,
            "titulo": "5ª Chamada Pública Conjunta - Finep e RCN",
            "situacao": {"key": "aberta", "name": "Aberta"},
            "publicoAlvo": [{"key": "empresa1", "name": "Empresas"}],
            "tipoDeOportunidade": {"key": "naoReembolsavel", "name": "Não reembolsável"},
            "regiao": {"key": "todoBrasil", "name": "Todo Brasil"},
            "dataDePublicacao": "2026-08-05T00:00:00Z",
            "prazoProposto": "2026-09-09T13:00:00Z",
            "taxonomyCategoryBriefs": [{"taxonomyCategoryName": "Energia e Transição Sustentável"}],
            "tema": "Mobilidade e Logística",
            "descricao": "<p>Recursos de subvenção econômica.</p><a href='https://finep.gov.br/edital.pdf'>Edital</a>",
            "descricaoRawText": "Orçamento da chamada: R$ 12 milhões. Público-alvo: Empresas e ICTs.",
        }])
        self.assertEqual(1, len(items))
        self.assertEqual("finep-1019381", items[0]["id"])
        self.assertEqual(12_000_000, items[0]["funding"]["program_budget"])
        self.assertEqual("subvencao_economica", items[0]["instrument"]["type"])
        self.assertEqual("2026-09-09", items[0]["dates"]["deadline"])
        self.assertEqual("Edital", items[0]["documents"][0]["title"])

    def test_finep_discovers_public_frontend_client_without_committed_secret(self):
        javascript = 'const aa="client-test",bb="credential-test",cc=async()=>{const dd=btoa(`${aa}:${bb}`)'
        self.assertEqual(
            ("client-test", "credential-test"),
            FinepAdapter._extract_oauth_credentials(javascript),
        )

    def test_cnpq_extracts_number_and_submission_window(self):
        items = CnpqAdapter(fixture=str(FIXTURES / "cnpq.html"), today=date(2026, 8, 19)).collect()
        self.assertEqual(2, len(items))
        self.assertEqual("cnpq-24-2026", items[0]["id"])
        self.assertEqual("2026-09-18", items[0]["dates"]["deadline"])
        self.assertIn("biotecnologia", items[0]["themes"])

    def test_fapesq_groups_retifications_and_uses_latest_deadline(self):
        items = FapesqAdapter(fixture=str(FIXTURES / "fapesq.html"), today=date(2026, 8, 19)).collect()
        self.assertEqual(1, len(items))
        self.assertEqual("fapesq-26-2026", items[0]["id"])
        self.assertEqual(3, len(items[0]["documents"]))
        self.assertEqual("2026-09-15", items[0]["dates"]["deadline"])
        self.assertEqual("open", items[0]["status"])

    def test_fapesq_reads_pdf_table_with_damaged_accents(self):
        text = "20 a 31/07/2026 Per�odo de Inscri��o at� �s 17h"
        self.assertEqual("2026-07-31", FapesqAdapter._deadline(text))


if __name__ == "__main__":
    unittest.main()
