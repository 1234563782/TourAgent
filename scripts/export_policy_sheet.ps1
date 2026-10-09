param(
    [Parameter(Mandatory=$true)][string]$Source,
    [Parameter(Mandatory=$true)][string]$Sheet,
    [Parameter(Mandatory=$true)][string]$Output
)
$ErrorActionPreference = "Stop"
$sourcePath = (Resolve-Path -LiteralPath $Source).Path
$outputPath = [IO.Path]::GetFullPath($Output)
if (Test-Path -LiteralPath $outputPath) { throw "Output already exists" }
if ([IO.Path]::GetExtension($sourcePath) -notin @(".xls", ".xlsx")) { throw "Expected Excel source" }
$excel = $null
$book = $null
$worksheet = $null
try {
    $hash = (Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $excel = New-Object -ComObject Excel.Application
    $excel.Visible = $false
    $excel.DisplayAlerts = $false
    $excel.AskToUpdateLinks = $false
    $excel.AutomationSecurity = 3
    $book = $excel.Workbooks.Open($sourcePath, 0, $true)
    $worksheet = $book.Worksheets.Item($Sheet)
    $range = $worksheet.UsedRange
    if ($range.Rows.Count -gt 5000 -or $range.Columns.Count -gt 100) { throw "Sheet exceeds import limit" }
    $rows = @()
    for ($row = $range.Row; $row -lt $range.Row + $range.Rows.Count; $row++) {
        $cells = @()
        for ($col = 1; $col -lt $range.Column + $range.Columns.Count; $col++) {
            $cell = $worksheet.Cells.Item($row, $col)
            $origin = if ($cell.MergeCells) { $cell.MergeArea.Cells.Item(1, 1) } else { $cell }
            if ($origin.HasFormula) { throw "Formula cells require a separately reviewed values export" }
            $cells += @{ value = $origin.Value2; source = $origin.Address($false, $false) }
        }
        $rows += @{ row = $row; cells = $cells }
    }
    if ((Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $hash) {
        throw "Source changed during export"
    }
    $payload = @{ schema_version = 1; source = [IO.Path]::GetFileName($sourcePath)
        source_sha256 = $hash; sheet = $Sheet; rows = $rows }
    $json = $payload | ConvertTo-Json -Depth 8
    $parent = [IO.Path]::GetDirectoryName($outputPath)
    [IO.Directory]::CreateDirectory($parent) | Out-Null
    $stream = [IO.File]::Open($outputPath, [IO.FileMode]::CreateNew)
    try {
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes($json)
        $stream.Write($bytes, 0, $bytes.Length)
    } finally { $stream.Dispose() }
    Write-Output "Exported sheet '$Sheet' with $($rows.Count) rows."
} finally {
    if ($book) { $book.Close($false) }
    if ($excel) { $excel.Quit() }
    foreach ($item in @($worksheet, $book, $excel)) {
        if ($item) { [Runtime.InteropServices.Marshal]::FinalReleaseComObject($item) | Out-Null }
    }
}
