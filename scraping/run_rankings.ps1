# run_rankings.ps1
# Runs AmzRankings per category so Chromium restarts between each one.
# This prevents the V8 heap OOM that happens when one browser processes
# thousands of nodes over many hours without restarting.
#
# Usage (from scraping/):
#   .\run_rankings.ps1                                                   # all categories, bestseller
#   .\run_rankings.ps1 -ListType new_release                             # all categories, new releases
#   .\run_rankings.ps1 -StartFrom "Home & Kitchen"                       # resume mid-list
#   .\run_rankings.ps1 -Category "Pet Supplies"                          # single category then stop
#   .\run_rankings.ps1 -Category "Pet Supplies" -ListType new_release
#   .\run_rankings.ps1 -Categories "Pet Supplies|Office Products"        # specific subset, pipe-delimited
#   .\run_rankings.ps1 -Categories "Pet Supplies|Office Products" -ListType new_release

param(
    [string]$ListType   = "bestseller",
    [string]$StartFrom  = "",
    [string]$Category   = "",
    [string]$Categories = ""
)

$allCategories = @(
    "Arts, Crafts & Sewing",
    "Clothing, Shoes & Jewelry",
    "Handmade Products",
    "Health & Household",
    "Home & Kitchen",
    "Kitchen & Dining",
    "Office Products",
    "Patio, Lawn & Garden",
    "Pet Supplies",
    "Tools & Home Improvement"
)

function Run-Category([string]$cat, [string]$listType) {
    $slug    = $cat -replace '[^\w]', '_'
    $logFile = "logs/amz_rankings_${listType}_${slug}.log"
    Write-Host ""
    Write-Host "========================================"
    Write-Host "Category : $cat"
    Write-Host "Log      : $logFile"
    Write-Host "========================================"
    scrapy crawl AmzRankings -a list_type=$listType -a "categories=$cat" -a include_descendants=true -s LOG_FILE=$logFile
}

# Single-category mode
if ($Category -ne "") {
    if ($allCategories -notcontains $Category) {
        Write-Host "ERROR: '$Category' not in category list. Valid values:"
        $allCategories | ForEach-Object { Write-Host "  $_" }
        exit 1
    }
    Run-Category $Category $ListType
    Write-Host ""
    Write-Host "Done. Run merge_rankings.sql to promote staging -> transformed."
    exit 0
}

# Subset mode — pipe-delimited list of categories
if ($Categories -ne "") {
    $subset = $Categories -split '\|' | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }
    $invalid = $subset | Where-Object { $allCategories -notcontains $_ }
    if ($invalid) {
        Write-Host "ERROR: unknown categories: $($invalid -join ', '). Valid values:"
        $allCategories | ForEach-Object { Write-Host "  $_" }
        exit 1
    }
    foreach ($cat in $subset) {
        Run-Category $cat $ListType
        if ($subset[-1] -ne $cat) {
            Write-Host "Finished: $cat -- waiting 90s before next category"
            Start-Sleep -Seconds 90
        }
    }
    Write-Host ""
    Write-Host "Done. Run merge_rankings.sql to promote staging -> transformed."
    exit 0
}

# Full-list mode (with optional resume via -StartFrom)
$skipping = $StartFrom -ne ""

foreach ($cat in $allCategories) {
    if ($skipping) {
        if ($cat -eq $StartFrom) { $skipping = $false } else { continue }
    }
    Run-Category $cat $ListType
    Write-Host "Finished: $cat -- waiting 90s before next category"
    Start-Sleep -Seconds 90
}

Write-Host ""
Write-Host "All categories done. Run merge_rankings.sql to promote staging -> transformed."
