# 本地一键启动 + 内网穿透（Cloudflare 快速隧道）
#
# 作用：在本机把「后端 API + 前端生产构建 + 公网隧道」一起拉起来，
#       最后打印一个 https://xxx.trycloudflare.com 地址，手机/外部网络可直接访问。
#
# 用法：
#   .\scripts\local-tunnel.ps1              # 正常启动
#   .\scripts\local-tunnel.ps1 -Rebuild     # 先重新构建前端再启动
#   .\scripts\local-tunnel.ps1 -Stop        # 停掉本机这一套（后端/前端/隧道）
#
# 注意：
#   1) 隧道地址每次启动都会变（免费快速隧道的特性），重启后请用新地址。
#   2) 本机不能休眠/关机，否则外网立刻断。
#   3) 外网速度上限 = 你家宽带的**上行**带宽。

[CmdletBinding()]
param(
    [int]$Port = 3000,
    [int]$BackendPort = 8000,
    [switch]$Rebuild,
    [switch]$Stop,
    [switch]$SkipTunnel
)

$ErrorActionPreference = 'Stop'

$Root      = Split-Path -Parent $PSScriptRoot
$Frontend  = Join-Path $Root 'frontend'
$Backend   = Join-Path $Root 'backend'
$LogDir    = Join-Path $Root '.logs'
$VenvPy    = Join-Path $Backend '.venv\Scripts\python.exe'
$Entry     = Join-Path $Frontend '.output\server\index.mjs'

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "    $msg" -ForegroundColor Yellow }

function Get-PidsOnPort([int]$p) {
    Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
}

function Stop-All {
    Write-Step '停止本机运行的服务'
    foreach ($p in @($Port, $BackendPort)) {
        foreach ($procId in Get-PidsOnPort $p) {
            try { Stop-Process -Id $procId -Force -ErrorAction Stop; Write-Ok "端口 $p -> 已停止 PID $procId" }
            catch { Write-Warn "端口 $p -> 停止 PID $procId 失败：$($_.Exception.Message)" }
        }
    }
    Get-Process cloudflared -ErrorAction SilentlyContinue |
        ForEach-Object { try { Stop-Process -Id $_.Id -Force; Write-Ok "隧道 -> 已停止 PID $($_.Id)" } catch {} }
    # 兜底：清掉可能残留的 uvicorn（否则会重复连接数据库）
    Get-CimInstance Win32_Process -Filter "name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'uvicorn' } |
        ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force; Write-Ok "uvicorn 残留 -> 已停止 PID $($_.ProcessId)" } catch {} }
}

if ($Stop) { Stop-All; Write-Host "`n已停止。" -ForegroundColor Green; return }

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# ---------- 前置检查 ----------
Write-Step '前置检查'
if (-not (Test-Path $VenvPy)) { throw "缺少后端虚拟环境：$VenvPy" }
Write-Ok "python: $VenvPy"
if (-not (Test-Path (Join-Path $Backend '.env'))) { throw "缺少 $Backend\.env（数据库/密钥配置）" }
Write-Ok ".env: 已就绪"

# 两个后端进程同时跑会双倍触发 Supabase 的认证熔断，先清干净
$stale = Get-CimInstance Win32_Process -Filter "name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'uvicorn' }
if ($stale) {
    Write-Warn "发现 $($stale.Count) 个残留 uvicorn，先清理（避免重复连接数据库）"
    $stale | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force } catch {} }
}

# dev 服务会锁住构建目录，构建前必须先停
if ($Rebuild) {
    foreach ($procId in Get-PidsOnPort $Port) {
        Write-Warn "停止占用端口 $Port 的进程 PID $procId（构建需要释放锁）"
        try { Stop-Process -Id $procId -Force } catch {}
    }
}

if ($Rebuild -or -not (Test-Path $Entry)) {
    Write-Step '构建前端生产版本'
    Push-Location $Frontend
    try {
        & npm run build
        if ($LASTEXITCODE -ne 0) { throw "前端构建失败（exit $LASTEXITCODE）" }
    } finally { Pop-Location }
    Write-Ok '构建完成'
} else {
    Write-Ok "复用已有构建：$Entry"
    Write-Warn '（源码若改过，请加 -Rebuild 重新构建）'
}

# ---------- 启动后端 ----------
Write-Step "启动后端 :$BackendPort"
$beOut = Join-Path $LogDir 'backend.out.log'
$beErr = Join-Path $LogDir 'backend.err.log'
$be = Start-Process -FilePath $VenvPy `
    -ArgumentList '-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', "$BackendPort" `
    -WorkingDirectory $Backend -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $beOut -RedirectStandardError $beErr
Write-Ok "后端 PID $($be.Id)  日志：$beErr"

$ok = $false
foreach ($i in 1..30) {
    Start-Sleep -Seconds 1
    try {
        $r = Invoke-WebRequest "http://127.0.0.1:$BackendPort/api/health" -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch {}
}
if (-not $ok) {
    Write-Warn "后端未就绪，错误日志末尾："
    if (Test-Path $beErr) { Get-Content $beErr -Tail 25 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray } }
    throw '后端启动失败'
}
Write-Ok '后端已就绪'

