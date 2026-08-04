$ErrorActionPreference = 'Stop'
Get-Content -LiteralPath 'C:\ProgramData\MySQL\MySQL Server 5.7\mysql.err' -Tail 80 |
    Set-Content -LiteralPath (Join-Path $PSScriptRoot 'mysql-error-tail.txt') -Encoding utf8
