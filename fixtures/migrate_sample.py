"""Synthetic Azure Migrate assessment data for tests and demos: the shape of the assessedMachines API pages
and of a portal .xlsx export. No real assessment is included; values are invented but plausible."""

from __future__ import annotations

from pathlib import Path

# name, recommended size, suitability, operatingSystemType, explanation, [(disk type, disk size)]
# disk type/size use the API enums (recommendedDiskType Premium|StandardSSD|Standard|Ultra|PremiumV2, size Premium_P30)
MACHINES = [
    ("web-01", "Standard_D4s_v5", "Suitable", "windowsGuest", None, [("Premium", "Premium_P10")]),
    ("web-02", "Standard_D4s_v5", "Suitable", "windowsGuest", None, [("Premium", "Premium_P10")]),
    ("app-01", "Standard_D8s_v5", "Suitable", "linuxGuest", None, [("Premium", "Premium_P30")]),
    (
        "app-02",
        "Standard_D8s_v5",
        "ConditionallySuitable",
        "linuxGuest",
        "Boot type UEFI needs a Gen2 image",
        [("Premium", "Premium_P30")],
    ),
    (
        "db-01",
        "Standard_E16s_v5",
        "Suitable",
        "linuxGuest",
        None,
        [("Premium", "Premium_P30"), ("Premium", "Premium_P40")],
    ),
    (
        "db-02",
        "Standard_E16s_v5",
        "Suitable",
        "linuxGuest",
        None,
        [("Premium", "Premium_P30"), ("Premium", "Premium_P40")],
    ),
    ("legacy-01", None, "NotSuitable", "windowsGuest", "Unsupported operating system", []),
    ("legacy-02", "Standard_A2_v2", "NotSuitable", "windowsGuest", "Unsupported operating system", []),
    ("unk-01", "Standard_D2s_v5", "Unknown", "other", "Not enough performance data", []),
    ("batch-01", "Standard_F8s_v2", "Suitable", "other", None, [("StandardSSD", "StandardSSD_E10")]),
    ("mem-01", None, "Suitable", "linuxGuest", None, []),
]
OS_NAMES = {"windowsGuest": "Windows Server 2019", "linuxGuest": "Red Hat Enterprise Linux 9.2", "other": "FreeBSD 13"}
# wording seen in a real export (2026-09): "Ready", "Ready With Conditions"; the others follow the same pattern
READINESS = {
    "Suitable": "Ready",
    "ConditionallySuitable": "Ready With Conditions",
    "NotSuitable": "Not Ready",
    "Unknown": "Unknown",
}
DISK_TYPE_WORDING = {
    "Premium": "Premium managed disks",
    "StandardSSD": "Standard SSD managed disks",
    "Standard": "Standard HDD managed disks",
    "Ultra": "Ultra disks",
}
ASSESSMENT_ID = (
    "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-migrate/providers/Microsoft.Migrate"
    "/assessmentProjects/contoso-proj/groups/wave1/assessments/wave1-assess"
)


def build_assessment() -> dict:
    return {
        "id": ASSESSMENT_ID,
        "name": "wave1-assess",
        "type": "Microsoft.Migrate/assessmentprojects/groups/assessments",
        "properties": {
            "azureLocation": "EastUS2",
            "sizingCriterion": "PerformanceBased",
            "azureVmFamilies": ["Dsv5_series", "Esv5_series", "Fsv2_series"],
            "status": "Completed",
        },
    }


def build_api_pages(
    page_size: int = 4, base_url: str = "https://management.azure.com" + ASSESSMENT_ID
) -> dict[str, dict]:
    """assessedMachines pages keyed by the URL that returns them, linked with nextLink."""
    first = f"{base_url}/assessedMachines?api-version=2023-03-15"
    items = []
    for name, size, suitability, os_type, explanation, disks in MACHINES:
        props = {
            "displayName": name,
            "recommendedSize": size or "",
            "suitability": suitability,
            "operatingSystemType": os_type,
            "disks": {
                f"scsi0:{i}": {"displayName": f"disk{i}", "recommendedDiskType": t, "recommendedDiskSize": z}
                for i, (t, z) in enumerate(disks)
            },
        }
        if explanation:
            props["suitabilityExplanation"] = explanation
        items.append({"id": f"{ASSESSMENT_ID}/assessedMachines/{name}", "name": name, "properties": props})
    pages: dict[str, dict] = {}
    url = first
    for start in range(0, len(items), page_size):
        chunk = items[start : start + page_size]
        page: dict = {"value": chunk}
        if start + page_size < len(items):
            page["nextLink"] = f"{first}&$skipToken=page{start // page_size + 1}"
        pages[url] = page
        url = page.get("nextLink", "")
    return pages


