<#
.SYNOPSIS
  Remote process runner for AgentScope's WindowsSSHBackend.

.DESCRIPTION
  Receives a Base64-encoded (UTF-16-LE) JSON payload describing an
  argv list, a working directory, and an optional timeout.  Starts the
  process using .NET ProcessStartInfo, captures stdout/stderr, and prints
  a JSON envelope on completion.

  This preserves the BackendBase.exec_shell argv contract — arguments
  are never concatenated into a command string, so spaces and special
  characters in argv elements are safe.

  Timeout: when the payload includes "timeout", the process tree is
  terminated with the Windows taskkill utility after that many seconds.

  Output JSON:
    {"exit_code": <int>, "stdout": "<base64>", "stderr": "<base64>"}
#>
param([Parameter(Mandatory=$true)][string]$Payload)

$ErrorActionPreference = "Stop"

# Decode payload.
$config = [System.Text.Encoding]::Unicode.GetString(
    [System.Convert]::FromBase64String($Payload)
) | ConvertFrom-Json

# Windows PowerShell 5.1 runs on .NET Framework, where
# ProcessStartInfo.ArgumentList is unavailable. Encode each argument using the
# CommandLineToArgvW escaping rules expected by native Windows processes.
function ConvertTo-WindowsCommandLineArg([string]$Argument) {
    if ($Argument.Length -gt 0 -and $Argument -notmatch '[\s"]') {
        return $Argument
    }

    $builder = [System.Text.StringBuilder]::new()
    [void]$builder.Append('"')
    $backslashes = 0
    foreach ($character in $Argument.ToCharArray()) {
        if ($character -eq '\') {
            $backslashes++
            continue
        }
        if ($character -eq '"') {
            [void]$builder.Append('\' * (2 * $backslashes + 1))
            [void]$builder.Append('"')
        } else {
            [void]$builder.Append('\' * $backslashes)
            [void]$builder.Append($character)
        }
        $backslashes = 0
    }
    [void]$builder.Append('\' * (2 * $backslashes))
    [void]$builder.Append('"')
    return $builder.ToString()
}

# Build ProcessStartInfo without invoking a shell.
$psi = [System.Diagnostics.ProcessStartInfo]::new()
$psi.FileName = $config.argv[0]
$encodedArgs = for ($i = 1; $i -lt $config.argv.Count; $i++) {
    ConvertTo-WindowsCommandLineArg ([string]$config.argv[$i])
}
$psi.Arguments = $encodedArgs -join ' '
$psi.WorkingDirectory = [string]$config.cwd
$psi.UseShellExecute = $false
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
# UTF-8 output so non-ASCII survives.
$psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
$psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8

# Process — no public constructor accepts ProcessStartInfo, so
# create the instance and assign .StartInfo afterwards.
$proc = [System.Diagnostics.Process]::new()
$proc.StartInfo = $psi

# Start both asynchronous reads before waiting so either pipe can fill without
# deadlocking the child process. Suppress Start()'s Boolean return value: the
# runner stdout must contain exactly one JSON envelope.
$null = $proc.Start()
$stdoutTask = $proc.StandardOutput.ReadToEndAsync()
$stderrTask = $proc.StandardError.ReadToEndAsync()

# Timeout handling.
$timedOut = $false
if ($config.timeout -and $config.timeout -gt 0) {
    $ms = [int]([double]$config.timeout * 1000)
    if (-not $proc.WaitForExit($ms)) {
        # Windows PowerShell 5.1 lacks Process.Kill(entireProcessTree).
        try {
            & "$env:SystemRoot\System32\taskkill.exe" `
                /PID $proc.Id /T /F 2>$null | Out-Null
        } catch {
            try { $proc.Kill() } catch {}
        }
        $timedOut = $true
        $proc.WaitForExit()
    }
} else {
    $proc.WaitForExit()
}

# Ensure the asynchronous reads have consumed all data after process exit.
$stdout = $stdoutTask.GetAwaiter().GetResult()
$stderrStr = $stderrTask.GetAwaiter().GetResult()

# Emit JSON envelope (stdout/stderr base64-encoded as UTF-8 bytes).
$stdoutB64 = [Convert]::ToBase64String(
    [System.Text.Encoding]::UTF8.GetBytes($stdout)
)
if ($timedOut) { $stderrStr += "`n[timed out]" }
$stderrB64 = [Convert]::ToBase64String(
    [System.Text.Encoding]::UTF8.GetBytes($stderrStr)
)

$exitCode = if ($timedOut) { -1 } else { $proc.ExitCode }

Write-Output (
    '{"exit_code":' + $exitCode +
    ',"stdout":"' + $stdoutB64 +
    '","stderr":"' + $stderrB64 + '"}'
)
