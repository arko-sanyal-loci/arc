param(
    [int]$Port = 7860,
    [string]$Host = "127.0.0.1",
    [string]$StateDir = "",
    [string]$DataDir = "",
    [switch]$NoHeartbeat,
    [switch]$NoGpu
)

$argsList = @("--host", $Host, "--port", $Port)
if ($StateDir) { $argsList += @("--state-dir", $StateDir) }
if ($DataDir) { $argsList += @("--data-dir", $DataDir) }
if ($NoHeartbeat) { $argsList += "--no-heartbeat" }
if ($NoGpu) { $argsList += "--no-gpu" }

python (Join-Path $PSScriptRoot "start.py") @argsList
