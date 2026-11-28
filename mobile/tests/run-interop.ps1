# run-interop.ps1 —— C# 移植版与 python 版协议互通验证
#
#   pwsh -File run-interop.ps1
#
# 覆盖：连接串编解码、双向上传/下载、续传、密码认证、密码错误拒绝、
#       文字通道（聊天/通知/推送）、服务器推送 + token 下载。

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$work = Join-Path $root ("work-" + (Get-Date -Format 'HHmmss'))
$peerPy = Join-Path $root 'interop_peer.py'
$csDir = Join-Path $root 'Ipv6Transfer.Interop'
$csExe = Join-Path $csDir 'bin\Debug\net10.0\Ipv6Transfer.Interop.exe'
$py = 'python'
$utf8 = New-Object System.Text.UTF8Encoding($false)

New-Item -ItemType Directory -Force $work | Out-Null
foreach ($sub in 'src', 'pyrecv', 'csrecv', 'csrecv2') {
    New-Item -ItemType Directory -Force (Join-Path $work $sub) | Out-Null
}

Write-Host '== 构建 C# 测试工具 =='
dotnet build $csDir -v q --nologo 2>&1 | Out-Null
if (-not (Test-Path $csExe)) { throw "找不到 $csExe" }

$script:failures = @()
$script:seq = 0
function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) {
        Write-Host ("  [PASS] " + $name) -ForegroundColor Green
    } else {
        Write-Host ("  [FAIL] " + $name + "  " + $detail) -ForegroundColor Red
        $script:failures += "$name $detail"
    }
}

function Quote-Args([string[]]$items) {
    return (($items | ForEach-Object {
        if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
    }) -join ' ')
}

function New-LogPaths([string]$name) {
    $script:seq++
    $base = Join-Path $work ("log-" + $script:seq + "-" + $name)
    return @{ Out = "$base.out"; Err = "$base.err" }
}

