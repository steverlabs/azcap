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

Workload verdicts (--profile)
-----------------------------
  A profile names the VMs a deployment needs (SKU, count, optional?) and whether it
  must be zone-resilient. Per region each VM gets the first verdict that applies:
  not offered -> restricted -> quota -> capacity -> deployable. A region is deployable
  only when every required VM is; --fail-on-blocked turns that into exit code 3.

Usage
-----
  python azcap.py --regions eastus,eastus2,canadacentral --families Dv5,Ev5,NC
  python azcap.py --regions saudiarabiaeast --include-quota --out ./out
  python azcap.py --regions eastus --fixture fixtures/sample.json   # offline
  python azcap.py --regions eastus --compare out/previous/raw.json  # diff
  python azcap.py --regions eastus2,brazilsouth --zonal --need Standard_D8s_v5:12
  python azcap.py --regions eastus2 --profile wave1.yaml --fail-on-blocked   # exit 3 if blocked

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
import math
import os
import re
import shutil
import subprocess
import sys
import time
import warnings
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


@dataclass
class ProfileVm:
    sku: str
    count: int
    optional: bool = False  # reported, never blocks the region verdict


@dataclass
class Profile:
    """A workload to judge regions against: what has to deploy, and whether it must span zones."""

    name: str
    os: str = "Linux"
    zonal: bool = False
    vms: list[ProfileVm] = field(default_factory=list)
    # Provenance and disk needs, e.g. from an Azure Migrate assessment. Recorded and shown; not judged yet.
    source: dict | None = None
    disks: list[dict] = field(default_factory=list)


@dataclass
class VmVerdict:
    region: str
    sku: str
    count: int
    optional: bool
    status: str  # deployable | not offered | restricted | quota | capacity | unknown
    reason: str
    family: str | None = None
    vcpu_need: int | None = None
    quota_limit: int | None = None
    quota_used: int | None = None
    probe_score: int | None = None
    probe_fulfillment: str | None = None


@dataclass
class RegionVerdict:
    region: str
    verdict: str  # deployable | blocked | unknown
    blocking: list[str]  # "quota: Standard_D8s_v5 (standardDSv5Family limit 0, family need 96 vCPU (this SKU 96))"
    vms: list[VmVerdict]


VM_BLOCKING_STATUSES = ("not offered", "restricted", "quota", "capacity")
EXIT_BLOCKED = 3

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


def arm_getter(credential):
    """A `get(url) -> (status, headers, text)` for ARM, same token handling as arm_poster."""
    import requests

    session = requests.Session()
    state: dict[str, Any] = {}

    def get(url: str) -> tuple[int, Any, str]:
        if "token" not in state:
            state["token"] = credential.get_token(ARM_SCOPE).token
        r = session.get(url, headers={"Authorization": f"Bearer {state['token']}"}, timeout=60)
        return r.status_code, r.headers, r.text

    return get


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
# Workload profiles and verdicts
# --------------------------------------------------------------------------- #

PROFILE_KEYS = {"name", "os", "zonal", "vms", "source", "disks"}
PROFILE_VM_KEYS = {"sku", "count", "optional"}
PROFILE_SOURCE_STR_KEYS = (
    "kind",
    "subscription",
    "project",
    "group",
    "assessment",
    "target_region",
    "sizing_criterion",
    "comfort_factor",
    "exported",
)
PROFILE_SOURCE_INT_KEYS = ("machines_total", "machines_required", "machines_optional")
PROFILE_SOURCE_KEYS = {*PROFILE_SOURCE_STR_KEYS, *PROFILE_SOURCE_INT_KEYS, "machines_excluded"}
PROFILE_DISK_KEYS = {"type", "size", "count"}


def _parse_profile_source(source: Any) -> dict | None:
    if source is None:
        return None
    if not isinstance(source, dict):
        raise ValueError("profile 'source' must be a mapping")
    unknown = sorted(set(source) - PROFILE_SOURCE_KEYS)
    if unknown:
        raise ValueError(f"profile 'source' has unknown key(s): {', '.join(unknown)}")
    if not isinstance(source.get("kind"), str) or not source["kind"].strip():
        raise ValueError("profile 'source.kind' is required and must be a string (e.g. azure-migrate)")
    for key in PROFILE_SOURCE_STR_KEYS:
        if source.get(key) is not None and not isinstance(source[key], str):
            raise ValueError(f"profile 'source.{key}' must be a string")
    for key in PROFILE_SOURCE_INT_KEYS:
        value = source.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValueError(f"profile 'source.{key}' must be a non-negative integer")
    excluded = source.get("machines_excluded")
    if excluded is not None:
        if not isinstance(excluded, dict) or any(
            not isinstance(k, str) or isinstance(v, bool) or not isinstance(v, int) or v < 0
            for k, v in excluded.items()
        ):
            raise ValueError("profile 'source.machines_excluded' must map reason names to non-negative counts")
    return dict(source)


def _parse_profile_disks(disks: Any) -> list[dict]:
    if disks is None:
        return []
    if not isinstance(disks, list):
        raise ValueError("profile 'disks' must be a list of {type, size, count}")
    parsed: list[dict] = []
    for i, d in enumerate(disks, 1):
        if not isinstance(d, dict):
            raise ValueError(f"profile disks[{i}] must be a mapping with type, size, count")
        unknown = sorted(set(d) - PROFILE_DISK_KEYS)
        if unknown:
            raise ValueError(
                f"profile disks[{i}] has unknown key(s): {', '.join(unknown)} (allowed: type, size, count)"
            )
        for key in ("type", "size"):
            if not isinstance(d.get(key), str) or not d[key].strip():
                raise ValueError(f"profile disks[{i}] '{key}' is required and must be a non-empty string")
        count = d.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError(f"profile disks[{i}] 'count' must be an integer of 1 or more, got {count!r}")
        parsed.append({"type": d["type"].strip(), "size": d["size"].strip(), "count": count})
    return parsed


