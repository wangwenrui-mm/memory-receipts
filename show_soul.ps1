# 她的人格状态（只读）—— 六维、关系温度、自我描述历史、信件清单
# 日记正文请开「看她日记.bat」，这里不再重复显示日记内容
$ErrorActionPreference = 'SilentlyContinue'
$Host.UI.RawUI.WindowTitle = '她的人格 · 只读'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$personaFile = Join-Path $root 'data\persona.json'

Write-Host ''
Write-Host '  ╔══════════════════════════════════════════════╗' -ForegroundColor Cyan
Write-Host '  ║        她 的 人 格   （只读）                ║' -ForegroundColor Cyan
Write-Host '  ╚══════════════════════════════════════════════╝' -ForegroundColor Cyan
Write-Host '  日记正文在「看她日记.bat」里，这里只看人格' -ForegroundColor DarkGray
Write-Host ''

if (-not (Test-Path $personaFile)) {
    Write-Host '  还没有 persona.json。' -ForegroundColor Yellow
    Read-Host '  按回车关闭'
    exit
}

$p = Get-Content $personaFile -Raw -Encoding UTF8 | ConvertFrom-Json

Write-Host '  【六维性格】初始 50，越极端越会体现在语气里' -ForegroundColor Green
Write-Host ''
if ($p.traits) {
    foreach ($k in @('黏人', '毒舌', '话量', '独立', '敏感', '主动')) {
        $v = $p.traits.$k
        if ($null -eq $v) { $v = 50 }
        $n = [int][Math]::Round($v / 5)
        $bar = ('█' * $n).PadRight(20, '·')
        $arrow = ''
        if ($v -ge 65) { $arrow = ' ↑' } elseif ($v -le 35) { $arrow = ' ↓' }
        Write-Host ('    ' + $k + '  ' + $bar + ' ' + [int]$v + $arrow)
    }
} else {
    Write-Host '    （还没生成）'
}

$bond = 55
if ($null -ne $p.bond) { $bond = [int]$p.bond }
$word = '还算亲近'
if ($bond -lt 30) { $word = '有点疏远' } elseif ($bond -lt 50) { $word = '还在别扭' }
elseif ($bond -lt 70) { $word = '还算亲近' } elseif ($bond -lt 85) { $word = '挺亲的' }
else { $word = '特别亲' }
Write-Host ''
Write-Host ('  【关系温度】' + $bond + ' / 100　' + $word) -ForegroundColor Green
if ($p.mood) { Write-Host ('  【此刻心情】' + $p.mood + '　精力 ' + $p.energy + '/100') -ForegroundColor Green }
if ($p.prefs) { Write-Host ('  【她的口味】' + ($p.prefs -join '、')) -ForegroundColor DarkGray }
if ($p.playlist) { Write-Host ('  【在听】' + $p.playlist) -ForegroundColor DarkGray }

Write-Host ''
Write-Host '  【她现在的自我描述】' -ForegroundColor Green
if ($p.soul) {
    Write-Host ('    ' + (([string]$p.soul) -replace "`n", "`n    "))
} else {
    Write-Host '    （还没生成，第一次大约在第 7 天）'
}

$log = @()
if ($p.soul_log) { $log = @($p.soul_log) }
Write-Host ''
if ($log.Count -gt 0) {
    Write-Host ('  【自我描述历史】共 ' + $log.Count + ' 版（越往下越早）') -ForegroundColor Green
    $i = 0
    foreach ($item in $log) {
        $i++
        $t = [DateTimeOffset]::FromUnixTimeSeconds([int64]$item.ts).ToLocalTime()
        Write-Host ''
        Write-Host ('    ── 第 ' + $i + ' 版　' + $t.ToString('yyyy-MM-dd HH:mm')) -ForegroundColor DarkGray
        Write-Host ('    ' + (([string]$item.text) -replace "`n", "`n    "))
    }
} else {
    Write-Host '  【自我描述历史】（还没有）'
}

Write-Host ''
Write-Host '  【信件档案】' -ForegroundColor Green
$dadDir = Join-Path $root 'data\dad_letters'
$herDir = Join-Path $root 'data\letters_to_dad'
if (Test-Path $dadDir) {
    Get-ChildItem $dadDir -Filter *.md | Sort-Object Name | ForEach-Object {
        Write-Host ('    他写的：' + $_.BaseName)
    }
}
if (Test-Path $herDir) {
    Get-ChildItem $herDir -Filter *.md | Sort-Object Name | ForEach-Object {
        Write-Host ('    她写的：' + $_.BaseName) -ForegroundColor DarkGray
    }
}

Write-Host ''
$dn = 0
if ($p.diary) { $dn = @($p.diary).Count }
$nn = 0
if ($p.notes) { $nn = @($p.notes).Count }
Write-Host ('  【统计】日记 ' + $dn + ' 篇　私人纸条 ' + $nn + ' 条　（内容见「看她日记.bat」）') -ForegroundColor DarkGray

Write-Host ''
Write-Host '  只读模式：这里不会改动任何文件。' -ForegroundColor DarkGray
Write-Host ''
Read-Host '  按回车关闭'
