$ErrorActionPreference = 'Stop'
$processInfoFile = Join-Path $PSScriptRoot '03_数据\服务进程.json'
if (-not (Test-Path -LiteralPath $processInfoFile)) { Write-Output '没有本项目的服务进程记录。'; return }
$record = Get-Content -LiteralPath $processInfoFile -Raw -Encoding UTF8 | ConvertFrom-Json
$ownedProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $($record.pid)"
if (-not $ownedProcess) { Write-Output '本项目服务已经停止。'; return }
$expectedScript = Join-Path $PSScriptRoot '02_实现\server.py'
if ($record.root -ne $PSScriptRoot -or -not $ownedProcess.CommandLine -or -not $ownedProcess.CommandLine.Contains($expectedScript)) {
    throw '进程归属不匹配，未终止任何进程。'
}
Stop-Process -Id $record.pid
Write-Output '已停止本项目看板，统计缓存和项目归属保留。'
