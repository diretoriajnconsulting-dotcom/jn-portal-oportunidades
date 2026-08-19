from __future__ import annotations

from typing import Any


def apply_matching(opportunity: dict[str, Any], portfolios: list[dict[str, Any]]) -> None:
    matches = []
    opportunity_orgs = set(opportunity["eligibility"]["organization_types"])
    opportunity_geo = set(opportunity["eligibility"]["geography"])
    opportunity_themes = set(opportunity["themes"])

    for portfolio in portfolios:
        reasons = []
        org_overlap = opportunity_orgs & set(portfolio["organization_types"])
        geo_overlap = opportunity_geo & set(portfolio["geography"])
        theme_overlap = opportunity_themes & set(portfolio["themes"])

        eligibility_score = 40 if org_overlap else 0
        geography_score = 20 if geo_overlap or "BR" in opportunity_geo else 0
        theme_score = min(40, 15 * len(theme_overlap))
        score = eligibility_score + geography_score + theme_score

        if org_overlap:
            reasons.append("proponente: " + ", ".join(sorted(org_overlap)))
        if geo_overlap or "BR" in opportunity_geo:
            reasons.append("território compatível")
        if theme_overlap:
            reasons.append("temas: " + ", ".join(sorted(theme_overlap)))

        matches.append({
            "portfolio_id": portfolio["id"],
            "portfolio_name": portfolio["name"],
            "score": score,
            "reasons": reasons,
        })

    matches.sort(key=lambda item: (-item["score"], item["portfolio_id"]))
    relevant = [item for item in matches if item["score"] >= 40]
    top_score = matches[0]["score"] if matches else 0
    matching_tags = []
    for item in relevant:
        portfolio = next(p for p in portfolios if p["id"] == item["portfolio_id"])
        matching_tags.extend(portfolio.get("tags", []))
    opportunity["matching"] = {
        "ponte_score": top_score,
        "portfolio_tags": list(dict.fromkeys(matching_tags)),
        "matches": relevant,
    }