def parse_profile(data: Any) -> Profile:
    """Validate a decoded profile (YAML or JSON) strictly; every problem is a ValueError with a short message."""
    if not isinstance(data, dict):
        raise ValueError("profile must be a mapping with name, vms and optionally os, zonal")
    unknown = sorted(set(data) - PROFILE_KEYS)
    if unknown:
        raise ValueError(
            f"profile has unknown key(s): {', '.join(unknown)} (allowed: name, os, zonal, vms, source, disks)"
        )
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("profile 'name' is required and must be a non-empty string")
    os_type = data.get("os", "Linux")
    if os_type not in ("Linux", "Windows"):
        raise ValueError(f"profile 'os' must be Linux or Windows, got {os_type!r}")
    zonal = data.get("zonal", False)
    if not isinstance(zonal, bool):
        raise ValueError(f"profile 'zonal' must be true or false, got {zonal!r}")
    vms = data.get("vms")
    if not isinstance(vms, list) or not vms:
        raise ValueError("profile 'vms' must be a non-empty list")
    parsed: list[ProfileVm] = []
    for i, vm in enumerate(vms, 1):
        if not isinstance(vm, dict):
            raise ValueError(f"profile vms[{i}] must be a mapping with sku and count")
        unknown = sorted(set(vm) - PROFILE_VM_KEYS)
        if unknown:
            raise ValueError(
                f"profile vms[{i}] has unknown key(s): {', '.join(unknown)} (allowed: sku, count, optional)"
            )
        sku = vm.get("sku")
        if not isinstance(sku, str) or not sku.strip() or any(c.isspace() for c in sku.strip()):
            raise ValueError(f"profile vms[{i}] 'sku' is required and must be a SKU name like Standard_D8s_v5")
        count = vm.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError(f"profile vms[{i}] ({sku}) 'count' must be an integer of 1 or more, got {count!r}")
        optional = vm.get("optional", False)
        if not isinstance(optional, bool):
            raise ValueError(f"profile vms[{i}] ({sku}) 'optional' must be true or false, got {optional!r}")
        sku = sku.strip()
        # a SKU may appear twice, as one required and one optional entry; never twice with the same flag
        if any(sku.lower() == existing.sku.lower() and optional == existing.optional for existing in parsed):
            raise ValueError(
                f"profile lists {sku} more than once as {'optional' if optional else 'required'}; "
                "a SKU may appear at most twice, once required and once optional"
            )
        parsed.append(ProfileVm(sku=sku, count=count, optional=optional))
    return Profile(
        name=name.strip(),
        os=os_type,
        zonal=zonal,
        vms=parsed,
        source=_parse_profile_source(data.get("source")),
        disks=_parse_profile_disks(data.get("disks")),
    )


def load_profile(path: Path) -> Profile:
    """Read a .yaml/.yml (PyYAML) or .json profile. Raises ValueError with a CLI-ready message."""
    suffix = path.suffix.lower()
    if suffix not in (".yaml", ".yml", ".json"):
        raise ValueError(f"profile {path}: expected a .yaml, .yml or .json file")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"Cannot read profile {path}: {e}") from e
    if suffix == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in profile {path}: line {e.lineno}, column {e.colno}") from e
    else:
        try:
            import yaml
        except ImportError as e:  # pyyaml is a declared dependency, but a .json profile works without it
            raise ValueError("Reading a YAML profile needs PyYAML (pip install pyyaml), or use a .json profile") from e
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML in profile {path}: {e}") from e
    try:
        return parse_profile(data)
    except ValueError as e:
        raise ValueError(f"Invalid profile {path}: {e}") from e


def _quota_reason(q: QuotaRow, need: int, own: int) -> str:
    """`need` is the family total the check used (every required VM in the profile, plus this entry when it is
    optional); `own` is this entry's vcpus x count, so a reader can see how much of the family need is its own."""
    used = f", used {q.current}" if q.current else ""
    return f"{q.family} limit {q.limit}{used}, family need {need} vCPU (this SKU {own})"


def workload_verdicts(
    profile: Profile, regions: list[str], rows: list[SkuRow], quota: list[QuotaRow], probes: list[ProbeResult]
) -> list[RegionVerdict]:
    """Judge every region against the profile. Per VM the first of these that applies wins:
    not offered -> restricted -> quota -> capacity -> deployable (unknown when the probe failed).
    `rows` and `quota` must be the unfiltered scan so SKUs outside --families are still judged."""
    by_sku = {(r.region, r.sku.lower()): r for r in rows}
    quota_idx = {(q.region, q.family.lower()): q for q in quota}
    probe_idx = {(p.region, p.sku.lower(), p.count): p for p in probes if p.sku != "mix" and not p.spot}
    out: list[RegionVerdict] = []
    for region in regions:
        # quota is shared per family, so required VMs are checked against the family total; an optional VM
        # is checked on top of that total and can only ever block itself
        required_need: dict[str, int] = defaultdict(int)
        for vm in profile.vms:
            row = by_sku.get((region, vm.sku.lower()))
            if row is not None and row.vcpus and not vm.optional:
                required_need[row.family.lower()] += row.vcpus * vm.count
        vms: list[VmVerdict] = []
        for vm in profile.vms:
            v = VmVerdict(region=region, sku=vm.sku, count=vm.count, optional=vm.optional, status="", reason="")
            row = by_sku.get((region, vm.sku.lower()))
            if row is None:
                v.status, v.reason = "not offered", "SKU is not in this region's SKU list for the subscription"
                vms.append(v)
                continue
            v.family = row.family
            v.vcpu_need = row.vcpus * vm.count if row.vcpus else None
            q = quota_idx.get((region, row.family.lower()))
            if q is not None:
                v.quota_limit, v.quota_used = q.limit, q.current
            probe = probe_idx.get((region, vm.sku.lower(), vm.count))
            if probe is not None and not probe.error:
                v.probe_score, v.probe_fulfillment = probe.score, probe.fulfillment
            need = required_need[row.family.lower()] + ((v.vcpu_need or 0) if vm.optional else 0)
            if row.status == "region_restricted":
                v.status, v.reason = "restricted", "region restricted for this subscription"
            elif profile.zonal and row.zones_total == 0:
                v.status, v.reason = "restricted", "region has no availability zones; the profile needs zones"
            elif profile.zonal and len(row.zones_available) < 2:
                usable = ", ".join(row.zones_available) or "none"
                v.status = "restricted"
                v.reason = f"usable zones: {usable}; zone-resilient placement needs 2 or more"
            elif q is not None and v.vcpu_need is not None and (q.limit == 0 or q.limit - q.current < need):
                v.status, v.reason = "quota", _quota_reason(q, need, v.vcpu_need)
            elif row.status == "quota_blocked":
                v.status, v.reason = "quota", "QuotaId restriction for this subscription (offer or quota eligibility)"
            elif probe is None or probe.error:
                detail = f": {probe.error}" if probe is not None else ""
                v.status, v.reason = "unknown", f"placement probe unavailable{detail}; restrictions and quota allow it"
            elif probe.fulfillment == "InsufficientCapacity":
                placed = sum(item.get("capacity") or 0 for item in probe.split)
                score = f"score {probe.score}, " if probe.score is not None else ""
                v.status, v.reason = "capacity", f"{score}{placed} of {vm.count} placed"
            elif probe.fulfillment == "InsufficientQuota":
                v.status = "quota"
                v.reason = f"placement probe: insufficient quota{': ' + probe.detail if probe.detail else ''}"
            elif probe.fulfillment == "None":
                v.status, v.reason = "deployable", f"placed (score {probe.score})"
            else:
                v.status, v.reason = "unknown", f"placement probe answered {probe.fulfillment}"
            vms.append(v)
        required = [v for v in vms if not v.optional]
        blocking = [f"{v.status}: {v.sku} ({v.reason})" for v in required if v.status in VM_BLOCKING_STATUSES]
        if blocking:
            verdict = "blocked"
        elif any(v.status == "unknown" for v in required):
            verdict = "unknown"
        else:
            verdict = "deployable"
        out.append(RegionVerdict(region=region, verdict=verdict, blocking=blocking, vms=vms))
    return out


