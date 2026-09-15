from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

from azcap import (
    FamilySummary,
    classify,
    diff_against,
    identifier_fingerprint,
    nonnegative_int,
    ordered_unique,
    region_scores,
    summarize,
    write_html,
)


def raw_sku(
    name: str,
    *,
    region: str = "eastus",
    family: str = "standardDv5Family",
    zones: list[str] | None = None,
    restrictions: list[dict] | None = None,
) -> dict:
    return {
        "region": region,
        "name": name,
        "family": family,
        "capabilities": {"vCPUs": "2", "MemoryGB": "8"},
        "zones": zones or [],
        "restrictions": restrictions or [],
    }


def restriction(kind: str, reason: str, *, zones: list[str] | None = None) -> dict:
    return {
        "type": kind,
        "reason": reason,
        "locations": ["eastus"],
        "zones": zones or [],
    }


def summary(family: str, *, assessed: int, score: float | None) -> FamilySummary:
    return FamilySummary(
        region="eastus",
        family=family,
        n_skus=assessed,
        n_available=assessed,
        n_zone_restricted=0,
        n_region_restricted=0,
        n_quota_blocked=0,
        n_assessable=assessed,
        score=score,
        zone_slots_total=0,
        zone_slots_blocked=0,
    )


def test_classify_capacity_and_quota_restrictions() -> None:
    quota = classify(raw_sku("quota", restrictions=[restriction("Location", "QuotaId")]))
    region = classify(raw_sku("region", restrictions=[restriction("Location", "NotAvailableForSubscription")]))
    zone = classify(
        raw_sku(
            "zone",
            zones=["1", "2", "3"],
            restrictions=[restriction("Zone", "NotAvailableForSubscription", zones=["1"])],
        )
    )

    assert (quota.status, quota.loss) == ("quota_blocked", 0.0)
    assert (region.status, region.loss) == ("region_restricted", 1.0)
    assert (zone.status, zone.loss, zone.zones_available) == ("zone_restricted", 0.333, ["2", "3"])


def test_all_quota_blocked_is_not_assessable() -> None:
    rows = [classify(raw_sku("quota", restrictions=[restriction("Location", "QuotaId")]))]
    result = summarize(rows)

    assert result[0].n_assessable == 0
    assert result[0].score is None
    assert region_scores(result)["eastus"] is None


def test_region_weighting_can_be_sku_or_family() -> None:
    summaries = [summary("large", assessed=10, score=100.0), summary("small", assessed=1, score=0.0)]

    assert region_scores(summaries, "sku")["eastus"] == 90.9
    assert region_scores(summaries, "family")["eastus"] == 50.0


def test_diff_reports_added_changed_and_removed_skus(tmp_path: Path) -> None:
    baseline = {
        "meta": {"subscription": "sub-a", "tenant_id": "tenant-a", "regions": ["eastus"]},
        "skus": [
            raw_sku(
                "changed",
                zones=["1", "2", "3"],
                restrictions=[restriction("Zone", "NotAvailableForSubscription", zones=["1"])],
            ),
            raw_sku("removed"),
        ],
    }
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    current = [
        classify(
            raw_sku(
                "changed",
                zones=["1", "2", "3"],
                restrictions=[restriction("Zone", "NotAvailableForSubscription", zones=["2"])],
            )
        ),
        classify(raw_sku("new")),
    ]

    changes, notes = diff_against(
        current,
        baseline_path,
        current_meta={"subscription": "sub-a", "tenant_id": "tenant-a", "regions": ["eastus"]},
        families=[],
        sku_glob=None,
        min_vcpu=0,
        exclude=[],
        zonal=False,
    )

    assert notes == []
    assert {change["sku"] for change in changes} == {"changed", "new", "removed"}
    assert next(c for c in changes if c["sku"] == "removed")["to"] == "(removed)"


def test_diff_rejects_incompatible_subscription_unless_overridden(tmp_path: Path) -> None:
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(
        json.dumps({"meta": {"subscription": "other", "regions": ["eastus"]}, "skus": []}),
        encoding="utf-8",
    )
    kwargs = {
        "current_meta": {"subscription": "current", "regions": ["eastus"]},
        "families": [],
        "sku_glob": None,
        "min_vcpu": 0,
        "exclude": [],
        "zonal": False,
    }

    with pytest.raises(ValueError, match="subscription differs"):
        diff_against([], baseline_path, **kwargs)

    _, notes = diff_against([], baseline_path, allow_incompatible=True, **kwargs)
    assert "subscription differs" in notes


