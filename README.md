# azcap — Azure VM SKU restriction scanner

Microsoft does not publish per-region capacity. What it does expose, per subscription, is the
set of VM SKUs currently marked unavailable in each region and zone — via the Resource SKUs API
(`az vm list-skus`). This tool reports the zone-adjusted share of VM SKUs that are restricted, per region ×
VM family, and renders a self-contained HTML report you can drop into a deck. Restrictions are a
subscription-specific availability proxy, not a published Azure capacity percentage or an allocation guarantee.

## Install

The easy way (any OS; [pipx](https://pipx.pypa.io) manages the virtual environment for you):

```bash
pipx install git+https://github.com/steverlabs/azcap.git
az login                       # or: az login --tenant <tenant-id>
azcap --help
```

Upgrade later with `pipx upgrade azcap`. If you don't have pipx: `brew install pipx` (macOS),
`py -m pip install --user pipx && py -m pipx ensurepath` (Windows), or `pip install --user pipx`.

From a clone, for development:

```bash
git clone https://github.com/steverlabs/azcap.git && cd azcap
python3 -m venv .venv && source .venv/bin/activate     # Windows: py -m venv .venv ; .\.venv\Scripts\Activate.ps1
pip install -e .
```

Requires Python 3.10+ and the Azure CLI (for `az login`). Open the report with `open out/report.html`
(macOS) or `start out\report.html` (Windows).

Auth uses the Azure CLI login by default (any DefaultAzureCredential source works). With
`--tenant`, the CLI token is requested for that tenant and the script refuses to run if the
subscription belongs to a different one. `az account list -o table` shows the tenant and
subscription ids you're signed into.

## Run

```bash
# scan a few regions, all VM families
azcap --regions eastus,eastus2,canadacentral,brazilsouth,saudiarabiaeast

# just the families you care about, with quota headroom
azcap --regions eastus,southeastasia --families Dv5,DSv5,Ev5,ESv5,NC,ND --include-quota

# name the tenant and subscription explicitly (results are subscription-specific;
# the report header is stamped with tenant id + subscription name so runs across
# different tenants stay distinguishable)
azcap --regions saudiarabiaeast --tenant <tenant-id> --subscription <sub-id>

# drop retired / legacy families that only add noise to the score
azcap --regions eastus,eastus2 --exclude-families "D,DS,G,GS,*Promo,LS,LSv2,Av2,A0_A7"

# only SKUs offered in availability zones (zone-resilient designs); non-AZ regions show n/a
azcap --regions eastus,eastus2,canadacentral --zonal

# skip the automatic paired-region scan
azcap --regions eastus --no-pairs

# diff against last week's snapshot
azcap --regions eastus --compare out-lastweek/raw.json

# give each VM family equal weight in the region figure (default is per-SKU weighting)
azcap --regions eastus,eastus2 --region-weighting family

# create shareable artifacts without tenant/subscription names or ids
azcap --regions eastus --redact-identifiers

# ask whether 12 x Standard_D8s_v5 would place across zones in each region right now
azcap --regions eastus2,brazilsouth --zonal --need Standard_D8s_v5:12

# offline / demo with synthetic data
python fixtures/make_fixture.py   # generates fixtures/sample.json
azcap --regions eastus,brazilsouth,saudiarabiaeast --fixture fixtures/sample.json
```

Outputs land in `--out` (default `out/`):

| file | contents |
|---|---|
| `report.html` | heatmap (region × family), paired regions, placement probes, zone-level restrictions, quota headroom, changes, filterable SKU table |
| `summary.csv` | one row per region × family with counts, assessed count, and zone-adjusted % restricted |
| `skus.csv` | one row per region × SKU with status, zones, reason codes |
| `pairs.csv` | one row per requested region: pair, geography match, zone support, both-side scores, placement notes |
| `probes.csv` | with `--need`: one row per region × placement probe — score, result, quota detail, SKU/zone split |
| `quota.csv` | with `--include-quota`: used vs limit per family |
| `raw.json` | normalized API snapshot — keep it, pass as `--compare` next run |

## How to read it

| status | source | meaning |
|---|---|---|
| `region_restricted` | `NotAvailableForSubscription` @ Location | Microsoft won't allocate this SKU to this subscription in this region. Strongest restriction signal. |
| `zone_restricted` | `NotAvailableForSubscription` @ Zone | Allocatable in some zones only. Early warning; also breaks zone-redundant designs. |
| `quota_blocked` | `QuotaId` | A quota or subscription-eligibility restriction. Not assessed (excluded from % restricted); use `--include-quota` to inspect the reported family limit and usage. |
| `available` | — | No restriction. Does not guarantee allocation at deploy time. |

**The figure is the zone-adjusted % of assessed SKUs restricted (0–100).** Per region × family it is the mean
restriction loss across assessed SKUs, where a region-restricted SKU counts 1.0 and a zone-restricted SKU
counts `blocked_zones / total_zones`; × 100. Quota-blocked SKUs are not assessed. If none remain, the
figure is `n/a`, not zero.

"Zone-adjusted" because a SKU blocked in one of three zones counts 0.33, not 1: the figure is lower than a
plain count of SKUs carrying any restriction (the region-restricted and zone-restricted counts are shown
alongside it). The region figure is weighted by assessed SKU count by default; families with more enumerated sizes have more
influence. Use `--region-weighting family` to weight every assessed
family equally (the figure is then an average of family rates, no longer a share of SKUs — the report
header shows which weighting was used), and use `--families` / `--sku` to make the scope resemble the
workload you intend to deploy.

## Paired regions

By default each requested region's Microsoft-designated pair (from the Subscriptions API
`locations[].metadata.pairedRegion`) is added to the scan and reported alongside it: region score vs.
pair score, per-family comparison, and flags for cross-geography pairs (e.g. Brazil South ↔ South
Central US), pairs without availability zones (e.g. Canada East), and families constrained on both
sides. Regions Microsoft launched without a pair (most regions since ~2021, including Mexico Central
and Saudi Arabia East) are reported as unpaired — DR for those is a region you pick, and the tool
can't pick it for you; pass both regions explicitly and compare rows.

## Placement probes

Restrictions say what Microsoft *won't* allocate; they say nothing about whether a specific request
*would* place today. Microsoft's Compute Recommender does: given a SKU, a count, and optionally zones,
it returns a **placement score from 0 (worst) to 9 (best)** for the best way it found to place that
request, whether the full request was placed, and — if not — whether the shortfall is
**insufficient capacity** or **insufficient quota**. That is the only capacity signal Azure exposes,
and it is subscription-specific like everything else here. `--need` sends one such probe per region
in the scan (pairs included) and puts the answer next to the restriction data: in the report
(“Placement probes” section, plus “pair can place / neither side can place / quota blocks … on the pair”
flags on the paired-regions table — quota and capacity verdicts are never merged into one note), the
console, `probes.csv`, and `raw.json`.

The API is **preview** (`skuMixPlacementScores`, `2026-05-05-preview`); when ARM moves to a newer
version, pass it with `--probe-api-version` rather than waiting for a release. A probe that fails (404
in a region where the provider isn't registered, 400 for a SKU it doesn't know, 403, a non-JSON body)
is recorded as an `error` row and reported on stderr; it never changes the restriction figures or the
exit code. Scores describe a hypothetical allocation at the time of the run and expire (`valid until`
is shown); they are not a reservation. For committed capacity use On-demand Capacity Reservations.

**A probe needs quota headroom in the SKU's family before it can say anything about capacity.** When
the family's vCPU quota is at its limit the API answers with a quota verdict instead of a placement,
shown as `insufficient quota: standardDSv5Family`. With `--include-quota` the tool checks each probe
against the family's quota first (limit 0, or fewer free vCPUs than the request needs) and records
`insufficient quota: limit N, used U, need M vCPU` without calling the API. On pay-as-you-go
subscriptions whole generations (e.g. every v5 family) can have zero quota, so an "insufficient quota"
row there means "request quota first", not "the region is full".

`--need SKU:COUNT[,SKU:COUNT...]` — each `SKU:COUNT` is a separate probe. With `--zonal` the probe
asks for zonal placement across the zones that SKU is offered in (regions without zones fall back to a
regional probe and are labelled `regional`); without it the probe is regional. `--os Windows` and
`--spot` change the OS and priority sent; `--need-mix` sends every SKU in one request, ranked in the
order given, and lets Azure pick the split.

```bash
# will 12 x Standard_D8s_v5 place across zones in eastus2 and brazilsouth (and their pairs)?
azcap --regions eastus2,brazilsouth --zonal --need Standard_D8s_v5:12

# 16 VMs from a ranked mix: prefer D8s_v5, fall back to E8s_v5, Windows, regional placement
azcap --regions eastus,westeurope --need Standard_D8s_v5:12,Standard_E8s_v5:4 --need-mix --os Windows
```

Console output, one line per region × probe:

```
Placement probes (regular VMs, Linux; Compute Recommender preview):
  eastus2                Standard_D8s_v5 x12   zonal   score 7  placed                 split 1:4 2:4 3:4
  brazilsouth            Standard_D8s_v5 x12   zonal   score 2  insufficient capacity  split 1:2 2:2 3:2
  canadacentral          Standard_D8s_v5 x12   zonal   score -  insufficient quota: standardDSv5Family
```

## Caveats

- Everything here is **per subscription**. Restrictions can differ from one subscription to another,
  including between EA, CSP, and pay-as-you-go offers. Run it under the subscription that will deploy.
- Absence of a restriction is not a capacity guarantee. `AllocationFailed` /
  `OverconstrainedAllocationRequest` at deploy time can still happen. `--need` asks the Compute
  Recommender whether a specific request would place right now, which is closer, but still not a
  guarantee. For committed capacity use On-demand Capacity Reservations.
- Region access restrictions (regions that require an access request to deploy at all) are not
  in this API; your account team or the Azure portal quota blade will show those.
- The API reflects Microsoft's current allocation policy, which changes without notice. Take
  snapshots and diff them (`--compare`) rather than trusting a single run.
- Snapshot comparison reports added, changed, and removed SKUs. It rejects a different tenant,
  subscription, or incomplete region scope unless `--allow-incompatible-compare` is supplied.
- Reports normally contain the subscription name/id, tenant id, and optionally quota usage. Treat
  them as internal operational data or pass `--redact-identifiers` before sharing them.

## Development

Run the offline checks without Azure credentials:

```bash
pip install -e '.[dev]'
ruff check .
ruff format --check .
pytest
python -m build
```

## Extending

- Add other resource types by relaxing the `resource_type == virtualMachines` filter in
  `fetch_skus_live` (disks, hostGroups, availabilitySets are also in the API).
- Run it on a schedule and ship `summary.csv` to Log Analytics if you want a trend line.
