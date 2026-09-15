#!/usr/bin/env python3
"""
azcap — Azure VM capacity scanner.

Reads the Resource SKUs API (the same data behind `az vm list-skus`) and,
optionally, compute quota usage, then reports the share of VM SKUs that are
restricted for this subscription, per region x VM family; with --need it also asks
the Compute Recommender whether a specific allocation would place today. Produces
CSVs and a self-contained HTML heatmap report.

What the signals mean
---------------------
  region_restricted  NotAvailableForSubscription at Location scope. The SKU
                     is defined in the region but Microsoft will not allocate
                     it to this subscription there. Strongest restriction signal.
  zone_restricted    NotAvailableForSubscription at Zone scope. SKU is
                     allocatable in some zones only. Common in constrained
                     regions before region-level restriction kicks in.
  quota_blocked      QuotaId restriction — a quota or subscription-eligibility
                     signal. It is tracked separately and excluded from the
                     % restricted figure.
  available          No restrictions.

Zone-adjusted % restricted per region x family
----------------------------------------------
  For each SKU: loss = 1.0 if region_restricted
                     = blocked_zones / total_zones if zone_restricted
                     = 0 otherwise
  % restricted = 100 * mean(loss) over assessed SKUs in the family. Quota-blocked
          SKUs are excluded; if none remain, the figure is n/a rather than zero.

This is a subscription-specific availability proxy, not a published Azure
capacity percentage. All data is subscription-specific; run it under the subscription
that will actually deploy, not a sandbox.

Placement probes (--need)
-------------------------
  Asks Microsoft's Compute Recommender (preview) whether a proposed allocation, e.g.
  12 x Standard_D8s_v5, would place in each scanned region right now: a score from
  0 (worst) to 9 (best), whether the full request was placed, and the SKU/zone split.
  This is the only capacity signal Azure exposes; it describes a hypothetical
  allocation at the time of the run and is not a reservation.

Usage
-----
  python azcap.py --regions eastus,eastus2,canadacentral --families Dv5,Ev5,NC
  python azcap.py --regions saudiarabiaeast --include-quota --out ./out
  python azcap.py --regions eastus --fixture fixtures/sample.json   # offline
  python azcap.py --regions eastus --compare out/previous/raw.json  # diff
  python azcap.py --regions eastus2,brazilsouth --zonal --need Standard_D8s_v5:12

Auth: DefaultAzureCredential (az login, env vars, managed identity, etc.).
Subscription: --subscription, else AZURE_SUBSCRIPTION_ID, else `az account show`.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass
class SkuRow:
    region: str
    sku: str
    family: str
    vcpus: int | None
    memory_gb: float | None
    zones_total: int
    zones_available: list[str]
    zones_blocked: list[str]
    status: str  # available | zone_restricted | region_restricted | quota_blocked
    reason_codes: list[str]
    loss: float  # 0..1 restriction loss used in scoring


@dataclass
class QuotaRow:
    region: str
    family: str
    localized: str
    current: int
    limit: int

    @property
    def headroom_pct(self) -> float | None:
        return None if self.limit == 0 else round(100 * (self.limit - self.current) / self.limit, 1)


@dataclass
class FamilySummary:
    region: str
    family: str
    n_skus: int
    n_available: int
    n_zone_restricted: int
    n_region_restricted: int
    n_quota_blocked: int
    n_assessable: int
    score: float | None  # 0..100; None when quota restrictions exclude every SKU
    zone_slots_total: int
    zone_slots_blocked: int


@dataclass
class ProbeResult:
    """One Compute Recommender placement probe: would `count` VMs of `sku` place in `region` right now?"""

    region: str
    sku: str  # SKU name, or "mix" when several SKUs were sent as one ranked request (see `skus`)
    count: int  # VMs requested (the total across SKUs for a mix)
    zonal: bool  # zones were sent; False means a regional placement
    spot: bool
    score: int | None  # 0 (worst) .. 9 (best) of the best placement choice; None if nothing placed or on error
    fulfillment: str | None  # API value: "None" (fully placed), "InsufficientCapacity", "InsufficientQuota"
    split: list[dict]  # [{name, zone, capacity, capacity_max}] of the best placement choice
    valid_until: str | None
    error: str | None  # "<status> <short message>" when the probe failed; the row is otherwise empty
    detail: str | None = None  # why: the quota family the API named, or the quota figures a pre-check tripped on
    skus: list[str] = field(default_factory=list)  # every SKU in the request, in rank order


ARM_ENDPOINT = "https://management.azure.com"
ARM_SCOPE = "https://management.azure.com/.default"
# Preview API; the version string changes every few months. The doc site already shows a newer one that
# ARM does not yet accept, so live-verified is what ships here.
PROBE_API_VERSION = "2026-05-05-preview"  # override with --probe-api-version when ARM moves on
# ARM answers 409 with this message instead of a placement when the family has no quota headroom
QUOTA_LIMIT_MESSAGE = re.compile(r"vCPU quota for (\S+?) has reached its limit", re.IGNORECASE)
PROBE_LABELS = {
    "None": "placed",
    "InsufficientCapacity": "insufficient capacity",
    "InsufficientQuota": "insufficient quota",
}


# --------------------------------------------------------------------------- #
# Collection
# --------------------------------------------------------------------------- #


def resolve_subscription(explicit: str | None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("AZURE_SUBSCRIPTION_ID")
    if env:
        return env
    az = shutil.which("az") or shutil.which("az.cmd")  # Windows installs az.cmd
    if not az:
        sys.exit("Azure CLI not found on PATH: pass --subscription or set AZURE_SUBSCRIPTION_ID.")
    try:
        out = subprocess.check_output([az, "account", "show", "--query", "id", "-o", "tsv"], text=True)
        return out.strip()
    except Exception:
        sys.exit("No subscription: pass --subscription or set AZURE_SUBSCRIPTION_ID or `az login`.")


def make_credential(tenant: str | None):
    """DefaultAzureCredential, pinned to a tenant when one is given so multi-tenant
    CLI logins can't silently pick the wrong directory."""
    from azure.identity import AzureCliCredential, ChainedTokenCredential, DefaultAzureCredential

    if tenant:
        # Prefer a CLI token minted for that tenant; fall back to the default chain if the CLI isn't logged in.
        return ChainedTokenCredential(
            AzureCliCredential(tenant_id=tenant),
            DefaultAzureCredential(additionally_allowed_tenants=[tenant]),
        )
    return DefaultAzureCredential()


def describe_subscription(credential, subscription: str, tenant: str | None) -> dict:
    """Subscription display name + owning tenant, so the report is stamped with where it ran."""
    from azure.mgmt.resource.subscriptions import SubscriptionClient

    sub = SubscriptionClient(credential).subscriptions.get(subscription)
    info = {"subscription_name": sub.display_name, "tenant_id": (sub.tenant_id or "").lower()}
    if tenant and info["tenant_id"] and info["tenant_id"] != tenant.lower():
        sys.exit(
            f"Subscription {subscription} belongs to tenant {info['tenant_id']}, not --tenant {tenant}. "
            f"Check `az account list -o table`."
        )
    return info


