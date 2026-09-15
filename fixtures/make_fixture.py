"""Generate a synthetic raw.json so azcap can be exercised without Azure access.
Shapes mirror what _sku_to_dict produces from the Resource SKUs API."""

import json
import random
from pathlib import Path

FAMILIES = {
    "standardDv5Family": ("Standard_D{n}_v5", [2, 4, 8, 16, 32, 48, 64, 96]),
    "standardDSv5Family": ("Standard_D{n}s_v5", [2, 4, 8, 16, 32, 48, 64, 96]),
    "standardEv5Family": ("Standard_E{n}_v5", [2, 4, 8, 16, 32, 48, 64, 96]),
    "standardESv5Family": ("Standard_E{n}s_v5", [2, 4, 8, 16, 32, 48, 64, 96]),
    "standardFSv2Family": ("Standard_F{n}s_v2", [2, 4, 8, 16, 32, 48, 64, 72]),
    "standardNCADSA100v4Family": ("Standard_NC{n}ads_A100_v4", [24, 48, 96]),
    "standardNDASv4_A100Family": ("Standard_ND{n}asr_v4", [96]),
    "standardNVADSA10v5Family": ("Standard_NV{n}ads_A10_v5", [6, 12, 18, 36, 72]),
}
# region -> (zones, region_restrict_prob, zone_restrict_prob, gpu_region_restrict_prob)
REGIONS = {
    "eastus": (["1", "2", "3"], 0.02, 0.10, 0.60),
    "westus": ([], 0.08, 0.00, 0.90),
    "eastus2": (["1", "2", "3"], 0.01, 0.05, 0.40),
    "centralus": (["1", "2", "3"], 0.04, 0.12, 0.70),
    "canadacentral": (["1", "2", "3"], 0.03, 0.15, 0.80),
    "canadaeast": ([], 0.05, 0.00, 1.00),
    "brazilsouth": (["1", "2", "3"], 0.10, 0.30, 0.95),
    "southcentralus": (["1", "2", "3"], 0.06, 0.18, 0.75),
    "mexicocentral": (["1", "2", "3"], 0.15, 0.25, 1.00),
    "saudiarabiaeast": (["1", "2", "3"], 0.20, 0.35, 1.00),
    "australiaeast": (["1", "2", "3"], 0.05, 0.20, 0.70),
    "australiasoutheast": ([], 0.10, 0.00, 1.00),
    "southeastasia": (["1", "2", "3"], 0.12, 0.30, 0.90),
    "eastasia": (["1", "2", "3"], 0.25, 0.40, 1.00),
}
# Illustrative pairing metadata (shape of Subscriptions API locations[].metadata). Pairs shown here
# follow the published Azure pair list at time of writing; newer regions launch unpaired.
LOCATIONS = {
    "eastus": ("United States", "US", "westus"),
    "westus": ("United States", "US", "eastus"),
    "eastus2": ("United States", "US", "centralus"),
    "centralus": ("United States", "US", "eastus2"),
    "canadacentral": ("Canada", "Canada", "canadaeast"),
    "canadaeast": ("Canada", "Canada", "canadacentral"),
    "brazilsouth": ("Brazil", "South America", "southcentralus"),
    "southcentralus": ("United States", "US", "northcentralus"),
    "mexicocentral": ("Mexico", "Mexico", None),
    "saudiarabiaeast": ("Saudi Arabia", "Middle East", None),
    "australiaeast": ("Australia", "Asia Pacific", "australiasoutheast"),
    "australiasoutheast": ("Australia", "Asia Pacific", "australiaeast"),
    "southeastasia": ("Asia Pacific", "Asia Pacific", "eastasia"),
    "eastasia": ("Asia Pacific", "Asia Pacific", "southeastasia"),
}
PROFILE_VMS = [("Standard_D8s_v5", 12, False), ("Standard_E16s_v5", 4, False), ("Standard_NC24ads_A100_v4", 1, True)]
WAVE1_YAML = """\
# azcap workload profile: what wave 1 needs in a region before it can deploy there.
name: wave1
os: Linux
zonal: true                     # the design is zone-resilient, so placement is judged across zones
vms:
  - sku: Standard_D8s_v5
    count: 12
  - sku: Standard_E16s_v5
    count: 4
  - sku: Standard_NC24ads_A100_v4
    count: 1
    optional: true              # reported, never blocks the verdict
"""
PROBE_OUTCOMES = {"brazilsouth": (2, "InsufficientCapacity", 6), "westus": (6, "InsufficientQuota", 8)}


def make_probe(region, sku, count, score, fulfillment, placed, zones, detail=None):
    split = []
    if placed:
        buckets = zones or [None]
        for i, zone in enumerate(buckets):
            n = placed // len(buckets) + (1 if i < placed % len(buckets) else 0)
            if n:
                split.append({"name": sku, "zone": zone, "capacity": n, "capacity_max": n})
    return {
        "region": region,
        "sku": sku,
        "count": count,
        "zonal": bool(zones),
        "spot": False,
        "score": score if placed else None,
        "fulfillment": fulfillment,
        "split": split,
        "valid_until": "2026-01-01T12:00:00Z",
        "error": None,
        "detail": detail,
        "skus": [sku],
    }


