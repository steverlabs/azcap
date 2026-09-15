from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from azcap import (
    EXIT_BLOCKED,
    PROBE_API_VERSION,
    FamilySummary,
    ProbeResult,
    Profile,
    ProfileVm,
    QuotaRow,
    RegionVerdict,
    classify,
    diff_against,
    identifier_fingerprint,
    load_profile,
    nonnegative_int,
    ordered_unique,
    pair_placement_notes,
    pair_verdict_notes,
    parse_need,
    parse_probe_response,
    parse_profile,
    probe_candidates,
    probe_once,
    probe_request_body,
    region_scores,
    run_probes,
    split_summary,
    summarize,
    workload_verdicts,
    write_html,
)

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "sample.json"
WAVE1 = Path(__file__).resolve().parent.parent / "fixtures" / "wave1.yaml"


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


# --------------------------------------------------------------------------- #
# Placement probes
# --------------------------------------------------------------------------- #


def probe(region: str, fulfillment: str | None = "None", *, error: str | None = None, **kw) -> ProbeResult:
    base = dict(
        region=region,
        sku="Standard_D8s_v5",
        count=12,
        zonal=True,
        spot=False,
        score=None if error else 7,
        fulfillment=None if error else fulfillment,
        split=[]
        if error
        else [{"name": "Standard_D8s_v5", "zone": z, "capacity": 4, "capacity_max": 4} for z in "123"],
        valid_until=None,
        error=error,
        skus=["Standard_D8s_v5"],
    )
    return ProbeResult(**{**base, **kw})


def test_parse_need_accepts_lists_and_rejects_bad_forms() -> None:
    assert parse_need("Standard_D8s_v5:12,Standard_E8s_v5:4") == [("Standard_D8s_v5", 12), ("Standard_E8s_v5", 4)]
    assert parse_need(" Standard_D8s_v5 : 1 ,") == [("Standard_D8s_v5", 1)]
    for bad in ("Standard_D8s_v5", "Standard_D8s_v5:", ":12", "Standard_D8s_v5:0", "Standard_D8s_v5:-1", "a b:2", ""):
        with pytest.raises(ValueError):
            parse_need(bad)
    with pytest.raises(ValueError, match="more than once"):
        parse_need("Standard_D8s_v5:1,standard_d8s_v5:2")


def test_probe_request_body_regular_spot_zonal_regional_and_mix() -> None:
    regional = probe_request_body([("Standard_D8s_v5", 12)])
    assert regional == {
        "capacityProfile": {
            "capacity": 12,
            "capacityType": "VM",
            "priority": "Regular",
            "allocationStrategy": "Prioritized",
            "osType": "Linux",
        },
        "instanceDescription": {"vmSizes": [{"name": "Standard_D8s_v5", "rank": 0}]},
    }
    assert "zones" not in regional

    zonal = probe_request_body([("Standard_D8s_v5", 12)], zones=["1", "3"], os_type="Windows")
    assert zonal["zones"] == ["1", "3"] and zonal["capacityProfile"]["osType"] == "Windows"

    spot = probe_request_body([("Standard_D8s_v5", 12)], spot=True)
    assert spot["capacityProfile"]["priority"] == "Spot"
    assert spot["capacityProfile"]["spotPriorityProfile"] == {"maxPricePerVm": -1}

    mix = probe_request_body([("Standard_E8s_v5", 4), ("Standard_D8s_v5", 12)])
    assert mix["capacityProfile"]["capacity"] == 16
    assert mix["instanceDescription"]["vmSizes"] == [
        {"name": "Standard_E8s_v5", "rank": 0},
        {"name": "Standard_D8s_v5", "rank": 1},
    ]


def test_parse_probe_response_picks_best_choice_and_tolerates_bad_bodies() -> None:
    body = {
        "id": "abc",
        "placementChoices": [
            {"score": 4, "skuSplit": [{"name": "Standard_D8s_v5", "capacity": 6, "zone": "1"}]},
            {
                "score": 8,
                "skuSplit": [
                    {"name": "Standard_D8s_v5", "priority": "Regular", "capacity": 4, "capacityMax": 6, "zone": "1"},
                    {"name": "Standard_D8s_v5", "priority": "Regular", "capacity": 4, "zone": "2"},
                    "garbage",
                ],
            },
        ],
        "partialFulfillmentReason": "None",
        "validUntil": "2026-09-05T18:00:00Z",
    }
    parsed = parse_probe_response(body)
    assert parsed["score"] == 8 and parsed["fulfillment"] == "None" and parsed["error"] is None
    assert parsed["valid_until"] == "2026-09-05T18:00:00Z"
    assert parsed["split"] == [
        {"name": "Standard_D8s_v5", "zone": "1", "capacity": 4, "capacity_max": 6},
        {"name": "Standard_D8s_v5", "zone": "2", "capacity": 4, "capacity_max": 4},
    ]

    for reason in ("InsufficientCapacity", "InsufficientQuota"):
        parsed = parse_probe_response(
            {"placementChoices": [{"score": 2, "skuSplit": []}], "partialFulfillmentReason": reason}
        )
        assert (parsed["score"], parsed["fulfillment"]) == (2, reason)

    empty = parse_probe_response({"placementChoices": [], "partialFulfillmentReason": "InsufficientCapacity"})
    assert (empty["score"], empty["fulfillment"], empty["split"], empty["error"]) == (
        None,
        "InsufficientCapacity",
        [],
        None,
    )

    for bad in (None, [], "text", {"placementChoices": "nope"}, 42):
        parsed = parse_probe_response(bad)
        assert parsed["error"] and parsed["score"] is None and parsed["split"] == []


