# Planned updates

Ranked by how much each would change a real deployment decision. Items are independent; none
is scheduled. Item 1 shipped in 0.3.0 (the only one that adds information Microsoft holds about
capacity rather than another view of restrictions), item 2 in 0.4.0, and item 2b in 0.5.0.

## 1. Placement scores (Compute Recommender) — done (0.3.0)

Shipped as `--need SKU:COUNT[,...]` with `--need-mix`, `--os`, `--spot`; zones follow `--zonal`.
One probe per region (pairs included) via `skuMixPlacementScores` (`2026-05-05-preview`; the doc
site already lists `2026-09-05-preview`, which ARM rejects — `--probe-api-version` covers the next
move). A 409 "vCPU quota for <family> has reached its limit" is a quota verdict, and with
`--include-quota` a request the family cannot hold is answered from quota data without a call. Score 0–9,
fulfillment reason, SKU/zone split, and `valid until` land in the report (“Placement probes” section
and “pair can place / neither side can place / quota blocks … ” flags on the paired-regions table), the console,
`probes.csv`, and `raw.json`; fixtures can carry a `probes` list for offline rendering. Per-probe
failures (404/400/403, non-JSON, 429 after one retry) become `error` rows and never affect the
restriction figures or the exit code.

Not done: the GA spot endpoint (`placementScores/spot/generate`, 2025-06-05) returns categorical
High/Medium/Low scores in a different shape; `--spot` uses the skuMix endpoint with `priority: Spot`
instead so the output stays one shape. Revisit if the skuMix API stops accepting spot.

## 2. Deployability against a workload profile — done (0.4.0)

Shipped as `--profile wave1.yaml` (YAML or JSON: name, os, zonal, vms[sku, count, optional]).
Every scanned region gets a verdict from restrictions, family quota (needs summed per family
across the profile), zone support, and placement probes, in the fixed order not offered →
restricted → quota → capacity → deployable; a failed probe leaves a VM unknown rather than
blocked, and optional VMs never block. Pairs carry both verdicts and a DR note. Output: a
“Workload verdict” report section, `verdicts.csv`, `raw.json["verdicts"]`, a console block, and
`--fail-on-blocked` (exit 3; `--require-pair` extends the gate to the paired region).

Possible follow-up: workload-weighted scoring — the profile is now the workload definition that
idea was waiting for, so the heatmap could weight families by what the profile actually deploys.

## 2b. Profiles from Azure Migrate assessments — done (0.5.0)

`azcap profile from-migrate` (API: assessment + paged assessedMachines, api-version 2023-03-15) and
`azcap profile from-migrate-xlsx` (portal export via the optional `openpyxl` extra) write a profile
from an assessment: Suitable → required, ConditionallySuitable → `--conditional`, NotSuitable /
Unknown / no size → excluded and tallied; grouped by recommended size; `--headroom`; one profile
per OS when mixed; `zonal: false` with a reminder. The profile schema gained optional `source`
and `disks` blocks that the verdict logic records but does not judge yet.

Caveat: Excel detection is heuristic (substring matches on "readiness", "recommended size",
"machine", "operating system", "recommended disk", "disk size"), calibrated against one real export
(2026-09: sheets Assessment_Summary / All_Assessed_Machines / All_Assessed_Disks /
Assessment_Properties, readiness "Ready" / "Ready With Conditions", disk types "Premium managed
disks"); a missing column lists the headers found so other layouts can be reported. Possible follow-ups: judge `disks`
against disk SKU availability (item 3), and read the `azureVmFamilies` filter into the scan scope.

## 3. Other resource types from the same Resource SKUs API

The API already returns `disks` (Premium SSD v2 and Ultra have real zone gaps), `hostGroups`
(dedicated hosts), and `availabilitySets`; the collector filters them out on one line. Storage
account SKUs (`az storage account list-skus`) use the same restriction shape, so ZRS/GZRS
availability per region fits the existing model. Add `--resource-types vm,disk,storage`.

## 4. Foundry / Azure OpenAI model availability and quota per region

Which models and versions are deployable in which regions, by deployment type (Standard,
Provisioned, Global, Data Zone), and TPM/PTU quota remaining per region. Cognitive Services
model-list and usage APIs. Same summary/report pattern, different collector; availability changes
monthly so `--compare` matters here.

## 5. Other platform services with per-region capability APIs

SQL Database / Managed Instance (service tiers, zone redundancy via location capabilities),
PostgreSQL Flexible Server (tiers and zone support), App Service (SKU availability), AKS (supported
versions per region), Cosmos DB (zone-redundant regions). One small collector each.

## 6. Multi-subscription comparison

Scan every visible subscription in a tenant (or a named set) and diff restrictions. A family
restricted on pay-as-you-go but not on EA is offer eligibility; one restricted on every offer is
much more likely capacity. Reuses `--compare`; adds a subscription dimension to the report.

## 7. Trend and alerting

Scheduled runs storing snapshots (SQLite locally or Log Analytics), % restricted per family over
time, and an alert when a family you depend on flips in a region you deploy to. GitHub Actions cron
with OIDC to a read-only service principal is enough for collection.

## 8. Fallback SKU suggestions with pricing

For a restricted SKU, list unrestricted SKUs in the same region with comparable vCPU/memory and
their price from the Retail Prices API (no auth). Turns a red cell into an answer.
