param(
    [switch]$DoNotOpenDefaultApps
)

$ErrorActionPreference = 'Stop'

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

# BuildAndInstall.cmd uses PUSHd so a UNC share is mapped to a temporary drive
# letter. Use that mapped path as the project/build root. This prevents
# PyInstaller from inheriting C:\Windows as its working directory.
if (
    $env:TD_WORK_ROOT -and
    (Test-Path -LiteralPath $env:TD_WORK_ROOT)
) {
    $root = $env:TD_WORK_ROOT
}
else {
    $root = $scriptRoot
}

Set-Location -LiteralPath $root

$appName = 'GetTorrent'
$appVersion = '1.0.0'
$progId = 'GetTorrent.File'
$magnetProgId = 'GetTorrent.Magnet'
$classes = 'HKCU:\Software\Classes'
$legacyAppName = 'Torrent Downloader'
$legacyProgId = 'TorrentDownloader.File'
$legacyMagnetProgId = 'TorrentDownloader.Magnet'
$legacyInstallDir = Join-Path $env:LOCALAPPDATA 'Programs\TorrentDownloader'

Write-Host '=== GetTorrent install + launch ===' -ForegroundColor Cyan

function Invoke-BoundedProcess {
    param(
        [Parameter(Mandatory=$true)][string]$FilePath,
        [string]$Arguments = '',
        [int]$TimeoutSeconds = 10
    )

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $psi.Arguments = $Arguments
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.WorkingDirectory = (Get-Location).Path

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi

    if (-not $proc.Start()) {
        throw "Could not start process: $FilePath"
    }

    $finished = $proc.WaitForExit($TimeoutSeconds * 1000)
    if (-not $finished) {
        try { $proc.Kill() } catch {}
        try { $proc.WaitForExit() } catch {}

        return [pscustomobject]@{
            TimedOut = $true
            ExitCode = $null
            Stdout = ''
            Stderr = ''
        }
    }

    # The second no-timeout WaitForExit() is intentional: on .NET Framework
    # it ensures redirected output streams are fully drained before reading.
    $proc.WaitForExit()

    $stdout = $proc.StandardOutput.ReadToEnd()
    $stderr = $proc.StandardError.ReadToEnd()
    $exitCode = $proc.ExitCode

    return [pscustomobject]@{
        TimedOut = $false
        ExitCode = $exitCode
        Stdout = $stdout
        Stderr = $stderr
    }
}


# --- Microsoft Visual C++ runtime prerequisite -------------------------------
function Get-VisualCppX64RuntimeInfo {
    $paths = @(
        'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64'
    )

    foreach ($path in $paths) {
        try {
            $item = Get-ItemProperty -Path $path -ErrorAction Stop
            if ([int]$item.Installed -eq 1) {
                return [pscustomobject]@{
                    Installed = $true
                    Version = [string]$item.Version
                    RegistryPath = $path
                }
            }
        }
        catch {}
    }

    return [pscustomobject]@{
        Installed = $false
        Version = ''
        RegistryPath = ''
    }
}

function Install-OrRepair-VisualCppRuntime {
    param([switch]$ForceRepair)

    $current = Get-VisualCppX64RuntimeInfo

    if ($current.Installed -and -not $ForceRepair) {
        Write-Host (
            "  [OK] Microsoft Visual C++ v14 x64 runtime installed " +
            "$($current.Version)"
        ) -ForegroundColor Green
        return $true
    }

    $vcUrl = 'https://aka.ms/vc14/vc_redist.x64.exe'
    $vcInstaller = Join-Path $env:TEMP 'TorrentDownloader-vc_redist.x64.exe'
    $vcLog = Join-Path $env:TEMP 'TorrentDownloader-vcredist.log'

    Remove-Item $vcInstaller -Force -ErrorAction SilentlyContinue

    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

        if ($current.Installed) {
            Write-Host (
                "Repairing Microsoft Visual C++ v14 x64 runtime " +
                "$($current.Version)..."
            ) -ForegroundColor Yellow
        }
        else {
            Write-Host (
                'Microsoft Visual C++ v14 x64 runtime is missing. Installing it...'
            ) -ForegroundColor Yellow
        }

        Write-Host 'Downloading latest supported Microsoft VC++ x64 runtime...' -ForegroundColor Cyan
        Invoke-WebRequest `
            -Uri $vcUrl `
            -OutFile $vcInstaller `
            -UseBasicParsing

        if (-not (Test-Path -LiteralPath $vcInstaller)) {
            throw 'Microsoft VC++ runtime download did not create a file.'
        }

        Write-Host 'Verifying Microsoft digital signature...' -ForegroundColor Cyan
        $signature = Get-AuthenticodeSignature -FilePath $vcInstaller
        if ($signature.Status -ne 'Valid') {
            throw (
                "Microsoft VC++ runtime signature is not valid. Status: " +
                $signature.Status
            )
        }

        $signer = [string]$signature.SignerCertificate.Subject
        if ($signer -notmatch 'Microsoft') {
            throw "Unexpected VC++ runtime signer: $signer"
        }

        $action = if ($current.Installed) { '/repair' } else { '/install' }
        $vcArgs = (
            "$action /passive /norestart /log " +
            '"' + $vcLog.Replace('"', '\"') + '"'
        )

        Write-Host (
            'Windows may show one UAC prompt for the Microsoft runtime.'
        ) -ForegroundColor DarkGray

        $proc = Start-Process `
            -FilePath $vcInstaller `
            -ArgumentList $vcArgs `
            -Verb RunAs `
            -Wait `
            -PassThru

        $exit = $proc.ExitCode

        if ($exit -ne 0 -and $exit -ne 3010) {
            if (Test-Path -LiteralPath $vcLog) {
                Write-Host "VC++ installer log: $vcLog" -ForegroundColor DarkYellow
            }
            throw "Microsoft VC++ runtime installer exited with code $exit."
        }

        $after = Get-VisualCppX64RuntimeInfo
        if (-not $after.Installed) {
            throw (
                'Microsoft VC++ runtime installer completed, but the x64 ' +
                'runtime is still not registered as installed.'
            )
        }

        Write-Host (
            "  [OK] Microsoft Visual C++ v14 x64 runtime ready " +
            "$($after.Version)"
        ) -ForegroundColor Green

        if ($exit -eq 3010) {
            Write-Warning (
                'Microsoft reports that Windows should be restarted to finish ' +
                'updating the VC++ runtime. The installer will retest now.'
            )
        }

        return $true
    }
    finally {
        Remove-Item $vcInstaller -Force -ErrorAction SilentlyContinue
    }
}

