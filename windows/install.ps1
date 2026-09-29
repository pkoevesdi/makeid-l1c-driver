# Start l1c-ippd at logon and add the printer "L1-C" (Microsoft IPP Class Driver).
# Usage: powershell -ExecutionPolicy Bypass -File windows\install.ps1 [-Addr 58:8C:81:xx:xx:xx]
# Adding the printer needs an elevated PowerShell; without it the script says what to do.
param([string]$Addr = $env:L1C_ADDR)
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Repo ".venv"
$Url = "http://localhost:8631/ipp/print"

if (-not (Test-Path "$Venv\Scripts\pythonw.exe")) {
    Write-Host "Creating $Venv"
    & py -3 -m venv $Venv
    if ($LASTEXITCODE) { throw "py -3 -m venv failed; install Python 3.9 or newer from python.org" }
}
& "$Venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check bleak pillow
if ($LASTEXITCODE) { throw "pip install failed" }

$IppdArgs = "`"$Repo\windows\l1c-ippd.pyw`""
if ($Addr) { $IppdArgs += " --addr $Addr" }
$Startup = [Environment]::GetFolderPath("Startup")
$Link = (New-Object -ComObject WScript.Shell).CreateShortcut("$Startup\l1c-ippd.lnk")
$Link.TargetPath = "$Venv\Scripts\pythonw.exe"
$Link.Arguments = $IppdArgs
$Link.WorkingDirectory = $Repo
$Link.Description = "IPP Everywhere server for the MakeID L1-C label printer"
$Link.Save()
Write-Host "Autostart: $Startup\l1c-ippd.lnk"

Start-Process -FilePath "$Venv\Scripts\pythonw.exe" -ArgumentList $IppdArgs -WorkingDirectory $Repo
$up = $false
foreach ($i in 1..20) {
    try { Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8631/" -TimeoutSec 1 | Out-Null; $up = $true; break }
    catch { Start-Sleep -Milliseconds 500 }
}
if (-not $up) { throw "l1c-ippd does not answer; see $env:LOCALAPPDATA\l1c-ippd\l1c-ippd.log" }
Write-Host "l1c-ippd runs on $Url"

$Printer = Get-Printer | Where-Object { $_.Name -like "*L1-C*" } | Select-Object -First 1
if ($Printer) {
    Write-Host "Printer $($Printer.Name) already installed."
} else {
    try {
        Add-Printer -Name "L1-C" -IppURL $Url -Comment "MakeID L1-C"
        $Printer = Get-Printer -Name "L1-C"
        Write-Host "Printer L1-C installed."
    } catch {
        Write-Host "Could not add the printer ($($_.Exception.Message))."
        Write-Host "Run this script in an elevated PowerShell, or add it by hand and run the script again:"
        Write-Host "  Settings > Bluetooth & devices > Printers & scanners > Add device > Add manually >"
        Write-Host "  Add a printer using an IP address or hostname > Device type: IPP Device > $Url"
        exit
    }
}

# Default to Grayscale: with Monochrome Windows makes the page black and white itself and drops
# thin lines; with Grayscale l1c-ippd does it.
$Config = Get-PrintConfiguration -PrinterName $Printer.Name
$Ticket = $Config.PrintTicketXML -replace '(name="psk:PageOutputColor">\s*<psf:Option name=")psk:Monochrome', '${1}psk:Grayscale'
if ($Ticket -ne $Config.PrintTicketXML) {
    Set-PrintConfiguration -PrinterName $Printer.Name -PrintTicketXml $Ticket
    Write-Host "Default colour mode: Grayscale."
}