def probe_candidates(
    profile: Profile, regions: list[str], rows: list[SkuRow], quota: list[QuotaRow]
) -> dict[str, list[tuple[str, int]]]:
    """Profile VMs per region that pass steps 1-3 (offered, not restricted, within quota) and so need a
    placement probe; the rest are already decided and are not worth a call."""
    undecided = workload_verdicts(profile, regions, rows, quota, [])
    # a SKU listed as both required and optional with the same count needs one probe, not two
    return {
        rv.region: list(dict.fromkeys((v.sku, v.count) for v in rv.vms if v.status == "unknown")) for rv in undecided
    }


def _dr_reasons(rv: RegionVerdict) -> str:
    """Why a region is blocked, in the same words the placement notes use (quota is never 'cannot place')."""
    words = {"quota": "quota blocks {sku}", "capacity": "cannot place {sku}"}
    parts = [
        words.get(v.status, "{sku} " + v.status).format(sku=v.sku)
        for v in rv.vms
        if not v.optional and v.status in VM_BLOCKING_STATUSES
    ]
    return "; ".join(parts)


def pair_verdict_notes(pairs: list[dict], verdicts: list[RegionVerdict]) -> None:
    """Annotate pair rows with both verdicts and a DR note: DR-ready / pair blocked / primary blocked / both blocked."""
    by_region = {rv.region: rv for rv in verdicts}
    for row in pairs:
        a, b = by_region.get(row["region"]), by_region.get(row["pair"] or "")
        row["verdict"] = a.verdict if a else None
        row["pair_verdict"] = b.verdict if b else None
        note = ""
        if a is not None and b is not None:
            if a.verdict == "deployable" and b.verdict == "deployable":
                note = "DR-ready"
            elif a.verdict == "blocked" and b.verdict == "blocked":
                note = f"both blocked — {row['region']}: {_dr_reasons(a)}; pair: {_dr_reasons(b)}"
            elif b.verdict == "blocked" and a.verdict == "deployable":
                note = f"pair blocked: {_dr_reasons(b)}"
            elif a.verdict == "blocked" and b.verdict == "deployable":
                note = f"primary blocked: {_dr_reasons(a)}"
            else:
                unknown = [r.region for r in (a, b) if r.verdict == "unknown"]
                blocked = [f"{r.region} blocked: {_dr_reasons(r)}" for r in (a, b) if r.verdict == "blocked"]
                note = "; ".join(blocked + [f"{u} not determined (placement probe unavailable)" for u in unknown])
        row["dr_note"] = note


# --------------------------------------------------------------------------- #
# Profiles from Azure Migrate assessments
# --------------------------------------------------------------------------- #

MIGRATE_API_VERSION = "2023-03-15"
MIGRATE_SUITABILITIES = ("Suitable", "ConditionallySuitable", "NotSuitable", "Unknown")


@dataclass
class AssessedMachine:
    """One machine from an assessment, reduced to what a profile needs."""

    name: str
    size: str | None  # recommended Azure VM size; None when Migrate gave none
    suitability: str  # Suitable | ConditionallySuitable | NotSuitable | Unknown
    os: str | None  # Linux | Windows | None (unknown)
    explanation: str | None = None  # suitabilityExplanation, tallied for excluded machines
    disks: list[tuple[str, str]] = field(default_factory=list)  # (recommended disk type, size), e.g. (Premium_LRS, P30)


class MigrateError(ValueError):
    """A concise, user-facing problem reading an assessment (auth, 403/404, bad shape)."""


def migrate_assessment_id(subscription: str, resource_group: str, project: str, group: str, assessment: str) -> str:
    return (
        f"/subscriptions/{subscription}/resourceGroups/{resource_group}/providers/Microsoft.Migrate"
        f"/assessmentProjects/{project}/groups/{group}/assessments/{assessment}"
    )


def _migrate_get_json(get, url: str, what: str) -> dict:
    try:
        status, _headers, text = get(url)
    except Exception as e:
        if "Credential" in type(e).__name__ or "token" in str(e).lower():
            raise MigrateError(
                "Azure authentication failed (token missing or expired). Run:\n"
                "  az login --scope https://management.azure.com/.default\nthen rerun."
            ) from e
        raise MigrateError(f"Azure request failed while reading {what} ({type(e).__name__}): {e}") from e
    try:
        payload = json.loads(text) if text else None
    except ValueError:
        payload = None
    if status in (401, 403):
        raise MigrateError(
            f"Access denied ({status}) reading {what}: the signed-in identity needs Reader on the Azure Migrate "
            "project (check --subscription / --tenant and `az account show`)."
        )
    if status == 404:
        raise MigrateError(
            f"Not found (404): {what}. Check --resource-group, --project, --group and --assessment "
            "(names, not display names) and that --subscription is the one holding the Migrate project."
        )
    if status != 200:
        raise MigrateError(f"Azure request failed reading {what}: {_short_error(status, payload, text)}")
    if not isinstance(payload, dict):
        raise MigrateError(f"Unexpected response reading {what}: body is not a JSON object")
    return payload