def test_probe_once_handles_http_errors_retries_once_on_429_and_never_raises() -> None:
    calls: list[tuple[str, dict]] = []
    slept: list[int] = []
    responses = iter(
        [
            (429, {"Retry-After": "3"}, ""),
            (
                200,
                {},
                json.dumps({"placementChoices": [{"score": 5, "skuSplit": []}], "partialFulfillmentReason": "None"}),
            ),
            (
                404,
                {},
                json.dumps(
                    {
                        "error": {
                            "code": "NoRegisteredProviderFound",
                            "message": "No registered  resource\nprovider found",
                        }
                    }
                ),
            ),
            (200, {}, "<html>not json</html>"),
            (403, {}, ""),
        ]
    )

    def post(url: str, body: dict):
        calls.append((url, body))
        return next(responses)

    ok = probe_once(post, "u", {}, sleep=slept.append)
    assert ok["score"] == 5 and ok["error"] is None and slept == [3] and len(calls) == 2
    assert probe_once(post, "u", {}, sleep=slept.append)["error"] == "404 No registered resource provider found"
    assert probe_once(post, "u", {}, sleep=slept.append)["error"] == "200 response body is not JSON"
    assert probe_once(post, "u", {}, sleep=slept.append)["error"] == "403"

    quota_hit = json.dumps(
        {
            "error": {
                "code": "OperationNotAllowed",
                "message": "vCPU quota for standardDSv5Family has reached its limit.",
            }
        }
    )
    verdict = probe_once(lambda u, b: (409, {}, quota_hit), "u", {})
    assert (verdict["fulfillment"], verdict["detail"], verdict["score"], verdict["error"]) == (
        "InsufficientQuota",
        "standardDSv5Family",
        None,
        None,
    )
    other_409 = probe_once(lambda u, b: (409, {}, json.dumps({"error": {"message": "Conflict"}})), "u", {})
    assert other_409["error"] == "409 Conflict" and other_409["fulfillment"] is None

    def boom(url: str, body: dict):
        raise ConnectionError("refused")

    failed = probe_once(boom, "u", {}, sleep=slept.append)
    assert failed["error"] == "request failed: ConnectionError: refused" and failed["score"] is None


def test_run_probes_sends_one_request_per_region_and_sku_with_the_regions_zones() -> None:
    raw = [
        raw_sku("Standard_D8s_v5", region="eastus", zones=["1", "2", "3"]),
        raw_sku("Standard_E8s_v5", region="eastus", zones=["2", "3"]),
        raw_sku("Standard_D8s_v5", region="westus"),
    ]
    locs = {"eastus": {"zonal": True}, "westus": {"zonal": False}}
    seen: list[tuple[str, dict]] = []

    def post(url: str, body: dict):
        seen.append((url, body))
        if "westus" in url:
            return 400, {}, json.dumps({"error": {"message": "bad region"}})
        return (
            200,
            {},
            json.dumps({"placementChoices": [{"score": 9, "skuSplit": []}], "partialFulfillmentReason": "None"}),
        )

    needs = [("Standard_D8s_v5", 12), ("Standard_E8s_v5", 4)]
    common = dict(os_type="Linux", spot=False, zonal=True, raw=raw, locs=locs, sleep=lambda _: None)
    results = run_probes(post, "sub", ["eastus", "westus"], needs, mix=False, **common)

    assert [(r.region, r.sku, r.count, r.zonal) for r in results] == [
        ("eastus", "Standard_D8s_v5", 12, True),
        ("eastus", "Standard_E8s_v5", 4, True),
        ("westus", "Standard_D8s_v5", 12, False),
        ("westus", "Standard_E8s_v5", 4, False),
    ]
    assert seen[0][0].endswith(
        f"/locations/eastus/skuMixPlacementScores/recommendations/generate?api-version={PROBE_API_VERSION}"
    )
    assert seen[0][1]["zones"] == ["1", "2", "3"] and seen[1][1]["zones"] == ["2", "3"]
    assert "zones" not in seen[2][1]
    assert results[2].error == "400 bad region" and results[0].score == 9

    seen.clear()
    mixed = run_probes(post, "sub", ["eastus"], needs, mix=True, **common)
    assert len(seen) == 1 and seen[0][1]["capacityProfile"]["capacity"] == 16
    assert (mixed[0].sku, mixed[0].skus, mixed[0].count) == ("mix", ["Standard_D8s_v5", "Standard_E8s_v5"], 16)

    seen.clear()
    run_probes(post, "sub", ["eastus"], needs[:1], mix=False, api_version="2099-01-01-preview", **common)
    assert seen[0][0].endswith("?api-version=2099-01-01-preview")


