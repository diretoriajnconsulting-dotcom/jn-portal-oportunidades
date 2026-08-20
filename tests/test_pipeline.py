import json
from pathlib import Path
import unittest

from funding_intelligence.matching import apply_matching
from funding_intelligence.models import opportunity
from funding_intelligence.pipeline import aggregate, deduplicate, validate_catalog


ROOT = Path(__file__).parents[1]
CHECKED = "2026-08-19T12:00:00+00:00"


def sample(deadline="2026-08-31", external_id="1", title="Economia Circular"):
    return opportunity(
        source_id="finep", source_name="Finep", source_url=f"https://finep.gov.br/chamada/{external_id}",
        checked_at=CHECKED, external_id=external_id, title=title,
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
        self.assertEqual("baseline", catalog["change_mode"])
        self.assertEqual(0, catalog["summary"]["new"])
        self.assertGreaterEqual(catalog["opportunities"][0]["matching"]["ponte_score"], 80)
        validate_catalog(catalog, str(ROOT / "schema/funding-opportunity-2.0.schema.json"))

    def test_deadline_change_is_audited(self):
        previous = aggregate([snapshot([sample("2026-08-31")])], None, self.portfolios, generated_at="2026-08-18T12:00:00+00:00")
        current = aggregate([snapshot([sample("2026-09-15")])], previous, self.portfolios, generated_at=CHECKED)
        change = current["opportunities"][0]["changes"][-1]
        self.assertEqual("dates.deadline", change["field"])
        self.assertEqual(15, change["impact_days"])
        self.assertEqual(CHECKED, current["sources"][0]["last_change_at"])

    def test_missing_source_artifact_preserves_previous_as_stale(self):
        previous = aggregate([snapshot([sample()])], None, self.portfolios, generated_at="2026-08-18T12:00:00+00:00")
        current = aggregate([
            snapshot([], status="error", error="source snapshot missing from workflow artifacts")
        ], previous, self.portfolios, generated_at=CHECKED)
        self.assertTrue(current["opportunities"][0]["source"]["stale"])
        self.assertEqual("unchanged", current["opportunities"][0]["change_status"])
        self.assertEqual("error", current["sources"][0]["status"])
        self.assertEqual(1, current["summary"]["stale_sources"])

    def test_upstream_stale_snapshot_publishes_current_data_with_warning(self):
        stale = snapshot([sample()], status="stale", error="upstream lag")
        catalog = aggregate([stale], None, self.portfolios, generated_at=CHECKED)
        self.assertEqual(1, catalog["summary"]["monitored"])
        self.assertEqual("stale", catalog["sources"][0]["status"])
        self.assertTrue(catalog["opportunities"][0]["source"]["stale"])

    def test_same_source_semantic_channels_are_not_deduplicated(self):
        first = sample()
        second = sample()
        second["id"] = "finep-1-second-channel"
        catalog = aggregate([snapshot([first, second])], None, self.portfolios, generated_at=CHECKED)
        self.assertEqual(2, catalog["summary"]["monitored"])

    def test_ineligible_organization_cannot_match(self):
        item = sample()
        municipal_only = [{
            "id": "municipal", "name": "Carteira Municipal",
            "organization_types": ["municipio"], "geography": ["BR"],
            "themes": ["economia_circular", "sustentabilidade"],
            "tags": ["clima"],
        }]
        apply_matching(item, municipal_only)
        self.assertFalse(item["matching"]["eligible"])
        self.assertEqual(0, item["matching"]["ponte_score"])
        self.assertEqual([], item["matching"]["matches"])

    def test_same_title_deadline_different_instruments_do_not_merge(self):
        first = sample()
        second = opportunity(
            source_id="cnpq", source_name="CNPq", source_url="https://cnpq.br/chamada/99",
            checked_at=CHECKED, external_id="99", title=first["title"],
            funder=first["funder"], status="open", deadline=first["dates"]["deadline"],
            organization_types=["empresa"], geography=["BR"], themes=first["themes"],
            instrument_type="bolsa", repayable=False,
        )
        self.assertEqual(2, len(deduplicate([first, second])))

    def test_initial_run_is_baseline_not_incremental(self):
        catalog = aggregate([snapshot([sample()])], None, self.portfolios, generated_at=CHECKED)
        self.assertEqual("baseline", catalog["change_mode"])
        self.assertEqual(0, catalog["summary"]["new"])
        self.assertEqual("baseline", catalog["opportunities"][0]["change_status"])

    def test_title_change_without_external_id_preserves_identity_or_is_flagged(self):
        common = {
            "source_id": "finep", "source_name": "Finep",
            "source_url": "https://finep.gov.br/chamada/sem-id-estavel",
            "checked_at": CHECKED, "external_id": None, "funder": "Finep",
            "status": "open", "deadline": "2026-09-30",
            "organization_types": ["empresa"], "geography": ["BR"],
            "themes": ["inovacao"], "instrument_type": "subvencao_economica",
            "repayable": False,
        }
        before = opportunity(title="Título original", **common)
        after = opportunity(title="Título retificado", **common)
        self.assertEqual(before["id"], after["id"])
        previous = aggregate([snapshot([before])], None, self.portfolios, generated_at="2026-08-18T12:00:00+00:00")
        current = aggregate([snapshot([after])], previous, self.portfolios, generated_at=CHECKED)
        self.assertEqual("changed", current["opportunities"][0]["change_status"])
        self.assertEqual("title", current["opportunities"][0]["changes"][-1]["field"])

    def test_incremental_run_classifies_new_closed_and_unchanged(self):
        previous = aggregate([
            snapshot([sample(external_id="1"), sample(external_id="2", title="Chamada encerrada")])
        ], None, self.portfolios, generated_at="2026-08-18T12:00:00+00:00")
        current = aggregate([
            snapshot([sample(external_id="1"), sample(external_id="3", title="Chamada nova")])
        ], previous, self.portfolios, generated_at=CHECKED)
        statuses = {item["id"]: item["change_status"] for item in current["opportunities"]}
        self.assertEqual("incremental", current["change_mode"])
        self.assertEqual("unchanged", statuses["finep-1"])
        self.assertEqual("new", statuses["finep-3"])
        self.assertEqual("closed", statuses["finep-2"])
        self.assertEqual(1, current["summary"]["new"])
        self.assertEqual(1, current["summary"]["closed"])
        self.assertEqual(1, current["summary"]["unchanged"])


if __name__ == "__main__":
    unittest.main()