function Show-RecentApplicationCrash {
    param(
        [string]$ProcessName,
        [datetime]$Since
    )

    try {
        Start-Sleep -Milliseconds 900

        $events = @(
            Get-WinEvent `
                -FilterHashtable @{
                    LogName = 'Application'
                    Id = 1000
                    StartTime = $Since.AddSeconds(-2)
                } `
                -ErrorAction SilentlyContinue
        )

        $event = $events |
            Where-Object {
                $_.ProviderName -eq 'Application Error' -and
                (
                    (
                        $_.Properties.Count -gt 0 -and
                        [string]$_.Properties[0].Value -ieq $ProcessName
                    ) -or
                    $_.Message -match [regex]::Escape($ProcessName)
                )
            } |
            Select-Object -First 1

        if (-not $event) {
            Write-Host (
                "  Windows Event ID 1000 for $ProcessName was not available."
            ) -ForegroundColor DarkYellow
            return
        }

        $faultingApp = ''
        $faultingModule = ''
        $exceptionCode = ''
        $faultOffset = ''
        $appPath = ''
        $modulePath = ''

        if ($event.Properties.Count -gt 0)  { $faultingApp = [string]$event.Properties[0].Value }
        if ($event.Properties.Count -gt 3)  { $faultingModule = [string]$event.Properties[3].Value }
        if ($event.Properties.Count -gt 6)  { $exceptionCode = [string]$event.Properties[6].Value }
        if ($event.Properties.Count -gt 7)  { $faultOffset = [string]$event.Properties[7].Value }
        if ($event.Properties.Count -gt 10) { $appPath = [string]$event.Properties[10].Value }
        if ($event.Properties.Count -gt 11) { $modulePath = [string]$event.Properties[11].Value }

        Write-Host ''
        Write-Host '=== WINDOWS NATIVE CRASH REPORT ===' -ForegroundColor Red
        Write-Host "Event time:       $($event.TimeCreated)"
        Write-Host "Faulting app:     $faultingApp"
        Write-Host "Faulting module:  $faultingModule" -ForegroundColor Yellow
        Write-Host "Exception code:   $exceptionCode" -ForegroundColor Yellow
        Write-Host "Fault offset:     $faultOffset"
        Write-Host "Application path: $appPath"
        Write-Host "Module path:      $modulePath"
        Write-Host '===================================' -ForegroundColor Red
        Write-Host ''
    }
    catch {
        Write-Warning (
            'Could not read Windows Application Error event: ' +
            $_.Exception.Message
        )
    }
}


# --- select/test compatible Python + libtorrent -----------------------------
# Preferred Windows build:
#   CPython 3.13.x + libtorrent 2.1.1
#
# Python 3.13.18 is source-only on Windows. Python 3.13.17 is the final
# Python 3.13 release with an official Windows installer, so that is the
# bootstrap version used when a 3.13 interpreter is not already installed.
#
# Fallback build:
#   CPython 3.12.x + libtorrent 2.0.15
#
# The preferred combination is NEVER trusted merely because imports succeed.
# A native libtorrent session is created in a separate bounded process. If that
# crashes/hangs/fails, the installer automatically falls back to 3.12/2.0.15.

function Test-PythonSeries {
    param(
        [string]$Candidate,
        [string]$Series
    )

    if (-not $Candidate -or -not (Test-Path -LiteralPath $Candidate)) {
        return $false
    }

    try {
        $version = & $Candidate -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $version) {
            return $false
        }
        return ($version.Trim() -eq $Series)
    }
    catch {
        return $false
    }
}

function Find-PythonSeries {
    param([string]$Series)

    $launcherTag = "-$Series"
    $pyLauncher = Get-Command 'py' -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        try {
            $resolved = & $pyLauncher.Source $launcherTag -c "import sys;print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $resolved) {
                $candidate = $resolved.Trim()
                if (Test-PythonSeries $candidate $Series) {
                    return $candidate
                }
            }
        }
        catch {}
    }

    $compact = $Series.Replace('.', '')
    $candidate = Join-Path $env:LOCALAPPDATA "Programs\Python\Python$compact\python.exe"
    if (Test-PythonSeries $candidate $Series) {
        return $candidate
    }

    $pythonCommand = Get-Command 'python' -ErrorAction SilentlyContinue
    if (
        $pythonCommand -and
        (Test-PythonSeries $pythonCommand.Source $Series)
    ) {
        return $pythonCommand.Source
    }

    return $null
}

