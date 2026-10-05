# scripts/pack_source.ps1 — Stage 9-H5.
#
# Build ttrronev_src.zip from the working tree for SHARING SOURCE without the
# ~900 MB of runtime state and without any credentials. Excludes:
#   .git\ .venv\ data\ logs\ __pycache__\ detectors\results\
#   backtesting\results\ paper_trade\data\ paper_trade\logs\  and  .env / .env.*
# .env.example (placeholders only) IS kept so a recipient has the env template.
#
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\pack_source.ps1
#         -> writes ttrronev_src.zip at the repo root.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot            # repo root (this file is in scripts\)
$zip  = Join-Path $root "ttrronev_src.zip"

# Fully-excluded top-level directories — skipped WITHOUT descending, so the huge
# .venv/.git/data trees are never even enumerated.
$topSkip = @(".git", ".venv", "data", "logs")
# Nested path fragments excluded anywhere below a kept top-level dir.
$nestedSkip = @("\__pycache__\", "\detectors\results\", "\backtesting\results\",
                "\paper_trade\data\", "\paper_trade\logs\")

Push-Location $root
try {
  $files = New-Object System.Collections.Generic.List[string]
  Get-ChildItem -Force | Where-Object {
    -not ($_.PSIsContainer -and $topSkip -contains $_.Name)
  } | ForEach-Object {
    $items = if ($_.PSIsContainer) { Get-ChildItem $_.FullName -Recurse -File -Force } else { $_ }
    foreach ($f in $items) {
      $rel = $f.FullName.Substring($root.Length) + "\"
      $skip = $false
      foreach ($n in $nestedSkip) { if ($rel -like "*$n*") { $skip = $true; break } }
      if ($skip) { continue }
      $nm = $f.Name
      if ($nm -eq ".env" -or ($nm -like ".env.*" -and $nm -ne ".env.example")) { continue }
      if ($nm -eq "ttrronev_src.zip") { continue }
      $files.Add($f.FullName)
    }
  }

  if (Test-Path $zip) { Remove-Item $zip -Force }
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  $archive = [System.IO.Compression.ZipFile]::Open($zip, "Create")
  try {
    foreach ($full in $files) {
      $entry = ($full.Substring($root.Length + 1)) -replace "\\", "/"   # preserve tree
      [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
        $archive, $full, $entry, [System.IO.Compression.CompressionLevel]::Optimal)
    }
  } finally { $archive.Dispose() }

  $mb = [math]::Round((Get-Item $zip).Length / 1MB, 2)
  Write-Host "[pack_source] packed $($files.Count) files -> $zip ($mb MB)"
} finally { Pop-Location }