# 关键校验：健康检查走内存也能 200，必须确认数据库真的连上了
Write-Step '校验数据库连接'
$dbOk = $true
try {
    $r = Invoke-WebRequest "http://127.0.0.1:$BackendPort/api/draws?limit=1" -UseBasicParsing -TimeoutSec 30
    Write-Ok "数据接口 /api/draws -> $($r.StatusCode)"
} catch {
    $dbOk = $false
    $code = $_.Exception.Response.StatusCode.value__
    Write-Warn "数据接口 /api/draws -> $code（数据库不可用）"
    if (Test-Path $beErr) { Get-Content $beErr -Tail 25 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray } }
}

# ---------- 启动前端 ----------
Write-Step "启动前端生产服务 :$Port"
foreach ($procId in Get-PidsOnPort $Port) {
    Write-Warn "端口 $Port 被 PID $procId 占用，先停止"
    try { Stop-Process -Id $procId -Force } catch {}
    Start-Sleep -Seconds 1
}
$env:PORT = "$Port"; $env:NITRO_PORT = "$Port"; $env:HOST = '127.0.0.1'
$feOut = Join-Path $LogDir 'frontend.out.log'
$feErr = Join-Path $LogDir 'frontend.err.log'
$fe = Start-Process -FilePath 'node' -ArgumentList '.output/server/index.mjs' `
    -WorkingDirectory $Frontend -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $feOut -RedirectStandardError $feErr
Remove-Item Env:PORT, Env:NITRO_PORT, Env:HOST -ErrorAction SilentlyContinue
Write-Ok "前端 PID $($fe.Id)  日志：$feErr"

$ok = $false
foreach ($i in 1..30) {
    Start-Sleep -Seconds 1
    try {
        $r = Invoke-WebRequest "http://127.0.0.1:$Port/api/health" -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch {}
}
if (-not $ok) {
    Write-Warn "前端未就绪，错误日志末尾："
    if (Test-Path $feErr) { Get-Content $feErr -Tail 25 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray } }
    throw '前端启动失败'
}
Write-Ok '前端已就绪（含 /api 同源反代）'

if ($SkipTunnel) {
    Write-Host "`n本机访问： http://127.0.0.1:$Port" -ForegroundColor Green
    return
}

# ---------- 启动隧道 ----------
Write-Step '启动 Cloudflare 快速隧道'
$cf = (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
if (-not $cf) {
    foreach ($c in @("$env:ProgramFiles\cloudflared\cloudflared.exe", "${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe")) {
        if (Test-Path $c) { $cf = $c; break }
    }
}
if (-not $cf) { throw '找不到 cloudflared。安装：winget install --id Cloudflare.cloudflared' }
Write-Ok "cloudflared: $cf"

$tunOut = Join-Path $LogDir 'tunnel.out.log'
$tunErr = Join-Path $LogDir 'tunnel.err.log'
Remove-Item $tunOut, $tunErr -ErrorAction SilentlyContinue
# --protocol http2：国内 UDP 常被 QoS，QUIC 容易反复断连
$tun = Start-Process -FilePath $cf `
    -ArgumentList 'tunnel', '--url', "http://localhost:$Port", '--protocol', 'http2', '--no-autoupdate' `
    -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $tunOut -RedirectStandardError $tunErr
Write-Ok "隧道 PID $($tun.Id)"

$url = $null
foreach ($i in 1..60) {
    Start-Sleep -Seconds 1
    $text = ''
    foreach ($f in @($tunErr, $tunOut)) { if (Test-Path $f) { $text += (Get-Content $f -Raw -ErrorAction SilentlyContinue) } }
    if ($text -match 'https://[a-z0-9-]+\.trycloudflare\.com') { $url = $Matches[0]; break }
    if ($tun.HasExited) { break }
}
if (-not $url) {
    Write-Warn '未能解析出隧道地址，日志末尾：'
    if (Test-Path $tunErr) { Get-Content $tunErr -Tail 25 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray } }
    throw '隧道启动失败'
}

# 从公网侧回环验证一次，确认真的可访问
Write-Step '验证公网可达性'
try {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $r = Invoke-WebRequest "$url/api/health" -UseBasicParsing -TimeoutSec 60
    $sw.Stop()
    Write-Ok "GET $url/api/health -> $($r.StatusCode)  ($($sw.ElapsedMilliseconds) ms)"
} catch {
    Write-Warn "公网自测失败：$($_.Exception.Message)"
}

Write-Host ''
Write-Host '================================================================' -ForegroundColor Green
Write-Host '  外网访问地址（手机可直接打开）' -ForegroundColor Green
Write-Host "  $url" -ForegroundColor White
Write-Host ''
Write-Host '  本机访问： http://127.0.0.1' -NoNewline; Write-Host ":$Port" -ForegroundColor White
if (-not $dbOk) { Write-Host '  ⚠ 数据库不可用，页面数据会是空的' -ForegroundColor Yellow }
Write-Host '================================================================' -ForegroundColor Green
Write-Host ''
Write-Host "停止全部服务： .\scripts\local-tunnel.ps1 -Stop" -ForegroundColor DarkGray
Write-Host "日志目录： $LogDir" -ForegroundColor DarkGray
Write-Host ''