function Install-PythonSeries {
    param(
        [string]$Series,
        [string]$Version,
        [string]$Url,
        [string]$TargetFolderName,
        [string]$ExpectedSha256 = ''
    )

    $installerName = "TorrentDownloader-Python-$Version-amd64.exe"
    $pythonInstaller = Join-Path $env:TEMP $installerName
    $pythonTarget = Join-Path $env:LOCALAPPDATA "Programs\Python\$TargetFolderName"

    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

        Write-Host "Downloading official Python $Version from python.org..." -ForegroundColor Cyan
        Invoke-WebRequest `
            -Uri $Url `
            -OutFile $pythonInstaller `
            -UseBasicParsing

        if (-not (Test-Path -LiteralPath $pythonInstaller)) {
            throw "Python $Version installer download did not create a file."
        }

        if ($ExpectedSha256) {
            $actualHash = (
                Get-FileHash -Algorithm SHA256 -LiteralPath $pythonInstaller
            ).Hash.ToLowerInvariant()

            if ($actualHash -ne $ExpectedSha256.ToLowerInvariant()) {
                throw (
                    "Python $Version installer SHA-256 mismatch. " +
                    "Expected $ExpectedSha256 but got $actualHash."
                )
            }
            Write-Host '  [OK] Python installer SHA-256 verified' -ForegroundColor Green
        }

        $signature = Get-AuthenticodeSignature -FilePath $pythonInstaller
        if ($signature.Status -ne 'Valid') {
            throw "Python installer signature is not valid. Status: $($signature.Status)"
        }

        $signerSubject = [string]$signature.SignerCertificate.Subject
        if (
            $signerSubject -notmatch 'Python Software Foundation' -and
            $signerSubject -notmatch 'Python Software'
        ) {
            throw "Unexpected Python installer signer: $signerSubject"
        }

        Write-Host (
            "Installing Python $Version side-by-side for GetTorrent..."
        ) -ForegroundColor Yellow

        $installArgs = @(
            '/passive',
            'InstallAllUsers=0',
            "TargetDir=$pythonTarget",
            'PrependPath=0',
            'Include_launcher=1',
            'InstallLauncherAllUsers=0',
            'Include_pip=1',
            'Include_tcltk=1',
            'Include_test=0',
            'Include_doc=0',
            'Shortcuts=0',
            'AssociateFiles=0'
        )

        $installProcess = Start-Process `
            -FilePath $pythonInstaller `
            -ArgumentList $installArgs `
            -Wait `
            -PassThru

        if ($installProcess.ExitCode -ne 0) {
            throw "Python installer exited with code $($installProcess.ExitCode)."
        }

        $installedPython = Join-Path $pythonTarget 'python.exe'
        if (-not (Test-PythonSeries $installedPython $Series)) {
            throw (
                "Python $Version installation completed, but a working " +
                "$Series interpreter was not found at $installedPython"
            )
        }

        return $installedPython
    }
    finally {
        Remove-Item $pythonInstaller -Force -ErrorAction SilentlyContinue
    }
}

function Ensure-PythonDependencies {
    param([string]$PythonExe)

    Write-Host 'Checking Python build dependencies...' -ForegroundColor Cyan

    # Check pip in a bounded child process. Do not use a normal PowerShell
    # native command for an expected failure because $ErrorActionPreference
    # is Stop and stderr from a fresh Python can otherwise abort the installer.
    $pipCheck = Invoke-BoundedProcess `
        -FilePath $PythonExe `
        -Arguments '-m pip --version' `
        -TimeoutSeconds 15

    if ($pipCheck.TimedOut -or $pipCheck.ExitCode -ne 0) {
        Write-Host '  [MISSING] pip - installing...' -ForegroundColor Yellow

        $ensurePip = Invoke-BoundedProcess `
            -FilePath $PythonExe `
            -Arguments '-m ensurepip --upgrade' `
            -TimeoutSeconds 120

        if (
            $ensurePip.TimedOut -or
            $ensurePip.ExitCode -ne 0
        ) {
            if ($ensurePip.Stdout) {
                Write-Host $ensurePip.Stdout.Trim() -ForegroundColor DarkYellow
            }
            if ($ensurePip.Stderr) {
                Write-Host $ensurePip.Stderr.Trim() -ForegroundColor DarkYellow
            }
            throw 'pip installation failed.'
        }
    }
    else {
        Write-Host '  [OK] pip already installed' -ForegroundColor Green
    }

    $dependencyChecks = @(
        @{ Package='pystray'; Label='pystray'; Code='import pystray' },
        @{ Package='pillow'; Label='Pillow'; Code='from PIL import Image' }
    )

    $missingPackages = @()

    foreach ($dep in $dependencyChecks) {
        # Write each tiny import check to a temporary .py file. This avoids all
        # cmd/PowerShell quoting ambiguity and lets a failed import be handled
        # as data instead of as a PowerShell terminating error.
        $depProbe = Join-Path $env:TEMP (
            'TorrentDownloader-dependency-' +
            $dep.Label.Replace(' ', '_') +
            '.py'
        )

        [System.IO.File]::WriteAllText(
            $depProbe,
            $dep.Code,
            [System.Text.UTF8Encoding]::new($false)
        )

        try {
            $quotedProbe = '"' + $depProbe.Replace('"', '\"') + '"'
            $depResult = Invoke-BoundedProcess `
                -FilePath $PythonExe `
                -Arguments $quotedProbe `
                -TimeoutSeconds 15
        }
        finally {
            Remove-Item $depProbe -Force -ErrorAction SilentlyContinue
        }

        if (
            -not $depResult.TimedOut -and
            $depResult.ExitCode -eq 0
        ) {
            Write-Host (
                "  [OK] {0} already installed" -f $dep.Label
            ) -ForegroundColor Green
        }
        else {
            Write-Host (
                "  [MISSING/BROKEN] {0}" -f $dep.Label
            ) -ForegroundColor Yellow
            $missingPackages += $dep.Package
        }
    }

    if ($missingPackages.Count -gt 0) {
        Write-Host (
            'Installing missing/broken packages: ' +
            ($missingPackages -join ', ')
        ) -ForegroundColor Yellow

        $packageArgs = (
            '-m pip install --upgrade ' +
            (($missingPackages | ForEach-Object {
                '"' + $_.Replace('"', '\"') + '"'
            }) -join ' ')
        )

        $installDeps = Invoke-BoundedProcess `
            -FilePath $PythonExe `
            -Arguments $packageArgs `
            -TimeoutSeconds 300

        if (
            $installDeps.TimedOut -or
            $installDeps.ExitCode -ne 0
        ) {
            if ($installDeps.Stdout) {
                Write-Host $installDeps.Stdout.Trim() -ForegroundColor DarkYellow
            }
            if ($installDeps.Stderr) {
                Write-Host $installDeps.Stderr.Trim() -ForegroundColor DarkYellow
            }
            throw (
                'Failed to install required package(s): ' +
                ($missingPackages -join ', ')
            )
        }

        # Verify every dependency after installation using the same safe probe.
        foreach ($dep in $dependencyChecks) {
            $verifyProbe = Join-Path $env:TEMP (
                'TorrentDownloader-verify-' +
                $dep.Label.Replace(' ', '_') +
                '.py'
            )

            [System.IO.File]::WriteAllText(
                $verifyProbe,
                $dep.Code,
                [System.Text.UTF8Encoding]::new($false)
            )

            try {
                $quotedVerify = '"' + $verifyProbe.Replace('"', '\"') + '"'
                $verifyResult = Invoke-BoundedProcess `
                    -FilePath $PythonExe `
                    -Arguments $quotedVerify `
                    -TimeoutSeconds 15
            }
            finally {
                Remove-Item $verifyProbe -Force -ErrorAction SilentlyContinue
            }

            if (
                $verifyResult.TimedOut -or
                $verifyResult.ExitCode -ne 0
            ) {
                if ($verifyResult.Stdout) {
                    Write-Host $verifyResult.Stdout.Trim() -ForegroundColor DarkYellow
                }
                if ($verifyResult.Stderr) {
                    Write-Host $verifyResult.Stderr.Trim() -ForegroundColor DarkYellow
                }
                throw (
                    'Dependency still cannot import after installation: ' +
                    $dep.Label
                )
            }

            Write-Host (
                "  [OK] {0} installed and verified" -f $dep.Label
            ) -ForegroundColor Green
        }
    }
    else {
        Write-Host (
            '  [OK] pystray and Pillow already work'
        ) -ForegroundColor Green
    }
}

