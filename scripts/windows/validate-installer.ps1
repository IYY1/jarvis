<#
.SYNOPSIS
  Installer acceptance on a clean Windows 10/11 account, using only built-in PowerShell.

.DESCRIPTION
  Installs an older Jarvis, creates user data, upgrades to the release in place
  while the old service is running, checks first launch, the bundled voice path
  (paced test audio, dry-run dispatch), start-at-sign-in, service restart and
  uninstall, then writes one JSON report. Nothing here uses Python or a
  developer tool; the voice check injects the public fixture instead of a
  microphone (MURMUR_TEST_CAPTURE_FILE) and never dispatches actions.
#>
param(
  [Parameter(Mandatory)] [string] $Previous,
  [Parameter(Mandatory)] [string] $Release,
  [Parameter(Mandatory)] [string] $Fixture,
  [Parameter(Mandatory)] [string] $Report,
  [string] $Phase = 'all'
)
$ErrorActionPreference = 'Stop'
# Test the app as an ordinary account would: no inherited developer environment.
Remove-Item Env:PYTHONPATH, Env:PYTHONHOME -ErrorAction SilentlyContinue
$env:PATH = (($env:PATH -split ';') | Where-Object { $_ -and $_ -notlike 'C:\JarvisTest*' }) -join ';'
$checks = [ordered]@{}
$install = Join-Path $env:LOCALAPPDATA 'Programs\Jarvis'
$data = Join-Path $env:LOCALAPPDATA 'Jarvis'
$uninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Jarvis'
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$console = Join-Path $install 'JarvisConsole.exe'

function Check([string] $name, [scriptblock] $test) {
  try {
    $value = & $test
    $checks[$name] = @{ ok = [bool]$value; detail = "$value" }
  } catch {
    $checks[$name] = @{ ok = $false; detail = $_.Exception.Message }
  }
}
function Save { $checks | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 $Report }
function Console([string[]] $arguments) {
  $output = & $console @arguments 2>&1 | Out-String
  if ($LASTEXITCODE -ne 0) { throw "JarvisConsole $arguments exited $LASTEXITCODE`: $output" }
  $output
}
function Wait-Until([scriptblock] $condition, [int] $seconds) {
  $deadline = (Get-Date).AddSeconds($seconds)
  while ((Get-Date) -lt $deadline) {
    try { if (& $condition) { return $true } } catch { }
    Start-Sleep -Milliseconds 500
  }
  $false
}
function Service-Status { (Console @('status') | ConvertFrom-Json).status }
function Run-Setup([string] $path) {
  $watch = [Diagnostics.Stopwatch]::StartNew()
  $process = Start-Process -FilePath $path -ArgumentList '/S' -PassThru -Wait
  @{ exit = $process.ExitCode; seconds = [math]::Round($watch.Elapsed.TotalSeconds, 1) }
}

trap { $checks['unexpected error'] = @{ ok = $false; detail = "$_ at $($_.InvocationInfo.PositionMessage)" }; Save; break }

