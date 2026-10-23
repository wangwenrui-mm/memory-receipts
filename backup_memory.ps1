# 备份记忆：把配置、记忆、日记、性格全部复制到 backup\时间戳\
$ErrorActionPreference = 'SilentlyContinue'
$Host.UI.RawUI.WindowTitle = '备份记忆'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$dest = Join-Path $root ('backup\' + $stamp)

Write-Host '==============================================' -ForegroundColor Cyan
Write-Host '   备份记忆 / 日记 / 性格' -ForegroundColor Cyan
Write-Host '==============================================' -ForegroundColor Cyan

New-Item -ItemType Directory -Force -Path $dest | Out-Null

$files = @(
    'config.json',
    'data\memory.json',
    'data\history.json',
    'data\archive.jsonl',
    'data\persona.json',
    'data\pending.json',
    'data\proactive.json',
    'data\usage.jsonl',
    'data\owner.json'
)

$copied = 0
$total = 0
foreach ($f in $files) {
    $src = Join-Path $root $f
    if (Test-Path $src) {
        $name = [System.IO.Path]::GetFileName($f)
        Copy-Item $src (Join-Path $dest $name) -Force
        $sz = (Get-Item $src).Length
        $total += $sz
        $copied++
        Write-Host ('  ✓ ' + $name.PadRight(20) + ' ' + [math]::Round($sz / 1KB, 1) + ' KB')
    }
}

Write-Host ''
Write-Host ('共复制 ' + $copied + ' 个文件，' + [math]::Round($total / 1KB, 1) + ' KB') -ForegroundColor Green
Write-Host ('保存到：' + $dest)

# 只留最近 20 份备份，免得占满盘
$all = Get-ChildItem (Join-Path $root 'backup') -Directory | Sort-Object Name -Descending
if ($all.Count -gt 20) {
    $all | Select-Object -Skip 20 | ForEach-Object {
        Remove-Item $_.FullName -Recurse -Force
        Write-Host ('  清理旧备份: ' + $_.Name)
    }
}

Write-Host ''
Write-Host '备份完成。想恢复：把文件复制回 <脚本目录>\ 对应位置即可。' -ForegroundColor Yellow
Write-Host ''
Read-Host '按回车关闭'
