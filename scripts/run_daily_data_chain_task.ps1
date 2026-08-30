[CmdletBinding()]
param(
    [switch]$EnableNetwork,
    [ValidateRange(1, 10799)]
    [int]$DeadlineSeconds = 9900
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ChainScript = Join-Path $Root "scripts\run_daily_data_chain.py"
$PreflightScript = Join-Path $Root "scripts\run_daily_data_chain_preflight.py"
$AuditDirectory = Join-Path $Root "results\data_chain\task_supervisor"
$StartedAtUtc = [DateTime]::UtcNow
$Status = "starting"
$ErrorText = ""
$PythonExecutable = ""
$ChildPid = $null
$ChildExitCode = $null
$SupervisorExitCode = 125
$Job = $null
$PreflightPayload = $null

function Resolve-TaskPythonRuntime {
    if (-not [string]::IsNullOrWhiteSpace($env:QUANTDESK_PYTHON)) {
        $Configured = $env:QUANTDESK_PYTHON.Trim().Trim('"')
        if (Test-Path -LiteralPath $Configured -PathType Leaf) {
            return [pscustomobject]@{
                Executable = (Resolve-Path -LiteralPath $Configured).Path
                PrefixArguments = @()
            }
        }
        throw "QUANTDESK_PYTHON does not name an existing executable: $Configured"
    }

    $VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $VenvPython -PathType Leaf) {
        return [pscustomobject]@{
            Executable = (Resolve-Path -LiteralPath $VenvPython).Path
            PrefixArguments = @()
        }
    }

    $PyLauncher = Get-Command "py.exe" -CommandType Application -ErrorAction SilentlyContinue
    if ($null -ne $PyLauncher) {
        return [pscustomobject]@{
            Executable = [string]$PyLauncher.Source
            PrefixArguments = @("-3")
        }
    }

    $SystemPython = Get-Command "python.exe" -CommandType Application -ErrorAction SilentlyContinue
    if ($null -ne $SystemPython) {
        return [pscustomobject]@{
            Executable = [string]$SystemPython.Source
            PrefixArguments = @()
        }
    }

    throw "QuantDesk Python runtime was not found."
}

function Write-SupervisorAudit {
    param([System.Collections.IDictionary]$Payload)

    [void][System.IO.Directory]::CreateDirectory($AuditDirectory)
    $Stamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssfffZ")
    $Destination = Join-Path $AuditDirectory ("task-supervisor-{0}-pid{1}.json" -f $Stamp, $PID)
    $Temporary = "$Destination.$([guid]::NewGuid().ToString('N')).tmp"
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $JsonText = ($Payload | ConvertTo-Json -Depth 6) + [Environment]::NewLine
    try {
        [System.IO.File]::WriteAllText($Temporary, $JsonText, $Utf8NoBom)
        [System.IO.File]::Move($Temporary, $Destination)
        return $Destination
    } finally {
        Remove-Item -LiteralPath $Temporary -Force -ErrorAction SilentlyContinue
    }
}

