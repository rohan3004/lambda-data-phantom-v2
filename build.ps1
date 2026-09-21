<#
.SYNOPSIS
    Builds the AWS Lambda deployment package (deployment.zip) for lambda-data-phantom-v2.

.DESCRIPTION
    Stages lambda_function.py and its vendored dependencies (bs4, soupsieve,
    typing_extensions + their .dist-info dirs) into a clean folder, strips
    __pycache__/*.pyc, and zips the CONTENTS at the archive root so that
    lambda_function.py sits at the top level (required by the Lambda runtime).

    Excludes: test fixtures, test_parser.py, .git, readme, LICENSE, build scripts.

.EXAMPLE
    .\build.ps1
#>
[CmdletBinding()]
param(
    [string]$OutFile = "deployment.zip"
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

Write-Host "==> Building Lambda deployment package" -ForegroundColor Cyan

# Items that must be included in the package.
$handler = "lambda_function.py"
$deps = @(
    "bs4",
    "soupsieve",
    "typing_extensions.py",
    "beautifulsoup4-4.14.2.dist-info",
    "soupsieve-2.8.dist-info",
    "typing_extensions-4.15.0.dist-info"
)

# --- Preflight: verify everything we need is present ---
$missing = @()
if (-not (Test-Path $handler)) { $missing += $handler }
foreach ($d in $deps) { if (-not (Test-Path $d)) { $missing += $d } }
if ($missing.Count -gt 0) {
    Write-Error "Missing required items: $($missing -join ', ')"
    exit 1
}

# --- Syntax check the handler before packaging ---
Write-Host "==> Compiling $handler" -ForegroundColor Cyan
python -m py_compile $handler
if ($LASTEXITCODE -ne 0) {
    Write-Error "py_compile failed; aborting build."
    exit 1
}

# --- Stage into a clean temp dir ---
$staging = Join-Path $env:TEMP ("lambda_pkg_" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $staging | Out-Null
try {
    Copy-Item $handler $staging
    foreach ($d in $deps) {
        if ((Get-Item $d).PSIsContainer) {
            Copy-Item -Recurse $d (Join-Path $staging $d)
        } else {
            Copy-Item $d $staging
        }
    }

    # Strip bytecode caches (dead weight; wrong Python version anyway).
    Get-ChildItem -Path $staging -Recurse -Directory -Filter "__pycache__" |
        Remove-Item -Recurse -Force

    # --- Zip the CONTENTS so files land at the archive root ---
    if (Test-Path $OutFile) { Remove-Item $OutFile -Force }
    Compress-Archive -Path (Join-Path $staging "*") -DestinationPath $OutFile
}
finally {
    Remove-Item -Recurse -Force $staging -ErrorAction SilentlyContinue
}

$sizeKb = [math]::Round((Get-Item $OutFile).Length / 1KB, 1)
Write-Host "==> Created $OutFile ($sizeKb KB)" -ForegroundColor Green
Write-Host "    Deploy with:" -ForegroundColor DarkGray
Write-Host "    aws lambda update-function-code --function-name GenerateReportSummary --zip-file fileb://$OutFile" -ForegroundColor DarkGray
