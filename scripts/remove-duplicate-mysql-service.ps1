$ErrorActionPreference = 'Stop'

$service = Get-Service -Name 'MySQL57Mizki'
if ($service.Status -ne 'Stopped') {
    Stop-Service -Name 'MySQL57Mizki' -Force
    $service.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
}

sc.exe delete MySQL57Mizki
