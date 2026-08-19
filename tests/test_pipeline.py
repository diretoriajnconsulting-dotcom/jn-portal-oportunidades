import json
from pathlib import Path
import unittest

from funding_intelligence.models import opportunity
from funding_intelligence.pipeline import aggregate, validate_catalog


ROOT = Path(__file__).parents[1]
CHECKED = "2026-08-19T12:00:00+00:00"


def sample(deadline="2026-08-31"):
    return opportunity(
        source_id="finep", source_name="Finep", source_url="https://finep.gov.br/chamada/1",
        checked_at=CHECKED, external_id="1", title="Economia Circular",
        funder="Finep / FNDCT", status="open", deadline=deadline,
        organization_types=["empresa"], geography=["BR"],
        themes=["economia_circular", "sustentabilidade"],
        instrument_type="subvencao_economica", repayable=False,
    )


def snapshot(items, status="healthy", error=None):
    return {
        "source": {"id": "finep", "name": "Finep", "url": "https://finep.gov.br/chamadas"},
        "checked_at": CHECKED, "status": status, "error": error, "opportunities": items,
    }


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.portfolios = json.loads((ROOT / "config/portfolios.json").read_text(encoding="utf-8"))["portfolios"]

    def test_catalog_matches_and_validates(self):
        catalog = aggregate([snapshot([sample()])], None, self.portfolios, generated_at=CHECKED)
        self.assertEqual(1, catalog["summary"]["open"])
        self.assertGreaterEqual(catalog["opportunities"][0]["matching"]["ponte_score"], 80)
        validate_catalog(catalog, str(ROOT / "schema/funding-opportunity-2.0.schema.json"))

    def test_deadline_change_is_audited(self):
        previous = aggregate([snapshot([sample("2026-08-31")])], None, self.portfolios, generated_at="2026-08-18T12:00:00+00:00")
        current = aggregate([snapshot([sample("2026-09-15")])], previous, self.portfolios, generated_at=CHECKED)
        change = current["opportunities"][0]["changes"][-1]
        self.assertEqual("dates.deadline", change["field"])
        self.assertEqual(15, change["impact_days"])
        self.assertEqual(CHECKED, current["sources"][0]["last_change_at"])

    def test_source_failure_carries_previous_data_as_stale(self):
        previous = aggregate([snapshot([sample()])], None, self.portfolios, generated_at="2026-08-18T12:00:00+00:00")
        current = aggregate([snapshot([], status="error", error="timeout")], previous, self.portfolios, generated_at=CHECKED)
        self.assertTrue(current["opportunities"][0]["source"]["stale"])
        self.assertEqual("error", current["sources"][0]["status"])
        self.assertEqual(1, current["summary"]["stale_sources"])

    def test_same_source_semantic_channels_are_not_deduplicated(self):
        first = sample()
        second = sample()
        second["id"] = "finep-1-second-channel"
        catalog = aggregate([snapshot([first, second])], None, self.portfolios, generated_at=CHECKED)
        self.assertEqual(2, catalog["summary"]["monitored"])


if __name__ == "__main__":
    unittest.main()