def fetch_skus_live(credential, subscription: str, regions: list[str]) -> list[dict]:
    from azure.mgmt.compute import ComputeManagementClient

    client = ComputeManagementClient(credential, subscription)
    raw: list[dict] = []
    for region in regions:
        # includeExtendedLocations not needed; filter server-side by location
        for sku in client.resource_skus.list(filter=f"location eq '{region}'"):
            if (sku.resource_type or "").lower() != "virtualmachines":
                continue
            raw.append(_sku_to_dict(sku, region))
        print(f"  {region}: {sum(1 for r in raw if r['region'] == region)} VM SKUs", file=sys.stderr)
    return raw


def _sku_to_dict(sku: Any, region: str) -> dict:
    """Normalize SDK model into a plain dict so fixtures and live runs share a shape."""
    caps = {c.name: c.value for c in (sku.capabilities or [])}
    zones: list[str] = []
    for li in sku.location_info or []:
        if (li.location or "").lower() == region.lower():
            zones = list(li.zones or [])
    restrictions = []
    for r in sku.restrictions or []:
        info = r.restriction_info
        restrictions.append(
            {
                "type": str(r.type),
                "reason": str(r.reason_code),
                "locations": [location.lower() for location in (info.locations or [])] if info else [],
                "zones": list(info.zones or []) if info else [],
            }
        )
    return {
        "region": region.lower(),
        "name": sku.name,
        "family": (sku.family or "").strip(),
        "capabilities": caps,
        "zones": zones,
        "restrictions": restrictions,
    }


def fetch_locations_live(credential, subscription: str) -> dict[str, dict]:
    """Region metadata from the Subscriptions API: pair, geography, zone support."""
    from azure.mgmt.resource.subscriptions import SubscriptionClient

    client = SubscriptionClient(credential)
    locs: dict[str, dict] = {}
    for loc in client.subscriptions.list_locations(subscription):
        md = loc.metadata
        # region_type may be a plain string or an enum whose str() is "RegionType.PHYSICAL"
        rtype = str(md.region_type or "").split(".")[-1].lower() if md else ""
        if md is None or rtype not in ("physical", ""):
            continue
        pair = None
        if md.paired_region:
            pair = (md.paired_region[0].name or "").lower() or None
        locs[loc.name.lower()] = {
            "name": loc.name.lower(),
            "display": loc.display_name,
            "geography": md.geography,
            "geography_group": md.geography_group,
            "pair": pair,
            "zonal": bool(loc.availability_zone_mappings),
        }
    return locs


def expand_with_pairs(regions: list[str], locs: dict[str, dict]) -> tuple[list[str], dict[str, str]]:
    """Append each region's pair (if any, not already listed). Returns (regions, {pair: primary})."""
    out = list(regions)
    added: dict[str, str] = {}
    for r in regions:
        p = (locs.get(r) or {}).get("pair")
        if p and p not in out:
            out.append(p)
            added[p] = r
    return out, added


def fetch_quota_live(credential, subscription: str, regions: list[str]) -> list[QuotaRow]:
    from azure.mgmt.compute import ComputeManagementClient

    client = ComputeManagementClient(credential, subscription)
    rows: list[QuotaRow] = []
    for region in regions:
        for u in client.usage.list(region):
            name = u.name.value or ""
            if not name.lower().endswith("family"):
                continue
            rows.append(
                QuotaRow(
                    region.lower(), name, u.name.localized_value or name, int(u.current_value or 0), int(u.limit or 0)
                )
            )
    return rows


# --------------------------------------------------------------------------- #
# Placement probes (Compute Recommender)
# --------------------------------------------------------------------------- #


def parse_need(text: str) -> list[tuple[str, int]]:
    """'Standard_D8s_v5:12,Standard_E8s_v5:4' -> [('Standard_D8s_v5', 12), ('Standard_E8s_v5', 4)]"""
    needs: list[tuple[str, int]] = []
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        sku, sep, count = item.rpartition(":")
        sku, count = sku.strip(), count.strip()
        if not sep or not sku or any(c.isspace() for c in sku) or not count.isdigit() or int(count) < 1:
            raise ValueError(f"--need entries must look like SKU:COUNT with a count of 1 or more, got {item!r}")
        if any(sku.lower() == existing.lower() for existing, _ in needs):
            raise ValueError(f"--need lists {sku} more than once")
        needs.append((sku, int(count)))
    if not needs:
        raise ValueError("--need must name at least one SKU:COUNT")
    return needs


def probe_request_body(
    needs: list[tuple[str, int]], *, os_type: str = "Linux", spot: bool = False, zones: list[str] | None = None
) -> dict:
    """Body for skuMixPlacementScores: one ranked request for every SKU in `needs` (rank = position)."""
    profile: dict[str, Any] = {
        "capacity": sum(count for _, count in needs),
        "capacityType": "VM",
        "priority": "Spot" if spot else "Regular",
        "allocationStrategy": "Prioritized",
        "osType": os_type,
    }
    if spot:
        profile["spotPriorityProfile"] = {"maxPricePerVm": -1}
    body: dict[str, Any] = {
        "capacityProfile": profile,
        "instanceDescription": {"vmSizes": [{"name": sku, "rank": rank} for rank, (sku, _) in enumerate(needs)]},
    }
    if zones:
        body["zones"] = list(zones)
    return body


def probe_zones(raw: list[dict], locs: dict[str, dict], region: str, skus: list[str]) -> list[str]:
    """Zones to send for a zonal probe: the zones the requested SKUs are actually offered in, in that region."""
    if not (locs.get(region) or {}).get("zonal"):
        return []
    wanted = {s.lower() for s in skus}
    zones: set[str] = set()
    for r in raw:
        if r.get("region") == region and str(r.get("name", "")).lower() in wanted:
            zones.update(str(z) for z in r.get("zones") or [])
    return sorted(zones)


def _score_of(choice: dict) -> int:
    score = _to_int(choice.get("score"))
    return -1 if score is None else score


def empty_probe_fields(**overrides: Any) -> dict:
    """The answer-shaped part of a ProbeResult with nothing in it, plus any fields given."""
    fields: dict[str, Any] = {
        "score": None,
        "fulfillment": None,
        "split": [],
        "valid_until": None,
        "error": None,
        "detail": None,
    }
    fields.update(overrides)
    return fields


def parse_probe_response(payload: Any) -> dict:
    """Reduce a skuMixPlacementScores response to the fields azcap reports. Never raises."""
    if not isinstance(payload, dict) or not isinstance(payload.get("placementChoices", []), list):
        return empty_probe_fields(error="unexpected response shape")
    choices = [c for c in payload.get("placementChoices") or [] if isinstance(c, dict)]
    best = max(choices, key=_score_of, default=None)
    split: list[dict] = []
    if best is not None:
        for item in best.get("skuSplit") or []:
            if not isinstance(item, dict):
                continue
            capacity = _to_int(item.get("capacity"))
            capacity_max = _to_int(item.get("capacityMax")) if item.get("capacityMax") is not None else capacity
            split.append(
                {
                    "name": str(item.get("name") or ""),
                    "zone": str(item.get("zone")) if item.get("zone") not in (None, "") else None,
                    "capacity": capacity,
                    "capacity_max": capacity_max,
                }
            )
    reason = payload.get("partialFulfillmentReason")
    return empty_probe_fields(
        score=_score_of(best) if best is not None and _score_of(best) >= 0 else None,
        fulfillment=str(reason) if reason is not None else None,
        split=split,
        valid_until=str(payload["validUntil"]) if payload.get("validUntil") else None,
    )


