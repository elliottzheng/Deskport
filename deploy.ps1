# deploy.ps1 - dev -> prod deploy with precheck, backup, conditional restart,
# smoke test and automatic rollback.
#
# Run from the dev directory:
#   powershell -NoProfile -ExecutionPolicy Bypass -File deploy.ps1
#   ... -DryRun          only prechecks, no sync/restart
#   ... -SkipWebRestart  sync + daemons only
#   ... -SkipSmoke       skip smoke test (not recommended)
#
param(
    [switch]$DryRun,
    [switch]$SkipWebRestart,
    [switch]$SkipSmoke,
    [switch]$SkipDaemonRestart,
    [switch]$ForceDaemonRestart
)

$ErrorActionPreference = 'Continue'
$src = (Get-Location).Path
$dst = Join-Path (Split-Path $src -Parent) 'web-platform'
$py = Join-Path $src '.venv\Scripts\python.exe'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backup = Join-Path $src ".deploy-backup\$stamp"

# 端口单一来源: 读 prod 的 config.json (sync 不会覆盖它); 缺失才回退常量
$cfg = $null
$cfgPath = Join-Path $dst 'config.json'
if (Test-Path $cfgPath) {
    try { $cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json } catch { }
}
$webPort = if ($cfg -and $cfg.port) { [int]$cfg.port } else { 6789 }
$localPort = if ($cfg -and $cfg.sessiondUrl) { ([uri]$cfg.sessiondUrl).Port } else { 6791 }
$sshPort = if ($cfg -and $cfg.sshSessiondUrl) { ([uri]$cfg.sshSessiondUrl).Port } else { 6792 }
# 守护进程的代码面: sessiond.py + 整个 modules/ 目录。
# 早期是硬编码文件清单, 模块经过多轮拆分后已跟不上 —— 曾漏掉 winenv.py,
# 导致"环境变量修复"部署后没重启守护进程, 修复根本没生效 (deploy 报
# local=False ssh=False)。现在按目录整体判断, 只有特性文件区分 local/ssh。
$allModules = Get-ChildItem (Join-Path $src 'modules\*.py') | ForEach-Object { $_.Name }
$sshOnly = @('ssh.py', 'ssh_base.py', 'ssh_hosts.py', 'ssh_terminal.py',
             'ssh_sftp.py', 'ssh_sftp_base.py', 'ssh_sftp_ops.py',
             'ssh_sftp_transfer.py')