def test_run_probes_answers_from_quota_before_calling_the_api() -> None:
    raw = [
        dict(raw_sku("Standard_D8s_v5", family="standardDSv5Family"), capabilities={"vCPUs": "8", "MemoryGB": "32"}),
        dict(raw_sku("Standard_E8s_v5", family="standardESv5Family"), capabilities={"vCPUs": "8", "MemoryGB": "64"}),
        dict(raw_sku("Standard_F8s_v2", family="standardFSv2Family"), capabilities={"vCPUs": "8", "MemoryGB": "16"}),
    ]
    quota = [
        QuotaRow("eastus", "standardDSv5Family", "Standard DSv5 Family vCPUs", 0, 0),
        QuotaRow("eastus", "standardESv5Family", "Standard ESv5 Family vCPUs", 40, 100),
        QuotaRow("eastus", "standardFSv2Family", "Standard FSv2 Family vCPUs", 0, 100),
    ]
    called: list[dict] = []

    def post(url: str, body: dict):
        called.append(body)
        return (
            200,
            {},
            json.dumps({"placementChoices": [{"score": 9, "skuSplit": []}], "partialFulfillmentReason": "None"}),
        )

    needs = [("Standard_D8s_v5", 12), ("Standard_E8s_v5", 8), ("Standard_F8s_v2", 12), ("Standard_X1", 1)]
    common = dict(os_type="Linux", spot=False, zonal=False, raw=raw, locs={}, quota=quota, sleep=lambda _: None)
    results = run_probes(post, "sub", ["eastus"], needs, mix=False, **common)

    assert [(r.sku, r.fulfillment, r.detail, r.score) for r in results] == [
        ("Standard_D8s_v5", "InsufficientQuota", "limit 0, used 0, need 96 vCPU", None),
        ("Standard_E8s_v5", "InsufficientQuota", "limit 100, used 40, need 64 vCPU", None),
        ("Standard_F8s_v2", "None", None, 9),
        ("Standard_X1", "None", None, 9),  # unknown SKU: nothing to check against, so the API is asked
    ]
    assert all(r.error is None for r in results)
    assert [b["instanceDescription"]["vmSizes"][0]["name"] for b in called] == ["Standard_F8s_v2", "Standard_X1"]

    called.clear()
    mixed = run_probes(post, "sub", ["eastus"], needs[1:3], mix=True, **common)
    assert mixed[0].detail == "standardESv5Family: limit 100, used 40, need 64 vCPU" and not called

    # without quota data nothing is pre-checked
    results = run_probes(post, "sub", ["eastus"], needs[:1], mix=False, **dict(common, quota=None))
    assert results[0].score == 9 and results[0].detail is None


def test_split_summary_and_pair_placement_notes() -> None:
    assert split_summary(probe("eastus")) == "1:4 2:4 3:4"
    regional = probe(
        "westus", zonal=False, split=[{"name": "Standard_D8s_v5", "zone": None, "capacity": 8, "capacity_max": 12}]
    )
    assert split_summary(regional) == "8-12"
    mix = probe(
        "eastus",
        sku="mix",
        skus=["Standard_D8s_v5", "Standard_E8s_v5"],
        split=[{"name": "Standard_D8s_v5", "zone": "1", "capacity": 8, "capacity_max": 8}],
    )
    assert split_summary(mix) == "Standard_D8s_v5@1:8"

    pairs = [
        {"region": "a", "pair": "b"},
        {"region": "c", "pair": "d"},
        {"region": "e", "pair": "f"},
        {"region": "g", "pair": None},
        {"region": "h", "pair": "i"},
        {"region": "j", "pair": "k"},
        {"region": "eastus2", "pair": "m"},
        {"region": "n", "pair": "o"},
        {"region": "p", "pair": "q"},
    ]
    probes = [
        probe("a", "InsufficientCapacity"),
        probe("b", "None"),
        probe("c", "InsufficientCapacity"),
        probe("d", "InsufficientCapacity"),
        probe("e", "None"),
        probe("f", "InsufficientCapacity"),
        probe("g", "InsufficientCapacity"),
        probe("h", "InsufficientCapacity"),
        probe("i", error="404 nope"),
        probe("j", "InsufficientQuota"),
        probe("k", "InsufficientQuota"),
        probe("eastus2", "InsufficientQuota"),
        probe("m", "None"),
        probe("n", "InsufficientCapacity"),  # capacity here, quota there: quota wins the wording
        probe("o", "InsufficientQuota"),
        probe("p", "None"),
        probe("q", "None"),
    ]
    pair_placement_notes(pairs, probes)
    assert [p["placement_notes"] for p in pairs] == [
        ["pair can place Standard_D8s_v5"],
        ["neither side can place Standard_D8s_v5"],
        ["pair cannot place Standard_D8s_v5"],
        [],
        [],
        ["quota blocks Standard_D8s_v5 on both sides"],
        ["quota blocks Standard_D8s_v5 on eastus2"],
        ["quota blocks Standard_D8s_v5 on the pair"],
        [],
    ]