def build(out_path: Path) -> None:
    """Write the synthetic raw.json to out_path and wave1.yaml next to it. Deterministic (fixed seed)."""
    random.seed(7)
    skus, quota = [], []
    for region, (zones, p_reg, p_zone, p_gpu) in REGIONS.items():
        for fam, (pat, sizes) in FAMILIES.items():
            gpu = fam.startswith(("standardNC", "standardND", "standardNV"))
            quota_zero = gpu and random.random() < 0.5
            for n in sizes:
                name = pat.format(n=n)
                r = []
                if quota_zero:
                    r.append({"type": "Location", "reason": "QuotaId", "locations": [region], "zones": []})
                elif random.random() < (p_gpu if gpu else p_reg):
                    r.append(
                        {
                            "type": "Location",
                            "reason": "NotAvailableForSubscription",
                            "locations": [region],
                            "zones": [],
                        }
                    )
                elif zones and random.random() < p_zone * (1.5 if n >= 32 else 1):
                    blocked = sorted(random.sample(zones, random.choice([1, 1, 2])))
                    r.append(
                        {
                            "type": "Zone",
                            "reason": "NotAvailableForSubscription",
                            "locations": [region],
                            "zones": blocked,
                        }
                    )
                skus.append(
                    {
                        "region": region,
                        "name": name,
                        "family": fam,
                        "capabilities": {"vCPUs": str(n), "MemoryGB": str(n * (8 if "E" in pat[9] else 4))},
                        "zones": zones,
                        "restrictions": r,
                    }
                )
            limit = 0 if quota_zero else random.choice([100, 200, 350, 500])
            quota.append(
                {
                    "region": region,
                    "family": fam,
                    "localized": fam,
                    "current": 0 if not limit else random.randint(0, limit),
                    "limit": limit,
                }
            )

    locations = {
        r: {"name": r, "display": r, "geography": g, "geography_group": gg, "pair": p, "zonal": bool(REGIONS[r][0])}
        for r, (g, gg, p) in LOCATIONS.items()
    }

    # Workload profile used by the demo and the tests: two required families plus an optional GPU SKU.

    # Pin four regions so `--profile fixtures/wave1.yaml` on eastus2,brazilsouth shows every verdict:
    #   eastus2         deployable
    #   centralus       deployable, optional GPU SKU restricted (pair of eastus2 -> DR-ready)
    #   brazilsouth     blocked by capacity (D8s_v5 probe cannot place)
    #   southcentralus  blocked by quota (DSv5 family limit 0; pair of brazilsouth)
    SHOWCASE = {"eastus2", "centralus", "brazilsouth", "southcentralus"}
    for sku in skus:
        if sku["region"] in SHOWCASE and sku["name"] in ("Standard_D8s_v5", "Standard_E16s_v5"):
            sku["restrictions"] = []
        if sku["region"] == "centralus" and sku["name"] == "Standard_NC24ads_A100_v4":
            sku["restrictions"] = [
                {"type": "Location", "reason": "NotAvailableForSubscription", "locations": ["centralus"], "zones": []}
            ]
        if sku["region"] == "eastus2" and sku["name"] == "Standard_NC24ads_A100_v4":
            sku["restrictions"] = []
    for q in quota:
        if q["region"] in SHOWCASE and q["family"] in ("standardDSv5Family", "standardESv5Family"):
            q["current"], q["limit"] = 100, 500
        if q["region"] == "southcentralus" and q["family"] == "standardDSv5Family":
            q["current"], q["limit"] = 0, 0
        if q["region"] == "eastus2" and q["family"] == "standardNCADSA100v4Family":
            q["current"], q["limit"] = 0, 100
    quota_idx = {(q["region"], q["family"]): q for q in quota}
    family_of = {(s_["region"], s_["name"]): s_["family"] for s_ in skus}
    vcpus_of = {(s_["region"], s_["name"]): int(s_["capabilities"]["vCPUs"]) for s_ in skus}

    # Placement probes (shape of azcap.ProbeResult): one per profile VM per region, zonal where the region has zones.
    # Score tracks the region's restriction pressure; a few are pinned so the demo shows every outcome
    # (brazilsouth cannot place D8s_v5 but its pair can; westus is quota-bound; eastasia has no preview API).
    probes = []
    for region, (zones, p_reg, _, _) in REGIONS.items():
        base = max(0, 9 - round(p_reg * 40))
        for sku, count, _optional in PROFILE_VMS:
            q = quota_idx.get((region, family_of[(region, sku)]))
            need = vcpus_of[(region, sku)] * count
            if sku == "Standard_D8s_v5":
                score, fulfillment, placed = PROBE_OUTCOMES.get(region, (base, "None", count))
                if score <= 2 and fulfillment == "None":
                    fulfillment, placed = "InsufficientCapacity", 4 * score
            else:
                score, fulfillment, placed = max(1, base - 1), "None", count
            detail = None
            if q and (q["limit"] == 0 or q["limit"] - q["current"] < need):  # what the live quota pre-check would say
                score, fulfillment, placed = None, "InsufficientQuota", 0
                detail = f"limit {q['limit']}, used {q['current']}, need {need} vCPU"
            probes.append(make_probe(region, sku, count, score, fulfillment, placed, zones, detail))
    # one region where the preview API is not available, to show how a failed probe renders
    for pr in probes:
        if pr["region"] == "eastasia" and pr["sku"] == "Standard_D8s_v5":
            pr.update(
                score=None, fulfillment=None, split=[], valid_until=None, error="404 No registered resource provider"
            )

    out = {
        "meta": {"subscription": "00000000-fixture", "generated": "synthetic"},
        "locations": locations,
        "skus": skus,
        "quota": quota,
        "probes": probes,
    }
    out_path.write_text(json.dumps(out, indent=1), encoding="utf-8")
    out_path.with_name("wave1.yaml").write_text(WAVE1_YAML, encoding="utf-8")


if __name__ == "__main__":
    target = Path(__file__).with_name("sample.json")
    build(target)
    data = json.loads(target.read_text(encoding="utf-8"))
    print(len(data["skus"]), "skus", len(data["quota"]), "quota rows", len(data["probes"]), "probes")