# Sheet and header layout of a real portal export (verified 2026-09): four sheets, headers in row 1.
MACHINE_HEADERS = [
    "Machine",
    "VM host",
    "Azure VM readiness",
    "Azure readiness issues",
    "Data collection issues",
    "Recommended size",
    "Operating system",
    "Boot type",
    "Processor",
    "Cores",
    "Memory(MB)",
    "CPU usage(%)",
    "Memory usage(%)",
    "Storage(GB)",
    "Group name",
]
DISK_HEADERS = [
    "Machine",
    "Disk name",
    "Azure disk readiness",
    "Recommended disk size SKU",
    "Recommended disk type",
    "Redundancy",
    "Source disk size(GB)",
]
SUMMARY_ROWS = [
    ("Assessment name", "wave1-assess"),
    ("Group name", "wave1"),
    ("Project name", "contoso-proj"),
    ("Subscription ID", "00000000-0000-0000-0000-000000000000"),
    ("Machines assessed", 11),
]
PROPERTY_ROWS = [
    ("Property", "Selected value"),
    ("Target location", "East US 2"),
    ("Sizing criterion", "Performance-based"),
    ("Comfort factor", "1.3"),
    ("Pricing tier", "Standard"),
]


def build_xlsx(
    path: Path,
    *,
    machine_headers: list[str] | None = None,
    disk_headers: list[str] | None = None,
    with_disks: bool = True,
) -> None:
    """A small workbook shaped like a portal export: Assessed_Machines (+ Assessed_Disks). Needs openpyxl."""
    import openpyxl

    headers = machine_headers or MACHINE_HEADERS
    book = openpyxl.Workbook()
    ws = book.active
    ws.title = "Assessment_Summary"
    for row in SUMMARY_ROWS:
        ws.append(list(row))
    wm = book.create_sheet("All_Assessed_Machines")
    wm.append(headers)
    for name, size, suitability, os_type, explanation, _disks in MACHINES:
        row = {
            "machine": name,
            "readiness": READINESS[suitability],
            "issues": explanation or "",
            "size": size or "",
            "os": OS_NAMES[os_type],
            "cores": 4,
            "memory": 16384,
        }
        # place values by what each header means so alternative header spellings land in the same columns
        wm.append([_value_for(h, row) for h in headers])
    if with_disks:
        dheaders = disk_headers or DISK_HEADERS
        wd = book.create_sheet("All_Assessed_Disks")
        wd.append(dheaders)
        for name, _size, _suitability, _os_type, _explanation, disks in MACHINES:
            for i, (t, z) in enumerate(disks):
                drow = {"machine": name, "disk": f"disk{i}", "type": DISK_TYPE_WORDING.get(t, t), "size": z, "gb": 128}
                wd.append([_value_for(h, drow) for h in dheaders])
    wp = book.create_sheet("Assessment_Properties")
    for row in PROPERTY_ROWS:
        wp.append(list(row))
    book.save(path)


def _value_for(header: str, row: dict) -> object:
    h = header.lower()
    if "readiness issues" in h or "ready?" in h:
        return row.get("issues", "")
    if "readiness" in h or h in ("ready?",):
        return row.get("readiness", "")
    if "disk type" in h:
        return row.get("type", "")
    if "size" in h and "disk" in h or "(recommended)" in h:
        return row.get("size", "")
    if "recommended size" in h or "vm size" in h or "target size" in h or h == "size":
        return row.get("size", "")
    if "operating system" in h or h == "os":
        return row.get("os", "")
    if "machine" in h or "server" in h or "host" in h or h == "name":
        return row.get("machine", "")
    if h.startswith("disk"):
        return row.get("disk", "")
    if "cores" in h:
        return row.get("cores", "")
    if "memory" in h or "mem" == h:
        return row.get("memory", "")
    if "gb" in h:
        return row.get("gb", "")
    return ""


if __name__ == "__main__":
    build_xlsx(Path(__file__).with_name("migrate_sample.xlsx"))
    print("wrote migrate_sample.xlsx")