def test_fixture_probes_render_report_section_and_csv(tmp_path: Path) -> None:
    fixture = {
        "locations": {"eastus": {"pair": None, "zonal": True}},
        "skus": [raw_sku("Standard_D8s_v5", zones=["1", "2", "3"])],
    }
    common = [sys.executable, "-m", "azcap", "--regions", "eastus", "--no-pairs"]

    without = tmp_path / "without"
    fixture_path = tmp_path / "plain.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    result = subprocess.run(
        [*common, "--fixture", str(fixture_path), "--need", "Standard_D8s_v5:12", "--out", str(without)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "has no 'probes' list" in result.stderr
    assert "Placement probes" not in result.stdout
    assert not (without / "probes.csv").exists()
    html = (without / "report.html").read_text(encoding="utf-8")
    assert '"probes": []' in html

    marker = "</script><script>globalThis.INJECTED=true</script>"
    fixture["probes"] = [
        asdict_probe(probe("eastus", "InsufficientCapacity", score=2)),
        asdict_probe(
            probe(
                "eastus", "InsufficientQuota", score=None, split=[], detail="standardDSv5Family", sku="Standard_D16s_v5"
            )
        ),
        asdict_probe(probe("eastus", error=f"404 {marker}", sku="Standard_E8s_v5", count=4)),
        asdict_probe(probe("westus")),  # not in scope: dropped
    ]
    fixture_path = tmp_path / "probes.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    with_probes = tmp_path / "with"
    result = subprocess.run(
        [*common, "--fixture", str(fixture_path), "--out", str(with_probes)], check=True, capture_output=True, text=True
    )
    assert "Placement probes" in result.stdout
    assert "insufficient capacity" in result.stdout and "Standard_E8s_v5 x4" in result.stdout
    rows = list(csv.DictReader((with_probes / "probes.csv").open(encoding="utf-8", newline="")))
    assert "insufficient quota: standardDSv5Family" in result.stdout
    assert [(r["region"], r["sku"], r["score"], r["fulfillment"], r["detail"], r["split_summary"]) for r in rows] == [
        ("eastus", "Standard_D8s_v5", "2", "InsufficientCapacity", "", "1:4 2:4 3:4"),
        ("eastus", "Standard_D16s_v5", "", "InsufficientQuota", "standardDSv5Family", ""),
        ("eastus", "Standard_E8s_v5", "", "", "", ""),
    ]
    assert rows[2]["error"].startswith("404 ")
    raw = json.loads((with_probes / "raw.json").read_text(encoding="utf-8"))
    assert [p["region"] for p in raw["probes"]] == ["eastus", "eastus", "eastus"]
    assert raw["meta"]["probe_api_version"] is None
    html = (with_probes / "report.html").read_text(encoding="utf-8")
    assert marker not in html and html.count("</script>") == 1
    assert '"fulfillment": "InsufficientCapacity"' in html

    (with_probes / "probes.csv").write_text("stale", encoding="utf-8")
    subprocess.run(
        [*common, "--fixture", str(tmp_path / "plain.json"), "--out", str(with_probes)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert not (with_probes / "probes.csv").exists()


def asdict_probe(p: ProbeResult) -> dict:
    return json.loads(json.dumps(p.__dict__))


def test_cli_rejects_bad_need_and_mix_without_need(tmp_path: Path) -> None:
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps({"skus": [raw_sku("Standard_D2_v5")]}), encoding="utf-8")
    common = [
        sys.executable,
        "-m",
        "azcap",
        "--regions",
        "eastus",
        "--fixture",
        str(fixture_path),
        "--out",
        str(tmp_path),
    ]
    bad = subprocess.run([*common, "--need", "Standard_D8s_v5"], capture_output=True, text=True)
    assert bad.returncode == 2 and "SKU:COUNT" in bad.stderr
    bad = subprocess.run([*common, "--need-mix"], capture_output=True, text=True)
    assert bad.returncode == 2 and "--need-mix requires --need" in bad.stderr


# --------------------------------------------------------------------------- #
# Workload profiles and verdicts
# --------------------------------------------------------------------------- #

VALID_PROFILE = {
    "name": "wave1",
    "os": "Windows",
    "zonal": True,
    "vms": [{"sku": "Standard_D8s_v5", "count": 12}, {"sku": "Standard_E8s_v5", "count": 4, "optional": True}],
}


def test_parse_profile_accepts_yaml_and_json_and_defaults(tmp_path: Path) -> None:
    yaml_path = tmp_path / "wave1.yaml"
    yaml_path.write_text(
        "name: wave1\nos: Windows\nzonal: true\nvms:\n  - sku: Standard_D8s_v5\n    count: 12\n"
        "  - sku: Standard_E8s_v5\n    count: 4\n    optional: true\n",
        encoding="utf-8",
    )
    json_path = tmp_path / "wave1.json"
    json_path.write_text(json.dumps(VALID_PROFILE), encoding="utf-8")
    expected = Profile(
        name="wave1",
        os="Windows",
        zonal=True,
        vms=[ProfileVm("Standard_D8s_v5", 12, False), ProfileVm("Standard_E8s_v5", 4, True)],
    )

    assert load_profile(yaml_path) == expected
    assert load_profile(json_path) == expected
    assert load_profile(WAVE1).name == "wave1" and load_profile(WAVE1).zonal is True
    assert parse_profile({"name": "min", "vms": [{"sku": "Standard_D2_v5", "count": 1}]}) == Profile(
        name="min", os="Linux", zonal=False, vms=[ProfileVm("Standard_D2_v5", 1, False)]
    )


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (["not", "a", "mapping"], "must be a mapping"),
        ({**VALID_PROFILE, "region": "eastus"}, "unknown key(s): region"),
        ({k: v for k, v in VALID_PROFILE.items() if k != "name"}, "'name' is required"),
        ({**VALID_PROFILE, "name": ""}, "'name' is required"),
        ({**VALID_PROFILE, "os": "BSD"}, "'os' must be Linux or Windows"),
        ({**VALID_PROFILE, "zonal": "yes"}, "'zonal' must be true or false"),
        ({k: v for k, v in VALID_PROFILE.items() if k != "vms"}, "'vms' must be a non-empty list"),
        ({**VALID_PROFILE, "vms": []}, "'vms' must be a non-empty list"),
        ({**VALID_PROFILE, "vms": ["Standard_D8s_v5"]}, "vms[1] must be a mapping"),
        (
            {**VALID_PROFILE, "vms": [{"sku": "Standard_D8s_v5", "count": 1, "zone": "1"}]},
            "vms[1] has unknown key(s): zone",
        ),
        ({**VALID_PROFILE, "vms": [{"count": 1}]}, "vms[1] 'sku' is required"),
        ({**VALID_PROFILE, "vms": [{"sku": "Standard D8", "count": 1}]}, "vms[1] 'sku' is required"),
        ({**VALID_PROFILE, "vms": [{"sku": "Standard_D8s_v5"}]}, "'count' must be an integer of 1 or more"),
        ({**VALID_PROFILE, "vms": [{"sku": "Standard_D8s_v5", "count": 0}]}, "'count' must be an integer of 1 or more"),
        ({**VALID_PROFILE, "vms": [{"sku": "Standard_D8s_v5", "count": "12"}]}, "'count' must be an integer"),
        ({**VALID_PROFILE, "vms": [{"sku": "Standard_D8s_v5", "count": True}]}, "'count' must be an integer"),
        ({**VALID_PROFILE, "vms": [{"sku": "Standard_D8s_v5", "count": 1, "optional": 1}]}, "'optional' must be true"),
        (
            {**VALID_PROFILE, "vms": [{"sku": "Standard_D8s_v5", "count": 1}, {"sku": "standard_d8s_v5", "count": 2}]},
            "more than once",
        ),
    ],
)
def test_parse_profile_rejects_bad_profiles(data: object, message: str) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        parse_profile(data)