def _short_error(status: int, payload: Any, text: str) -> str:
    message = ""
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        err = payload["error"]
        message = str(err.get("message") or err.get("code") or "")
    message = " ".join((message or text or "").split())
    return f"{status} {message[:120]}".strip()


def probe_once(post, url: str, body: dict, sleep=time.sleep) -> dict:
    """POST one probe; retry once on 429 honoring Retry-After. Returns parse_probe_response() fields, never raises."""
    try:
        status, headers, text = post(url, body)
        if status == 429:
            retry_after = {str(k).lower(): v for k, v in dict(headers or {}).items()}.get("retry-after")
            sleep(min(max(_to_int(retry_after) or 5, 1), 60))
            status, headers, text = post(url, body)
    except Exception as e:
        message = " ".join(f"request failed: {type(e).__name__}: {e}".split())[:160]
        return empty_probe_fields(error=message)
    try:
        payload = json.loads(text) if text else None
    except ValueError:
        payload = None
    if status == 409:
        # a quota verdict, not a failure: the API refuses to place anything in a family at its vCPU limit
        match = QUOTA_LIMIT_MESSAGE.search(_short_error(status, payload, text))
        if match:
            return empty_probe_fields(fulfillment="InsufficientQuota", detail=match.group(1))
    if status != 200:
        return empty_probe_fields(error=_short_error(status, payload, text))
    if payload is None:
        return empty_probe_fields(error="200 response body is not JSON")
    return parse_probe_response(payload)


def arm_poster(credential):
    """A `post(url, body) -> (status, headers, text)` for ARM using a bearer token from the credential.
    The token is fetched on first use so an auth failure surfaces as a per-probe error, not an abort."""
    import requests

    session = requests.Session()
    state: dict[str, Any] = {}

    def post(url: str, body: dict) -> tuple[int, Any, str]:
        if "token" not in state:
            state["token"] = credential.get_token(ARM_SCOPE).token
        headers = {"Authorization": f"Bearer {state['token']}", "Content-Type": "application/json"}
        r = session.post(url, json=body, headers=headers, timeout=60)
        return r.status_code, r.headers, r.text

    return post


def quota_precheck(raw: list[dict], quota: list[QuotaRow], region: str, group: list[tuple[str, int]]) -> str | None:
    """Reason the request cannot fit in the family's vCPU quota, or None when it fits or can't be judged.

    A probe only says something about capacity once quota headroom exists, so this saves a call that
    would come back InsufficientQuota anyway. Skips silently when the SKU or family isn't in the scan."""
    skus_in_region = {str(r.get("name", "")).lower(): r for r in raw if r.get("region") == region}
    need_by_family: dict[str, int] = defaultdict(int)
    for sku, count in group:
        entry = skus_in_region.get(sku.lower())
        vcpus = _to_int((entry or {}).get("capabilities", {}).get("vCPUs")) if entry else None
        family = str((entry or {}).get("family") or "").strip()
        if not family or vcpus is None:
            continue
        need_by_family[family.lower()] += vcpus * count
    quota_in_region = {q.family.lower(): q for q in quota if q.region == region}
    for family, need in need_by_family.items():
        q = quota_in_region.get(family)
        if q is None:
            continue
        if q.limit == 0 or q.limit - q.current < need:
            prefix = f"{q.family}: " if len(group) > 1 else ""
            return f"{prefix}limit {q.limit}, used {q.current}, need {need} vCPU"
    return None


def run_probes(
    post,
    subscription: str,
    regions: list[str],
    needs: list[tuple[str, int]],
    *,
    mix: bool,
    os_type: str,
    spot: bool,
    zonal: bool,
    raw: list[dict],
    locs: dict[str, dict],
    api_version: str = PROBE_API_VERSION,
    quota: list[QuotaRow] | None = None,
    sleep=time.sleep,
) -> list[ProbeResult]:
    """One probe per region per SKU (or one per region for the whole mix). Failures become error rows.
    With `quota`, a request the family's vCPU quota cannot hold is answered locally instead of sent."""
    groups = [needs] if mix else [[n] for n in needs]
    results: list[ProbeResult] = []
    for region in regions:
        for group in groups:
            skus = [sku for sku, _ in group]
            zones = probe_zones(raw, locs, region, skus) if zonal else []
            body = probe_request_body(group, os_type=os_type, spot=spot, zones=zones)
            url = (
                f"{ARM_ENDPOINT}/subscriptions/{subscription}/providers/Microsoft.Compute/locations/{region}"
                f"/skuMixPlacementScores/recommendations/generate?api-version={api_version}"
            )
            short = quota_precheck(raw, quota, region, group) if quota is not None else None
            if short:
                fields = empty_probe_fields(fulfillment="InsufficientQuota", detail=short)
            else:
                fields = probe_once(post, url, body, sleep)
            probe = ProbeResult(
                region=region,
                sku="mix" if mix else skus[0],
                count=sum(count for _, count in group),
                zonal=bool(zones),
                spot=spot,
                skus=skus,
                **fields,
            )
            if probe.error:
                label = probe_request_label(probe)
                print(f"  ! {region}: placement probe {label} failed: {probe.error}", file=sys.stderr)
            elif short:
                label = probe_request_label(probe)
                print(f"  {region}: placement probe {label} not sent — quota {short}", file=sys.stderr)
            results.append(probe)
    return results


def fetch_probes_live(credential, subscription: str, regions: list[str], needs: list[tuple[str, int]], **kw):
    return run_probes(arm_poster(credential), subscription, regions, needs, **kw)


def probe_request_label(p: ProbeResult) -> str:
    """'Standard_D8s_v5 x12' or 'Standard_D8s_v5+Standard_E8s_v5 x16'"""
    return f"{'+'.join(p.skus) if p.sku == 'mix' else p.sku} x{p.count}"


def probe_label(p: ProbeResult) -> str:
    """Plain-English outcome: placed / insufficient capacity / insufficient quota / error / unknown,
    with the detail appended when there is one ('insufficient quota: standardDSv5Family')."""
    if p.error:
        return "error"
    label = "unknown" if p.fulfillment is None else PROBE_LABELS.get(p.fulfillment, p.fulfillment)
    return f"{label}: {p.detail}" if p.detail else label


def split_summary(p: ProbeResult) -> str:
    """'1:4 2:4 3:4' (zone:count), '12' (regional), or 'Standard_D8s_v5@1:8 Standard_E8s_v5@1:4' for a mix."""
    parts = []
    for s in p.split:
        cap = s.get("capacity")
        cap_max = s.get("capacity_max")
        count = f"{cap}-{cap_max}" if cap_max not in (None, cap) else str(cap)
        key = "@".join(k for k in ((s.get("name") or "") if p.sku == "mix" else "", s.get("zone") or "") if k)
        parts.append(f"{key}:{count}" if key else count)
    return " ".join(parts)


