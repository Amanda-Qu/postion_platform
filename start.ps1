param([switch]$Background)
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    python -m venv (Join-Path $projectRoot '.venv')
    $dependencyFile = if (Test-Path -LiteralPath (Join-Path $projectRoot 'requirements.lock')) { 'requirements.lock' } else { 'requirements.txt' }
    & $pythonExe -m pip install -r (Join-Path $projectRoot $dependencyFile)
    if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，请检查网络后重试。' }
}
Set-Location -LiteralPath $projectRoot
if ($Background) {
    New-Item -ItemType Directory -Force (Join-Path $projectRoot 'data') | Out-Null
    $process = Start-Process -FilePath $pythonExe -ArgumentList 'run.py' -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $projectRoot 'data\server.log') -RedirectStandardError (Join-Path $projectRoot 'data\server-error.log')
    $process.Id | Set-Content -LiteralPath (Join-Path $projectRoot 'data\server.pid')
    Write-Output "后台服务 PID $($process.Id) 已启动。访问 http://127.0.0.1:8765，密码见 data/access-password.txt。"
} else {
    & $pythonExe run.py
}