def test_load_profile_rejects_bad_files(tmp_path: Path) -> None:
    (tmp_path / "p.toml").write_text("name = 'x'", encoding="utf-8")
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    (tmp_path / "bad.yaml").write_text("name: [\n", encoding="utf-8")
    with pytest.raises(ValueError, match="expected a .yaml, .yml or .json"):
        load_profile(tmp_path / "p.toml")
    with pytest.raises(ValueError, match="Invalid JSON in profile"):
        load_profile(tmp_path / "bad.json")
    with pytest.raises(ValueError, match="Invalid YAML in profile"):
        load_profile(tmp_path / "bad.yaml")
    with pytest.raises(ValueError, match="Cannot read profile"):
        load_profile(tmp_path / "missing.yaml")


def scan_row(name: str, region: str = "eastus", **kw):
    """A classified SKU row with 8 vCPUs in standardDSv5Family and all three zones unless overridden."""
    raw = raw_sku(name, region=region, family=kw.pop("family", "standardDSv5Family"), **kw)
    raw["capabilities"] = {"vCPUs": "8", "MemoryGB": "32"}
    return classify(raw)


def test_workload_verdict_precedence_covers_every_status() -> None:
    zones = ["1", "2", "3"]
    restriction_in = lambda region, kind, reason, z=None: {  # noqa: E731
        "type": kind,
        "reason": reason,
        "locations": [region],
        "zones": z or [],
    }
    rows = [
        scan_row(
            "Standard_R",
            zones=zones,
            restrictions=[restriction_in("eastus", "Location", "NotAvailableForSubscription")],
        ),
        scan_row(
            "Standard_Z",
            zones=zones,
            restrictions=[restriction_in("eastus", "Zone", "NotAvailableForSubscription", ["1", "2"])],
        ),
        scan_row("Standard_Q", zones=zones, family="standardEmptyFamily"),
        scan_row(
            "Standard_QID",
            zones=zones,
            family="standardQidFamily",
            restrictions=[restriction_in("eastus", "Location", "QuotaId")],
        ),
        scan_row("Standard_C", zones=zones),
        scan_row("Standard_OK", zones=zones),
        scan_row("Standard_U", zones=zones),
        scan_row("Standard_N", zones=[]),
        scan_row("Standard_P", zones=zones, family="standardPqFamily"),
    ]
    quota = [
        QuotaRow("eastus", "standardDSv5Family", "DSv5", 0, 1000),
        QuotaRow("eastus", "standardEmptyFamily", "Empty", 0, 0),
    ]
    probes = [
        probe(
            "eastus",
            "InsufficientCapacity",
            sku="Standard_C",
            count=2,
            score=3,
            split=[{"name": "Standard_C", "zone": "1", "capacity": 1, "capacity_max": 1}],
        ),
        probe("eastus", "None", sku="Standard_OK", count=2, score=9),
        probe("eastus", error="404 nope", sku="Standard_U", count=2),
        probe(
            "eastus", "InsufficientQuota", sku="Standard_P", count=2, score=None, split=[], detail="standardPqFamily"
        ),
    ]
    vms = [
        ProfileVm(sku, 2)
        for sku in (
            "Standard_R",
            "Standard_Z",
            "Standard_Q",
            "Standard_QID",
            "Standard_C",
            "Standard_OK",
            "Standard_U",
            "Standard_N",
            "Standard_X",
            "Standard_P",
        )
    ]
    zonal = Profile("t", zonal=True, vms=vms)

    [rv] = workload_verdicts(zonal, ["eastus"], rows, quota, probes)
    by = {v.sku: v for v in rv.vms}
    assert rv.verdict == "blocked"
    assert (by["Standard_R"].status, by["Standard_R"].reason) == (
        "restricted",
        "region restricted for this subscription",
    )
    assert by["Standard_Z"].status == "restricted" and "usable zones: 3" in by["Standard_Z"].reason
    assert (by["Standard_Q"].status, by["Standard_Q"].reason) == ("quota", "standardEmptyFamily limit 0, need 16 vCPU")
    assert by["Standard_QID"].status == "quota" and "QuotaId" in by["Standard_QID"].reason
    assert (by["Standard_C"].status, by["Standard_C"].reason, by["Standard_C"].probe_score) == (
        "capacity",
        "score 3, 1 of 2 placed",
        3,
    )
    assert (by["Standard_OK"].status, by["Standard_OK"].reason) == ("deployable", "placed (score 9)")
    assert by["Standard_U"].status == "unknown" and "404 nope" in by["Standard_U"].reason
    assert by["Standard_N"].status == "restricted" and "no availability zones" in by["Standard_N"].reason
    assert by["Standard_X"].status == "not offered"
    assert by["Standard_P"].status == "quota" and "standardPqFamily" in by["Standard_P"].reason
    assert by["Standard_OK"].family == "standardDSv5Family" and by["Standard_OK"].vcpu_need == 16
    assert (by["Standard_OK"].quota_limit, by["Standard_OK"].quota_used) == (1000, 0)
    assert rv.blocking[0] == "restricted: Standard_R (region restricted for this subscription)"
    assert len(rv.blocking) == 8

    # only VMs that survive steps 1-3 are worth a probe: C, OK, U (and P, whose quota verdict came from its probe)
    assert probe_candidates(zonal, ["eastus"], rows, quota) == {
        "eastus": [("Standard_C", 2), ("Standard_OK", 2), ("Standard_U", 2), ("Standard_P", 2)]
    }

    # a regional profile: the zone rule no longer applies, so Z and N fall through to the probe (absent -> unknown)
    regional = Profile("t", zonal=False, vms=[ProfileVm("Standard_Z", 2), ProfileVm("Standard_N", 2)])
    [rv] = workload_verdicts(regional, ["eastus"], rows, quota, probes)
    assert [v.status for v in rv.vms] == ["unknown", "unknown"] and rv.verdict == "unknown"

    # deployable region: only placed VMs
    [rv] = workload_verdicts(Profile("t", vms=[ProfileVm("Standard_OK", 2)]), ["eastus"], rows, quota, probes)
    assert rv.verdict == "deployable" and rv.blocking == []


