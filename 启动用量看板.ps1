param([switch]$NoBrowser, [ValidateRange(1,65535)][int]$Port = 43129)
$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$projectRoot = $PSScriptRoot
$url = "http://127.0.0.1:$Port"
$expectedApp = 'codex-project-usage-dashboard-v1'
$health = $null
try { $health = Invoke-RestMethod "$url/api/health" -TimeoutSec 2 } catch {}
if ($health) {
    if ($health.app -ne $expectedApp -or $health.root -ne $projectRoot) {
        throw "$Port 已由其他服务占用，请用 -Port 指定其他端口。"
    }
} else {
    $pythonCommand = Get-Command python -ErrorAction Stop
    $runtimePath = (& $pythonCommand.Source -c 'import sys; sys.exit("需要 Python 3.10 或更高版本") if sys.version_info < (3, 10) else print(sys.executable)').Trim()
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $runtimePath)) { throw '需要可用的 Python 3.10 或更高版本；未安装任何依赖。' }
    $dataPath = Join-Path $projectRoot '03_数据'
    New-Item -ItemType Directory -Path $dataPath -Force | Out-Null
    $scriptPath = Join-Path $projectRoot '02_实现\server.py'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $arguments = @('-B', ('"' + $scriptPath + '"'), '--port', "$Port")
    $process = Start-Process -FilePath $runtimePath -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $dataPath '服务输出.log') -RedirectStandardError (Join-Path $dataPath '服务错误.log')
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 250
        try { $health = Invoke-RestMethod "$url/api/health" -TimeoutSec 1 } catch {}
        if ($health) { break }
        if ($process.HasExited) { throw '服务启动失败，请查看本项目 03_数据\服务错误.log。' }
    }
    if (-not $health -or $health.app -ne $expectedApp -or $health.root -ne $projectRoot) {
        throw '无法确认看板服务，未打开网页。'
    }
}
if (-not $NoBrowser) { Start-Process $url }
Write-Output "用量看板：$url"
Write-Output '只读取本机用量；刷新不会调用 AI，也不会消耗模型 token。'