def pair_placement_notes(pairs: list[dict], probes: list[ProbeResult]) -> None:
    """Annotate pair rows with what the probes say about failing over: 'pair can place X' etc.

    Capacity and quota are never conflated: a quota verdict on either side is reported as
    'quota blocks X on ...' rather than as the pair being able or unable to place."""
    by_region: dict[str, dict[tuple, ProbeResult]] = defaultdict(dict)
    for p in probes:
        by_region[p.region][(p.sku, tuple(p.skus), p.count, p.spot)] = p
    for row in pairs:
        notes: list[str] = []
        for key, a in by_region.get(row["region"], {}).items():
            b = by_region.get(row["pair"] or "", {}).get(key)
            if b is None or a.error or b.error:
                continue
            what = "+".join(a.skus) if a.sku == "mix" else a.sku
            quota_side = [a.fulfillment == "InsufficientQuota", b.fulfillment == "InsufficientQuota"]
            if all(quota_side):
                notes.append(f"quota blocks {what} on both sides")
            elif quota_side[0]:
                notes.append(f"quota blocks {what} on {row['region']}")
            elif quota_side[1]:
                notes.append(f"quota blocks {what} on the pair")
            elif a.fulfillment == "InsufficientCapacity" and b.fulfillment == "None":
                notes.append(f"pair can place {what}")
            elif a.fulfillment == "None" and b.fulfillment == "InsufficientCapacity":
                notes.append(f"pair cannot place {what}")
            elif a.fulfillment == "InsufficientCapacity" and b.fulfillment == "InsufficientCapacity":
                notes.append(f"neither side can place {what}")
        row["placement_notes"] = notes


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #


def _to_int(v: Any) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _to_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def classify(raw: dict) -> SkuRow:
    region = raw["region"]
    zones_total = list(raw.get("zones") or [])
    blocked: set[str] = set()
    region_blocked = False
    quota_blocked = False
    reasons: list[str] = []

    for r in raw.get("restrictions", []):
        # enum reprs vary by SDK version: 'Zone' / 'ResourceSkuRestrictionsType.ZONE',
        # 'QuotaId' / 'ResourceSkuRestrictionsReasonCode.QUOTA_ID' — normalize both
        rtype = r["type"].split(".")[-1].replace("_", "").lower()
        reason = r["reason"].split(".")[-1].replace("_", "").lower()
        locs = r.get("locations") or []
        if locs and region not in locs:
            continue
        reasons.append(f"{rtype}:{reason}")
        if reason == "quotaid":
            quota_blocked = True
        elif rtype == "location":
            region_blocked = True
        elif rtype == "zone":
            # the API can list zones the SKU isn't offered in; only count zones it actually has
            blocked.update(set(r.get("zones") or []) & set(zones_total) if zones_total else set(r.get("zones") or []))

    if quota_blocked and not region_blocked and not blocked:
        status, loss = "quota_blocked", 0.0
    elif region_blocked:
        status, loss = "region_restricted", 1.0
    elif blocked:
        status = "zone_restricted"
        loss = min(1.0, len(blocked) / len(zones_total)) if zones_total else 0.5
    else:
        status, loss = "available", 0.0

    caps = raw.get("capabilities", {})
    return SkuRow(
        region=region,
        sku=raw["name"],
        family=raw.get("family") or "",
        vcpus=_to_int(caps.get("vCPUs")),
        memory_gb=_to_float(caps.get("MemoryGB")),
        zones_total=len(zones_total),
        zones_available=sorted(set(zones_total) - blocked),
        zones_blocked=sorted(blocked),
        status=status,
        reason_codes=sorted(set(reasons)),
        loss=round(loss, 3),
    )


def family_label(family: str) -> str:
    """standardDv5Family -> Dv5 ; standardNCADSA100v4Family -> NCADSA100v4"""
    f = family.strip()
    if f.lower().startswith("standard"):
        f = f[len("standard") :]
    if f.lower().endswith("family"):
        f = f[: -len("family")]
    return f.strip() or "(none)"


def matches_filters(
    row: SkuRow, families: list[str], sku_glob: str | None, min_vcpu: int, exclude: list[str] = ()
) -> bool:
    lab = family_label(row.family).lower()
    if families and not any(lab.startswith(f.lower()) for f in families):
        return False
    if exclude and any(fnmatch.fnmatch(lab, e.lower()) for e in exclude):
        return False
    if sku_glob and not fnmatch.fnmatch(row.sku.lower(), sku_glob.lower()):
        return False
    if min_vcpu and (row.vcpus or 0) < min_vcpu:
        return False
    return True


def summarize(rows: list[SkuRow]) -> list[FamilySummary]:
    groups: dict[tuple[str, str], list[SkuRow]] = defaultdict(list)
    for r in rows:
        groups[(r.region, family_label(r.family))].append(r)
    out: list[FamilySummary] = []
    for (region, fam), items in sorted(groups.items()):
        scored = [i for i in items if i.status != "quota_blocked"]
        score = round(100 * sum(i.loss for i in scored) / len(scored), 1) if scored else None
        out.append(
            FamilySummary(
                region=region,
                family=fam,
                n_skus=len(items),
                n_available=sum(i.status == "available" for i in items),
                n_zone_restricted=sum(i.status == "zone_restricted" for i in items),
                n_region_restricted=sum(i.status == "region_restricted" for i in items),
                n_quota_blocked=sum(i.status == "quota_blocked" for i in items),
                n_assessable=len(scored),
                score=score,
                zone_slots_total=sum(i.zones_total for i in items),
                zone_slots_blocked=sum(len(i.zones_blocked) for i in items),
            )
        )
    return out


def region_scores(summ: list[FamilySummary], weighting: str = "sku") -> dict[str, float | None]:
    """Mean family restriction score per region, weighted by assessed SKUs or equally by family."""
    totals: dict[str, float] = defaultdict(float)
    weights: dict[str, int] = defaultdict(int)
    regions = {s.region for s in summ}
    for s in summ:
        if s.score is None:
            continue
        weight = s.n_assessable if weighting == "sku" else 1
        totals[s.region] += s.score * weight
        weights[s.region] += weight
    return {r: round(totals[r] / weights[r], 1) if weights[r] else None for r in regions}


