param(
    [string]$SmtpHost = 'smtp.gmail.com',
    [ValidateSet('ssl', 'starttls')][string]$Mode = 'ssl',
    [int]$Port = 8790,
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'
if ($env:TRACEWISE_WEBHOOK_URL) {
    throw 'Webhook delivery is already configured. Review its pending queue before choosing email instead.'
}
$root = Split-Path $PSScriptRoot -Parent
$sender = Read-Host 'Sender mailbox (for Gmail, use your personal Gmail address)'
$recipient = Read-Host 'Recipient mailbox (press Enter to send to yourself)'
if (-not $recipient) { $recipient = $sender }
$username = Read-Host 'SMTP username (press Enter to use sender mailbox)'
if (-not $username) { $username = $sender }
$password = Read-Host 'SMTP app password (hidden; never paste into chat)' -AsSecureString
$settings = @{
    TRACEWISE_SMTP_HOST = $SmtpHost
    TRACEWISE_SMTP_MODE = $Mode
    TRACEWISE_SMTP_PORT = $(if ($Mode -eq 'ssl') { '465' } else { '587' })
    TRACEWISE_SMTP_USERNAME = $username
    TRACEWISE_SMTP_FROM = $sender
    TRACEWISE_SMTP_TO = $recipient
    TRACEWISE_SMTP_PASSWORD = [System.Net.NetworkCredential]::new('', $password).Password
}
$previous = @{}
foreach ($key in $settings.Keys) {
    $previous[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
}
Push-Location $root
try {
    foreach ($key in $settings.Keys) {
        [Environment]::SetEnvironmentVariable($key, $settings[$key], 'Process')
    }
    # Isolated demo DB prevents old webhook messages being sent to a new recipient.
    Write-Host "Local email demo: http://127.0.0.1:$Port/#monitors"
    Write-Host 'No email is sent until a configured monitor opens or recovers an alert.'
    Write-Host 'Use synthetic monitor names for this demo. Press Ctrl+C to stop.'
    & $Python server.py --port $Port --db storage/email-demo.sqlite3
    if ($LASTEXITCODE -ne 0) { throw 'Tracewise stopped with an error. Check the server diagnostics above.' }
}
finally {
    foreach ($key in $settings.Keys) {
        [Environment]::SetEnvironmentVariable($key, $previous[$key], 'Process')
    }
    $settings.Clear()
    $password.Dispose()
    Pop-Location
}
