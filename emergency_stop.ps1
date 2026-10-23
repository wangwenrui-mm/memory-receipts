# 紧急停止：只关进程，不删任何数据（配置/记忆/日记/模型全部保留）
$ErrorActionPreference = 'SilentlyContinue'
$Host.UI.RawUI.WindowTitle = '紧急停止 QQ-DeepSeek'
$self = $PID
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Write-Head($t) {
    Write-Host ''
    Write-Host ('=== ' + $t + ' ===') -ForegroundColor Cyan
}

function Get-OurProcs {
    Get-CimInstance Win32_Process | Where-Object {
        $_.ProcessId -ne $self -and
        $_.Name -in @('node.exe', 'python.exe', 'wscript.exe', 'cmd.exe') -and
        $_.CommandLine -and (
            $_.CommandLine -like '*qq-deepseek*' -or
            $_.CommandLine -like '*run.py*' -or
            $_.CommandLine -like '*napcat*' -or
            $_.CommandLine -like '*bridge.py*'
        )
    }
}

Write-Host '==============================================' -ForegroundColor Yellow
Write-Host '   紧急停止：QQ <-> DeepSeek' -ForegroundColor Yellow
Write-Host '   只关进程，不删任何数据' -ForegroundColor Yellow
Write-Host '==============================================' -ForegroundColor Yellow

# 1. 先停守护进程（否则它会把子进程重新拉起来）
Write-Head '1/4 停止守护进程 run.py'
$supervisors = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'python.exe' -and $_.CommandLine -and $_.CommandLine -like '*run.py*' -and $_.ProcessId -ne $self
}
if ($supervisors) {
    foreach ($p in $supervisors) {
        Write-Host ('  结束 python run.py (PID ' + $p.ProcessId + ')')
        Stop-Process -Id $p.ProcessId -Force
    }
    Start-Sleep -Milliseconds 800
} else {
    Write-Host '  没有在跑的守护进程'
}

# 2. 停桥接 + NapCat + 启动器
Write-Head '2/4 停止桥接、QQ(NapCat) 和启动器'
for ($i = 1; $i -le 3; $i++) {
    $procs = Get-OurProcs
    if (-not $procs) { break }
    foreach ($p in $procs) {
        Write-Host ('  结束 ' + $p.Name + ' (PID ' + $p.ProcessId + ')')
        Stop-Process -Id $p.ProcessId -Force
    }
    Start-Sleep -Milliseconds 900
}

# 3. 关掉 adb 服务（手机抓屏通道）
Write-Head '3/4 关闭 adb 服务'
$adb = Join-Path $root 'tools\platform-tools\adb.exe'
if (Test-Path $adb) {
    & $adb kill-server 2>$null
    Write-Host '  adb 服务已关闭'
} else {
    Write-Host '  未找到 adb，跳过'
}

# 4. 检查
Write-Head '4/4 检查端口'
$ports = @(3001, 6099, 3011, 3012)
$open = @()
foreach ($port in $ports) {
    $c = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    if ($c) { $open += $port }
}
if ($open.Count -eq 0) {
    Write-Host '  所有端口已关闭，QQ 已离线 ✅' -ForegroundColor Green
} else {
    Write-Host ('  ⚠ 这些端口还开着: ' + ($open -join ', ')) -ForegroundColor Yellow
    Write-Host '  可能是别的程序占用了，可以不管'
}

$left = Get-OurProcs
if ($left) {
    Write-Host ''
    Write-Host '  ⚠ 仍有残留进程:' -ForegroundColor Yellow
    foreach ($p in $left) { Write-Host ('    ' + $p.Name + ' PID ' + $p.ProcessId) }
} else {
    Write-Host '  没有残留进程 ✅' -ForegroundColor Green
}

# 数据完整性
Write-Head '数据检查（一个都没删）'
$files = @('config.json', 'data\memory.json', 'data\history.json', 'data\archive.jsonl',
           'data\persona.json', 'data\proactive.json', 'data\usage.jsonl')
foreach ($f in $files) {
    $p = Join-Path $root $f
    if (Test-Path $p) {
        $it = Get-Item $p
        Write-Host ('  ' + $f.PadRight(22) + ' ' + [math]::Round($it.Length / 1KB, 1) + ' KB')
    } else {
        Write-Host ('  ' + $f.PadRight(22) + ' （暂无）')
    }
}

Write-Host ''
Write-Host '已经全部停掉了。' -ForegroundColor Green
Write-Host '数据、记忆、日记、配置、模型都还在，什么都没删。'
Write-Host '开机自启也保持原样，没有动。'
Write-Host ''
Write-Host '想重新开起来：双击桌面上的「启动QQ.bat」'
Write-Host ''

Write-Host ''
Read-Host '按回车关闭这个窗口'