def test_workload_quota_sums_needs_per_family_and_optional_never_blocks() -> None:
    rows = [scan_row("Standard_D8s_v5"), scan_row("Standard_D16s_v5"), scan_row("Standard_G")]
    rows[1].vcpus = 16
    quota = [QuotaRow("eastus", "standardDSv5Family", "DSv5", 20, 200)]  # 180 free
    probes = [
        probe("eastus", "None", sku="Standard_D8s_v5", count=10, score=8),
        probe("eastus", "None", sku="Standard_D16s_v5", count=5, score=8),
        probe("eastus", "InsufficientCapacity", sku="Standard_G", count=1, score=1, split=[]),
    ]
    # 80 + 80 = 160 fits alone; together with an 80 more they would not, but each alone does
    fits = Profile("fits", vms=[ProfileVm("Standard_D8s_v5", 10), ProfileVm("Standard_D16s_v5", 5)])
    [rv] = workload_verdicts(fits, ["eastus"], rows, quota, probes)
    assert rv.verdict == "deployable"

    # 8*15 + 16*5 = 200 > 180 free: both required VMs are quota-blocked because they share the family
    probes.append(probe("eastus", "None", sku="Standard_D8s_v5", count=15, score=8))
    shared = Profile("shared", vms=[ProfileVm("Standard_D8s_v5", 15), ProfileVm("Standard_D16s_v5", 5)])
    [rv] = workload_verdicts(shared, ["eastus"], rows, quota, probes)
    assert [v.status for v in rv.vms] == ["quota", "quota"]
    assert rv.vms[0].reason == "standardDSv5Family limit 200, used 20, need 200 vCPU"

    # an optional VM is checked on top of the required total and can only block itself
    probes.append(probe("eastus", "None", sku="Standard_D16s_v5", count=10, score=8))
    optional = Profile(
        "opt",
        vms=[
            ProfileVm("Standard_D8s_v5", 10),
            ProfileVm("Standard_D16s_v5", 10, optional=True),
            ProfileVm("Standard_G", 1, optional=True),
        ],
    )
    [rv] = workload_verdicts(optional, ["eastus"], rows, quota, probes)
    assert rv.verdict == "deployable" and rv.blocking == []
    assert [(v.sku, v.status) for v in rv.vms] == [
        ("Standard_D8s_v5", "deployable"),
        ("Standard_D16s_v5", "quota"),  # 80 required + 160 optional > 180 free
        ("Standard_G", "capacity"),
    ]