function Get-NormalizedLibtorrentVersion {
    param([string]$PythonExe)

    $versionProbe = Join-Path $env:TEMP 'TorrentDownloader-libtorrent-version.py'
    [System.IO.File]::WriteAllText(
        $versionProbe,
        "import libtorrent as lt`nprint(getattr(lt, '__version__', ''))`n",
        [System.Text.UTF8Encoding]::new($false)
    )

    try {
        $quotedProbe = '"' + $versionProbe.Replace('"', '\"') + '"'
        $result = Invoke-BoundedProcess `
            -FilePath $PythonExe `
            -Arguments $quotedProbe `
            -TimeoutSeconds 15
    }
    finally {
        Remove-Item $versionProbe -Force -ErrorAction SilentlyContinue
    }

    if (
        $result.TimedOut -or
        $result.ExitCode -ne 0 -or
        -not $result.Stdout
    ) {
        return ''
    }

    $version = $result.Stdout.Trim()

    if ($version -match '^(\d+\.\d+\.\d+)\.0$') {
        return $Matches[1]
    }

    return $version
}

function Ensure-LibtorrentVersion {
    param(
        [string]$PythonExe,
        [string]$RequiredVersion
    )

    $currentVersion = Get-NormalizedLibtorrentVersion $PythonExe

    if ($currentVersion -eq $RequiredVersion) {
        Write-Host (
            "  [OK] libtorrent $currentVersion already installed"
        ) -ForegroundColor Green
        return
    }

    if ($currentVersion) {
        Write-Host (
            "  [CHANGE] libtorrent $currentVersion -> $RequiredVersion"
        ) -ForegroundColor Yellow
    }
    else {
        Write-Host (
            "  [MISSING] libtorrent -> $RequiredVersion"
        ) -ForegroundColor Yellow
    }

    $libtorrentArgs = (
        '-m pip install --disable-pip-version-check --no-cache-dir ' +
        '--force-reinstall "libtorrent==' +
        $RequiredVersion +
        '"'
    )

    $libtorrentInstall = Invoke-BoundedProcess `
        -FilePath $PythonExe `
        -Arguments $libtorrentArgs `
        -TimeoutSeconds 300

    if (
        $libtorrentInstall.TimedOut -or
        $libtorrentInstall.ExitCode -ne 0
    ) {
        if ($libtorrentInstall.Stdout) {
            Write-Host $libtorrentInstall.Stdout.Trim() -ForegroundColor DarkYellow
        }
        if ($libtorrentInstall.Stderr) {
            Write-Host $libtorrentInstall.Stderr.Trim() -ForegroundColor DarkYellow
        }
        throw "Failed to install libtorrent $RequiredVersion."
    }

    if ($libtorrentInstall.Stdout) {
        Write-Host $libtorrentInstall.Stdout.Trim()
    }
}

function Test-NativeLibtorrent {
    param(
        [string]$PythonExe,
        [string]$ExpectedVersion,
        [string]$Label
    )

    $probeFile = Join-Path $env:TEMP 'TorrentDownloader-libtorrent-probe.py'
    $probeCode = @'
import os
import sys
import time
import libtorrent as lt

mode = sys.argv[1] if len(sys.argv) > 1 else "runtime"

startup_settings = {
    "enable_dht": False,
    "enable_lsd": False,
    "enable_upnp": False,
    "enable_natpmp": False,
    "enable_incoming_tcp": False,
    "enable_incoming_utp": False,
    "listen_interfaces": "0.0.0.0:0",
}

settings_steps = [
    ("enable_dht", True),
    ("enable_lsd", False),
    ("enable_upnp", False),
    ("enable_natpmp", False),
    ("unchoke_slots_limit", 0),
    ("allowed_fast_set_size", 0),
    ("num_optimistic_unchoke_slots", 0),
    ("active_downloads", 5),
    ("active_limit", 500),
    ("dont_count_slow_torrents", False),
    ("auto_manage_interval", 1),
]

def mark(text):
    print(text, flush=True)

mark("IMPORT_OK " + str(getattr(lt, "__version__", "unknown")))

if mode == "import":
    os._exit(0)

mark("BEFORE_SAFE_SESSION")
s = lt.session(startup_settings)
mark("SAFE_SESSION_OK")

time.sleep(1.0)
mark("SAFE_SESSION_STABLE_1S")

if mode == "session":
    # Deliberately bypass normal Python/native destruction. This stage answers
    # only whether session creation/runtime itself is stable.
    os._exit(0)

for key, value in settings_steps:
    mark("BEFORE_SETTING " + key)
    s.apply_settings({key: value})
    mark("SETTING_RETURNED " + key)
    # apply_settings is asynchronous; allow the native session thread time to
    # process each individual setting before moving to the next one.
    time.sleep(0.35)
    mark("SETTING_STABLE " + key)

mark("ALL_SETTINGS_STABLE")
time.sleep(1.0)
mark("RUNTIME_OK")

if mode == "runtime":
    # Avoid destructor/shutdown while determining whether startup/settings are
    # safe. Shutdown is tested separately below.
    os._exit(0)

# Separate clean-shutdown test for the Python binding.
# The pip Python bindings do not expose the C++ session::abort() method.
# Deleting the Python session object invokes libtorrent's native destructor.
import gc

mark("BEFORE_SESSION_DELETE")
del s
mark("SESSION_DELETE_RETURNED")
gc.collect()
mark("GC_RETURNED")
time.sleep(0.5)
mark("SHUTDOWN_OK")
sys.stdout.flush()
sys.stderr.flush()
'@

    [System.IO.File]::WriteAllText(
        $probeFile,
        $probeCode,
        [System.Text.UTF8Encoding]::new($false)
    )

    try {
        Write-Host (
            "Testing $Label native libtorrent runtime with safe startup in stages..."
        ) -ForegroundColor Cyan

        foreach ($stage in @('import', 'session', 'runtime')) {
            $args = (
                '"' + $probeFile.Replace('"', '\"') + '" ' + $stage
            )

            $stageStarted = Get-Date
            $result = Invoke-BoundedProcess `
                -FilePath $PythonExe `
                -Arguments $args `
                -TimeoutSeconds 15

            if ($result.Stdout) {
                Write-Host $result.Stdout.Trim() -ForegroundColor DarkGray
            }
            if ($result.Stderr) {
                Write-Host $result.Stderr.Trim() -ForegroundColor DarkYellow
            }

            if ($result.TimedOut) {
                Write-Host (
                    "  [FAIL] $Label stage '$stage' timed out."
                ) -ForegroundColor Red
                return $false
            }

            if ($result.ExitCode -ne 0) {
                Write-Host (
                    "  [FAIL] $Label stage '$stage' crashed/exited with code " +
                    "$($result.ExitCode). The final checkpoint above identifies " +
                    "the native operation immediately before the failure."
                ) -ForegroundColor Red

                Show-RecentApplicationCrash `
                    -ProcessName ([System.IO.Path]::GetFileName($PythonExe)) `
                    -Since $stageStarted

                return $false
            }

            $requiredMarker = switch ($stage) {
                'import'  { 'IMPORT_OK' }
                'session' { 'SAFE_SESSION_STABLE_1S' }
                'runtime' { 'RUNTIME_OK' }
            }

            if ($result.Stdout -notmatch [regex]::Escape($requiredMarker)) {
                Write-Host (
                    "  [FAIL] $Label stage '$stage' exited without the expected " +
                    "checkpoint '$requiredMarker'."
                ) -ForegroundColor Red
                return $false
            }

            Write-Host (
                "  [OK] $Label stage '$stage' passed"
            ) -ForegroundColor Green
        }

        # Test normal libtorrent shutdown separately. A shutdown-only failure
        # must not be confused with session creation or apply_settings.
        $shutdownArgs = (
            '"' + $probeFile.Replace('"', '\"') + '" shutdown'
        )
        $shutdownStarted = Get-Date
        $shutdownResult = Invoke-BoundedProcess `
            -FilePath $PythonExe `
            -Arguments $shutdownArgs `
            -TimeoutSeconds 20

        if ($shutdownResult.Stdout) {
            Write-Host $shutdownResult.Stdout.Trim() -ForegroundColor DarkGray
        }
        if ($shutdownResult.Stderr) {
            Write-Host $shutdownResult.Stderr.Trim() -ForegroundColor DarkYellow
        }

        if ($shutdownResult.TimedOut) {
            Write-Host (
                "  [FAIL] $Label clean-shutdown stage timed out."
            ) -ForegroundColor Red
            return $false
        }

        if (
            $shutdownResult.ExitCode -ne 0 -or
            $shutdownResult.Stdout -notmatch 'SHUTDOWN_OK'
        ) {
            Write-Host (
                "  [FAIL] $Label runtime worked, but libtorrent clean shutdown " +
                "failed with exit code $($shutdownResult.ExitCode)."
            ) -ForegroundColor Red

            if ($shutdownResult.ExitCode -lt 0) {
                Show-RecentApplicationCrash `
                    -ProcessName ([System.IO.Path]::GetFileName($PythonExe)) `
                    -Since $shutdownStarted
            }

            return $false
        }

        Write-Host (
            "  [OK] $Label clean-shutdown stage passed"
        ) -ForegroundColor Green
        return $true
    }
    finally {
        Remove-Item $probeFile -Force -ErrorAction SilentlyContinue
    }
}


$pythonExe = $null
$requiredLibtorrent = $null
$selectedBuild = $null
$vcRepairAttempted = $false

Write-Host 'Checking Microsoft Visual C++ native runtime prerequisite...' -ForegroundColor Cyan
Install-OrRepair-VisualCppRuntime | Out-Null

# Preferred: Python 3.13 + libtorrent 2.1.1.
Write-Host (
    'Preferred build: Python 3.13 + libtorrent 2.1.1'
) -ForegroundColor Cyan

$preferredPython = Find-PythonSeries '3.13'

if (-not $preferredPython) {
    try {
        # 3.13.18 is source-only. 3.13.17 is the last 3.13 release with a
        # normal Windows installer.
        $preferredPython = Install-PythonSeries `
            -Series '3.13' `
            -Version '3.13.17' `
            -Url 'https://www.python.org/ftp/python/3.13.17/python-3.13.17-amd64.exe' `
            -TargetFolderName 'Python313' `
            -ExpectedSha256 '5da012101ba2e17113a9a1cd901e9ce1441086f2b7ecb5de06ec94163899a190'
    }
    catch {
        Write-Warning (
            "Could not prepare preferred Python 3.13 runtime: " +
            $_.Exception.Message
        )
        $preferredPython = $null
    }
}

if ($preferredPython) {
    try {
        Write-Host "Preferred Python: $preferredPython" -ForegroundColor DarkGray
        Ensure-PythonDependencies $preferredPython
        Ensure-LibtorrentVersion $preferredPython '2.1.1'

        $preferredPassed = Test-NativeLibtorrent `
            -PythonExe $preferredPython `
            -ExpectedVersion '2.1.1' `
            -Label 'Python 3.13 + libtorrent 2.1.1'

        if (-not $preferredPassed -and -not $vcRepairAttempted) {
            $vcRepairAttempted = $true
            Write-Host ''
            Write-Host (
                'Native libtorrent crashed. Repairing the Microsoft Visual C++ ' +
                'x64 runtime, then retrying the preferred build once.'
            ) -ForegroundColor Yellow

            Install-OrRepair-VisualCppRuntime -ForceRepair | Out-Null

            $preferredPassed = Test-NativeLibtorrent `
                -PythonExe $preferredPython `
                -ExpectedVersion '2.1.1' `
                -Label 'Python 3.13 + libtorrent 2.1.1 after VC++ repair'
        }

        if ($preferredPassed) {
            $pythonExe = $preferredPython
            $requiredLibtorrent = '2.1.1'
            $selectedBuild = 'preferred'
        }
    }
    catch {
        Write-Warning (
            "Preferred Python 3.13/libtorrent 2.1.1 preparation failed: " +
            $_.Exception.Message
        )
    }
}

# Fallback only when the preferred native combination does not actually work.
if (-not $pythonExe) {
    Write-Host (
        'Preferred combination failed. Falling back to ' +
        'Python 3.12 + libtorrent 2.0.15.'
    ) -ForegroundColor Yellow

    $fallbackPython = Find-PythonSeries '3.12'

    if (-not $fallbackPython) {
        $fallbackPython = Install-PythonSeries `
            -Series '3.12' `
            -Version '3.12.10' `
            -Url 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' `
            -TargetFolderName 'Python312'
    }

    Ensure-PythonDependencies $fallbackPython
    Ensure-LibtorrentVersion $fallbackPython '2.0.15'

    if (-not (
        Test-NativeLibtorrent `
            -PythonExe $fallbackPython `
            -ExpectedVersion '2.0.15' `
            -Label 'Python 3.12 + libtorrent 2.0.15'
    )) {
        throw (
            'Both supported libtorrent build combinations failed even after ' +
            'the Microsoft Visual C++ x64 runtime check/repair. The Windows ' +
            'native crash report above identifies the faulting DLL. ' +
            'Installation stopped before building a broken app.'
        )
    }

    $pythonExe = $fallbackPython
    $requiredLibtorrent = '2.0.15'
    $selectedBuild = 'fallback'
}

$pythonVersionText = & $pythonExe -c "import sys; print(sys.version.split()[0])"
$pythonArchText = & $pythonExe -c "import platform; print(platform.architecture()[0])"
$libtorrentVersionText = & $pythonExe -c "import libtorrent as lt; print(getattr(lt, '__version__', 'unknown'))"

Write-Host '' 
Write-Host 'Selected GetTorrent build environment:' -ForegroundColor Green
Write-Host "  Python:     $pythonVersionText ($pythonArchText)"
Write-Host "  Interpreter: $pythonExe"
Write-Host "  libtorrent: $libtorrentVersionText"
Write-Host "  Strategy:   $selectedBuild"
Write-Host ''

# --- install the Python application directly --------------------------------
# There is intentionally NO PyInstaller/EXE build here. GetTorrent is
# installed as its Python source and launched with the selected interpreter's
# pythonw.exe so the GUI opens without a console window.

$src = Join-Path $root 'gettorrent.py'
$iconSource = Join-Path $root 'GetTorrent.ico'
$uninstallSource = Join-Path $root 'Uninstall.ps1'

if (-not (Test-Path -LiteralPath $src)) {
    throw "Source not found: $src"
}
if (-not (Test-Path -LiteralPath $iconSource)) {
    throw "App icon not found: $iconSource"
}

$pythonDir = Split-Path -Parent $pythonExe
$pythonwExe = Join-Path $pythonDir 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonwExe)) {
    throw "pythonw.exe was not found beside the selected Python: $pythonwExe"
}

$installDir = Join-Path $env:LOCALAPPDATA 'Programs\GetTorrent'
$installedScript = Join-Path $installDir 'gettorrent.py'
$installedIcon = Join-Path $installDir 'GetTorrent.ico'
$installedUninstall = Join-Path $installDir 'Uninstall.ps1'
$legacyExe = Join-Path $installDir 'GetTorrent.exe'

New-Item -ItemType Directory -Force -Path $installDir | Out-Null

# Ask any currently running GetTorrent instance to exit cleanly before
# replacing its source. This works for both the old EXE build and the new
# pythonw-hosted build because the app's IPC watcher is the same.
try {
    $appDataRoot = [Environment]::GetFolderPath('ApplicationData')
    $ipcTargets = @(
        @{ Path=(Join-Path (Join-Path $appDataRoot 'GetTorrent') 'ipc'); Token='__GETTORRENT_EXIT_FOR_UPDATE__' },
        @{ Path=(Join-Path (Join-Path $appDataRoot 'TorrentDownloader') 'ipc'); Token='__TORRENTDOWNLOADER_EXIT_FOR_UPDATE__' }
    )

    foreach ($target in $ipcTargets) {
        $ipcDir = $target.Path
        New-Item -ItemType Directory -Path $ipcDir -Force | Out-Null
        $ipcFile = Join-Path $ipcDir (
            'update-exit-{0}-{1}.msg' -f $PID, [DateTime]::UtcNow.Ticks
        )
        [System.IO.File]::WriteAllText(
            $ipcFile,
            $target.Token,
            [System.Text.UTF8Encoding]::new($false)
        )
    }
    Start-Sleep -Milliseconds 900
}
catch {
    Write-Host (
        "Could not send update-exit IPC request: $($_.Exception.Message)"
    ) -ForegroundColor DarkYellow
}

# If an old frozen build is still running, give it a few more seconds to exit.
$legacyProcesses = @(
    Get-Process -Name 'TorrentDownloader' -ErrorAction SilentlyContinue
)
if ($legacyProcesses.Count -gt 0) {
    $deadline = [DateTime]::UtcNow.AddSeconds(6)
    do {
        Start-Sleep -Milliseconds 250
        $legacyProcesses = @(
            Get-Process -Name 'TorrentDownloader' -ErrorAction SilentlyContinue
        )
    } while (
        $legacyProcesses.Count -gt 0 -and
        [DateTime]::UtcNow -lt $deadline
    )
}

Copy-Item -LiteralPath $src -Destination $installedScript -Force
Copy-Item -LiteralPath $iconSource -Destination $installedIcon -Force
Copy-Item -LiteralPath $uninstallSource -Destination $installedUninstall -Force

# Remove the obsolete PyInstaller executable from earlier builds after its
# process has exited. It is no longer used by GetTorrent.
if (Test-Path -LiteralPath $legacyExe) {
    try {
        Remove-Item -LiteralPath $legacyExe -Force -ErrorAction Stop
        Write-Host 'Removed obsolete GetTorrent.exe from the old installer.' -ForegroundColor DarkGray
    }
    catch {
        Write-Host (
            'The old GetTorrent.exe could not be removed yet. ' +
            'It is no longer registered or used.'
        ) -ForegroundColor DarkYellow
    }
}

function Ensure-Key([string]$Path) {
    New-Item -Path $Path -Force | Out-Null
}

# This exact command is used for .torrent files and magnet URLs.
$openCommand = '"{0}" "{1}" "%1"' -f $pythonwExe, $installedScript
$launchArguments = '"{0}"' -f $installedScript

# --- ProgID and .torrent shell command --------------------------------------
$progKey = Join-Path $classes $progId
Ensure-Key $progKey
Set-Item -Path $progKey -Value 'Torrent File - GetTorrent'
Ensure-Key (Join-Path $progKey 'DefaultIcon')
Set-Item -Path (Join-Path $progKey 'DefaultIcon') -Value ('"{0}"' -f $installedIcon)
Ensure-Key (Join-Path $progKey 'shell\open\command')
Set-Item -Path (Join-Path $progKey 'shell\open\command') -Value $openCommand

$extensions = @('.torrent')
foreach ($ext in $extensions) {
    $openWith = Join-Path $classes ($ext + '\OpenWithProgids')
    Ensure-Key $openWith
    New-ItemProperty `
        -Path $openWith `
        -Name $progId `
        -PropertyType String `
        -Value '' `
        -Force | Out-Null
}

# --- magnet: URL protocol handler -------------------------------------------
$magnetKey = Join-Path $classes 'magnet'
Ensure-Key $magnetKey
Set-Item -Path $magnetKey -Value 'URL:Magnet Protocol'
Set-ItemProperty `
    -Path $magnetKey `
    -Name 'URL Protocol' `
    -Value '' `
    -Type String `
    -Force
Ensure-Key (Join-Path $magnetKey 'DefaultIcon')
Set-Item -Path (Join-Path $magnetKey 'DefaultIcon') -Value ('"{0}"' -f $installedIcon)
Ensure-Key (Join-Path $magnetKey 'shell\open\command')
Set-Item -Path (Join-Path $magnetKey 'shell\open\command') -Value $openCommand

$magnetProgKey = Join-Path $classes $magnetProgId
Ensure-Key $magnetProgKey
Set-Item -Path $magnetProgKey -Value 'URL:Magnet Protocol - GetTorrent'
Set-ItemProperty `
    -Path $magnetProgKey `
    -Name 'URL Protocol' `
    -Value '' `
    -Type String `
    -Force
Ensure-Key (Join-Path $magnetProgKey 'DefaultIcon')
Set-Item -Path (Join-Path $magnetProgKey 'DefaultIcon') -Value ('"{0}"' -f $installedIcon)
Ensure-Key (Join-Path $magnetProgKey 'shell\open\command')
Set-Item -Path (Join-Path $magnetProgKey 'shell\open\command') -Value $openCommand


# --- legacy upgrade compatibility -------------------------------------------
# Windows protects UserChoice defaults. Existing systems may still point to
# the old ProgIDs, so keep them valid and forward them to GetTorrent.
$legacyProgKey = Join-Path $classes $legacyProgId
Ensure-Key $legacyProgKey
Set-Item -Path $legacyProgKey -Value 'Torrent File - GetTorrent'
Ensure-Key (Join-Path $legacyProgKey 'DefaultIcon')
Set-Item -Path (Join-Path $legacyProgKey 'DefaultIcon') -Value ('"{0}"' -f $installedIcon)
Ensure-Key (Join-Path $legacyProgKey 'shell\open\command')
Set-Item -Path (Join-Path $legacyProgKey 'shell\open\command') -Value $openCommand

$legacyMagnetKey = Join-Path $classes $legacyMagnetProgId
Ensure-Key $legacyMagnetKey
Set-Item -Path $legacyMagnetKey -Value 'URL:Magnet Protocol - GetTorrent'
Set-ItemProperty -Path $legacyMagnetKey -Name 'URL Protocol' -Value '' -Type String -Force
Ensure-Key (Join-Path $legacyMagnetKey 'DefaultIcon')
Set-Item -Path (Join-Path $legacyMagnetKey 'DefaultIcon') -Value ('"{0}"' -f $installedIcon)
Ensure-Key (Join-Path $legacyMagnetKey 'shell\open\command')
Set-Item -Path (Join-Path $legacyMagnetKey 'shell\open\command') -Value $openCommand

# --- Capabilities / RegisteredApplications ----------------------------------
$appRoot = 'HKCU:\Software\GetTorrent'
$capabilities = Join-Path $appRoot 'Capabilities'
Ensure-Key $capabilities
New-ItemProperty `
    -Path $capabilities `
    -Name 'ApplicationName' `
    -PropertyType String `
    -Value $appName `
    -Force | Out-Null
New-ItemProperty `
    -Path $capabilities `
    -Name 'ApplicationDescription' `
    -PropertyType String `
    -Value 'Windows torrent downloader with DHT discovery, queue management, and persistent download history.' `
    -Force | Out-Null

$fileAssociations = Join-Path $capabilities 'FileAssociations'
Ensure-Key $fileAssociations
foreach ($ext in $extensions) {
    New-ItemProperty `
        -Path $fileAssociations `
        -Name $ext `
        -PropertyType String `
        -Value $progId `
        -Force | Out-Null
}

$urlAssociations = Join-Path $capabilities 'URLAssociations'
Ensure-Key $urlAssociations
New-ItemProperty `
    -Path $urlAssociations `
    -Name 'magnet' `
    -PropertyType String `
    -Value $magnetProgId `
    -Force | Out-Null

$registeredApps = 'HKCU:\Software\RegisteredApplications'
Ensure-Key $registeredApps
New-ItemProperty `
    -Path $registeredApps `
    -Name $appName `
    -PropertyType String `
    -Value 'Software\GetTorrent\Capabilities' `
    -Force | Out-Null

# Remove obsolete EXE application registrations from older builds.
Remove-Item `
    'HKCU:\Software\Classes\Applications\GetTorrent.exe' `
    -Recurse `
    -Force `
    -ErrorAction SilentlyContinue
Remove-Item `
    'HKCU:\Software\Microsoft\Windows\CurrentVersion\App Paths\GetTorrent.exe' `
    -Recurse `
    -Force `
    -ErrorAction SilentlyContinue

# --- Start Menu shortcut -----------------------------------------------------
$startMenuDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$shortcutPath = Join-Path $startMenuDir 'GetTorrent.lnk'
$ws = New-Object -ComObject WScript.Shell
$shortcut = $ws.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonwExe
$shortcut.Arguments = $launchArguments
$shortcut.WorkingDirectory = $installDir
$shortcut.IconLocation = $installedIcon
$shortcut.Description = 'GetTorrent'
$shortcut.Save()

# --- Uninstall registration --------------------------------------------------
$uninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\GetTorrent'
Ensure-Key $uninstallKey
New-ItemProperty -Path $uninstallKey -Name 'DisplayName' -PropertyType String -Value $appName -Force | Out-Null
New-ItemProperty -Path $uninstallKey -Name 'DisplayVersion' -PropertyType String -Value $appVersion -Force | Out-Null
New-ItemProperty -Path $uninstallKey -Name 'Publisher' -PropertyType String -Value 'GetTorrent' -Force | Out-Null
New-ItemProperty -Path $uninstallKey -Name 'DisplayIcon' -PropertyType String -Value $installedIcon -Force | Out-Null
$uninstallString = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $installedUninstall
New-ItemProperty -Path $uninstallKey -Name 'UninstallString' -PropertyType String -Value $uninstallString -Force | Out-Null
New-ItemProperty -Path $uninstallKey -Name 'InstallLocation' -PropertyType String -Value $installDir -Force | Out-Null
New-ItemProperty -Path $uninstallKey -Name 'NoModify' -PropertyType DWord -Value 1 -Force | Out-Null
New-ItemProperty -Path $uninstallKey -Name 'NoRepair' -PropertyType DWord -Value 1 -Force | Out-Null

# --- default handler ---------------------------------------------------------
$needsPrompt = @()

$torrentUserChoice = Get-ItemProperty `
    -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.torrent\UserChoice" `
    -ErrorAction SilentlyContinue

if ($torrentUserChoice -and ($torrentUserChoice.ProgId -eq $progId -or $torrentUserChoice.ProgId -eq $legacyProgId)) {
    Write-Host 'GetTorrent is already the Windows default for .torrent files.' -ForegroundColor Green
}
else {
    $extKey = Join-Path $classes '.torrent'
    Ensure-Key $extKey
    New-ItemProperty `
        -Path $extKey `
        -Name 'Content Type' `
        -PropertyType String `
        -Value 'application/x-bittorrent' `
        -Force | Out-Null

    if (-not $torrentUserChoice) {
        Set-Item -Path $extKey -Value $progId
    }
    $needsPrompt += '.torrent'
}

$magnetUserChoice = Get-ItemProperty `
    -Path "HKCU:\Software\Microsoft\Windows\Shell\Associations\UrlAssociations\magnet\UserChoice" `
    -ErrorAction SilentlyContinue

if ($magnetUserChoice -and ($magnetUserChoice.ProgId -eq $magnetProgId -or $magnetUserChoice.ProgId -eq $legacyMagnetProgId)) {
    Write-Host 'GetTorrent is already the Windows default for magnet links.' -ForegroundColor Green
}
else {
    $needsPrompt += 'magnet'
}

if ($needsPrompt.Count -gt 0) {
    Write-Host ''
    Write-Host (
        'GetTorrent is not yet the confirmed Windows default for: ' +
        ($needsPrompt -join ', ')
    ) -ForegroundColor Yellow
    Write-Host (
        'Windows may require you to choose GetTorrent once in Default apps.'
    ) -ForegroundColor Yellow

    if (-not $DoNotOpenDefaultApps) {
        Start-Process 'ms-settings:defaultapps'
    }
}

# Tell Explorer/browsers that associations changed.
try {
    Add-Type -Namespace Win32 -Name Shell32 -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("shell32.dll")]
public static extern void SHChangeNotify(int eventId, int flags, System.IntPtr item1, System.IntPtr item2);
'@ -ErrorAction Stop

    [Win32.Shell32]::SHChangeNotify(
        0x08000000,
        0x0000,
        [System.IntPtr]::Zero,
        [System.IntPtr]::Zero
    )
    Write-Host 'Notified Windows that torrent/magnet associations changed.' -ForegroundColor Green
}
catch {
    Write-Host (
        'Could not notify the Windows shell; associations are still registered.'
    ) -ForegroundColor DarkYellow
}

# Verify the installed script and both protocol commands before launch.
if (-not (Test-Path -LiteralPath $installedScript)) {
    throw "Installed application source is missing: $installedScript"
}

$torrentCmd = (
    Get-ItemProperty `
        -Path (Join-Path $progKey 'shell\open\command') `
        -Name '(default)' `
        -ErrorAction SilentlyContinue
).'(default)'

$magnetCmd = (
    Get-ItemProperty `
        -Path (Join-Path $magnetKey 'shell\open\command') `
        -Name '(default)' `
        -ErrorAction SilentlyContinue
).'(default)'

$magnetProgCmd = (
    Get-ItemProperty `
        -Path (Join-Path $magnetProgKey 'shell\open\command') `
        -Name '(default)' `
        -ErrorAction SilentlyContinue
).'(default)'

if (
    $torrentCmd -ne $openCommand -or
    $magnetCmd -ne $openCommand -or
    $magnetProgCmd -ne $openCommand
) {
    throw 'Torrent/magnet handler verification failed after registry installation.'
}

Write-Host ''
Write-Host "Installed source: $installedScript" -ForegroundColor Green
Write-Host "Python GUI host:  $pythonwExe" -ForegroundColor Green
Write-Host 'Torrent/magnet handlers verified.' -ForegroundColor Green


# --- clean old branding ------------------------------------------------------
Remove-Item `
    (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Torrent Downloader.lnk') `
    -Force -ErrorAction SilentlyContinue

Remove-Item `
    'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\TorrentDownloader' `
    -Recurse -Force -ErrorAction SilentlyContinue

Remove-ItemProperty `
    'HKCU:\Software\RegisteredApplications' `
    -Name $legacyAppName `
    -Force -ErrorAction SilentlyContinue

# The old user-data folder is intentionally retained because GetTorrent can
# migrate from it and it provides a rollback copy.
if (Test-Path -LiteralPath $legacyInstallDir) {
    try {
        Remove-Item -LiteralPath $legacyInstallDir -Recurse -Force -ErrorAction Stop
    }
    catch {
        Write-Host 'Old TorrentDownloader install folder could not be removed yet.' -ForegroundColor DarkYellow
    }
}

# --- launch immediately ------------------------------------------------------
Write-Host 'Launching GetTorrent...' -ForegroundColor Cyan

$launched = Start-Process `
    -FilePath $pythonwExe `
    -ArgumentList $launchArguments `
    -WorkingDirectory $installDir `
    -PassThru

Start-Sleep -Milliseconds 1200

# A single-instance app can hand off to an already-running instance and exit,
# so do not require this exact child PID to remain alive. Instead, fail only
# when the launch itself could not start.
if (-not $launched) {
    throw 'GetTorrent could not be launched.'
}

Write-Host ''
Write-Host 'GetTorrent installed and launched successfully.' -ForegroundColor Green
Write-Host 'Clicking a magnet link will launch/forward it to GetTorrent.' -ForegroundColor Green
Write-Host 'Opening a .torrent file will do the same.' -ForegroundColor Green
exit 0
