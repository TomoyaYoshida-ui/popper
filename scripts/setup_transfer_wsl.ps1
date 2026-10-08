param([switch]$Install,[switch]$Offline)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot
$taskStateDir = Join-Path $taskRoot '.acceptance\transfer-environment'
New-Item -ItemType Directory -Path $taskStateDir -Force | Out-Null
$taskState = Join-Path $taskStateDir 'windows-setup.json'
function Save-SetupState($phase, $detail) {
    @{ phase=$phase; detail=$detail; time=[DateTime]::UtcNow.ToString('o'); automatic_reboot=$false } |
        ConvertTo-Json | Set-Content -LiteralPath $taskState -Encoding utf8
}
$taskIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$taskPrincipal = New-Object Security.Principal.WindowsPrincipal($taskIdentity)
if (-not $taskPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Save-SetupState 'awaiting_elevation' 'Windows requires administrator privileges to install WSL; no automatic restart.'
    if ($Install) {
        $taskArgs = @('-NoProfile','-ExecutionPolicy','Bypass','-File',('"' + $PSCommandPath + '"'),'-Install')
        if ($Offline) { $taskArgs += '-Offline' }
        try {
            $taskProcess = Start-Process powershell.exe -Verb RunAs -WindowStyle Hidden -ArgumentList $taskArgs -PassThru
            Write-Output ('Elevated setup process: ' + $taskProcess.Id)
        } catch {
            Save-SetupState 'elevation_not_completed' $_.Exception.Message
            throw
        }
    }
    exit
}
if (-not $Install) { Save-SetupState 'ready_to_install' 'Administrator available.'; exit }
if ($Offline) {
    $taskMsi = Join-Path $taskStateDir 'wsl.3.0.1.0.x64.msi'
    $taskSignature = Get-AuthenticodeSignature -LiteralPath $taskMsi
    if ($taskSignature.Status -ne 'Valid' -or $taskSignature.SignerCertificate.Subject -notmatch 'Microsoft Corporation') {
        Save-SetupState 'signature_failed' 'Microsoft installer signature is not valid.'
        throw 'Refuse to install an unverified MSI'
    }
    Save-SetupState 'enabling_windows_features' 'Official offline WSL installation; no automatic reboot.'
    $taskRestartNeeded = $false
    foreach ($taskFeatureName in @('VirtualMachinePlatform','Microsoft-Windows-Subsystem-Linux')) {
        $taskFeature = Get-WindowsOptionalFeature -Online -FeatureName $taskFeatureName
        if ($taskFeature.State -ne 'Enabled') {
            $taskEnabled = Enable-WindowsOptionalFeature -Online -FeatureName $taskFeatureName -All -NoRestart
            if ($taskEnabled.RestartNeeded) { $taskRestartNeeded = $true }
        }
    }
    Save-SetupState 'installing_verified_msi' 'Microsoft-signed MSI; restart suppressed.'
    $taskMsiArgs = @('/i',('"' + $taskMsi + '"'),'/qn','/norestart','/L*v',('"' + (Join-Path $taskStateDir 'msi-install.log') + '"'))
    $taskMsiProcess = Start-Process msiexec.exe -ArgumentList $taskMsiArgs -WindowStyle Hidden -Wait -PassThru
    if ($taskMsiProcess.ExitCode -eq 3010) { $taskRestartNeeded = $true }
    if ($taskMsiProcess.ExitCode -notin @(0,3010)) { Save-SetupState 'msi_failed' ('Exit ' + $taskMsiProcess.ExitCode); exit 1 }
    if ($taskRestartNeeded) { Save-SetupState 'restart_required' 'Save work and restart Windows manually; installation checkpoint retained.' }
    else { Save-SetupState 'wsl_installed' 'WSL MSI and required features installed; Ubuntu remains to be installed.' }
    exit
}
Save-SetupState 'installing_wsl' 'Installing WSL 2 and Ubuntu-24.04 from Microsoft; distro will not launch interactively.'
try {
    & wsl.exe --install --distribution Ubuntu-24.04 --no-launch *> (Join-Path $taskStateDir 'wsl-install.log')
    $taskExit = $LASTEXITCODE
    Save-SetupState 'wsl_install_returned' ('Exit code ' + $taskExit + '; inspect the log and reboot requirement before proceeding.')
} catch { Save-SetupState 'installation_error' $_.Exception.Message; throw }
