# azcap — Azure VM SKU restriction scanner

Microsoft does not publish per-region capacity. What it does expose, per subscription, is the
set of VM SKUs currently marked unavailable in each region and zone — via the Resource SKUs API
(`az vm list-skus`). This tool reports the share of VM SKUs that are restricted, per region ×
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
# SII and customer tenants stay distinguishable)
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

# offline / demo with synthetic data
python fixtures/make_fixture.py   # generates fixtures/sample.json
azcap --regions eastus,brazilsouth,saudiarabiaeast --fixture fixtures/sample.json
```

Outputs land in `--out` (default `out/`):

| file | contents |
|---|---|
| `report.html` | heatmap (region × family), zone-level restrictions, quota headroom, changes, filterable SKU table |
| `summary.csv` | one row per region × family with counts, assessed count, and % restricted |
| `skus.csv` | one row per region × SKU with status, zones, reason codes |
| `pairs.csv` | one row per requested region: pair, geography match, zone support, both-side scores |
| `quota.csv` | with `--include-quota`: used vs limit per family |
| `raw.json` | normalized API snapshot — keep it, pass as `--compare` next run |

## How to read it

| status | source | meaning |
|---|---|---|
| `region_restricted` | `NotAvailableForSubscription` @ Location | Microsoft won't allocate this SKU to this subscription in this region. Strongest restriction signal. |
| `zone_restricted` | `NotAvailableForSubscription` @ Zone | Allocatable in some zones only. Early warning; also breaks zone-redundant designs. |
| `quota_blocked` | `QuotaId` | A quota or subscription-eligibility restriction. Not assessed (excluded from % restricted); use `--include-quota` to inspect the reported family limit and usage. |
| `available` | — | No restriction. Does not guarantee allocation at deploy time. |

**The figure is % of assessed SKUs restricted (0–100).** Per region × family it is the mean restriction
loss across assessed SKUs, where a region-restricted SKU counts 1.0 and a zone-restricted SKU
counts `blocked_zones / total_zones`; × 100. Quota-blocked SKUs are not assessed. If none remain, the
figure is `n/a`, not zero.

The region figure is weighted by assessed SKU count by default, so it is literally the share of
assessed SKUs in that region that are restricted; families with more enumerated sizes have more
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

## Caveats

- Everything here is **per subscription**. Restrictions can differ between your tenant and the
  customer's, and between EA / CSP / PAYG offers. Run it under the deploying subscription.
- Absence of a restriction is not a capacity guarantee. `AllocationFailed` /
  `OverconstrainedAllocationRequest` at deploy time can still happen. For committed capacity use
  On-demand Capacity Reservations.
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