def verdict(region: str, verdict: str, *vms: tuple[str, str]) -> RegionVerdict:
    rows = [VmVerdict_(region, sku, status) for sku, status in vms]
    blocking = [
        f"{v.status}: {v.sku} ({v.reason})"
        for v in rows
        if v.status in ("quota", "capacity", "restricted", "not offered")
    ]
    return RegionVerdict(region=region, verdict=verdict, blocking=blocking, vms=rows)


def VmVerdict_(region: str, sku: str, status: str):
    from azcap import VmVerdict

    return VmVerdict(region=region, sku=sku, count=1, optional=False, status=status, reason="r")


def test_pair_verdict_notes_use_quota_and_capacity_wording() -> None:
    pairs = [
        {"region": "a", "pair": "b"},
        {"region": "c", "pair": "d"},
        {"region": "e", "pair": "f"},
        {"region": "g", "pair": "h"},
        {"region": "i", "pair": "j"},
        {"region": "k", "pair": None},
    ]
    verdicts = [
        verdict("a", "deployable", ("S", "deployable")),
        verdict("b", "deployable", ("S", "deployable")),
        verdict("c", "deployable", ("S", "deployable")),
        verdict("d", "blocked", ("S", "quota"), ("T", "capacity")),
        verdict("e", "blocked", ("S", "restricted")),
        verdict("f", "deployable", ("S", "deployable")),
        verdict("g", "blocked", ("S", "capacity")),
        verdict("h", "blocked", ("S", "quota")),
        verdict("i", "unknown", ("S", "unknown")),
        verdict("j", "blocked", ("S", "not offered")),
        verdict("k", "blocked", ("S", "quota")),
    ]
    pair_verdict_notes(pairs, verdicts)
    assert [(p["verdict"], p["pair_verdict"], p["dr_note"]) for p in pairs] == [
        ("deployable", "deployable", "DR-ready"),
        ("deployable", "blocked", "pair blocked: quota blocks S; cannot place T"),
        ("blocked", "deployable", "primary blocked: S restricted"),
        ("blocked", "blocked", "both blocked — g: cannot place S; pair: quota blocks S"),
        ("unknown", "blocked", "j blocked: S not offered; i not determined (placement probe unavailable)"),
        ("blocked", None, ""),
    ]
    assert "cannot place" not in pairs[3]["dr_note"].split("pair:")[1]


def run_cli(*args: str, out: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "azcap", "--fixture", str(FIXTURE), "--out", str(out), *args],
        capture_output=True,
        text=True,
    )


