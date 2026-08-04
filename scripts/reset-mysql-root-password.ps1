param(
    [Parameter(Mandatory = $true)]
    [string]$NewRootPassword
)

$ErrorActionPreference = 'Stop'
$mysqlBin = 'C:\Program Files\MySQL\MySQL Server 5.7\bin'
$defaultsFile = 'C:\ProgramData\MySQL\MySQL Server 5.7\my.ini'
$temporarySql = Join-Path $env:TEMP "mysql-root-reset-$PID.sql"
$outputLog = Join-Path $PSScriptRoot 'mysql-reset-output.log'
$errorLog = Join-Path $PSScriptRoot 'mysql-reset-error.log'
$exceptionLog = Join-Path $PSScriptRoot 'mysql-reset-exception.log'
$escapedPassword = $NewRootPassword.Replace("'", "''")

try {
    Set-Content -LiteralPath $temporarySql -Value "ALTER USER 'root'@'localhost' IDENTIFIED BY '$escapedPassword';" -Encoding ascii
    Remove-Item -LiteralPath $outputLog, $errorLog -Force -ErrorAction SilentlyContinue

    Stop-Service -Name 'MySQL57' -Force
    $temporaryServer = Start-Process -FilePath (Join-Path $mysqlBin 'mysqld.exe') -PassThru -WindowStyle Hidden -ArgumentList @(
        "--defaults-file=`"$defaultsFile`"",
        "--init-file=`"$temporarySql`"",
        '--console'
    ) -RedirectStandardOutput $outputLog -RedirectStandardError $errorLog
    Start-Sleep -Seconds 12
    if ($temporaryServer.HasExited) {
        throw "Temporary MySQL startup failed. See $errorLog"
    }

    & (Join-Path $mysqlBin 'mysqladmin.exe') -u root ("-p$NewRootPassword") ping
    if ($LASTEXITCODE -ne 0) {
        throw 'Temporary MySQL did not accept the new password.'
    }

    & (Join-Path $mysqlBin 'mysqladmin.exe') -u root ("-p$NewRootPassword") shutdown
    $temporaryServer.WaitForExit(15000)
    Start-Service -Name 'MySQL57'
    Start-Sleep -Seconds 5

    & (Join-Path $mysqlBin 'mysqladmin.exe') -u root ("-p$NewRootPassword") ping
    if ($LASTEXITCODE -ne 0) {
        throw 'MySQL service did not accept the new password after restart.'
    }
}
catch {
    Set-Content -LiteralPath $exceptionLog -Value $_.Exception.ToString() -Encoding utf8
    throw
}
finally {
    if (Test-Path -LiteralPath $temporarySql) {
        Remove-Item -LiteralPath $temporarySql -Force
    }
}
