# Remove the autostart of l1c-ippd, stop it, and remove the printer if this script is elevated.
$ErrorActionPreference = "Stop"
Remove-Item -ErrorAction SilentlyContinue "$([Environment]::GetFolderPath('Startup'))\l1c-ippd.lnk"
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object CommandLine -like "*l1c-ippd.pyw*" |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Get-Printer | Where-Object { $_.Name -like "*L1-C*" } | ForEach-Object {
    try { Remove-Printer -Name $_.Name; Write-Host "Removed printer $($_.Name)." }
    catch { Write-Host "Remove printer $($_.Name) in Settings or from an elevated PowerShell." }
}
Write-Host "l1c-ippd autostart removed."