function Read-Log([string]$path) {
    if (-not (Test-Path $path)) { return '' }
    $fs = $null
    $reader = $null
    try {
        # 子进程还开着重定向文件，必须允许共享读写才能读
        $fs = [System.IO.File]::Open($path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        $reader = New-Object System.IO.StreamReader($fs, $utf8)
        return $reader.ReadToEnd()
    } catch {
        return ''
    } finally {
        if ($reader) { $reader.Dispose() }
        if ($fs) { $fs.Dispose() }
    }
}

function Invoke-Tool([string]$exe, [string[]]$toolArgs, [string]$name, [int]$timeoutMs = 120000) {
    $logs = New-LogPaths $name
    $proc = Start-Process -FilePath $exe -ArgumentList (Quote-Args $toolArgs) -WorkingDirectory $root `
        -RedirectStandardOutput $logs.Out -RedirectStandardError $logs.Err -PassThru -NoNewWindow
    if (-not $proc.WaitForExit($timeoutMs)) {
        try { $proc.Kill($true) } catch {}
    }
    $text = Read-Log $logs.Out
    $errText = Read-Log $logs.Err
    if ($errText.Trim().Length -gt 0) { $text = $text + "`n[stderr] " + $errText }
    return @{ Text = $text; Code = $proc.ExitCode }
}

function Invoke-Python([string[]]$peerArgs, [int]$timeoutMs = 120000) {
    return Invoke-Tool $py (@($peerPy) + $peerArgs) ('py-' + $peerArgs[0]) $timeoutMs
}

function Invoke-Cs([string[]]$csArgs, [int]$timeoutMs = 120000) {
    return Invoke-Tool $csExe $csArgs ('cs-' + $csArgs[0]) $timeoutMs
}

function Wait-Ready([string]$file, [int]$timeoutMs = 25000) {
    $deadline = (Get-Date).AddMilliseconds($timeoutMs)
    while ((Get-Date) -lt $deadline) {
        if (Test-Path $file) {
            if ((Read-Log $file) -match 'READY') { return $true }
        }
        Start-Sleep -Milliseconds 100
    }
    return $false
}

function Start-Peer([string]$exe, [string[]]$peerArgs, [string]$name) {
    $logs = New-LogPaths ('bg-' + $name)
    $proc = Start-Process -FilePath $exe -ArgumentList (Quote-Args $peerArgs) -WorkingDirectory $root `
        -RedirectStandardOutput $logs.Out -RedirectStandardError $logs.Err -PassThru -NoNewWindow
    [void](Wait-Ready $logs.Out)
    return @{ Proc = $proc; Out = $logs.Out; Err = $logs.Err }
}

function Start-PythonPeer([string[]]$peerArgs, [string]$name) {
    return Start-Peer $py (@($peerPy) + $peerArgs) $name
}

function Start-CsPeer([string[]]$csArgs, [string]$name) {
    return Start-Peer $csExe $csArgs $name
}

function Stop-Peer($peer) {
    try { $peer.Proc.WaitForExit(15000) | Out-Null } catch {}
    if (-not $peer.Proc.HasExited) { try { $peer.Proc.Kill($true) } catch {} }
    $text = Read-Log $peer.Out
    $errText = Read-Log $peer.Err
    if ($errText.Trim().Length -gt 0) { $text = $text + "`n[stderr] " + $errText }
    return $text
}

function New-TestFile([string]$path, [int]$size) {
    $bytes = New-Object byte[] $size
    $rng = [System.Random]::new(20261128)
    $rng.NextBytes($bytes)
    [System.IO.File]::WriteAllBytes($path, $bytes)
    return (Get-FileHash -Algorithm SHA256 -Path $path).Hash.ToLowerInvariant()
}

function Sha([string]$path) {
    if (-not (Test-Path $path)) { return '<missing>' }
    return (Get-FileHash -Algorithm SHA256 -Path $path).Hash.ToLowerInvariant()
}

$payload = Join-Path $work 'src\payload 测试.bin'
$payloadHash = New-TestFile $payload (2 * 1024 * 1024 + 777)
Write-Host "== 测试负载：$payload ($([math]::Round(((Get-Item $payload).Length / 1MB), 2)) MiB) =="

# ---------------------------------------------------------------- 连接串
Write-Host '== 1. 连接配置串 =='
$pyToken = (Invoke-Python @('cfg-encode', '2409:8a00::1234', '9000', 'p@ss 中文')).Text.Trim()
$csDecoded = Invoke-Cs @('cfg-decode', $pyToken)
Check 'python 生成 → C# 解析' ($csDecoded.Text -match 'host=2409:8a00::1234' -and $csDecoded.Text -match 'password=p@ss 中文') $csDecoded.Text

$csToken = (Invoke-Cs @('cfg-encode', '2409:8a00::1234', '9000', 'p@ss 中文')).Text.Trim()
$pyDecoded = (Invoke-Python @('cfg-decode', $csToken)).Text.Trim()
Check 'C# 生成 → python 解析' ($pyDecoded -match 'host=2409:8a00::1234' -and $pyDecoded -match 'password=p@ss 中文') $pyDecoded
Check '两端生成的连接串逐字节一致' ($pyToken -eq $csToken) "py=$pyToken cs=$csToken"

# ---------------------------------------------------------------- 上传：python 服务器 ← C# 客户端
Write-Host '== 2. C# 上传 → python 服务器 =='
$srvDir = Join-Path $work 'pysrv'
New-Item -ItemType Directory -Force $srvDir | Out-Null
$peer = Start-PythonPeer @('server', $srvDir, '::1', '9601', '-', '1') 'pysrv1'
$upload = Invoke-Cs @('send', '::1', '9601', $payload)
$srvLog = Stop-Peer $peer
Check 'C# 上传结果 ok' ($upload.Text -match 'RESULT ok=True' -and $upload.Text -match 'matched=True') $upload.Text
Check 'python 服务器收到同名文件且哈希一致' ((Sha (Join-Path $srvDir 'payload 测试.bin')) -eq $payloadHash) $srvLog

# ---------------------------------------------------------------- 下载：python 服务器 → C# 客户端
Write-Host '== 3. C# 从 python 服务器下载 =='
$peer = Start-PythonPeer @('server', $srvDir, '::1', '9602', '-', '1') 'pysrv2'
$download = Invoke-Cs @('recv', '::1', '9602', 'payload 测试.bin', (Join-Path $work 'csrecv'))
$downloadPath = Join-Path $work 'csrecv\payload 测试.bin'
[void](Stop-Peer $peer)
Check 'C# 下载结果 ok' ($download.Text -match 'RESULT ok=True' -and $download.Text -match 'matched=True') $download.Text
Check '下载内容哈希一致' ((Sha $downloadPath) -eq $payloadHash) (Sha $downloadPath)

# ---------------------------------------------------------------- 上传：C# 服务器 ← python 客户端
Write-Host '== 4. python 上传 → C# 服务器 =='
$csSrvDir = Join-Path $work 'cssrv'
New-Item -ItemType Directory -Force $csSrvDir | Out-Null
$peer = Start-CsPeer @('server', $csSrvDir, '::1', '9603', '-', '1') 'cssrv1'
$pySend = Invoke-Python @('send', '::1', '9603', $payload)
$csLog = Stop-Peer $peer
Check 'python 上传结果 ok' ($pySend.Text -match 'RESULT ok=True' -and $pySend.Text -match 'matched=True') $pySend.Text
Check 'C# 服务器收到文件且哈希一致' ((Sha (Join-Path $csSrvDir 'payload 测试.bin')) -eq $payloadHash) $csLog

# ---------------------------------------------------------------- 下载：C# 服务器 → python 客户端
Write-Host '== 5. python 从 C# 服务器下载 =='
$peer = Start-CsPeer @('server', $csSrvDir, '::1', '9604', '-', '1') 'cssrv2'
$pyRecv = Invoke-Python @('recv', '::1', '9604', 'payload 测试.bin', (Join-Path $work 'pyrecv'))
$pyRecvPath = Join-Path $work 'pyrecv\payload 测试.bin'
[void](Stop-Peer $peer)
Check 'python 下载结果 ok' ($pyRecv.Text -match 'RESULT ok=True' -and $pyRecv.Text -match 'matched=True') $pyRecv.Text
Check 'python 收到的内容哈希一致' ((Sha $pyRecvPath) -eq $payloadHash) (Sha $pyRecvPath)

# ---------------------------------------------------------------- 续传（C# 服务器 ← 半截 .part）
Write-Host '== 6. 续传 =='
$resumeDir = Join-Path $work 'resume'
New-Item -ItemType Directory -Force $resumeDir | Out-Null
$all = [System.IO.File]::ReadAllBytes($payload)
$partBytes = New-Object byte[] 500000
[Array]::Copy($all, $partBytes, 500000)
[System.IO.File]::WriteAllBytes((Join-Path $resumeDir 'payload 测试.bin.part'), $partBytes)
$peer = Start-CsPeer @('server', $resumeDir, '::1', '9605', '-', '1') 'resumesrv'
$resumeSend = Invoke-Python @('send', '::1', '9605', $payload)
$csLog = Stop-Peer $peer
$finalPath = Join-Path $resumeDir 'payload 测试.bin'
Check '续传后文件哈希一致' ((Sha $finalPath) -eq $payloadHash) ($csLog + $resumeSend.Text)
Check '服务端记录了 resume_from=500000' ($csLog -match 'resume=500000') $csLog

# ---------------------------------------------------------------- 密码认证
Write-Host '== 7. 连接密码 =='
$pwDir = Join-Path $work 'pwsrv'
New-Item -ItemType Directory -Force $pwDir | Out-Null
$peer = Start-CsPeer @('server', $pwDir, '::1', '9606', 'secret-密码', '1') 'pwsrv'
$pwOk = Invoke-Python @('send', '::1', '9606', $payload, 'secret-密码')
[void](Stop-Peer $peer)
Check '正确密码可以连接（python → C#）' ($pwOk.Text -match 'RESULT ok=True') $pwOk.Text

$peer = Start-CsPeer @('server', $pwDir, '::1', '9607', 'secret-密码', '1') 'pwsrv2'
$pwBad = Invoke-Python @('send', '::1', '9607', $payload, 'wrong-pass')
[void](Stop-Peer $peer)
Check '错误密码被拒绝（python → C#）' ($pwBad.Text -match '连接密码错误' -or $pwBad.Text -match 'REJECTED') $pwBad.Text

$pwDir2 = Join-Path $work 'pwsrv2'
New-Item -ItemType Directory -Force $pwDir2 | Out-Null
$peer = Start-PythonPeer @('server', $pwDir2, '::1', '9608', 'secret-密码', '1') 'pypwsrv'
$pwBadCs = Invoke-Cs @('send', '::1', '9608', $payload, 'bad')
[void](Stop-Peer $peer)
Check '错误密码被拒绝（C# → python）' ($pwBadCs.Text -match '连接密码错误') $pwBadCs.Text

$peer = Start-PythonPeer @('server', $pwDir2, '::1', '9609', 'secret-密码', '1') 'pypwsrv2'
$pwOkCs = Invoke-Cs @('send', '::1', '9609', $payload, 'secret-密码')
[void](Stop-Peer $peer)
Check '正确密码可以上传（C# → python）' ($pwOkCs.Text -match 'RESULT ok=True') $pwOkCs.Text

# ---------------------------------------------------------------- 文字通道
Write-Host '== 8. 文字通道（聊天 / 通知 / 推送） =='
$peer = Start-PythonPeer @('control-serve', '::1', '9610', '-') 'pyctl'
$ctl = Invoke-Cs @('control-send', '::1', '9610', '你好 python', '-')
$pyLog = Stop-Peer $peer
Check 'C# 发消息 → python 收到' ($pyLog -match 'SAY 你好 python') $pyLog
Check 'python 回通知 + 推送 → C# 收到' ($ctl.Text -match 'NOTICE 收到：你好 python' -and $ctl.Text -match 'PUSH pushed.bin') $ctl.Text

$peer = Start-CsPeer @('control-serve', '::1', '9611', '-') 'csctl'
$pyCtl = Invoke-Python @('control-send', '::1', '9611', '你好 C#', '-')
$csLog = Stop-Peer $peer
Check 'python 发消息 → C# 收到' ($csLog -match 'SAY 你好 C#') $csLog
Check 'C# 回通知 + 推送 → python 收到' ($pyCtl.Text -match 'NOTICE 收到：你好 C#' -and $pyCtl.Text -match 'PUSH pushed.bin') $pyCtl.Text

# ---------------------------------------------------------------- 服务器推送文件
Write-Host '== 9. 服务器推送 + token 下载 =='
$pushDir = Join-Path $work 'push'
New-Item -ItemType Directory -Force $pushDir | Out-Null
$peer = Start-CsPeer @('push-serve', $pushDir, '::1', '9612', $payload) 'cspush'
$pyPull = Invoke-Python @('push-recv', '::1', '9612', (Join-Path $work 'pyrecv'), '-')
$csLog = Stop-Peer $peer
Check 'C# 推送 → python 用 token 下载成功' ($pyPull.Text -match 'RESULT ok=True' -and $pyPull.Text -match 'matched=True') $pyPull.Text
Check 'python 落盘哈希一致' ((Sha (Join-Path $work 'pyrecv\payload 测试.bin')) -eq $payloadHash) $csLog

$pushDir2 = Join-Path $work 'push2'
New-Item -ItemType Directory -Force $pushDir2 | Out-Null
$peer = Start-PythonPeer @('push-serve', $pushDir2, '::1', '9613', $payload, '-') 'pypush'
$csPull = Invoke-Cs @('push-recv', '::1', '9613', (Join-Path $work 'csrecv2'), '-')
$pyLog = Stop-Peer $peer
Check 'python 推送 → C# 用 token 下载成功' ($csPull.Text -match 'RESULT ok=True' -and $csPull.Text -match 'matched=True') $csPull.Text
Check 'C# 落盘哈希一致' ((Sha (Join-Path $work 'csrecv2\payload 测试.bin')) -eq $payloadHash) $pyLog

# ---------------------------------------------------------------- 汇总
Write-Host ''
if ($script:failures.Count -eq 0) {
    Write-Host '全部互通测试通过' -ForegroundColor Green
    exit 0
}

Write-Host ("失败 $($script:failures.Count) 项：") -ForegroundColor Red
$script:failures | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
Write-Host "日志目录：$work"
exit 1
