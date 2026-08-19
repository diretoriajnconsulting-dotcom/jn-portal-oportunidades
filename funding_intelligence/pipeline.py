from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import jsonschema

from funding_intelligence.matching import apply_matching
from funding_intelligence.models import ascii_fold, utc_now


TRACKED_FIELDS = ("title", "status", "dates.deadline", "funding.program_budget")


def _get(item: dict[str, Any], field: str) -> Any:
    value: Any = item
    for key in field.split("."):
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _fingerprint(item: dict[str, Any]) -> str:
    title = ascii_fold(item["title"])
    title = re.sub(r"\b(?:chamada|edital|publica|finep|cnpq|fapesq|mcti|fndct|n|no)\b", " ", title)
    title = re.sub(r"[^a-z0-9]+", " ", title)
    normalized = " ".join(title.split())
    raw = f"{normalized}|{item['dates'].get('deadline') or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _source_signature(items: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return sorted(
        (item["id"], *(_get(item, field) for field in TRACKED_FIELDS))
        for item in items
    )


def deduplicate(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for item in items:
        existing = by_id.get(item["id"])
        if not existing or len(item["documents"]) > len(existing["documents"]):
            by_id[item["id"]] = item

    # Cross-source co-publications: retain one canonical record, but never merge
    # records on title alone; the deadline must also be identical.
    by_fingerprint: dict[str, dict[str, Any]] = {}
    result: list[dict[str, Any]] = []
    for item in by_id.values():
        fingerprint = _fingerprint(item)
        existing = by_fingerprint.get(fingerprint)
        if not existing:
            by_fingerprint[fingerprint] = item
            result.append(item)
            continue
        if existing["source"]["id"] == item["source"]["id"]:
            # Source semantics win over lexical similarity. In TransfereGov,
            # for example, proposal and amendment windows must remain distinct.
            result.append(item)
            continue
        known_urls = {doc["url"] for doc in existing["documents"]}
        existing["documents"].extend(doc for doc in item["documents"] if doc["url"] not in known_urls)
    return result


def detect_changes(current: dict[str, Any], previous: dict[str, Any] | None, detected_at: str) -> None:
    if not previous:
        return
    current["changes"] = copy.deepcopy(previous.get("changes", []))
    for field in TRACKED_FIELDS:
        before, after = _get(previous, field), _get(current, field)
        if before == after:
            continue
        impact_days = None
        if field == "dates.deadline" and before and after:
            impact_days = (date.fromisoformat(after) - date.fromisoformat(before)).days
        change_documents = [doc for doc in current["documents"] if re.search(
            r"retifica|rerratifica|prorroga", ascii_fold(doc["title"])
        )]
        source_document = (
            change_documents[-1]["url"] if change_documents
            else current["documents"][-1]["url"] if current["documents"]
            else current["source"]["url"]
        )
        current["changes"].append({
            "detected_at": detected_at,
            "field": field,
            "before": before,
            "after": after,
            "impact_days": impact_days,
            "source_document": source_document,
        })


def aggregate(
    snapshots: list[dict[str, Any]], previous: dict[str, Any] | None,
    portfolios: list[dict[str, Any]], generated_at: str | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or utc_now()
    previous = previous or {"sources": [], "opportunities": []}
    previous_ops = {item["id"]: item for item in previous.get("opportunities", [])}
    previous_sources = {item["id"]: item for item in previous.get("sources", [])}
    all_items: list[dict[str, Any]] = []
    source_health = []

    for snapshot in snapshots:
        source = snapshot["source"]
        source_id = source["id"]
        prior_health = previous_sources.get(source_id, {})
        current_items = snapshot.get("opportunities", [])
        error = snapshot.get("error")
        if snapshot.get("status") == "healthy":
            last_success = snapshot["checked_at"]
            consecutive_errors = 0
            prior_items = [item for item in previous_ops.values() if item["source"]["id"] == source_id]
            last_change = (
                snapshot["checked_at"]
                if _source_signature(current_items) != _source_signature(prior_items)
                else prior_health.get("last_change_at")
            )
            status = "healthy"
        else:
            last_success = prior_health.get("last_success_at")
            last_change = prior_health.get("last_change_at")
            consecutive_errors = prior_health.get("consecutive_errors", 0) + 1
            status = "error"
            current_items = [copy.deepcopy(item) for item in previous_ops.values() if item["source"]["id"] == source_id]
            for item in current_items:
                item["source"]["stale"] = True
                item["source"]["checked_at"] = snapshot["checked_at"]

        all_items.extend(current_items)
        source_health.append({
            "id": source_id,
            "name": source["name"],
            "url": source["url"],
            "status": status,
            "checked_at": snapshot["checked_at"],
            "last_success_at": last_success,
            "last_change_at": last_change,
            "opportunity_count": len(current_items),
            "consecutive_errors": consecutive_errors,
            "error": str(error)[:500] if error else None,
        })

    items = deduplicate(all_items)
    for item in items:
        detect_changes(item, previous_ops.get(item["id"]), generated_at)
        apply_matching(item, portfolios)

    items.sort(key=lambda item: (
        item["status"] != "open",
        item["dates"].get("deadline") or "9999-12-31",
        -item["matching"]["ponte_score"],
        item["title"],
    ))
    prior_ids = set(previous_ops)
    now = datetime.fromisoformat(generated_at.replace("Z", "+00:00")).date()
    urgent = 0
    for item in items:
        deadline = item["dates"].get("deadline")
        if item["status"] == "open" and deadline and 0 <= (date.fromisoformat(deadline) - now).days <= 15:
            urgent += 1

    return {
        "version": "2.0",
        "generated_at": generated_at,
        "sources": sorted(source_health, key=lambda source: source["id"]),
        "summary": {
            "monitored": len(items),
            "open": sum(item["status"] == "open" for item in items),
            "urgent": urgent,
            "new": sum(item["id"] not in prior_ids for item in items),
            "changed": sum(bool(item["changes"] and item["changes"][-1]["detected_at"] == generated_at) for item in items),
            "priority_a": sum(item["matching"]["ponte_score"] >= 80 for item in items),
            "stale_sources": sum(source["status"] != "healthy" for source in source_health),
        },
        "opportunities": items,
    }


def validate_catalog(catalog: dict[str, Any], schema_path: str) -> None:
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    errors = sorted(validator.iter_errors(catalog), key=lambda error: list(error.absolute_path))
    if errors:
        details = "\n".join(f"{'.'.join(map(str, error.absolute_path))}: {error.message}" for error in errors[:20])
        raise ValueError(f"Catalog violates v2 schema:\n{details}")