def pair_summary(
    primaries: list[str], locs: dict[str, dict], summ: list[FamilySummary], rscores: dict[str, float | None]
) -> list[dict]:
    """One row per requested region: its pair, geography relationship, and score comparison."""
    fam_idx: dict[tuple[str, str], FamilySummary] = {(s.region, s.family): s for s in summ}
    fams = sorted({s.family for s in summ})
    rows = []
    for r in primaries:
        meta = locs.get(r) or {}
        pair = meta.get("pair")
        pmeta = locs.get(pair) or {} if pair else {}
        per_family = []
        for f in fams:
            a, b = fam_idx.get((r, f)), fam_idx.get((pair, f)) if pair else None
            if a is None and b is None:
                continue
            per_family.append(
                {
                    "family": f,
                    "primary": a.score if a else None,
                    "pair": b.score if b else None,
                    "pair_missing": pair is not None and b is None,
                }
            )
        # Families heavily restricted on both sides: nowhere to fail over.
        # pair lacking the family entirely counts as constrained (100); no pair at all -> not applicable
        pair_has_data = pair is not None and any(s.region == pair for s in summ)
        both_bad = [
            p["family"]
            for p in per_family
            if pair_has_data
            and p["primary"] is not None
            and p["primary"] >= 50
            and ((p["pair_missing"] and p["pair"] is None) or (p["pair"] is not None and p["pair"] >= 50))
        ]
        rows.append(
            {
                "region": r,
                "geography": meta.get("geography"),
                "geography_group": meta.get("geography_group"),
                "zonal": meta.get("zonal"),
                "score": rscores.get(r),
                "pair": pair,
                "pair_geography": pmeta.get("geography"),
                "pair_zonal": pmeta.get("zonal"),
                "pair_score": rscores.get(pair) if pair else None,
                "same_geography": (meta.get("geography") == pmeta.get("geography")) if pair and pmeta else None,
                "pair_has_data": pair_has_data,
                "per_family": per_family,
                "both_constrained": both_bad,
            }
        )
    return rows


