$ErrorActionPreference = 'SilentlyContinue'
$appName = 'GetTorrent'
$progId = 'GetTorrent.File'
$installDir = Join-Path $env:LOCALAPPDATA 'Programs\GetTorrent'

# Legacy names from releases before the GetTorrent rename.
Remove-Item 'HKCU:\Software\Classes\TorrentDownloader.File' -Recurse -Force
Remove-Item 'HKCU:\Software\Classes\TorrentDownloader.Magnet' -Recurse -Force
Remove-Item 'HKCU:\Software\Classes\Applications\TorrentDownloader.exe' -Recurse -Force
Remove-Item 'HKCU:\Software\Microsoft\Windows\CurrentVersion\App Paths\TorrentDownloader.exe' -Recurse -Force
Remove-Item 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\TorrentDownloader' -Recurse -Force
Remove-ItemProperty 'HKCU:\Software\RegisteredApplications' -Name 'Torrent Downloader' -Force
Remove-Item (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Torrent Downloader.lnk') -Force


# ProgID, protocol, and Applications registrations
Remove-Item "HKCU:\Software\Classes\$progId" -Recurse -Force
Remove-Item 'HKCU:\Software\Classes\GetTorrent.Magnet' -Recurse -Force
Remove-Item 'HKCU:\Software\Classes\magnet' -Recurse -Force
Remove-Item 'HKCU:\Software\Classes\Applications\TorrentDownloader.exe' -Recurse -Force
Remove-Item 'HKCU:\Software\Classes\Applications\TorrentDownloader.py' -Recurse -Force

# Capabilities / RegisteredApplications / App Paths / Uninstall
Remove-Item 'HKCU:\Software\GetTorrent' -Recurse -Force
Remove-Item 'HKCU:\Software\Microsoft\Windows\CurrentVersion\App Paths\GetTorrent.exe' -Recurse -Force
Remove-Item 'HKCU:\Software\Microsoft\Windows\CurrentVersion\App Paths\TorrentDownloader.py' -Recurse -Force
Remove-Item 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\GetTorrent' -Recurse -Force
Remove-ItemProperty 'HKCU:\Software\RegisteredApplications' -Name $appName -Force

# Per-extension OpenWithProgids + any direct class default we set
$extensions = @('.torrent')
foreach ($ext in $extensions) {
    Remove-ItemProperty ("HKCU:\Software\Classes\$ext\OpenWithProgids") -Name $progId -Force
    $extKey = "HKCU:\Software\Classes\$ext"
    $key = Get-Item $extKey -ErrorAction SilentlyContinue
    if ($key -and ($key.GetValue('') -eq $progId)) {
        Remove-ItemProperty $extKey -Name '(default)' -Force
    }
}

# Start Menu shortcut
Remove-Item (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\GetTorrent.lnk') -Force

# Remove the application folder after this script exits (can't delete a running script's dir while open).
$cleanup = 'Start-Sleep -Milliseconds 700; Remove-Item -LiteralPath "{0}" -Recurse -Force' -f $installDir.Replace('"','`"')
Start-Process powershell.exe -WindowStyle Hidden -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-Command',$cleanup
