# azcap — Azure VM capacity scanner

Microsoft does not publish per-region capacity. azcap gets as close as the platform allows,
from two sources. The Resource SKUs API (`az vm list-skus`) shows which VM SKUs are restricted
for your subscription in each region and zone; azcap reports that as a zone-adjusted % of SKUs
restricted per region × family, with paired regions alongside. The Compute Recommender
(preview) answers whether a specific allocation — N of a given SKU, zonal or regional — would
place today, and distinguishes insufficient capacity from insufficient quota. Both are
subscription-specific and neither is a published capacity figure or a reservation. Output is a
self-contained HTML report plus CSVs.

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

# judge each region against a workload profile; exit 3 if a requested region is blocked
azcap --regions eastus2 --profile fixtures/wave1.yaml --fail-on-blocked

# offline / demo with synthetic data
python fixtures/make_fixture.py   # generates fixtures/sample.json
azcap --regions eastus,brazilsouth,saudiarabiaeast --fixture fixtures/sample.json
```

Outputs land in `--out` (default `out/`):

| file | contents |
|---|---|
| `report.html` | heatmap (region × family), paired regions, workload verdict, placement probes, zone-level restrictions, quota headroom, changes, filterable SKU table |
| `summary.csv` | one row per region × family with counts, assessed count, and zone-adjusted % restricted |
| `skus.csv` | one row per region × SKU with status, zones, reason codes |
| `pairs.csv` | one row per requested region: pair, geography match, zone support, both-side scores, placement notes, verdicts and DR note |
| `probes.csv` | with `--need` or `--profile`: one row per region × placement probe — score, result, quota detail, SKU/zone split |
| `verdicts.csv` | with `--profile`: one row per region × profile VM — status, reason, family, vCPU need, quota, probe score, region verdict |
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

## Workload profiles

`--profile PATH` turns the scan into a per-region verdict for a specific deployment: can this workload go
here, and if not, what stops it. The profile is a small YAML (or JSON, same keys) file:

```yaml
name: wave1                       # required; appears in labels
os: Linux                         # Linux | Windows (default Linux); sent with the placement probes
zonal: true                       # default false; true = the design must be zone-resilient
vms:                              # required, at least one
  - sku: Standard_D8s_v5          # exact SKU name
    count: 12                     # integer >= 1
  - sku: Standard_NC24ads_A100_v4
    count: 1
    optional: true                # default false; optional VMs are reported but never block
```

Unknown keys, missing fields, wrong types, an empty `vms`, and duplicate SKUs are rejected up front.
`--profile` implies `--include-quota` and a placement probe for each VM that is not already decided by
steps 1–3 below (so it cannot be combined with `--need`); the profile's `zonal` controls the probe zones
and the zone check, while `--zonal` still only narrows the restriction scan.

Every scanned region — pairs included — gets a verdict. For each VM the first of these that applies
wins:

1. **not offered** — the SKU is absent from the region's SKU list for this subscription.
2. **restricted** — region-restricted; or, for a zonal profile, fewer than two usable zones (a SKU
   blocked in all but one zone cannot be placed zone-resiliently), including regions with no zones.
3. **quota** — the family's vCPU quota is 0, or the free vCPUs are fewer than the profile needs.
   Needs are summed per family across the profile's required VMs before checking, since two SKUs
   in the same family share one quota; an optional VM is checked on top of that total.
4. **capacity** — the placement probe could not place the request (`InsufficientCapacity`).
5. **deployable** — the probe placed it. If the probe failed (preview API error) the VM is
   **unknown**: restrictions and quota allow it, but capacity could not be checked.

A region is **deployable** only when every required VM is deployable, **blocked** when any required VM
hits 1–4 (the blocking VMs and reasons are listed), and **unknown** when nothing blocks but a probe
failed. Optional VMs show their own status with an "(optional)" mark and never change the region
verdict. Paired regions get a DR note: `DR-ready`, `pair blocked: …`, `primary blocked: …`, or
`both blocked …`, using the same wording as the placement notes (quota is "quota blocks X", never
"cannot place").

Exit codes, for gating a pipeline:

| code | meaning |
|---|---|
| 0 | finished; verdicts are informational unless `--fail-on-blocked` is set |
| 1 | could not run: authentication, Azure request, or invalid input/fixture/profile |
| 2 | usage error |
| 3 | `--fail-on-blocked` and a requested region is blocked (with `--require-pair`: or its paired region is) |

```bash
# refuse to deploy wave 1 unless eastus2 can take it right now
azcap --regions eastus2 --profile wave1.yaml --fail-on-blocked || exit 1

# the same, but the DR pair must be deployable too
azcap --regions eastus2 --profile wave1.yaml --fail-on-blocked --require-pair || exit 1
```

Console output, one line per region (the offline demo: `--fixture fixtures/sample.json --profile fixtures/wave1.yaml`):

```
Workload "wave1" (zonal, Linux):
  eastus2                deployable  D8s_v5 x12 placed (score 9), E16s_v5 x4 placed (score 8), NC24ads_A100_v4 x1 placed (score 8) (optional)
                                     pair: DR-ready
  brazilsouth            BLOCKED     capacity: Standard_D8s_v5 (score 2, 6 of 12 placed); optional: NC24ads_A100_v4 restricted
                                     pair: both blocked — brazilsouth: cannot place Standard_D8s_v5; pair: quota blocks Standard_D8s_v5
  centralus              deployable  D8s_v5 x12 placed (score 7), E16s_v5 x4 placed (score 6), NC24ads_A100_v4 x1 restricted (optional)  (pair of eastus2)
  southcentralus         BLOCKED     quota: Standard_D8s_v5 (standardDSv5Family limit 0, need 96 vCPU)  (pair of brazilsouth)
```

A verdict is as good as its inputs: restrictions and quota are current, but a placement score is a
hypothetical allocation at the time of the run, not a reservation. Use it to pick and gate, then
reserve capacity if the deployment cannot tolerate `AllocationFailed`.

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

`python fixtures/make_fixture.py` regenerates `fixtures/sample.json` and `fixtures/wave1.yaml`; the
fixture pins eastus2 / centralus / brazilsouth / southcentralus so a profile run shows every verdict.

## Extending

- Add other resource types by relaxing the `resource_type == virtualMachines` filter in
  `fetch_skus_live` (disks, hostGroups, availabilitySets are also in the API).
- Run it on a schedule and ship `summary.csv` to Log Analytics if you want a trend line.