if ($Phase -in 'all', 'install') {
  Check 'clean: no Jarvis installed' { -not (Test-Path $uninstallKey) -and -not (Test-Path $install) }
  # Windows 10 ships Store aliases under WindowsApps; they are not an installed Python.
  Check 'clean: no Python installed on PATH' {
    $found = @(Get-Command python, python3, py -All -ErrorAction SilentlyContinue | Where-Object { $_.Source -notlike '*\Microsoft\WindowsApps\*' })
    if ($found) { throw ($found.Source -join ', ') }
    $true
  }
  $previousSetup = Run-Setup $Previous
  Check 'previous: silent install exit 0' { $previousSetup.exit -eq 0; }
  Check 'previous: installed version' { (Get-ItemProperty $uninstallKey).DisplayVersion }
  Check 'previous: first launch starts service' {
    Start-Process (Join-Path $install 'Jarvis.exe') -ArgumentList '--background'
    Wait-Until { (Service-Status).state } 60
  }
  # User data the upgrade must keep.
  Console @('shutdown') | Out-Null
  # Jarvis writes config.json on the first save; partial files are merged with defaults.
  $configPath = Join-Path $data 'config.json'
  $config = if (Test-Path $configPath) { Get-Content -Raw $configPath | ConvertFrom-Json } else { [pscustomobject]@{ commands = [pscustomobject]@{} } }
  $config.commands | Add-Member -NotePropertyName 'upgrade marker' -NotePropertyValue 'start recording' -Force
  [IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json -Depth 8))  # UTF-8 without BOM
  Set-Content -Encoding UTF8 (Join-Path $data 'user-marker.txt') 'keep me'
  $videos = Join-Path ([Environment]::GetFolderPath('MyVideos')) 'Jarvis'
  New-Item -ItemType Directory -Force $videos | Out-Null
  Set-Content (Join-Path $videos 'marker-clip.mp4') 'not a real clip'
  Start-Process (Join-Path $install 'Jarvis.exe') -ArgumentList '--background'
  Check 'previous: service running before upgrade' { Wait-Until { (Service-Status).state } 60 }

  $releaseSetup = Run-Setup $Release
  $checks['release: setup seconds'] = @{ ok = $true; detail = "$($releaseSetup.seconds)" }
  Check 'upgrade: silent setup exit 0 while old service ran' { $releaseSetup.exit -eq 0 }
  Check 'upgrade: version registered' { (Get-ItemProperty $uninstallKey).DisplayVersion }
  Check 'upgrade: uninstall entry complete' {
    $entry = Get-ItemProperty $uninstallKey
    $entry.UninstallString -and $entry.QuietUninstallString -and $entry.DisplayIcon -and $entry.Publisher -and $entry.EstimatedSize
  }
  Check 'upgrade: old Python speech runtime removed' { -not (Test-Path (Join-Path $install '_internal\sherpa_onnx')) -and -not (Test-Path (Join-Path $install '_internal\vosk')) }
  Check 'upgrade: both command models bundled (Small and Light)' {
    (Test-Path (Join-Path $install 'models\sherpa-onnx-whisper-small.en\small.en-encoder.int8.onnx')) -and
      (Test-Path (Join-Path $install 'models\sherpa-onnx-whisper-tiny.en\tiny.en-encoder.int8.onnx'))
  }
  Check 'upgrade: Rust voice worker and Vosk runtime bundled' { (Test-Path (Join-Path $install 'tools\jarvis-voice.exe')) -and (Test-Path (Join-Path $install 'tools\libvosk.dll')) }
  Check 'upgrade: settings kept' { (Get-Content -Raw (Join-Path $data 'config.json')) -match 'upgrade marker' }
  Check 'upgrade: user files kept' { (Test-Path (Join-Path $data 'user-marker.txt')) -and (Test-Path (Join-Path $videos 'marker-clip.mp4')) }
  Check 'upgrade: Start menu shortcut' { Test-Path (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Jarvis\Jarvis.lnk') }
  Check 'upgrade: desktop shortcut (default component)' { Test-Path (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Jarvis.lnk') }
  Check 'upgrade: start at sign-in stays off by default' { -not (Get-ItemProperty $runKey -Name Jarvis -ErrorAction SilentlyContinue) }
  Check 'release: version' { Console @('--version') }
  Check 'release: doctor finds models, tools and voice worker' {
    $doctor = Console @('doctor') | ConvertFrom-Json
    $doctor.voice_engine.model_ready -and $doctor.voice_engine.accurate_model_ready -and $doctor.vosk_library -and
      -not ($doctor.tools.PSObject.Properties.Value | Where-Object { -not $_.ok })
  }
  Check 'release: Start menu launch opens the settings window' {
    Start-Process (Join-Path $install 'Jarvis.exe')
    Wait-Until { Get-Process Jarvis -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle -like 'Jarvis*' } } 60
  }
  # Documented privacy behaviour: opening Settings never starts the service or capture.
  Check 'release: settings alone does not start the service' { -not (Get-Process JarvisConsole -ErrorAction SilentlyContinue) }
  Check 'release: background start (tray) brings the service up' {
    Start-Process (Join-Path $install 'Jarvis.exe') -ArgumentList '--background'
    Wait-Until { (Service-Status).state } 60
  }
  Get-Process Jarvis -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle -like 'Jarvis*' } | ForEach-Object { $_.CloseMainWindow() | Out-Null }
  Check 'release: closing settings keeps the service' { Start-Sleep 3; (Service-Status).state }
  Save

  # Core voice workflow with the bundled Rust worker: wake, command, dry-run dispatch.
  Console @('shutdown') | Out-Null
  $work = Join-Path $env:TEMP ('jarvis-voice-check-' + [guid]::NewGuid())
  New-Item -ItemType Directory $work | Out-Null
  $pcm = [IO.File]::ReadAllBytes($Fixture)
  $silence = New-Object byte[] (32000 * 3)
  $stream = [IO.File]::Create((Join-Path $work 'capture.raw'))
  $stream.Write($silence, 0, $silence.Length); $stream.Write($pcm, 0, $pcm.Length)
  $tail = New-Object byte[] (32000 * 120); $stream.Write($tail, 0, $tail.Length); $stream.Close()
  $voiceConfig = [ordered]@{ privacy = @{ onboarded = $true }; activation = @{ wake_phrase = 'go forward' };
                    commands = @{ 'ten meters' = 'start recording' } }
  [IO.File]::WriteAllText((Join-Path $work 'config.json'), ($voiceConfig | ConvertTo-Json -Depth 8))
  $env:MURMUR_CONFIG = Join-Path $work 'config.json'
  $env:MURMUR_ENDPOINT = Join-Path $work 'run\control.json'
  $env:MURMUR_DRY_RUN = '1'
  $env:MURMUR_TEST_CAPTURE_FILE = Join-Path $work 'capture.raw'
  $service = Start-Process $console -ArgumentList '--internal', 'daemon' -PassThru -WindowStyle Hidden
  Check 'voice: worker opens and reaches idle' { Wait-Until { $s = Service-Status; $s.microphone_open -and $s.state -eq 'idle' } 90 }
  Check 'voice: wake + command recognized and dispatched (dry run)' { Wait-Until { (Service-Status).message -like 'Dry run: *' } 60 }
  Check 'voice: worker is the bundled Rust executable' {
    (Get-CimInstance Win32_Process -Filter "ParentProcessId=$($service.Id)" | Where-Object { $_.Name -eq 'jarvis-voice.exe' }) -ne $null
  }
  Check 'voice: no Jarvis process loads a DLL from outside Windows or the install folder' {
    $foreign = Get-Process | Where-Object { $_.Path -like "$install*" } | ForEach-Object { $_.Modules } |
      Where-Object { $_.FileName -notlike "$install*" -and $_.FileName -notlike "$env:windir*" }
    if ($foreign) { throw ($foreign.FileName | Sort-Object -Unique | Out-String) }
    $true
  }
  Check 'voice: shutdown stops service and worker' {
    Console @('shutdown') | Out-Null
    Wait-Until { -not (Get-Process -Id $service.Id -ErrorAction SilentlyContinue) -and -not (Get-Process jarvis-voice -ErrorAction SilentlyContinue) } 30
  }
  Remove-Item Env:MURMUR_CONFIG, Env:MURMUR_ENDPOINT, Env:MURMUR_DRY_RUN, Env:MURMUR_TEST_CAPTURE_FILE
  Remove-Item -Recurse -Force $work

  Check 'startup: on writes Run entry and setting' {
    Console @('startup', 'on') | Out-Null
    ((Get-ItemProperty $runKey).Jarvis -like "*Jarvis.exe*--background*") -and ((Get-Content -Raw (Join-Path $data 'config.json') | ConvertFrom-Json).startup -eq $true)
  }
  Save
}

if ($Phase -in 'all', 'after-restart') {
  # Run after signing out/in or rebooting with start at sign-in on.
  Check 'restart: service started at sign-in' { Wait-Until { (Service-Status).state } 120 }
  Check 'startup: off removes Run entry' {
    Console @('startup', 'off') | Out-Null
    -not (Get-ItemProperty $runKey -Name Jarvis -ErrorAction SilentlyContinue)
  }
  Check 'restart: service restarts after shutdown' {
    Console @('shutdown') | Out-Null
    Start-Process (Join-Path $install 'Jarvis.exe') -ArgumentList '--background'
    Wait-Until { (Service-Status).state } 60
  }
  $uninstaller = Join-Path $install 'Uninstall.exe'
  Start-Process $uninstaller -ArgumentList '/S' -Wait
  Check 'uninstall: program folder removed' { Wait-Until { -not (Test-Path $console) } 60 }
  Check 'uninstall: no Jarvis process left' { -not (Get-Process Jarvis, JarvisConsole, jarvis-voice -ErrorAction SilentlyContinue) }
  Check 'uninstall: registry and shortcuts removed' {
    -not (Test-Path $uninstallKey) -and -not (Test-Path (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Jarvis')) -and
      -not (Test-Path (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Jarvis.lnk')) -and
      -not (Get-ItemProperty $runKey -Name Jarvis -ErrorAction SilentlyContinue)
  }
  Check 'uninstall: settings and recordings kept by default' {
    (Test-Path (Join-Path $data 'config.json')) -and (Test-Path (Join-Path ([Environment]::GetFolderPath('MyVideos')) 'Jarvis\marker-clip.mp4'))
  }
  Save
}
$failed = @($checks.Keys | Where-Object { -not $checks[$_].ok })
Save
if ($failed.Count) { Write-Output "FAILED: $($failed -join '; ')"; exit 1 }
Write-Output "PASSED $($checks.Count) checks"