$localOnly = @('terminal.py')
$daemonFiles = @('sessiond.py') + ($allModules | ForEach-Object { 'modules\' + $_ })
$localFiles = @('sessiond.py') + ($allModules | Where-Object { $sshOnly -notcontains $_ } |
                                  ForEach-Object { 'modules\' + $_ })
$sshFiles = @('sessiond.py') + ($allModules | Where-Object { $localOnly -notcontains $_ } |
                                ForEach-Object { 'modules\' + $_ })

function Say($msg) { Write-Output ("[deploy] " + $msg) }

# 代理/部署进程是否跑在平台会话里 (祖先链含 sessiond.py)
function Test-InsideSessiond {
    $procId = $PID
    for ($i = 0; $i -lt 15; $i++) {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue
        if (-not $p) { return $false }
        if ($p.CommandLine -and $p.CommandLine -match 'sessiond\.py') { return $true }
        if (-not $p.ParentProcessId -or $p.ParentProcessId -eq 0 -or $p.ParentProcessId -eq $procId) {
            return $false
        }
        $procId = $p.ParentProcessId
    }
    return $false
}

function Stop-Port([int]$port) {
    $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
         Select-Object -First 1
    if ($c) {
        Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 900
        Say ("stopped port " + $port + " pid " + $c.OwningProcess)
    }
}

function Test-DaemonStale([int]$port, [string[]]$files) {
    # 守护进程是否在跑旧代码: 它加载的任一文件比进程启动时间还新, 就是旧代码。
    # 只看"本次发布改没改文件"不够 —— 上一次用旧清单漏判时, 文件已同步但守护
    # 进程仍在跑旧代码, 之后每次发布都报 False, 修复一直不生效 (踩过)。
    $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
         Select-Object -First 1
    if (-not $c) { return $true }              # 没在跑 = 必须拉起
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($c.OwningProcess)" -ErrorAction SilentlyContinue
    if (-not $p) { return $true }
    foreach ($f in $files) {
        $item = Get-Item (Join-Path $dst $f) -ErrorAction SilentlyContinue
        if ($item -and $item.LastWriteTime -gt $p.CreationDate) { return $true }
    }
    return $false
}

function Start-Vbs([string]$name) {
    $path = Join-Path $dst $name
    if (Test-Path $path) {
        Start-Process -FilePath 'wscript.exe' -ArgumentList ('"' + $path + '"') `
                      -WorkingDirectory $dst `
                      -WindowStyle Hidden
        Say ("started " + $name)
    } else {
        Say ("missing " + $name)
    }
}

function Wait-Http([string]$url, [int]$seconds = 25) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2
            if ($r.StatusCode -lt 500) { return $true }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Hash-File([string]$path) {
    # 不依赖 Get-FileHash (Git Bash 继承的 PSModulePath 可能找不到该 cmdlet)。
    # 统一换行后再哈希：dev 与 prod 的 CRLF/LF 差异不应被当成代码变更，
    # 否则每次发布都会误判守护进程需要重启、白白杀掉会话。
    if (-not (Test-Path $path)) { return '' }
    try {
        $bytes = [System.IO.File]::ReadAllBytes($path)
        $text = [System.Text.Encoding]::UTF8.GetString($bytes)
        $normalized = $text.Replace("`r`n", "`n").Replace("`r", "`n")
        $data = [System.Text.Encoding]::UTF8.GetBytes($normalized)
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try { return [BitConverter]::ToString($sha.ComputeHash($data)) }
        finally { $sha.Dispose() }
    } catch { return '' }
}

function Test-Http([string]$url) {
    try {
        $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2
        return ($r.StatusCode -lt 500)
    } catch { return $false }
}

# ---------------- 0. precheck ----------------
Say ("source=" + $src)
Say ("target=" + $dst)
if (-not (Test-Path $dst)) { Say 'target not found'; exit 1 }
if (-not (Test-Path $py)) { Say 'venv python not found in dev'; exit 1 }

$dirty = & git status --porcelain 2>$null
if ($dirty) { Say 'WARNING: git working tree is dirty' }

Say 'py_compile...'
$pyFiles = @('app.py', 'sessiond.py', 'watchdog.py') + (Get-ChildItem (Join-Path $src 'modules\*.py') | ForEach-Object { $_.FullName })
& $py -m py_compile @pyFiles
if ($LASTEXITCODE -ne 0) { Say 'py_compile failed'; exit 1 }

if (Get-Command node -ErrorAction SilentlyContinue) {
    Say 'node --check...'
    foreach ($js in Get-ChildItem (Join-Path $src 'frontend\assets\js\*.js')) {
        & node --check $js.FullName
        if ($LASTEXITCODE -ne 0) { Say ("node --check failed: " + $js.Name); exit 1 }
    }
} else {
    Say 'node not found, skip js syntax check'
}

Say 'structure check...'
& $py (Join-Path $src 'tools\check_structure.py')
if ($LASTEXITCODE -ne 0) { Say 'structure check failed'; exit 1 }

Say 'frontend check...'
& $py (Join-Path $src 'tools\check_frontend.py')
if ($LASTEXITCODE -ne 0) { Say 'frontend check failed'; exit 1 }

if ($DryRun) { Say 'DryRun OK'; exit 0 }

# 守护进程代码有改动 = 需要重启 sessiond, 会杀死所有终端/SSH 会话。
# 若部署者本身就跑在平台会话里, 拒绝执行, 避免"上线把自己做掉"。
$daemonDiff = $false
foreach ($f in $daemonFiles) {
    if ((Hash-File (Join-Path $src $f)) -ne (Hash-File (Join-Path $dst $f))) { $daemonDiff = $true }
}
if ($daemonDiff -and (Test-InsideSessiond) -and -not $ForceDaemonRestart -and -not $SkipDaemonRestart) {
    Say 'REFUSE: sessiond/termcore 有改动, 重启守护进程会杀死当前承载本进程的终端会话。'
    Say '请从平台会话之外部署, 或确认后加 -ForceDaemonRestart (或 -SkipDaemonRestart 保留会话)。'
    exit 2
}

# ---------------- 1. backup ----------------
Say ("backup -> " + $backup)
New-Item -ItemType Directory -Force -Path $backup | Out-Null
$cfgPath = Join-Path $dst 'config.json'
if (Test-Path $cfgPath) { Copy-Item $cfgPath (Join-Path $backup 'config.json') }
$stateDir = Join-Path $env:APPDATA 'web-platform'
if (Test-Path $stateDir) {
    $stateBackup = Join-Path $backup 'state'
    New-Item -ItemType Directory -Force -Path $stateBackup | Out-Null
    Get-ChildItem $stateDir -File | Copy-Item -Destination $stateBackup
    Say 'state files backed up'
}
robocopy $dst (Join-Path $backup 'prod') /E /XD .venv .git logs __pycache__ .run .deploy-backup `
    /XF *.log /R:2 /W:2 /NFL /NDL /NJH /NP | Out-Null
Say ("prod code backup exit=" + $LASTEXITCODE)

# 备份保留最近 3 份：历史备份曾把旧备份一起复制，导致目录指数级膨胀
Get-ChildItem (Join-Path $src '.deploy-backup') -Directory -ErrorAction SilentlyContinue |
    Sort-Object Name | Select-Object -SkipLast 3 |
    ForEach-Object { Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }

# ---------------- 2. hash before sync ----------------
$before = @{}
foreach ($f in $daemonFiles) { $before[$f] = Hash-File (Join-Path $dst $f) }

# ---------------- 3. sync ----------------
Say 'robocopy sync...'
# /XF *.log：运行中的守护进程/节点写着自己的日志，robocopy 遇到被占用的文件会按
# /R 次数重试，默认 100 万次 × 30 秒 = 永远卡住（2026-11-12 实际卡了 12 分钟）。
# 日志本来也不该从 dev 覆盖到 prod。重试限制在 2 次 × 2 秒，宁可报错也别静默卡死。
robocopy $src $dst /E `
    /XF config.json .rc.cookies .rc.cookies.dev *.log `
    /XD logs __pycache__ .venv .git .run .deploy-backup /R:2 /W:2 /NFL /NDL /NJH /NP | Out-Null
$rc = $LASTEXITCODE
Say ("sync exit=" + $rc)
if ($rc -ge 8) { Say 'sync failed'; exit 1 }

# 发布戳：meta.BUILD 优先读取它, 保证静态资源 ?v= 每次发布都变化
Set-Content -Path (Join-Path $dst '.deploy-version') -Value $stamp -Encoding ASCII

$localChanged = $false
$sshChanged = $false
foreach ($f in $localFiles) {
    if ($before[$f] -ne (Hash-File (Join-Path $dst $f))) { $localChanged = $true }
}
foreach ($f in $sshFiles) {
    if ($before[$f] -ne (Hash-File (Join-Path $dst $f))) { $sshChanged = $true }
}
# 守护进程没在跑也必须拉起来, 否则新 web 的代理会 503
$localChanged = $localChanged -or (Test-DaemonStale $localPort $localFiles)
$sshChanged = $sshChanged -or (Test-DaemonStale $sshPort $sshFiles)
Say ("daemon restart needed: local=" + $localChanged + " ssh=" + $sshChanged)

# ---------------- 4. restart daemons if needed ----------------
if (($localChanged -or $sshChanged) -and $SkipDaemonRestart) {
    Say 'daemon restart SKIPPED (-SkipDaemonRestart); existing sessions preserved'
}
else {
    if ($localChanged) {
        Stop-Port $localPort
        Start-Vbs 'start-locald.vbs'
        if (-not (Wait-Http ("http://127.0.0.1:{0}/health" -f $localPort) 20)) { Say 'locald failed to start' }
    }
    if ($sshChanged) {
        Stop-Port $sshPort
        Start-Vbs 'start-sshd.vbs'
        if (-not (Wait-Http ("http://127.0.0.1:{0}/health" -f $sshPort) 20)) { Say 'sshd failed to start' }
    }
}

# ---------------- 5. restart web ----------------
if (-not $SkipWebRestart) {
    Stop-Port $webPort
    Start-Vbs 'start-hidden.vbs'
    if (-not (Wait-Http ("http://127.0.0.1:{0}/login" -f $webPort) 25)) { Say 'web failed to start' }
}

# ---------------- 6. smoke ----------------
$smokeOk = $true
if (-not $SkipSmoke) {
    Say 'smoke...'
    try {
        $cfg = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $base = "http://127.0.0.1:$webPort"
        $login = Invoke-WebRequest -Uri ($base + '/login') -Method Post `
            -Body @{ password = $cfg.password } -SessionVariable sess -UseBasicParsing
        $version = Invoke-RestMethod -Uri ($base + '/api/version') -WebSession $sess
        $summary = Invoke-RestMethod -Uri ($base + '/api/control/summary') -WebSession $sess
        $inbox = Invoke-WebRequest -Uri ($base + '/inbox') -WebSession $sess -UseBasicParsing
        $term = Invoke-RestMethod -Uri ($base + '/api/terminal/sessions') -WebSession $sess
        Say ("smoke build=" + $version.build + " inbox=" + $inbox.StatusCode +
             " services=" + $summary.services.Count)
        if (-not $version.build) { $smokeOk = $false }
        if ($inbox.StatusCode -ne 200) { $smokeOk = $false }
        if ($null -eq $term.sessions) { $smokeOk = $false }
        foreach ($svc in $summary.services) {
            if ($svc.kind -ne 'web' -and $svc.configured -and -not $svc.ok) {
                Say ("service down: " + $svc.kind)
                $smokeOk = $false
            }
        }
    } catch {
        Say ("smoke exception: " + $_.Exception.Message)
        $smokeOk = $false
    }
}

if (-not $smokeOk) {
    Say 'SMOKE FAILED -> rollback'
    $prodBackup = Join-Path $backup 'prod'
    if (Test-Path $prodBackup) {
        robocopy $prodBackup $dst /E /XD .venv .git logs __pycache__ .run .deploy-backup `
            /XF *.log /R:2 /W:2 /NFL /NDL /NJH /NP | Out-Null
    }
    if (Test-Path (Join-Path $backup 'config.json')) {
        Copy-Item (Join-Path $backup 'config.json') $cfgPath -Force
    }
    Stop-Port $webPort
    Start-Vbs 'start-hidden.vbs'
    if (-not (Test-Http ("http://127.0.0.1:{0}/health" -f $localPort))) { Stop-Port $localPort; Start-Vbs 'start-locald.vbs' }
    if (-not (Test-Http ("http://127.0.0.1:{0}/health" -f $sshPort))) { Stop-Port $sshPort; Start-Vbs 'start-sshd.vbs' }
    Say 'rollback done'
    exit 1
}

# ensure exactly one watchdog is running (it self-locks)
Start-Vbs 'start-watchdog.vbs'

Say ("DONE build=" + $stamp)
exit 0