def fetch_migrate_assessment(get, assessment_id: str) -> tuple[dict, list[dict]]:
    """GET the assessment and every page of its assessedMachines. Returns (assessment, machine resources)."""
    base = f"{ARM_ENDPOINT}{assessment_id}"
    assessment = _migrate_get_json(get, f"{base}?api-version={MIGRATE_API_VERSION}", "the assessment")
    machines: list[dict] = []
    url: str | None = f"{base}/assessedMachines?api-version={MIGRATE_API_VERSION}"
    seen: set[str] = set()
    while url and url not in seen:
        seen.add(url)
        page = _migrate_get_json(get, url, "assessed machines")
        values = page.get("value")
        if not isinstance(values, list):
            raise MigrateError("Unexpected response reading assessed machines: no 'value' list")
        machines.extend(v for v in values if isinstance(v, dict))
        url = page.get("nextLink") or None
    return assessment, machines


def _os_from_guest(value: Any) -> str | None:
    text = str(value or "").lower()
    if "windows" in text:
        return "Windows"
    if "linux" in text:
        return "Linux"
    return None


def machine_from_api(resource: dict) -> AssessedMachine:
    """Reduce an assessedMachines item (properties.*) to an AssessedMachine."""
    props = resource.get("properties") if isinstance(resource.get("properties"), dict) else {}
    disks: list[tuple[str, str]] = []
    for d in (props.get("disks") or {}).values() if isinstance(props.get("disks"), dict) else []:
        if isinstance(d, dict) and d.get("recommendedDiskType") and d.get("recommendedDiskSize"):
            disks.append((normalize_disk_type(d["recommendedDiskType"]), str(d["recommendedDiskSize"]).strip()))
    suitability = str(props.get("suitability") or "Unknown")
    return AssessedMachine(
        name=str(props.get("displayName") or resource.get("name") or "?"),
        size=str(props["recommendedSize"]).strip() or None if props.get("recommendedSize") else None,
        suitability=suitability if suitability in MIGRATE_SUITABILITIES else "Unknown",
        os=_os_from_guest(props.get("operatingSystemType")),
        explanation=str(props["suitabilityExplanation"]) if props.get("suitabilityExplanation") else None,
        disks=disks,
    )


def _os_from_name(value: Any) -> str | None:
    """'Windows Server 2019 Datacenter' -> Windows; 'Ubuntu 22.04' -> Linux; else None."""
    text = str(value or "").lower()
    if "windows" in text:
        return "Windows"
    if any(
        k in text for k in ("linux", "ubuntu", "centos", "red hat", "rhel", "suse", "sles", "debian", "rocky", "alma")
    ):
        return "Linux"
    return None


def _norm(value: Any) -> str:
    """Lower-case with whitespace collapsed, for matching export wording."""
    return " ".join(str(value or "").split()).lower()


def _suitability_from_readiness(value: Any) -> str:
    """Excel 'Azure VM readiness' wording -> API suitability value ('Ready With Conditions' is conditional)."""
    text = _norm(value)
    if "ready with conditions" in text or "conditionally ready" in text or "conditionally suitable" in text:
        return "ConditionallySuitable"
    if "not ready" in text or "not suitable" in text:
        return "NotSuitable"
    if "ready" in text or "suitable" in text:
        return "Suitable"
    return "Unknown"


DISK_TYPE_NAMES = (  # export wording -> API recommendedDiskType enum; order matters (premium v2 before premium)
    ("premium ssd v2", "PremiumV2"),
    ("premiumv2", "PremiumV2"),
    ("standard ssd", "StandardSSD"),
    ("standardssd", "StandardSSD"),
    ("standard hdd", "Standard"),
    ("ultra", "Ultra"),
    ("premium", "Premium"),
    ("standard", "Standard"),
)


def normalize_sizing_criterion(value: Any) -> str | None:
    """'Performance-based' -> PerformanceBased; 'As on-premises' / 'As-is' -> AsOnPremises; API enums pass through."""
    text = _norm(value).replace("-", " ")
    if not text:
        return None
    if "performance" in text:
        return "PerformanceBased"
    if "on prem" in text or "onprem" in text or text in ("as is", "asis"):
        return "AsOnPremises"
    return str(value).strip()


def normalize_disk_type(value: Any) -> str:
    """'Premium managed disks' / 'Standard SSD managed disks' / API 'Premium' -> Premium / StandardSSD / ..."""
    text = _norm(value)
    for needle, enum in DISK_TYPE_NAMES:
        if needle in text:
            return enum
    return str(value or "").strip()