def read_json_object(path: Path, label: str) -> dict:
    """Read a JSON object with concise, CLI-friendly errors."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"Cannot read {label} {path}: {e}") from e
    try:
        value = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in {label} {path}: line {e.lineno}, column {e.colno}") from e
    if not isinstance(value, dict):
        raise ValueError(f"Invalid {label} {path}: expected a JSON object")
    return value


def identifier_fingerprint(value: Any) -> str | None:
    """Stable comparison token for identifiers without requiring them in shareable reports."""
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip().lower()
    if text.startswith("sha256:"):
        return text
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _snapshot_fingerprint(meta: dict, key: str) -> str | None:
    return meta.get(f"{key}_fingerprint") or identifier_fingerprint(meta.get(key))


def _row_state(row: SkuRow) -> str:
    zones = f" zones={','.join(row.zones_blocked)}" if row.zones_blocked else ""
    return f"{row.status}{zones}"


def diff_against(
    rows: list[SkuRow],
    baseline_path: Path,
    *,
    current_meta: dict,
    families: list[str],
    sku_glob: str | None,
    min_vcpu: int,
    exclude: list[str],
    zonal: bool,
    allow_incompatible: bool = False,
) -> tuple[list[dict], list[str]]:
    """Diff the complete current/baseline key union after applying the current scope."""
    base = read_json_object(baseline_path, "baseline")
    raw_skus = base.get("skus")
    if not isinstance(raw_skus, list):
        raise ValueError(f"Invalid baseline {baseline_path}: missing 'skus' array")

    base_meta = base.get("meta") if isinstance(base.get("meta"), dict) else {}
    incompatible: list[str] = []
    notes: list[str] = []
    old_schema, new_schema = base_meta.get("schema_version"), current_meta.get("schema_version")
    if old_schema is not None and new_schema is not None and old_schema != new_schema:
        incompatible.append(f"snapshot schema differs ({old_schema} vs {new_schema})")
    elif old_schema is None and new_schema is not None:
        notes.append("baseline predates snapshot schema versioning")
    for key, label in (("subscription", "subscription"), ("tenant_id", "tenant")):
        old, new = _snapshot_fingerprint(base_meta, key), _snapshot_fingerprint(current_meta, key)
        if old and new and old != new:
            incompatible.append(f"{label} differs")
        elif not old:
            notes.append(f"baseline does not record a comparable {label} identifier")

    current_regions = set(current_meta.get("regions") or [])
    baseline_regions = set(base_meta.get("regions") or [])
    missing_regions = sorted(current_regions - baseline_regions) if baseline_regions else []
    if missing_regions:
        incompatible.append("baseline lacks region(s): " + ", ".join(missing_regions))
    elif not baseline_regions:
        notes.append("baseline does not record its region scope")

    if incompatible and not allow_incompatible:
        details = "; ".join(incompatible)
        raise ValueError(
            f"Baseline is incompatible with this run: {details}. "
            "Use --allow-incompatible-compare only if this comparison is intentional."
        )
    notes.extend(incompatible)

    try:
        previous_rows = [classify(r) for r in raw_skus if r.get("region") in current_regions]
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        raise ValueError(f"Invalid SKU record in baseline {baseline_path}: {e}") from e
    previous_rows = [r for r in previous_rows if matches_filters(r, families, sku_glob, min_vcpu, exclude)]
    if zonal:
        previous_rows = [r for r in previous_rows if r.zones_total > 0]

    previous = {(r.region, r.sku): r for r in previous_rows}
    current = {(r.region, r.sku): r for r in rows}
    changes: list[dict] = []
    for key in sorted(previous.keys() | current.keys()):
        old, new = previous.get(key), current.get(key)
        region, sku = key
        if old is None and new is not None:
            changes.append({"region": region, "sku": sku, "from": "(new)", "to": _row_state(new)})
        elif new is None and old is not None:
            changes.append({"region": region, "sku": sku, "from": _row_state(old), "to": "(removed)"})
        elif (
            old is not None and new is not None and (old.status != new.status or old.zones_blocked != new.zones_blocked)
        ):
            changes.append({"region": region, "sku": sku, "from": _row_state(old), "to": _row_state(new)})
    return changes, notes


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


def write_csvs(
    out: Path, rows: list[SkuRow], summ: list[FamilySummary], quota: list[QuotaRow], probes: list[ProbeResult] = ()
) -> None:
    with (out / "skus.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "region",
                "sku",
                "family",
                "vcpus",
                "memory_gb",
                "zones_total",
                "zones_available",
                "zones_blocked",
                "status",
                "reason_codes",
                "loss",
            ]
        )
        for r in rows:
            w.writerow(
                [
                    r.region,
                    r.sku,
                    family_label(r.family),
                    r.vcpus,
                    r.memory_gb,
                    r.zones_total,
                    " ".join(r.zones_available),
                    " ".join(r.zones_blocked),
                    r.status,
                    " ".join(r.reason_codes),
                    r.loss,
                ]
            )
    with (out / "summary.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(summ[0]).keys()) if summ else ["region"])
        w.writeheader()
        for s in summ:
            w.writerow(asdict(s))
    if quota:
        with (out / "quota.csv").open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["region", "family", "localized", "current", "limit", "headroom_pct"])
            for q in quota:
                w.writerow([q.region, family_label(q.family), q.localized, q.current, q.limit, q.headroom_pct])
    else:
        (out / "quota.csv").unlink(missing_ok=True)
    if probes:
        with (out / "probes.csv").open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(
                [
                    "region",
                    "sku",
                    "count",
                    "zonal",
                    "spot",
                    "score",
                    "fulfillment",
                    "detail",
                    "split_summary",
                    "error",
                    "skus",
                ]
            )
            for p in probes:
                w.writerow(
                    [
                        p.region,
                        p.sku,
                        p.count,
                        p.zonal,
                        p.spot,
                        p.score,
                        p.fulfillment,
                        p.detail,
                        split_summary(p),
                        p.error,
                        " ".join(p.skus),
                    ]
                )
    else:
        (out / "probes.csv").unlink(missing_ok=True)


def json_for_html(value: Any) -> str:
    """Serialize data for an executable script block without allowing a closing-tag breakout."""
    return json.dumps(value, ensure_ascii=True).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def write_html(
    out: Path,
    rows: list[SkuRow],
    summ: list[FamilySummary],
    quota: list[QuotaRow],
    changes: list[dict],
    meta: dict,
    pairs: list[dict],
    locs: dict[str, dict],
    probes: list[ProbeResult] = (),
) -> None:
    template = (Path(__file__).parent / "report_template.html").read_text(encoding="utf-8")
    report_meta = {k: v for k, v in meta.items() if not k.endswith("_fingerprint")}
    payload = {
        "meta": report_meta,
        "regionScores": region_scores(summ, meta.get("region_weighting", "sku")),
        "pairs": pairs,
        "locations": {r: locs[r] for r in meta["regions"] if r in locs},
        "summary": [asdict(s) for s in summ],
        "skus": [dict(asdict(r), family=family_label(r.family)) for r in rows],
        "quota": [dict(asdict(q), family=family_label(q.family), headroom_pct=q.headroom_pct) for q in quota],
        "changes": changes,
        "probes": [asdict(p) for p in probes],
    }
    html = template.replace("/*__DATA__*/null", json_for_html(payload))
    (out / "report.html").write_text(html, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError("must be an integer") from e
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def ordered_unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def package_version() -> str:
    try:
        return version("azcap")
    except PackageNotFoundError:
        return "0.1.0+source"


def display_identifier(value: str, redact: bool) -> str:
    if not value:
        return ""
    return (identifier_fingerprint(value) or "") if redact else value


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(
        prog="azcap",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""\
Azure VM capacity scanner.

Reads the Resource SKUs API (what `az vm list-skus` shows) for each region and reports the share
of VM SKUs that are restricted for THIS subscription — per region, per family, and per
availability zone — plus the same view for each region's paired region. With --need it also asks
the Compute Recommender (preview) whether a specific allocation would place today.

  zone-adjusted % restricted   0 = no assessed SKU restricted, 100 = all restricted
    region-restricted SKU  -> counts fully      (NotAvailableForSubscription @ Location)
    zone-restricted SKU    -> fraction of zones blocked, e.g. 1 of 3 = 0.33
    quota-blocked SKU      -> reported, but not assessed (offer/quota eligibility, not capacity)

Restrictions are a subscription-specific availability proxy, not a published Azure capacity figure.
Run under the subscription that will actually deploy.""",
        epilog="""\
examples:
  azcap --regions eastus,eastus2,canadacentral
      scan three regions (and their pairs), all VM families, current az login

  azcap --regions eastus,brazilsouth --zonal --exclude-families "*Promo,D,DS,G,GS,LS"
      zone-resilient view: only SKUs offered in AZs, retired families dropped

  azcap --regions saudiarabiaeast,uaenorth --tenant <tenant-id> --subscription <sub-id>
      scan under a specific tenant/subscription; report is stamped with both

  azcap --regions eastus --families Dv5,DSv5,Ev5,ESv5,NC,ND --include-quota
      only the families you care about, with vCPU quota headroom per family

  azcap --regions eastus --compare out-lastweek/raw.json --out out-today
      diff against an earlier snapshot; changes appear in the report

  azcap --regions eastus2,brazilsouth --zonal --need Standard_D8s_v5:12
      ask Microsoft's Compute Recommender (preview) whether 12 x Standard_D8s_v5 would place
      across zones in each region right now; score 0-9 and reason appear next to the restrictions

  azcap --regions eastus,brazilsouth --fixture fixtures/sample.json
      offline run against a saved raw.json (no Azure calls)

outputs (in --out, default ./out):
  report.html   self-contained report: heatmap, pairs, placement probes, zone restrictions, quota, SKU table
  summary.csv   region x family counts and score      skus.csv   one row per region x SKU
  pairs.csv     region vs paired region               raw.json   API snapshot; reuse with --compare/--fixture
  probes.csv    with --need: one row per region x placement probe

auth: Azure CLI login by default (any DefaultAzureCredential source works).
      az login [--tenant <id>]   then   az account list -o table   to see what you're signed into.
""",
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {package_version()}")

    g = ap.add_argument_group("scope")
    g.add_argument(
        "--regions",
        required=True,
        metavar="R1,R2,...",
        help="region names to scan, e.g. eastus,canadacentral (az account list-locations -o table)",
    )
    g.add_argument(
        "--no-pairs",
        action="store_true",
        help="do not add each region's Microsoft-designated paired region to the scan",
    )
    g.add_argument(
        "--region-weighting",
        choices=("sku", "family"),
        default="sku",
        help="aggregate the region figure by assessed SKU count or equally by family (default: sku)",
    )

    g = ap.add_argument_group("identity")
    g.add_argument(
        "--subscription", metavar="ID", help="subscription id (default: $AZURE_SUBSCRIPTION_ID, else `az account show`)"
    )
    g.add_argument(
        "--tenant",
        metavar="ID",
        help="Entra tenant id to authenticate against; refuses to run if the subscription belongs elsewhere",
    )
    g.add_argument(
        "--redact-identifiers",
        action="store_true",
        help="replace tenant/subscription identifiers and omit the subscription name in output artifacts",
    )

    g = ap.add_argument_group("filters")
    g.add_argument(
        "--families",
        default="",
        metavar="F1,F2,...",
        help="keep only families whose label starts with one of these, e.g. Dv5,Ev5,NC,ND",
    )
    g.add_argument(
        "--exclude-families",
        default="",
        metavar="G1,G2,...",
        help="drop families matching these globs, e.g. 'D,DS,G,GS,*Promo,LS,LSv2'",
    )
    g.add_argument(
        "--sku", dest="sku_glob", metavar="GLOB", help="keep only SKU names matching a glob, e.g. 'Standard_D*s_v5'"
    )
    g.add_argument("--min-vcpu", type=nonnegative_int, default=0, metavar="N", help="drop SKUs smaller than N vCPUs")
    g.add_argument(
        "--zonal",
        action="store_true",
        help="keep only SKUs offered in availability zones in that region; regions without AZs show n/a",
    )

    g = ap.add_argument_group("data")
    g.add_argument(
        "--include-quota",
        action="store_true",
        help="also pull compute vCPU quota (used vs limit) per family per region",
    )
    g.add_argument(
        "--compare", metavar="RAW.JSON", help="diff against a previous run's raw.json and list SKU status changes"
    )
    g.add_argument(
        "--allow-incompatible-compare",
        action="store_true",
        help="allow a baseline from a different subscription, tenant, or incomplete region scope",
    )
    g.add_argument(
        "--need",
        metavar="SKU:COUNT[,SKU:COUNT...]",
        help="placement probe: ask whether COUNT VMs of SKU would place in each region right now "
        "(one probe per SKU per region; zones follow --zonal)",
    )
    g.add_argument(
        "--need-mix",
        action="store_true",
        help="send all --need SKUs as one request, ranked in the order given, and let Azure pick the split",
    )
    g.add_argument(
        "--os", choices=("Linux", "Windows"), default="Linux", help="OS for placement probes (default: Linux)"
    )
    g.add_argument("--spot", action="store_true", help="probe spot placement instead of regular")
    g.add_argument(
        "--probe-api-version",
        default=PROBE_API_VERSION,
        metavar="VER",
        help=f"api-version for the placement probe endpoint (preview; default: {PROBE_API_VERSION})",
    )
    g.add_argument(
        "--fixture", metavar="RAW.JSON", help="offline: read SKU data from a saved raw.json instead of calling Azure"
    )
    g.add_argument("--out", default="out", metavar="DIR", help="output directory (default: out)")

    args = ap.parse_args()
    needs: list[tuple[str, int]] = []
    if args.need:
        try:
            needs = parse_need(args.need)
        except ValueError as e:
            ap.error(str(e))
    if args.need_mix and not needs:
        ap.error("--need-mix requires --need")
    mix = args.need_mix and len(needs) > 1

    primaries = ordered_unique([r.strip().lower() for r in args.regions.split(",") if r.strip()])
    families = ordered_unique([f.strip() for f in args.families.split(",") if f.strip()])
    if not primaries:
        ap.error("--regions must contain at least one region name")
    out = Path(args.out)
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        sys.exit(f"Cannot create output directory {out}: {e}")

    quota: list[QuotaRow] = []
    probes: list[ProbeResult] = []
    locs: dict[str, dict] = {}
    if args.fixture:
        try:
            fx = read_json_object(Path(args.fixture), "fixture")
        except ValueError as e:
            sys.exit(str(e))
        if not isinstance(fx.get("skus"), list):
            sys.exit(f"Invalid fixture {args.fixture}: missing 'skus' array")
        if not isinstance(fx.get("locations", {}), dict):
            sys.exit(f"Invalid fixture {args.fixture}: 'locations' must be an object")
        locs = fx.get("locations", {})
        if any(not isinstance(r, dict) or "region" not in r for r in fx["skus"]):
            sys.exit(f"Invalid record in fixture {args.fixture}: every SKU must be an object with a 'region'")
        present = set(locs) | {str(r["region"]).lower() for r in fx["skus"]}
        unknown = [r for r in primaries if r not in present]
        for r in unknown:
            print(f"  ! {r}: not in fixture {args.fixture} — skipped", file=sys.stderr)
        primaries = [r for r in primaries if r in present]
        if not primaries:
            sys.exit(f"None of the requested regions are in fixture {args.fixture}.")
        regions, added = (primaries, {}) if args.no_pairs else expand_with_pairs(primaries, locs)
        try:
            raw = [r for r in fx["skus"] if r["region"] in regions]
            if args.include_quota:
                quota_values = fx.get("quota", [])
                if not isinstance(quota_values, list):
                    raise ValueError("'quota' must be an array")
                quota = [QuotaRow(**q) for q in quota_values if q["region"] in regions]
            probe_values = fx.get("probes", [])
            if not isinstance(probe_values, list):
                raise ValueError("'probes' must be an array")
            probes = [ProbeResult(**pv) for pv in probe_values if pv["region"] in regions]
        except (KeyError, TypeError, ValueError) as e:
            sys.exit(f"Invalid record in fixture {args.fixture}: {e}")
        if needs and not probes:
            print(f"  fixture {args.fixture} has no 'probes' list — placement probes skipped", file=sys.stderr)
        fx_meta = fx.get("meta") if isinstance(fx.get("meta"), dict) else {}
        subscription = str(fx_meta.get("subscription", "fixture"))
        subinfo = {
            "subscription_name": fx_meta.get("subscription_name", "synthetic fixture"),
            "tenant_id": str(fx_meta.get("tenant_id", "fixture")),
        }
    else:
        subscription = resolve_subscription(args.subscription)
        credential = make_credential(args.tenant)
        try:
            subinfo = describe_subscription(credential, subscription, args.tenant)
            locs = fetch_locations_live(credential, subscription)
        except Exception as e:  # ClientAuthenticationError and friends
            if "Credential" in type(e).__name__ or "token" in str(e).lower():
                hint = f" --tenant {args.tenant}" if args.tenant else ""
                sys.exit(
                    "Azure authentication failed (token missing or expired). Run:\n"
                    f"  az login{hint} --scope https://management.azure.com/.default\n"
                    "then rerun."
                )
            sys.exit(f"Azure request failed while reading subscription metadata ({type(e).__name__}): {e}")
        shown_tenant = display_identifier(str(subinfo["tenant_id"]), args.redact_identifiers)
        shown_subscription = display_identifier(subscription, args.redact_identifiers)
        shown_name = "(redacted)" if args.redact_identifiers else subinfo["subscription_name"]
        print(f"Tenant {shown_tenant} | subscription {shown_name} ({shown_subscription})", file=sys.stderr)
        unknown = [r for r in primaries if r not in locs]
        if unknown:
            if not locs:
                sys.exit(
                    "Subscriptions API returned no physical regions for this subscription; cannot resolve "
                    "region names or pairs. Retry with --no-pairs to scan without region metadata."
                )
            # A region the Subscriptions API doesn't list is either misspelled or not enabled for this
            # subscription (access-restricted / newer regions). Report it, don't abort.
            for r in unknown:
                near = [n for n in locs if r[:6] in n][:5]
                hint = f" (did you mean: {', '.join(near)}?)" if near else ""
                print(f"  ! {r}: not visible to this subscription — skipped{hint}", file=sys.stderr)
            primaries = [r for r in primaries if r in locs]
            if not primaries:
                sys.exit(
                    "None of the requested regions are visible to this subscription. "
                    "Use `az account list-locations -o table` for the names it can see."
                )
        regions, added = (primaries, {}) if args.no_pairs else expand_with_pairs(primaries, locs)
        for p, src in added.items():
            print(f"  + {p} (pair of {src})", file=sys.stderr)
        unpaired = [r for r in primaries if not locs[r].get("pair")]
        if unpaired and not args.no_pairs:
            print(f"  no paired region: {', '.join(unpaired)}", file=sys.stderr)
        print(f"Scanning {len(regions)} region(s) under subscription {shown_subscription}", file=sys.stderr)
        try:
            raw = fetch_skus_live(credential, subscription, regions)
            if args.include_quota:
                quota = fetch_quota_live(credential, subscription, regions)
        except Exception as e:
            sys.exit(f"Azure request failed while collecting SKU/quota data ({type(e).__name__}): {e}")
        if needs:
            # Preview API: every failure is recorded per probe and never changes the restriction results.
            print(f"Placement probes: {1 if mix else len(needs)} per region via Compute Recommender", file=sys.stderr)
            probes = fetch_probes_live(
                credential,
                subscription,
                regions,
                needs,
                mix=mix,
                os_type=args.os,
                spot=args.spot,
                zonal=args.zonal,
                raw=raw,
                locs=locs,
                api_version=args.probe_api_version,
                quota=quota if args.include_quota else None,
            )

    try:
        rows = [classify(r) for r in raw]
    except (KeyError, TypeError, ValueError) as e:
        source = f"fixture {args.fixture}" if args.fixture else "Azure response"
        sys.exit(f"Invalid SKU record in {source}: {e}")
    exclude = ordered_unique([e.strip() for e in args.exclude_families.split(",") if e.strip()])
    rows = [r for r in rows if matches_filters(r, families, args.sku_glob, args.min_vcpu, exclude)]
    if args.zonal:
        before = {reg: sum(1 for r in rows if r.region == reg) for reg in regions}
        rows = [r for r in rows if r.zones_total > 0]
        for reg in regions:
            after = sum(1 for r in rows if r.region == reg)
            if after == 0 and before.get(reg):
                print(
                    f"  {reg}: no zonal SKUs (region has no availability zones) — dropped by --zonal", file=sys.stderr
                )
    if not rows:
        sys.exit("No SKUs matched. Check region names / filters.")

    if families:
        want = [f.lower() for f in families]
        quota = [q for q in quota if any(family_label(q.family).lower().startswith(f) for f in want)]

    original_tenant = str(subinfo.get("tenant_id") or "")
    shown_subscription = display_identifier(subscription, args.redact_identifiers)
    shown_tenant = display_identifier(original_tenant, args.redact_identifiers)
    meta = {
        "schema_version": 1,
        "azcap_version": package_version(),
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "subscription": shown_subscription,
        "subscription_fingerprint": identifier_fingerprint(subscription),
        "subscription_name": "" if args.redact_identifiers else subinfo["subscription_name"],
        "tenant_id": shown_tenant,
        "tenant_id_fingerprint": identifier_fingerprint(original_tenant),
        "identifiers_redacted": bool(args.redact_identifiers),
        "regions": regions,
        "primaries": primaries,
        "not_visible": unknown,
        "pair_of": added,
        "families": families,
        "exclude_families": exclude,
        "zonal_only": bool(args.zonal),
        "sku_glob": args.sku_glob,
        "min_vcpu": args.min_vcpu,
        "region_weighting": args.region_weighting,
        "include_quota": bool(args.include_quota),
        "compare": args.compare,
        "need": [{"sku": sku, "count": count} for sku, count in needs],
        "need_mix": bool(mix),
        "probe_os": args.os,
        "probe_spot": bool(args.spot),
        "probe_api_version": args.probe_api_version if needs and not args.fixture else None,
    }
    summ = summarize(rows)
    changes: list[dict] = []
    compare_notes: list[str] = []
    if args.compare:
        try:
            changes, compare_notes = diff_against(
                rows,
                Path(args.compare),
                current_meta=meta,
                families=families,
                sku_glob=args.sku_glob,
                min_vcpu=args.min_vcpu,
                exclude=exclude,
                zonal=args.zonal,
                allow_incompatible=args.allow_incompatible_compare,
            )
        except ValueError as e:
            sys.exit(str(e))
        for note in compare_notes:
            print(f"  ! baseline comparison: {note}", file=sys.stderr)
    meta["compare_notes"] = compare_notes

    rs = region_scores(summ, args.region_weighting)
    pairs = [] if args.no_pairs else pair_summary(primaries, locs, summ, rs)
    pair_placement_notes(pairs, probes)

    snapshot = {
        "meta": meta,
        "skus": raw,
        "locations": locs,
        "quota": [asdict(q) for q in quota],
        "probes": [asdict(p) for p in probes],
    }
    (out / "raw.json").write_text(json.dumps(snapshot, indent=1), encoding="utf-8")
    write_csvs(out, rows, summ, quota, probes)
    if pairs:
        with (out / "pairs.csv").open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(
                [
                    "region",
                    "geography",
                    "zonal",
                    "score",
                    "pair",
                    "pair_geography",
                    "pair_zonal",
                    "pair_score",
                    "same_geography",
                    "families_constrained_both_sides",
                    "placement_notes",
                ]
            )
            for p in pairs:
                w.writerow(
                    [
                        p["region"],
                        p["geography"],
                        p["zonal"],
                        p["score"],
                        p["pair"] or "(none)",
                        p["pair_geography"],
                        p["pair_zonal"],
                        p["pair_score"],
                        p["same_geography"],
                        " ".join(p["both_constrained"]),
                        "; ".join(p["placement_notes"]),
                    ]
                )
    else:
        (out / "pairs.csv").unlink(missing_ok=True)
    write_html(out, rows, summ, quota, changes, meta, pairs, locs, probes)

    # console summary
    print(f"\nZone-adjusted % of assessed VM SKUs restricted for this subscription ({args.region_weighting}-weighted):")
    for region in regions:
        n = sum(1 for r in rows if r.region == region)
        rr = sum(1 for r in rows if r.region == region and r.status == "region_restricted")
        zr = sum(1 for r in rows if r.region == region and r.status == "zone_restricted")
        qb = sum(1 for r in rows if r.region == region and r.status == "quota_blocked")
        tag = f"  (pair of {added[region]})" if region in added else ""
        sc = f"{rs[region]:5.1f}% restricted" if rs.get(region) is not None else "  n/a           "
        print(
            f"  {region:<22} {sc}   skus {n:4d}  region-restricted {rr:4d}  "
            f"zone-restricted {zr:4d}  quota-blocked {qb:4d}{tag}"
        )
    if pairs:
        print("\nPairs:")
        for p in pairs:
            if not p["pair"]:
                print(f"  {p['region']:<22} no paired region")
                continue
            geo = "same geo" if p["same_geography"] else f"CROSS-GEO ({p['pair_geography']})"
            zonal = "" if p["pair_zonal"] else ", pair has no zones"
            if not p["pair_has_data"]:
                zonal += ", no SKUs evaluated in pair"
            both = f", both sides constrained: {' '.join(p['both_constrained'])}" if p["both_constrained"] else ""
            placement = "".join(f", {note}" for note in p["placement_notes"])
            ps = f"{p['score']:5.1f}" if p["score"] is not None else "  n/a"
            pp = f"{p['pair_score']:5.1f}" if p["pair_score"] is not None else "  n/a"
            print(f"  {p['region']:<22} {ps}  ->  {p['pair']:<22} {pp}  {geo}{zonal}{both}{placement}")
    if probes:
        print(f"\nPlacement probes ({'spot' if args.spot else 'regular'} VMs, {args.os}; Compute Recommender preview):")
        for p in probes:
            scope = "zonal" if p.zonal else "regional"
            score = f"score {p.score}" if p.score is not None else "score -"
            tail = p.error or (f"split {split_summary(p)}" if p.split else "")
            print(f"  {p.region:<22} {probe_request_label(p):<36} {scope:<9} {score:<8} {probe_label(p):<22} {tail}")
    if changes:
        print(f"\n{len(changes)} change(s) since baseline — see report.html")
    extra = f", {out / 'probes.csv'}" if probes else ""
    print(f"\nWrote {out / 'report.html'}, {out / 'summary.csv'}, {out / 'skus.csv'}, {out / 'raw.json'}{extra}")


if __name__ == "__main__":
    main()
