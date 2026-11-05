$ErrorActionPreference = 'Stop'
$secretDirectory = Join-Path $env:USERPROFILE '.codex\research-data'
New-Item -ItemType Directory -Path $secretDirectory -Force | Out-Null
$databaseToken = Read-Host 'Paste the research-catalog database token (input hidden)' -AsSecureString
if ($databaseToken.Length -eq 0) { throw 'Token is empty; nothing saved.' }
$databaseToken | Export-Clixml -LiteralPath (Join-Path $secretDirectory 'token.xml')
Write-Host 'Saved with Windows user encryption. The token was not printed.'