def build_profiles(
    machines: list[AssessedMachine],
    *,
    name: str,
    conditional: str = "optional",
    headroom: int = 0,
    source: dict | None = None,
) -> tuple[list[Profile], dict]:
    """Turn assessed machines into one profile (or one per OS when mixed) plus a summary for the console.

    Suitable -> required; ConditionallySuitable -> --conditional (optional | required | skip);
    NotSuitable / Unknown / no recommended size -> excluded and tallied. Headroom inflates required
    counts by ceil(count * pct / 100)."""
    included: list[tuple[AssessedMachine, bool]] = []  # (machine, optional)
    excluded: dict[str, int] = defaultdict(int)
    explanations: dict[str, int] = defaultdict(int)
    by_suitability: dict[str, int] = defaultdict(int)
    for m in machines:
        by_suitability[m.suitability] += 1
        if m.suitability == "Suitable":
            optional = False
        elif m.suitability == "ConditionallySuitable" and conditional != "skip":
            optional = conditional == "optional"
        else:
            reason = "ConditionallySuitable (skipped)" if m.suitability == "ConditionallySuitable" else m.suitability
            excluded[reason] += 1
            if m.explanation:
                explanations[m.explanation] += 1
            continue
        if not m.size:
            excluded["NoRecommendedSize"] += 1
            continue
        included.append((m, optional))

    os_counts: dict[str, int] = defaultdict(int)
    for m, _ in included:
        if m.os:
            os_counts[m.os] += 1
    known_os = sorted(os_counts, key=lambda o: (-os_counts[o], o))
    majority = known_os[0] if known_os else "Linux"
    unknown_os = sum(1 for m, _ in included if not m.os)
    mixed = len(known_os) > 1

    profiles: list[Profile] = []
    for os_type in known_os or ["Linux"]:
        group = [(m, opt) for m, opt in included if (m.os or majority) == os_type]
        if not group:
            continue
        counts: dict[str, dict[str, int]] = defaultdict(lambda: {"required": 0, "optional": 0})
        disk_counts: dict[tuple[str, str], int] = defaultdict(int)
        for m, opt in group:
            counts[m.size]["optional" if opt else "required"] += 1
            for disk in m.disks:
                disk_counts[disk] += 1
        vms: list[ProfileVm] = []
        for sku in sorted(counts):
            # a size with both suitable and conditionally suitable machines becomes two entries
            c = counts[sku]
            if c["required"]:
                vms.append(ProfileVm(sku=sku, count=c["required"] + math.ceil(c["required"] * headroom / 100)))
            if c["optional"]:
                vms.append(ProfileVm(sku=sku, count=c["optional"], optional=True))
        disks = [{"type": t, "size": z, "count": n} for (t, z), n in sorted(disk_counts.items())]
        src = None
        if source is not None:
            src = {k: source[k] for k in PROFILE_SOURCE_STR_KEYS if source.get(k) is not None}
            src.update(
                machines_total=len(machines),
                machines_required=sum(1 for _, opt in group if not opt),
                machines_optional=sum(1 for _, opt in group if opt),
                machines_excluded=dict(sorted(excluded.items())),
            )
        profiles.append(
            Profile(
                name=f"{name}-{os_type.lower()}" if mixed else name,
                os=os_type,
                zonal=False,
                vms=vms,
                source=src,
                disks=disks,
            )
        )
    summary = {
        "by_suitability": dict(by_suitability),
        "excluded": dict(excluded),
        "explanations": dict(explanations),
        "included": len(included),
        "unknown_os": unknown_os,
        "majority_os": majority,
        "mixed_os": mixed,
        "headroom": headroom,
    }
    return profiles, summary


def profile_to_yaml(profile: Profile) -> str:
    """Profile as YAML, keys in schema order, without empty optional blocks."""
    import yaml

    data: dict[str, Any] = {
        "name": profile.name,
        "os": profile.os,
        "zonal": profile.zonal,
        "vms": [
            {"sku": vm.sku, "count": vm.count, **({"optional": True} if vm.optional else {})} for vm in profile.vms
        ],
    }
    if profile.disks:
        data["disks"] = list(profile.disks)
    if profile.source:
        data["source"] = dict(profile.source)
    header = (
        "# azcap workload profile. zonal is false because Azure Migrate does not assess zone resilience;\n"
        "# set it to true if the design must span availability zones.\n"
    )
    return header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)


def _cell(row: list[Any], col: int | None) -> Any:
    return row[col] if col is not None and col < len(row) else None


def _find_header(headers: list[str], *needles: str) -> int | None:
    """Index of the first header containing any needle (case-insensitive)."""
    for i, h in enumerate(headers):
        low = h.lower()
        if any(n in low for n in needles):
            return i
    return None


def read_migrate_xlsx(path: Path) -> tuple[list[AssessedMachine], dict]:
    """Read an Azure Migrate assessment export (.xlsx): the Assessed_Machines sheet, plus Assessed_Disks when present.
    Column detection is by header text and heuristic; a missing required column lists the headers found."""
    try:
        import openpyxl
    except ImportError as e:
        raise MigrateError("Reading an .xlsx export needs openpyxl: pip install azcap[migrate-xlsx]") from e
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Workbook contains no default style", category=UserWarning)
            book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as e:
        raise MigrateError(f"Cannot read {path}: {e}") from e

    def sheet_named(fragment: str):
        for ws in book.worksheets:
            if fragment in ws.title.lower():
                return ws
        return None

    def rows_of(ws) -> tuple[list[str], list[list[Any]]]:
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        for i, row in enumerate(rows):
            if any(c not in (None, "") for c in row):
                headers = ["" if c is None else str(c).strip() for c in row]
                return headers, [r for r in rows[i + 1 :] if any(c not in (None, "") for c in r)]
        return [], []

    # Assessment_Properties (Property | Selected value) and Assessment_Summary (label | value rows) carry the
    # target region and names; both are optional and matched on the first non-empty cell.
    props: dict[str, str] = {}
    for fragment in ("assessment_summary", "assessment_properties"):
        ws = sheet_named(fragment)
        if ws is None:
            continue
        for row in ws.iter_rows(values_only=True):
            cells = [c for c in row if c not in (None, "")]
            if len(cells) >= 2:
                props.setdefault(_norm(cells[0]), str(cells[1]).strip())

    def prop(*labels: str) -> str | None:
        return next((props[label] for label in labels if props.get(label)), None)

    target = prop("target location", "target region", "azure location")
    info: dict[str, Any] = {
        "target_region": target.replace(" ", "").lower() if target else None,
        "sizing_criterion": normalize_sizing_criterion(prop("sizing criterion", "sizing criteria")),
        "comfort_factor": prop("comfort factor"),
        "subscription": prop("subscription id", "subscription"),
        "project": prop("project name", "project", "azure migrate project", "migrate project"),
        "group": prop("group name", "group"),
        "assessment": prop("assessment name", "assessment"),
    }

    machines_ws = sheet_named("assessed_machines")
    if machines_ws is None:
        names = ", ".join(ws.title for ws in book.worksheets)
        raise MigrateError(f"{path}: no sheet named like 'Assessed_Machines' (sheets: {names})")
    headers, rows = rows_of(machines_ws)
    col_ready = _find_header(headers, "readiness")
    col_size = _find_header(headers, "recommended size", "azure vm size", "target size")
    col_name = _find_header(headers, "machine", "server")
    col_os = _find_header(headers, "operating system")
    missing = [
        label
        for label, col in (("readiness", col_ready), ("recommended size", col_size), ("machine name", col_name))
        if col is None
    ]
    if missing:
        raise MigrateError(
            f"{path}: sheet '{machines_ws.title}' has no column for {', '.join(missing)}. "
            f"Headers found: {', '.join(h for h in headers if h) or '(none)'}. "
            "Please report these headers so detection can be fixed."
        )
    machines: dict[str, AssessedMachine] = {}
    for row in rows:
        name = str(_cell(row, col_name) or "").strip() or f"row{len(machines) + 1}"
        size = str(_cell(row, col_size) or "").strip() or None
        machines[name] = AssessedMachine(
            name=name,
            size=size,
            suitability=_suitability_from_readiness(_cell(row, col_ready)),
            os=_os_from_name(_cell(row, col_os)) if col_os is not None else None,
        )
    info.update(
        machines_sheet=machines_ws.title, disks_sheet=None, os_column=headers[col_os] if col_os is not None else None
    )
    disks_ws = sheet_named("assessed_disks")
    unattached: list[tuple[str, str]] = []
    if disks_ws is not None:
        info["disks_sheet"] = disks_ws.title
        dheaders, drows = rows_of(disks_ws)
        col_dtype = next(
            (i for i, h in enumerate(dheaders) if "recommended disk" in h.lower() and "size" not in h.lower()), None
        )
        col_dsize = _find_header(dheaders, "disk size", "recommended size")
        col_dmachine = _find_header(dheaders, "machine", "server")
        if col_dtype is not None and col_dsize is not None:
            for row in drows:
                dtype = normalize_disk_type(_cell(row, col_dtype))
                dsize = str(_cell(row, col_dsize) or "").strip()
                if not dtype or not dsize:
                    continue
                owner_name = str(_cell(row, col_dmachine) or "").strip() if col_dmachine is not None else ""
                owner = machines.get(owner_name)
                (owner.disks if owner else unattached).append((dtype, dsize))
        else:
            info["disks_sheet"] = (
                f"{disks_ws.title} (disk type/size columns not recognised; headers: {', '.join(dheaders)})"
            )
    info["unattached_disks"] = unattached
    return list(machines.values()), info