if (-not ("QuantDesk.TaskJob" -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

namespace QuantDesk
{
    public sealed class TaskJob : IDisposable
    {
        private const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;
        private const uint CREATE_SUSPENDED = 0x00000004;
        private const uint CREATE_UNICODE_ENVIRONMENT = 0x00000400;
        private const uint WAIT_OBJECT_0 = 0x00000000;
        private const uint WAIT_TIMEOUT = 0x00000102;
        private const uint WAIT_FAILED = 0xFFFFFFFF;
        private const int JobObjectExtendedLimitInformation = 9;

        private IntPtr jobHandle;
        private IntPtr processHandle;
        private bool disposed;

        public int ProcessId { get; private set; }

        private TaskJob()
        {
            jobHandle = IntPtr.Zero;
            processHandle = IntPtr.Zero;
        }

        public static TaskJob Start(string executable, string[] arguments, string workingDirectory)
        {
            if (String.IsNullOrWhiteSpace(executable))
                throw new ArgumentException("Executable is required.", "executable");

            TaskJob owner = new TaskJob();
            PROCESS_INFORMATION processInfo = new PROCESS_INFORMATION();
            bool processCreated = false;
            try
            {
                owner.jobHandle = CreateJobObject(IntPtr.Zero, null);
                if (owner.jobHandle == IntPtr.Zero)
                    ThrowLastWin32("CreateJobObject");

                JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits =
                    new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
                limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                int limitsSize = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
                if (!SetInformationJobObject(
                        owner.jobHandle,
                        JobObjectExtendedLimitInformation,
                        ref limits,
                        (uint)limitsSize))
                {
                    ThrowLastWin32("SetInformationJobObject");
                }

                STARTUPINFO startupInfo = new STARTUPINFO();
                startupInfo.cb = Marshal.SizeOf(typeof(STARTUPINFO));
                StringBuilder commandLine = new StringBuilder(
                    BuildCommandLine(executable, arguments));
                processCreated = CreateProcess(
                    executable,
                    commandLine,
                    IntPtr.Zero,
                    IntPtr.Zero,
                    false,
                    CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT,
                    IntPtr.Zero,
                    workingDirectory,
                    ref startupInfo,
                    out processInfo);
                if (!processCreated)
                    ThrowLastWin32("CreateProcess");

                owner.processHandle = processInfo.hProcess;
                owner.ProcessId = unchecked((int)processInfo.dwProcessId);
                if (!AssignProcessToJobObject(owner.jobHandle, owner.processHandle))
                    ThrowLastWin32("AssignProcessToJobObject");

                uint resumeResult = ResumeThread(processInfo.hThread);
                if (resumeResult == UInt32.MaxValue)
                    ThrowLastWin32("ResumeThread");

                CloseHandle(processInfo.hThread);
                processInfo.hThread = IntPtr.Zero;
                return owner;
            }
            catch
            {
                if (processInfo.hThread != IntPtr.Zero)
                    CloseHandle(processInfo.hThread);
                if (processCreated && processInfo.hProcess != IntPtr.Zero)
                    TerminateProcess(processInfo.hProcess, 125);
                if (processInfo.hProcess != IntPtr.Zero && owner.processHandle == IntPtr.Zero)
                    CloseHandle(processInfo.hProcess);
                owner.Dispose();
                throw;
            }
        }

        public bool WaitForExit(int timeoutMilliseconds)
        {
            ThrowIfDisposed();
            uint result = WaitForSingleObject(processHandle, unchecked((uint)timeoutMilliseconds));
            if (result == WAIT_OBJECT_0)
                return true;
            if (result == WAIT_TIMEOUT)
                return false;
            if (result == WAIT_FAILED)
                ThrowLastWin32("WaitForSingleObject");
            throw new InvalidOperationException("Unexpected process wait result: " + result);
        }

        public int GetExitCode()
        {
            ThrowIfDisposed();
            uint exitCode;
            if (!GetExitCodeProcess(processHandle, out exitCode))
                ThrowLastWin32("GetExitCodeProcess");
            return unchecked((int)exitCode);
        }

        public void Terminate(uint exitCode)
        {
            ThrowIfDisposed();
            if (!TerminateJobObject(jobHandle, exitCode))
                ThrowLastWin32("TerminateJobObject");
        }

        public void Dispose()
        {
            if (disposed)
                return;
            disposed = true;
            if (processHandle != IntPtr.Zero)
            {
                CloseHandle(processHandle);
                processHandle = IntPtr.Zero;
            }
            if (jobHandle != IntPtr.Zero)
            {
                CloseHandle(jobHandle);
                jobHandle = IntPtr.Zero;
            }
        }

        private void ThrowIfDisposed()
        {
            if (disposed)
                throw new ObjectDisposedException("TaskJob");
        }

        private static void ThrowLastWin32(string operation)
        {
            throw new Win32Exception(Marshal.GetLastWin32Error(), operation + " failed");
        }

        private static string BuildCommandLine(string executable, string[] arguments)
        {
            StringBuilder result = new StringBuilder();
            result.Append(QuoteArgument(executable));
            if (arguments != null)
            {
                foreach (string argument in arguments)
                {
                    result.Append(' ');
                    result.Append(QuoteArgument(argument ?? String.Empty));
                }
            }
            return result.ToString();
        }

        private static string QuoteArgument(string argument)
        {
            if (argument.Length == 0)
                return "\"\"";
            bool needsQuotes = argument.IndexOfAny(new char[] { ' ', '\t', '\n', '\v', '"' }) >= 0;
            if (!needsQuotes)
                return argument;

            StringBuilder result = new StringBuilder();
            result.Append('"');
            int backslashes = 0;
            foreach (char current in argument)
            {
                if (current == '\\')
                {
                    backslashes++;
                }
                else if (current == '"')
                {
                    result.Append('\\', (backslashes * 2) + 1);
                    result.Append('"');
                    backslashes = 0;
                }
                else
                {
                    result.Append('\\', backslashes);
                    result.Append(current);
                    backslashes = 0;
                }
            }
            result.Append('\\', backslashes * 2);
            result.Append('"');
            return result.ToString();
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct STARTUPINFO
        {
            public int cb;
            public string lpReserved;
            public string lpDesktop;
            public string lpTitle;
            public uint dwX;
            public uint dwY;
            public uint dwXSize;
            public uint dwYSize;
            public uint dwXCountChars;
            public uint dwYCountChars;
            public uint dwFillAttribute;
            public uint dwFlags;
            public ushort wShowWindow;
            public ushort cbReserved2;
            public IntPtr lpReserved2;
            public IntPtr hStdInput;
            public IntPtr hStdOutput;
            public IntPtr hStdError;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct PROCESS_INFORMATION
        {
            public IntPtr hProcess;
            public IntPtr hThread;
            public uint dwProcessId;
            public uint dwThreadId;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
        {
            public long PerProcessUserTimeLimit;
            public long PerJobUserTimeLimit;
            public uint LimitFlags;
            public UIntPtr MinimumWorkingSetSize;
            public UIntPtr MaximumWorkingSetSize;
            public uint ActiveProcessLimit;
            public UIntPtr Affinity;
            public uint PriorityClass;
            public uint SchedulingClass;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct IO_COUNTERS
        {
            public ulong ReadOperationCount;
            public ulong WriteOperationCount;
            public ulong OtherOperationCount;
            public ulong ReadTransferCount;
            public ulong WriteTransferCount;
            public ulong OtherTransferCount;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        {
            public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
            public IO_COUNTERS IoInfo;
            public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit;
            public UIntPtr PeakProcessMemoryUsed;
            public UIntPtr PeakJobMemoryUsed;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateJobObject(IntPtr jobAttributes, string name);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetInformationJobObject(
            IntPtr job,
            int informationClass,
            ref JOBOBJECT_EXTENDED_LIMIT_INFORMATION information,
            uint informationLength);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool CreateProcess(
            string applicationName,
            StringBuilder commandLine,
            IntPtr processAttributes,
            IntPtr threadAttributes,
            bool inheritHandles,
            uint creationFlags,
            IntPtr environment,
            string currentDirectory,
            ref STARTUPINFO startupInfo,
            out PROCESS_INFORMATION processInformation);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint ResumeThread(IntPtr thread);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetExitCodeProcess(IntPtr process, out uint exitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool TerminateJobObject(IntPtr job, uint exitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool TerminateProcess(IntPtr process, uint exitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr handle);
    }
}
'@
}

try {
    if (-not (Test-Path -LiteralPath $ChainScript -PathType Leaf)) {
        throw "Daily data chain entry point was not found: $ChainScript"
    }
    if (-not (Test-Path -LiteralPath $PreflightScript -PathType Leaf)) {
        throw "Daily data chain preflight entry point was not found: $PreflightScript"
    }

    $Runtime = Resolve-TaskPythonRuntime
    $PythonExecutable = [string]$Runtime.Executable
    $PreflightArguments = @($Runtime.PrefixArguments) + @(
        $PreflightScript,
        "--project-root", $Root,
        "--json", "true"
    )
    $PreflightRaw = & $PythonExecutable @PreflightArguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Daily data chain preflight failed with exit code $LASTEXITCODE."
    }
    $PreflightText = ($PreflightRaw -join [Environment]::NewLine).Trim()
    if ([string]::IsNullOrWhiteSpace($PreflightText)) {
        throw "Daily data chain preflight returned no JSON payload."
    }
    try {
        $PreflightPayload = $PreflightText | ConvertFrom-Json
    } catch {
        throw "Daily data chain preflight returned invalid JSON: $PreflightText"
    }
    if (
        [string]$PreflightPayload.action -eq "skip" -and
        [string]$PreflightPayload.status -eq "skipped_already_completed"
    ) {
        # A completed authoritative run is a successful no-op. Keep this
        # terminal string explicit in the supervisor contract so the receipt,
        # tests, and operator diagnostics cannot silently drift apart.
        $Status = "skipped_already_completed"
        $ChildExitCode = 0
        $SupervisorExitCode = 0
    } elseif ([string]$PreflightPayload.action -eq "skip") {
        throw "Daily data chain preflight requested an unsupported skip status: $($PreflightPayload.status)"
    } else {
        $ChildArguments = @($Runtime.PrefixArguments) + @(
            $ChainScript,
            "--trigger", "windows_scheduled_task"
        )
        if ($EnableNetwork) {
            $ChildArguments += "--enable-network"
        } else {
            $ChildArguments += @("--network", "false")
        }
        $ChildArguments += @("--json", "true", "--summary", "true")

        $env:PYTHONUTF8 = "1"
        $env:PYTHONIOENCODING = "utf-8"
        $Job = [QuantDesk.TaskJob]::Start(
            $PythonExecutable,
            [string[]]$ChildArguments,
            $Root
        )
        $ChildPid = $Job.ProcessId
        $DeadlineMilliseconds = [int]($DeadlineSeconds * 1000)
        $Exited = $Job.WaitForExit($DeadlineMilliseconds)
        if ($Exited) {
            $ChildExitCode = $Job.GetExitCode()
            $SupervisorExitCode = $ChildExitCode
            $Status = "child_exited"
        } else {
            $Status = "deadline_exceeded"
            $ErrorText = "The daily data chain exceeded the supervisor deadline."
            $Job.Terminate(124)
            [void]$Job.WaitForExit(10000)
            $ChildExitCode = $Job.GetExitCode()
            $SupervisorExitCode = 124
            [Console]::Error.WriteLine(
                "QuantDesk daily data chain exceeded its {0}-second supervisor deadline; the Job Object was terminated." -f $DeadlineSeconds
            )
        }
    }
} catch {
    $Status = "supervisor_failed"
    $ErrorText = $_.Exception.Message
    $SupervisorExitCode = 125
    if ($null -ne $Job) {
        try {
            $Job.Terminate(125)
            [void]$Job.WaitForExit(10000)
        } catch {
            $ErrorText += " Job cleanup also failed: $($_.Exception.Message)"
        }
    }
    [Console]::Error.WriteLine("QuantDesk task supervisor failed: $ErrorText")
} finally {
    if ($null -ne $Job) {
        $Job.Dispose()
    }
    $CompletedAtUtc = [DateTime]::UtcNow
    $Audit = [ordered]@{
        schema_version = 1
        component = "daily_data_chain_task_supervisor"
        status = $Status
        started_at_utc = $StartedAtUtc.ToString("o")
        completed_at_utc = $CompletedAtUtc.ToString("o")
        elapsed_seconds = [Math]::Round(($CompletedAtUtc - $StartedAtUtc).TotalSeconds, 3)
        deadline_seconds = $DeadlineSeconds
        network_authorized = [bool]$EnableNetwork
        trigger = "windows_scheduled_task"
        python_executable = $PythonExecutable
        child_pid = $ChildPid
        child_exit_code = $ChildExitCode
        supervisor_exit_code = $SupervisorExitCode
        error = $ErrorText
        preflight = $PreflightPayload
    }
    try {
        $WrittenAuditPath = Write-SupervisorAudit -Payload $Audit
        Write-Verbose "Task supervisor audit written to $WrittenAuditPath"
    } catch {
        [Console]::Error.WriteLine("Could not write task supervisor audit: $($_.Exception.Message)")
    }
}

exit $SupervisorExitCode
