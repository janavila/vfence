param(
    [ValidatePattern('^[A-Za-z0-9_.-]+@[A-Za-z0-9][A-Za-z0-9_.-]*$')]
    [string]$Target = 'vfence-base01@100.117.140.104',
    [string]$AlteraRoot = 'C:\altera\13.1',
    [string]$Cable,
    [int]$Seconds = 180,
    [switch]$FromStdin
)

$ErrorActionPreference = 'Stop'
if ($Seconds -lt 1) { throw 'Seconds must be positive.' }

# Pass the existing parser as an argument. The remote process needs only Python 3.
$parser = [IO.File]::ReadAllBytes((Join-Path $PSScriptRoot 'listen_fpga_result.py'))
$encoded = [Convert]::ToBase64String($parser)
$remoteCommand = "python3 -u -c 'import sys, base64; exec(base64.b64decode(sys.argv.pop(1)))' $encoded --stdin"
$oldPath = $env:PATH
$frames = 0
$ssh = $null

try {
    if (!$FromStdin) {
        $terminal = Join-Path $AlteraRoot 'nios2eds\bin\nios2-terminal.exe'
        $quartus = Join-Path $AlteraRoot 'quartus\bin'
        # Quartus 13.1's terminal depends on the bundled Cygwin DLLs on Windows.
        $env:PATH = "$(Join-Path $quartus 'cygwin\bin');$quartus;$oldPath"
        $terminalArgs = @('--hardware', '--no-gmon', "--quit-after=$Seconds")
        if ($Cable) { $terminalArgs += "--cable=$Cable" }
    }

    # Windows PowerShell can buffer a native program's piped stdin until EOF.
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = (Get-Command ssh.exe -CommandType Application | Select-Object -First 1).Source
    $start.Arguments = "-T -o ConnectTimeout=8 -o ServerAliveInterval=10 -o ServerAliveCountMax=2 $Target `"$remoteCommand`""
    $start.UseShellExecute = $false
    $start.RedirectStandardInput = $true
    $ssh = [System.Diagnostics.Process]::Start($start)
    $ssh.StandardInput.AutoFlush = $true

    & {
        if ($FromStdin) {
            while ($null -ne ($line = [Console]::ReadLine())) { $line }
        } else {
            & $terminal @terminalArgs
            if ($LASTEXITCODE) { throw "nios2-terminal exited with code $LASTEXITCODE." }
        }
    } | ForEach-Object {
        if ($_ -cmatch '^VFENCE\|P[0-3]\|[0-7]\|[01]\r?$') {
            $ssh.StandardInput.WriteLine($_)
            $frames++
        }
    }

    $ssh.StandardInput.Close()
    $ssh.WaitForExit()
    if ($ssh.ExitCode -ne 0) { exit $ssh.ExitCode }
    if ($frames -eq 0) {
        Write-Warning 'No VFENCE result was captured.'
        exit 1
    }
    exit 0
} finally {
    if ($null -ne $ssh) {
        $ssh.StandardInput.Close()
        $ssh.Dispose()
    }
    $env:PATH = $oldPath
}