def profile_output_path(out_profile: str | None, profile: Profile, mixed: bool) -> Path:
    """<name>.yaml in cwd by default; with mixed OS the -linux/-windows suffix is already in the profile name,
    and an explicit --out-profile gets that suffix inserted before its extension."""
    if not out_profile:
        return Path(f"{profile.name}.yaml")
    path = Path(out_profile)
    if not mixed:
        return path
    suffix = "-" + profile.os.lower()
    return path.with_name(f"{path.stem}{suffix}{path.suffix or '.yaml'}")


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text.strip()).strip("-") or "profile"


def short_sku(sku: str) -> str:
    """Standard_D8s_v5 -> D8s_v5, for the console where the prefix is noise."""
    return sku[len("Standard_") :] if sku.lower().startswith("standard_") else sku


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
    out: Path,
    rows: list[SkuRow],
    summ: list[FamilySummary],
    quota: list[QuotaRow],
    probes: list[ProbeResult] = (),
    verdicts: list[RegionVerdict] = (),
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
    if verdicts:
        with (out / "verdicts.csv").open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(
                [
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
            )
            for rv in verdicts:
                for v in rv.vms:
                    w.writerow(
                        [
                            v.region,
                            v.sku,
                            v.count,
                            v.optional,
                            v.status,
                            v.reason,
                            v.family,
                            v.vcpu_need,
                            v.quota_limit,
                            v.quota_used,
                            v.probe_score,
                            v.probe_fulfillment,
                            rv.verdict,
                        ]
                    )
    else:
        (out / "verdicts.csv").unlink(missing_ok=True)


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
    profile: Profile | None = None,
    verdicts: list[RegionVerdict] = (),
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
        "profile": asdict(profile) if profile else None,
        "verdicts": [asdict(v) for v in verdicts],
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


def _add_profile_build_args(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--out-profile", metavar="PATH", help="where to write the profile (default: <name>.yaml in cwd)")
    sp.add_argument(
        "--conditional",
        choices=("optional", "required", "skip"),
        default="optional",
        help="how to treat ConditionallySuitable machines (default: optional)",
    )
    sp.add_argument(
        "--headroom",
        type=nonnegative_int,
        default=0,
        metavar="PCT",
        help="inflate each required count by ceil(count * PCT / 100) (default: 0)",
    )
    sp.add_argument("--name", help="profile name (default: the assessment name, or the export file's name)")


def profile_main(argv: list[str]) -> None:
    """`azcap profile from-migrate ...` / `azcap profile from-migrate-xlsx ...`: build a profile from Azure Migrate."""
    ap = argparse.ArgumentParser(
        prog="azcap profile",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Build a workload profile for `azcap --profile` from an Azure Migrate assessment.",
        epilog="""\
mapping:
  Suitable                -> required VM       ConditionallySuitable -> --conditional (default optional)
  NotSuitable / Unknown   -> excluded, tallied  no recommended size   -> excluded, tallied
  machines are grouped by recommended size; --headroom adds ceil(count * PCT / 100) to required counts
  mixed Linux/Windows -> one profile per OS (<name>-linux.yaml, <name>-windows.yaml)
  zonal is written as false: Azure Migrate does not assess zone resilience — set it yourself if needed

examples:
  azcap profile from-migrate --resource-group rg-migrate --project contoso-proj --group wave1 --assessment wave1-assess
  azcap profile from-migrate --assessment-id /subscriptions/.../assessments/wave1-assess --headroom 10
  azcap profile from-migrate-xlsx wave1-export.xlsx --conditional required
  azcap --regions eastus2 --profile wave1-assess.yaml --fail-on-blocked     # then judge regions against it
""",
    )
    sub = ap.add_subparsers(dest="command", required=True, metavar="{from-migrate,from-migrate-xlsx}")

    api = sub.add_parser("from-migrate", help="read the assessment from the Azure Migrate API")
    api.add_argument("--project", metavar="NAME", help="assessment project name")
    api.add_argument("--group", metavar="NAME", help="group name within the project")
    api.add_argument("--assessment", metavar="NAME", help="assessment name within the group")
    api.add_argument("--resource-group", metavar="RG", help="resource group holding the Migrate project")
    api.add_argument(
        "--assessment-id", metavar="ID", help="full ARM id of the assessment (alternative to the four name flags)"
    )
    api.add_argument(
        "--subscription", metavar="ID", help="subscription id (default: $AZURE_SUBSCRIPTION_ID, else `az account show`)"
    )
    api.add_argument("--tenant", metavar="ID", help="Entra tenant id to authenticate against")
    _add_profile_build_args(api)

    xlsx = sub.add_parser("from-migrate-xlsx", help="read an assessment exported from the portal (.xlsx)")
    xlsx.add_argument("export", metavar="EXPORT.XLSX", help="assessment export with an Assessed_Machines sheet")
    _add_profile_build_args(xlsx)

    args = ap.parse_args(argv)
    source: dict[str, Any] = {"exported": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    target_region: str | None = None
    notes: list[str] = []
    if args.command == "from-migrate":
        if args.assessment_id:
            match = re.fullmatch(
                r"/subscriptions/([^/]+)/resourceGroups/([^/]+)/providers/Microsoft\.Migrate/assessmentProjects/([^/]+)"
                r"/groups/([^/]+)/assessments/([^/]+)/?",
                args.assessment_id.strip(),
                re.IGNORECASE,
            )
            if not match:
                api.error(
                    "--assessment-id must be /subscriptions/../resourceGroups/../providers/Microsoft.Migrate/"
                    "assessmentProjects/../groups/../assessments/.."
                )
            subscription, resource_group, project, group, assessment = match.groups()
        else:
            missing = [f for f in ("project", "group", "assessment", "resource_group") if not getattr(args, f)]
            if missing:
                api.error(
                    "--assessment-id or all of --project, --group, --assessment, --resource-group are required "
                    f"(missing: {', '.join('--' + m.replace('_', '-') for m in missing)})"
                )
            subscription = resolve_subscription(args.subscription)
            resource_group, project, group, assessment = args.resource_group, args.project, args.group, args.assessment
        assessment_id = migrate_assessment_id(subscription, resource_group, project, group, assessment)
        print(f"Reading assessment {assessment} (project {project}, group {group})", file=sys.stderr)
        try:
            body, resources = fetch_migrate_assessment(arm_getter(make_credential(args.tenant)), assessment_id)
        except MigrateError as e:
            sys.exit(str(e))
        props = body.get("properties") if isinstance(body.get("properties"), dict) else {}
        target_region = str(props["azureLocation"]).lower() if props.get("azureLocation") else None
        source.update(
            kind="azure-migrate",
            project=project,
            group=group,
            assessment=assessment,
            target_region=target_region,
            sizing_criterion=normalize_sizing_criterion(props.get("sizingCriterion")),
        )
        if props.get("azureVmFamilies"):
            notes.append(f"assessment limited to VM families: {', '.join(str(f) for f in props['azureVmFamilies'])}")
        machines = [machine_from_api(r) for r in resources]
        default_name = assessment
    else:
        path = Path(args.export)
        try:
            machines, info = read_migrate_xlsx(path)
        except MigrateError as e:
            sys.exit(str(e))
        target_region = info.get("target_region")
        source.update(
            kind="azure-migrate",
            subscription=info.get("subscription"),
            project=info.get("project"),
            group=info.get("group"),
            assessment=info.get("assessment") or path.name,
            target_region=target_region,
            sizing_criterion=info.get("sizing_criterion"),
            comfort_factor=info.get("comfort_factor"),
        )
        if info.get("unattached_disks"):
            notes.append(f"{len(info['unattached_disks'])} disks could not be matched to a machine and were left out")
        if info.get("os_column") is None:
            notes.append("no 'operating system' column found; OS assumed Linux")
        default_name = info.get("assessment") or path.stem
    name = slug(args.name or default_name)
    profiles, summary = build_profiles(
        machines, name=name, conditional=args.conditional, headroom=args.headroom, source=source
    )
    if not profiles or not any(p.vms for p in profiles):
        sys.exit(
            f"No machines to build a profile from: {summary['by_suitability'] or 'no machines'}; "
            f"excluded {summary['excluded']}"
        )
    written: list[tuple[Profile, Path]] = []
    for profile in profiles:
        if not profile.vms:
            continue
        out = profile_output_path(args.out_profile, profile, summary["mixed_os"])
        try:
            out.write_text(profile_to_yaml(profile), encoding="utf-8")
        except OSError as e:
            sys.exit(f"Cannot write {out}: {e}")
        written.append((profile, out))

    # summary
    print(f"\nMachines: {len(machines)} assessed")
    for suit in (*MIGRATE_SUITABILITIES, *sorted(set(summary["by_suitability"]) - set(MIGRATE_SUITABILITIES))):
        n = summary["by_suitability"].get(suit)
        if n:
            print(f"  {suit:<24} {n:4d}")
    if summary["excluded"]:
        print("  excluded: " + ", ".join(f"{k} {v}" for k, v in summary["excluded"].items()))
    for text, n in sorted(summary["explanations"].items(), key=lambda kv: -kv[1])[:5]:
        print(f"    {n:4d} x {text}")
    for profile, out in written:
        print(f"\nProfile {profile.name} ({profile.os}) -> {out}")
        for vm in profile.vms:
            print(f"  {vm.sku:<32} x{vm.count:<5}{'optional' if vm.optional else 'required'}")
        if profile.disks:
            print("  disks: " + ", ".join(f"{d['type']} {d['size']} x{d['count']}" for d in profile.disks))
    if summary["headroom"]:
        print(f"\nRequired counts include {summary['headroom']}% headroom.")
    if summary["unknown_os"]:
        majority = summary["majority_os"]
        print(f"{summary['unknown_os']} machine(s) with unknown OS were counted with the majority ({majority}).")
    if summary["mixed_os"]:
        print("Mixed Linux and Windows machines: one profile per OS was written.")
    for note in notes:
        print(note)
    print(
        "zonal is false in the profile: Azure Migrate does not assess zone resilience. "
        "Set zonal: true if the design must span availability zones."
    )
    region = target_region or "<region>"
    if target_region:
        print(f"Target region in the assessment: {target_region}")
    print("\nNext:")
    for _profile, out in written:
        print(f"  azcap --regions {region} --profile {out} --fail-on-blocked")


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    if sys.argv[1:2] == ["profile"]:
        profile_main(sys.argv[2:])
        return
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

  azcap --regions eastus2 --profile wave1.yaml --fail-on-blocked
      judge each region against a workload (SKUs, counts, zonal?) and exit 3 if a requested
      region is blocked by restriction, quota, or capacity — for gating a pipeline

  azcap --regions eastus,brazilsouth --fixture fixtures/sample.json
      offline run against a saved raw.json (no Azure calls)

outputs (in --out, default ./out):
  report.html   self-contained report: heatmap, pairs, placement probes, zone restrictions, quota, SKU table
  summary.csv   region x family counts and score      skus.csv   one row per region x SKU
  pairs.csv     region vs paired region               raw.json   API snapshot; reuse with --compare/--fixture
  probes.csv    with --need: one row per region x placement probe
  verdicts.csv  with --profile: one row per region x profile VM with status, reason, and region verdict

exit codes:
  0   finished (verdicts are informational unless --fail-on-blocked is set)
  1   could not run: authentication, Azure request, or invalid input/fixture/profile
  2   usage error (bad arguments)
  3   --fail-on-blocked and a requested region is blocked (with --require-pair: or its pair is)

auth: Azure CLI login by default (any DefaultAzureCredential source works).
      az login [--tenant <id>]   then   az account list -o table   to see what you're signed into.

profiles from Azure Migrate:
  azcap profile from-migrate --help          build a workload profile from an assessment (API)
  azcap profile from-migrate-xlsx --help     ... or from a portal .xlsx export
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
        "--profile",
        metavar="PATH",
        help="workload profile (.yaml/.yml/.json: name, os, zonal, vms[sku,count,optional]); judges every region "
        "deployable or blocked; implies --include-quota and one placement probe per profile VM",
    )
    g.add_argument(
        "--fail-on-blocked",
        action="store_true",
        help=f"with --profile: exit {EXIT_BLOCKED} when any requested region is blocked",
    )
    g.add_argument(
        "--require-pair",
        action="store_true",
        help="with --fail-on-blocked: also fail when a requested region's paired region is blocked",
    )
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
    profile: Profile | None = None
    if args.profile:
        if needs:
            ap.error("--profile and --need are mutually exclusive (the profile defines the probes)")
        try:
            profile = load_profile(Path(args.profile))
        except ValueError as e:
            ap.error(str(e))
        needs = [(vm.sku, vm.count) for vm in profile.vms]
        args.include_quota = True
        args.os, args.spot = profile.os, False
    if args.fail_on_blocked and not profile:
        ap.error("--fail-on-blocked requires --profile")
    if args.require_pair and not args.fail_on_blocked:
        ap.error("--require-pair requires --fail-on-blocked")
    probe_zonal = profile.zonal if profile else args.zonal

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
        if needs and not profile:
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
                zonal=probe_zonal,
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
    all_rows, all_quota = list(rows), list(quota)  # verdicts judge the profile against the unfiltered scan
    if profile:
        # Only VMs still undecided after the not-offered / restricted / quota steps are worth a placement probe.
        candidates = probe_candidates(profile, regions, all_rows, all_quota)
        wanted = {(region, sku, count) for region, group in candidates.items() for sku, count in group}
        if args.fixture:
            probes = [p for p in probes if (p.region, p.sku, p.count) in wanted]
        else:
            skipped = len(regions) * len(profile.vms) - len(wanted)
            print(
                f"Placement probes: {len(wanted)} via Compute Recommender; {skipped} not needed "
                "(decided by restrictions or quota)",
                file=sys.stderr,
            )
            post = arm_poster(credential)
            for region in regions:
                if candidates[region]:
                    probes += run_probes(
                        post,
                        subscription,
                        [region],
                        candidates[region],
                        mix=False,
                        os_type=profile.os,
                        spot=False,
                        zonal=profile.zonal,
                        raw=raw,
                        locs=locs,
                        api_version=args.probe_api_version,
                        quota=None,  # step 3 already checked the family total, which is stricter than per-SKU
                    )
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
        "profile_name": profile.name if profile else None,
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
    verdicts = workload_verdicts(profile, regions, all_rows, all_quota, probes) if profile else []
    pair_verdict_notes(pairs, verdicts)

    snapshot = {
        "meta": meta,
        "skus": raw,
        "locations": locs,
        "quota": [asdict(q) for q in quota],
        "probes": [asdict(p) for p in probes],
        "profile": asdict(profile) if profile else None,
        "verdicts": [asdict(v) for v in verdicts],
    }
    (out / "raw.json").write_text(json.dumps(snapshot, indent=1), encoding="utf-8")
    write_csvs(out, rows, summ, quota, probes, verdicts)
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
                    "verdict",
                    "pair_verdict",
                    "dr_note",
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
                        p["verdict"],
                        p["pair_verdict"],
                        p["dr_note"],
                    ]
                )
    else:
        (out / "pairs.csv").unlink(missing_ok=True)
    write_html(out, rows, summ, quota, changes, meta, pairs, locs, probes, profile, verdicts)

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
    if profile:
        dr = {p["region"]: p.get("dr_note") for p in pairs}
        print(f'\nWorkload "{profile.name}" ({"zonal" if profile.zonal else "regional"}, {profile.os}):')
        for rv in verdicts:
            if rv.verdict == "blocked":
                detail = "; ".join(rv.blocking)
            else:
                detail = ", ".join(
                    f"{short_sku(v.sku)} x{v.count} {v.reason if v.status == 'deployable' else v.status}"
                    + (" (optional)" if v.optional else "")
                    for v in rv.vms
                )
            optional_failing = [v for v in rv.vms if v.optional and v.status in VM_BLOCKING_STATUSES]
            if rv.verdict == "blocked" and optional_failing:
                detail += "; optional: " + ", ".join(f"{short_sku(v.sku)} {v.status}" for v in optional_failing)
            tag = f"  (pair of {added[rv.region]})" if rv.region in added else ""
            label = "BLOCKED" if rv.verdict == "blocked" else rv.verdict
            print(f"  {rv.region:<22} {label:<11} {detail}{tag}")
            if dr.get(rv.region):
                print(f"  {'':<22} {'':<11} pair: {dr[rv.region]}")
    if changes:
        print(f"\n{len(changes)} change(s) since baseline — see report.html")
    extra = (f", {out / 'probes.csv'}" if probes else "") + (f", {out / 'verdicts.csv'}" if verdicts else "")
    print(f"\nWrote {out / 'report.html'}, {out / 'summary.csv'}, {out / 'skus.csv'}, {out / 'raw.json'}{extra}")

    if args.fail_on_blocked:
        verdict_of = {rv.region: rv for rv in verdicts}
        gate = list(primaries)
        if args.require_pair:
            gate += [pair for pair, primary in added.items() if primary in primaries]
        blocked = [r for r in ordered_unique(gate) if r in verdict_of and verdict_of[r].verdict == "blocked"]
        if blocked:
            print(
                f'\nBlocked for workload "{profile.name}": {", ".join(blocked)} — exit {EXIT_BLOCKED}', file=sys.stderr
            )
            sys.exit(EXIT_BLOCKED)


if __name__ == "__main__":
    main()
