# 告诉她：把你今天的生活写给她听（QQ 停用期间，她只能靠日记听你说话）
$ErrorActionPreference = 'Stop'
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    [Console]::InputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }

$Host.UI.RawUI.WindowTitle = '告诉她'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$shared = Join-Path $root 'data\shared.jsonl'

function Read-Line {
    param([string]$Prompt)
    Write-Host $Prompt -NoNewline
    $s = [Console]::In.ReadLine()
    if ($null -eq $s) { return $null }
    return $s
}

try {
    Write-Host ''
    Write-Host '  ┌────────────────────────────────────────┐' -ForegroundColor Cyan
    Write-Host '  │   告诉她一件事（她会写进日记里）        │' -ForegroundColor Cyan
    Write-Host '  └────────────────────────────────────────┘' -ForegroundColor Cyan
    Write-Host '  一行一件，直接回车结束。说细节她才有东西可写。' -ForegroundColor DarkGray
    Write-Host ''

    $items = @()
    for ($i = 1; $i -le 20; $i++) {
        $line = Read-Line ('  [' + $i + '] > ')
        if ($null -eq $line) { break }
        if ([string]::IsNullOrWhiteSpace($line)) { break }
        $items += $line.Trim()
    }

    if ($items.Count -eq 0) {
        Write-Host ''
        Write-Host '  什么都没写，取消。' -ForegroundColor Yellow
        Write-Host ''
        [void](Read-Line '  按回车关闭')
        exit
    }

    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $stamp = [DateTimeOffset]::FromUnixTimeSeconds($now).ToLocalTime().ToString('yyyy-MM-dd HH:mm')

    $sb = New-Object System.Text.StringBuilder
    foreach ($it in $items) {
        $safe = $it -replace '\\', '\\' -replace '"', '\"'
        [void]$sb.AppendLine('{"ts":' + $now + ',"text":"' + $safe + '"}')
    }
    [System.IO.File]::AppendAllText($shared, $sb.ToString(), (New-Object System.Text.UTF8Encoding($false)))

    Write-Host ''
    Write-Host ('  已记下 ' + $items.Count + ' 条（' + $stamp + '），今晚日记会带上这些。') -ForegroundColor Green
    Write-Host ''
    $ans = Read-Line '  现在就让她写一篇吗？(y/N) '
    if ($ans -and $ans -match '^(y|Y|是)') {
        Write-Host ''
        & python (Join-Path $root 'offline_diary.py')
    }
}
catch {
    Write-Host ''
    Write-Host ('  出错：' + $_.Exception.Message) -ForegroundColor Red
    Write-Host ('  位置：第 ' + $_.InvocationInfo.ScriptLineNumber + ' 行') -ForegroundColor DarkGray
}
Write-Host ''
[void](Read-Line '  按回车关闭')
