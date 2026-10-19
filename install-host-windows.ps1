# Installiert opentop-host unter Windows 10/11 für den aktuellen Benutzer.
#   powershell -ExecutionPolicy Bypass -File install-host-windows.ps1
#
# STATUS: Gerüst, bisher nicht auf Windows getestet. Stellen, die in einer
# Windows-VM geprüft werden müssen, sind mit TODO(VM) markiert.

$ErrorActionPreference = "Stop"
$Repo = $PSScriptRoot
$Msys = "C:\msys64"
$Python = "$Msys\ucrt64\bin\python.exe"
$Target = Join-Path $env:LOCALAPPDATA "opentop"
$VddSettings = "C:\VirtualDisplayDriver\vdd_settings.xml"

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

# 1. MSYS2 mit Python, PyGObject und GStreamer (UCRT64-Umgebung).
if (-not (Test-Path $Python)) {
    if (-not (Test-Path "$Msys\usr\bin\bash.exe")) {
        Write-Host "Installiere MSYS2 ..."
        winget install --id MSYS2.MSYS2 --exact --accept-source-agreements --accept-package-agreements
    }
    $packages = @(
        "python", "python-gobject", "python-cryptography",
        "gstreamer", "gst-plugins-base", "gst-plugins-good",
        "gst-plugins-bad", "gst-plugins-ugly", "gst-libav"
    ) | ForEach-Object { "mingw-w64-ucrt-x86_64-$_" }
    # TODO(VM): Prüfen, ob gst-plugins-bad in MSYS2 d3d11, srtp, qsv, nvcodec
    # und amfcodec enthält.
    & "$Msys\usr\bin\bash.exe" -lc "pacman -Syu --noconfirm && pacman -S --needed --noconfirm $($packages -join ' ')"
}

# 2. opentop-Dateien kopieren.
New-Item -ItemType Directory -Force -Path $Target | Out-Null
foreach ($file in "opentop-host", "opentop_host.py", "opentop_proto.py", "opentop_windows.py") {
    Copy-Item -Force (Join-Path $Repo $file) $Target
}

# 3. Virtual Display Driver.
if (-not (Test-Path $VddSettings)) {
    Write-Warning ("Virtual Display Driver nicht gefunden. Bitte von " +
        "https://github.com/VirtualDrivers/Virtual-Display-Driver/releases installieren " +
        "und dieses Skript erneut ausführen.")
    # TODO(VM): Installation automatisieren (winget-Paket oder Installer mit /S).
}

# 4. Firewall: Anmeldungen der Clients auf UDP 47800 zulassen (privates Netz).
if (Test-Admin) {
    if (-not (Get-NetFirewallRule -DisplayName "opentop" -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName "opentop" -Direction Inbound -Protocol UDP `
            -LocalPort 47800 -Profile Private -Action Allow | Out-Null
    }
} else {
    Write-Warning "Ohne Administratorrechte wurde keine Firewall-Regel angelegt (UDP 47800, eingehend)."
}

# 5. Autostart bei der Anmeldung. Der Host muss in der Benutzersitzung laufen,
#    damit er den Desktop erfassen und den Kopplungsdialog anzeigen kann.
$action = New-ScheduledTaskAction -Execute $Python -Argument "`"$Target\opentop-host`"" `
    -WorkingDirectory $Target
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit 0 -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "opentop-host" -Action $action -Trigger $trigger `
    -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName "opentop-host"

Write-Host "opentop-host ist installiert und startet ab jetzt bei der Anmeldung."