def test_cli_profile_writes_verdicts_and_gates_with_exit_codes(tmp_path: Path) -> None:
    ok = run_cli("--regions", "eastus2,brazilsouth", "--profile", str(WAVE1), out=tmp_path / "a")
    assert ok.returncode == 0
    assert 'Workload "wave1" (zonal, Linux):' in ok.stdout
    rows = list(csv.DictReader((tmp_path / "a" / "verdicts.csv").open(encoding="utf-8", newline="")))
    assert list(rows[0].keys()) == [
        "region",
        "sku",
        "count",
        "optional",
        "status",
        "reason",
        "family",
        "vcpu_need",
        "quota_limit",
        "quota_used",
        "probe_score",
        "probe_fulfillment",
        "region_verdict",
    ]
    by_region = {
        r: {row["sku"]: row for row in rows if row["region"] == r}
        for r in ("eastus2", "centralus", "brazilsouth", "southcentralus")
    }
    assert by_region["eastus2"]["Standard_D8s_v5"]["region_verdict"] == "deployable"
    assert by_region["brazilsouth"]["Standard_D8s_v5"]["status"] == "capacity"
    assert by_region["brazilsouth"]["Standard_D8s_v5"]["region_verdict"] == "blocked"
    assert by_region["southcentralus"]["Standard_D8s_v5"]["status"] == "quota"
    assert by_region["southcentralus"]["Standard_D8s_v5"]["quota_limit"] == "0"
    assert by_region["centralus"]["Standard_NC24ads_A100_v4"]["status"] == "restricted"
    assert by_region["centralus"]["Standard_NC24ads_A100_v4"]["optional"] == "True"
    assert by_region["centralus"]["Standard_NC24ads_A100_v4"]["region_verdict"] == "deployable"
    raw = json.loads((tmp_path / "a" / "raw.json").read_text(encoding="utf-8"))
    assert raw["profile"]["name"] == "wave1" and raw["meta"]["profile_name"] == "wave1"
    assert {v["region"]: v["verdict"] for v in raw["verdicts"]} == {
        "eastus2": "deployable",
        "brazilsouth": "blocked",
        "centralus": "deployable",
        "southcentralus": "blocked",
    }
    # probes are only kept (live: only sent) for VMs undecided after the not-offered/restricted/quota steps
    probe_rows = list(csv.DictReader((tmp_path / "a" / "probes.csv").open(encoding="utf-8", newline="")))
    probed = {(r["region"], r["sku"]) for r in probe_rows}
    assert ("centralus", "Standard_NC24ads_A100_v4") not in probed  # restricted
    assert ("southcentralus", "Standard_D8s_v5") not in probed  # quota
    assert ("brazilsouth", "Standard_NC24ads_A100_v4") not in probed  # restricted (optional)
    assert {
        ("eastus2", "Standard_D8s_v5"),
        ("southcentralus", "Standard_E16s_v5"),
        ("brazilsouth", "Standard_D8s_v5"),
    } <= probed
    assert len(probe_rows) == 8
    pairs = {r["region"]: r for r in csv.DictReader((tmp_path / "a" / "pairs.csv").open(encoding="utf-8", newline=""))}
    assert pairs["eastus2"]["dr_note"] == "DR-ready"
    assert pairs["brazilsouth"]["dr_note"].startswith("both blocked")
    html = (tmp_path / "a" / "report.html").read_text(encoding="utf-8")
    assert '"verdicts": [' in html and '"profile": {' in html

    # no --fail-on-blocked: exit 0 even though brazilsouth is blocked
    assert run_cli("--regions", "brazilsouth", "--profile", str(WAVE1), out=tmp_path / "b").returncode == 0
    blocked = run_cli(
        "--regions", "eastus2,brazilsouth", "--profile", str(WAVE1), "--fail-on-blocked", out=tmp_path / "c"
    )
    assert blocked.returncode == EXIT_BLOCKED and "brazilsouth" in blocked.stderr.splitlines()[-1]
    # the auto-added pair (southcentralus) is blocked but only primaries gate without --require-pair
    fine = run_cli("--regions", "eastus2", "--profile", str(WAVE1), "--fail-on-blocked", out=tmp_path / "d")
    assert fine.returncode == 0
    gpu = tmp_path / "gpu.json"
    gpu.write_text(json.dumps({"name": "gpu", "zonal": True, "vms": [{"sku": "Standard_NC24ads_A100_v4", "count": 1}]}))
    assert (
        run_cli("--regions", "eastus2", "--profile", str(gpu), "--fail-on-blocked", out=tmp_path / "e").returncode == 0
    )
    pair_gate = run_cli(
        "--regions", "eastus2", "--profile", str(gpu), "--fail-on-blocked", "--require-pair", out=tmp_path / "f"
    )
    assert pair_gate.returncode == EXIT_BLOCKED and "centralus" in pair_gate.stderr.splitlines()[-1]

    # stale verdicts.csv is removed on a run without a profile
    assert (tmp_path / "a" / "verdicts.csv").exists()
    assert run_cli("--regions", "eastus2", out=tmp_path / "a").returncode == 0
    assert not (tmp_path / "a" / "verdicts.csv").exists()


def test_cli_profile_argument_rules(tmp_path: Path) -> None:
    both = run_cli("--regions", "eastus2", "--profile", str(WAVE1), "--need", "Standard_D8s_v5:1", out=tmp_path)
    assert both.returncode == 2 and "mutually exclusive" in both.stderr
    no_profile = run_cli("--regions", "eastus2", "--fail-on-blocked", out=tmp_path)
    assert no_profile.returncode == 2 and "--fail-on-blocked requires --profile" in no_profile.stderr
    no_gate = run_cli("--regions", "eastus2", "--profile", str(WAVE1), "--require-pair", out=tmp_path)
    assert no_gate.returncode == 2 and "--require-pair requires --fail-on-blocked" in no_gate.stderr
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: x\nvms: []\n", encoding="utf-8")
    invalid = run_cli("--regions", "eastus2", "--profile", str(bad), out=tmp_path)
    assert invalid.returncode == 2 and "'vms' must be a non-empty list" in invalid.stderr
