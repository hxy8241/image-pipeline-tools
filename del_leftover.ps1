param(
    [int]$Start = -1,
    [string]$List = 'c:\Users\hxy82\Documents\trae_projects\pythons\leftover_sources_v3.txt'
)

# prevent system sleep while running, restore at exit
$code = @'
using System;
using System.Runtime.InteropServices;
public class SleepUtil {
    [DllImport("kernel32.dll")]
    public static extern uint SetThreadExecutionState(uint esFlags);
}
'@
Add-Type -TypeDefinition $code
[SleepUtil]::SetThreadExecutionState([uint32]2147483649) | Out-Null

# state/log file names follow the list file name, so different lists resume independently
$stem  = [System.IO.Path]::GetFileNameWithoutExtension($List)
$log   = "C:\Users\hxy82\AppData\Local\Temp\$stem`_del.log"
$state = "C:\Users\hxy82\AppData\Local\Temp\$stem`_state.txt"

# auto-resume: read last position from state file unless -Start given
if ($Start -lt 0) {
    if (Test-Path $state) { $Start = [int](Get-Content $state -Raw).Trim() } else { $Start = 0 }
}

$rows = @(Get-Content -LiteralPath $List -Encoding UTF8 | Where-Object { $_ -and -not $_.StartsWith('#') } | ForEach-Object { ($_ -split "`t")[0].Trim() })
$total = $rows.Count
if ($Start -ge $total) { Write-Output "Nothing to do. list=$List total=$total start=$Start"; exit 0 }

$ok = 0; $gone = 0
Add-Content -LiteralPath $log -Value "RUN list=$List start=$Start total=$total $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -Encoding UTF8

for ($i = $Start; $i -lt $total; $i++) {
    $p = $rows[$i]
    try {
        if ([System.IO.File]::Exists($p)) {
            [System.IO.File]::Delete($p)
            $ok++
        } else { $gone++ }
    } catch {
        Set-Content -LiteralPath $state -Value $i
        Add-Content -LiteralPath $log -Value "BLOCKED idx=$i ok=$ok gone=$gone :: $p :: $($_.Exception.Message)" -Encoding UTF8
        [SleepUtil]::SetThreadExecutionState([uint32]2147483648) | Out-Null
        Write-Output "BLOCKED at idx=$i/$total (this run ok=$ok gone=$gone). Just re-run the same command to resume."
        exit 1
    }
    if ((($i - $Start + 1) % 1000) -eq 0) {
        Add-Content -LiteralPath $log -Value "progress idx=$($i+1)/$total ok=$ok gone=$gone" -Encoding UTF8
        Write-Output "progress $($i+1)/$total"
    }
}

Set-Content -LiteralPath $state -Value $total
Add-Content -LiteralPath $log -Value "ALL DONE total=$total ok=$ok gone=$gone $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -Encoding UTF8
[SleepUtil]::SetThreadExecutionState([uint32]2147483648) | Out-Null
Write-Output "ALL DONE total=$total this-run ok=$ok gone=$gone"