def test_html_payload_cannot_close_its_script_block(tmp_path: Path) -> None:
    marker = "</script><script>globalThis.INJECTED=true</script>"
    write_html(
        tmp_path,
        [],
        [],
        [],
        [],
        {"regions": [], "region_weighting": "sku", "subscription_name": marker},
        [],
        {},
    )
    html = (tmp_path / "report.html").read_text(encoding="utf-8")

    assert marker not in html
    assert "\\u003c/script\\u003e" in html
    assert html.count("</script>") == 1


def test_cli_deduplicates_regions_controls_quota_and_redacts(tmp_path: Path) -> None:
    fixture = {
        "meta": {"subscription": "secret-sub", "subscription_name": "Secret Name", "tenant_id": "secret-tenant"},
        "locations": {"eastus": {"pair": None, "zonal": True}},
        "skus": [
            raw_sku(
                "Standard_D2_v5",
                zones=["1", "2", "3"],
                restrictions=[restriction("Location", "QuotaId")],
            )
        ],
        "quota": [
            {
                "region": "eastus",
                "family": "standardDv5Family",
                "localized": "Standard Dv5 Family vCPUs",
                "current": 2,
                "limit": 20,
            }
        ],
    }
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    without_quota = tmp_path / "without-quota"
    with_quota = tmp_path / "with-quota"
    common = [
        sys.executable,
        "-m",
        "azcap",
        "--regions",
        "eastus,eastus",
        "--no-pairs",
        "--fixture",
        str(fixture_path),
        "--redact-identifiers",
    ]

    subprocess.run([*common, "--out", str(without_quota)], check=True, capture_output=True, text=True)
    subprocess.run(
        [*common, "--include-quota", "--out", str(with_quota)],
        check=True,
        capture_output=True,
        text=True,
    )

    raw = json.loads((without_quota / "raw.json").read_text(encoding="utf-8"))
    html = (without_quota / "report.html").read_text(encoding="utf-8")
    summary_rows = list(csv.DictReader((without_quota / "summary.csv").open(encoding="utf-8", newline="")))
    assert raw["meta"]["regions"] == ["eastus"]
    assert raw["meta"]["subscription"] == identifier_fingerprint("secret-sub")
    assert raw["meta"]["subscription_name"] == ""
    assert "secret-sub" not in html and "Secret Name" not in html and "secret-tenant" not in html
    assert summary_rows[0]["n_assessable"] == "0" and summary_rows[0]["score"] == ""
    assert not (without_quota / "quota.csv").exists()
    assert (with_quota / "quota.csv").exists()

    (without_quota / "quota.csv").write_text("stale", encoding="utf-8")
    (without_quota / "pairs.csv").write_text("stale", encoding="utf-8")
    subprocess.run([*common, "--out", str(without_quota)], check=True, capture_output=True, text=True)
    assert not (without_quota / "quota.csv").exists()
    assert not (without_quota / "pairs.csv").exists()


def test_small_cli_helpers() -> None:
    assert ordered_unique(["a", "b", "a"]) == ["a", "b"]
    assert nonnegative_int("0") == 0
    with pytest.raises(Exception, match="zero or greater"):
        nonnegative_int("-1")


def test_cli_reports_regions_missing_from_fixture(tmp_path: Path) -> None:
    fixture = {"locations": {"eastus": {"pair": None, "zonal": True}}, "skus": [raw_sku("Standard_D2_v5")]}
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    common = [sys.executable, "-m", "azcap", "--no-pairs", "--fixture", str(fixture_path)]

    result = subprocess.run(
        [*common, "--regions", "eastus,nowhere", "--out", str(tmp_path / "out")],
        check=True,
        capture_output=True,
        text=True,
    )
    raw = json.loads((tmp_path / "out" / "raw.json").read_text(encoding="utf-8"))
    assert "nowhere: not in fixture" in result.stderr
    assert raw["meta"]["not_visible"] == ["nowhere"]
    assert raw["meta"]["regions"] == ["eastus"]

    failed = subprocess.run(
        [*common, "--regions", "nowhere", "--out", str(tmp_path / "out2")], capture_output=True, text=True
    )
    assert failed.returncode != 0
    assert "None of the requested regions" in failed.stderr
