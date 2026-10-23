# 她的日记（只读）—— 按天分组，一天一篇或几篇，标清日期与时间
# 本脚本只读取文件，绝不修改任何内容
$ErrorActionPreference = 'SilentlyContinue'
$Host.UI.RawUI.WindowTitle = '她的日记 · 只读'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$personaFile = Join-Path $root 'data\persona.json'

Write-Host ''
Write-Host '  ╔══════════════════════════════════════════════╗' -ForegroundColor Magenta
Write-Host '  ║           她 的 日 记   （只读）             ║' -ForegroundColor Magenta
Write-Host '  ╚══════════════════════════════════════════════╝' -ForegroundColor Magenta
Write-Host '  只能看不能改。人格/六维请开「看她以前.bat」' -ForegroundColor DarkGray
Write-Host ''

if (-not (Test-Path $personaFile)) {
    Write-Host '  还没有 persona.json（她还没开始写）。' -ForegroundColor Yellow
    Read-Host '  按回车关闭'
    exit
}

$p = Get-Content $personaFile -Raw -Encoding UTF8 | ConvertFrom-Json

# ── 顶部状态：今晚说说 ──
$planTs = $null
if ($p.diary_plan -and $p.diary_plan.ts) {
    $planTs = [DateTimeOffset]::FromUnixTimeSeconds([int64]$p.diary_plan.ts).ToLocalTime()
}
$written = $p.last_diary
$todayStr = (Get-Date).ToString('yyyy-MM-dd')
$todayTid = ''
foreach ($x in @($p.diary)) { if ("$($x.date)" -eq $todayStr) { $todayTid = $x.tid } }

if ($written -eq $todayStr -and $todayTid) {
    Write-Host '  今晚的说说：已发布到 QQ 空间 ✅' -ForegroundColor Green
} elseif ($written -eq $todayStr) {
    Write-Host '  今晚的日记：已写（离线，未发空间）' -ForegroundColor Cyan
} elseif ($planTs) {
    Write-Host ('  今晚的说说：计划 ' + $planTs.ToString('HH:mm')) -ForegroundColor Yellow
} else {
    Write-Host '  今晚的说说：还没排时间' -ForegroundColor DarkYellow
}
Write-Host ''

$diary = @()
if ($p.diary) { $diary = @($p.diary) }
$notes = @()
if ($p.notes) { $notes = @($p.notes) }

if ($diary.Count -eq 0) {
    Write-Host '  还没有日记。她每晚 21:30–22:00 写一篇。' -ForegroundColor DarkGray
    Read-Host '  按回车关闭'
    exit
}

$groups = $diary | Group-Object date | Sort-Object Name

Write-Host ('  共 ' + $diary.Count + ' 篇，' + $groups.Count + ' 天　（最早 ' + $groups[0].Name + '，最近 ' + $groups[-1].Name + '）') -ForegroundColor Cyan

foreach ($g in $groups) {
    $day = $g.Name
    $items = @($g.Group | Sort-Object time)
    Write-Host ''
    Write-Host ('  ┏━ ' + $day + '　共 ' + $items.Count + ' 篇 ') -ForegroundColor Yellow

    foreach ($d in $items) {
        if ($d.time) { Write-Host ('  ┃  [' + $d.time + ']') -ForegroundColor DarkGray }
        $text = [string]$d.text
        Write-Host ('  ┃  ' + ($text -replace "`n", "`n  ┃  "))
        if ($d.tid) {
            Write-Host '  ┃  （已发 QQ 空间）' -ForegroundColor DarkGray
        } else {
            Write-Host '  ┃  （没有发空间）' -ForegroundColor DarkGray
        }
        Write-Host ''
    }

    $dayNotes = @($notes | Where-Object { "$($_.date)" -eq $day })
    if ($dayNotes.Count -gt 0) {
        Write-Host '  ┣━ 她写给自己的话' -ForegroundColor DarkCyan
        foreach ($n in $dayNotes) {
            Write-Host ('  ┃  ' + (([string]$n.text) -replace "`n", "`n  ┃  ")) -ForegroundColor Gray
            Write-Host ''
        }
    }
    Write-Host ('  ┗' + ('━' * 50)) -ForegroundColor Yellow
}

Write-Host ''
Write-Host '  只读模式：这里不会改动任何文件。' -ForegroundColor DarkGray
Write-Host ''
Read-Host '  按回车关闭'
