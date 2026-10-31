<#
.SYNOPSIS
  Turns a Windows 10/11 PC into a headless home server lane. Every lane includes "base".

.DESCRIPTION
  base       never sleep (lid closed too), hibernate/Fast Startup off, Wake-on-LAN on wired ports,
             OpenSSH server (keys only), Tailscale (unattended mode), firewall on
  loki       base + Jellyfin (+ -WithDownloads: Sonarr, Radarr, Prowlarr, qBittorrent)
  heimdall   base + Docker Desktop (uptime-kuma, ntfy) + wake-watch every 2 minutes (Task Scheduler)
  thor       base + Ollama (tailnet only)

  The lanes are the server half of the loki, heimdall and thor profiles (profiles\<name>\README.md).
  Old names still work: media = loki, workhorse = thor. Run it from the kit's root folder (your clone).

  Safe to re-run; a file it replaces stays beside it as <file>.bak-<time>. Every action is printed
  ("+" = will do / did, "=" = already so).
  Preview with -DryRun (changes nothing, no admin needed).

  Windows caveats (read servers\common.md): Docker Desktop and the Ollama app only run while a user is logged in,
  so a headless Windows server needs automatic sign-in. Tailscale SSH does not run on Windows; you use
  OpenSSH over the tailnet instead.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\servers\windows\setup.ps1 -Lane heimdall -DryRun
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\servers\windows\setup.ps1 -Lane loki -WithDownloads
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('base', 'loki', 'heimdall', 'thor', 'media', 'workhorse')]
    [string]$Lane,
    [switch]$DryRun,
    [switch]$WithDownloads
)

$ErrorActionPreference = 'Stop'
if ($Lane -eq 'media') { $Lane = 'loki' }          # old lane names
if ($Lane -eq 'workhorse') { $Lane = 'thor' }
$Kit = Split-Path -Parent $PSScriptRoot
$Root = 'C:\homeserver'
$NextSteps = New-Object System.Collections.Generic.List[string]

function Write-Step([string]$Message) { Write-Host "`n==> $Message" }
function Write-Same([string]$Message) { Write-Host "  = $Message" }
function Add-NextStep([string]$Message) { $NextSteps.Add($Message) }
function Invoke-Change {
    param([string]$Description, [scriptblock]$Action)
    Write-Host "  + $Description"
    if (-not $DryRun) { & $Action }
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Install-WingetPackage {
    param([string]$Id, [string[]]$Extra = @())
    & winget list --id $Id --exact --accept-source-agreements *> $null
    if ($LASTEXITCODE -eq 0) { Write-Same "installed: $Id"; return }
    Invoke-Change "winget install $Id $($Extra -join ' ')" {
        & winget install --id $Id --exact --silent --accept-package-agreements --accept-source-agreements @Extra
        if ($LASTEXITCODE -ne 0) { throw "winget install $Id failed (exit $LASTEXITCODE)" }
    }
}

function Write-TextFile {
    # Write a text file (UTF-8, no BOM) only if its content differs; a file it replaces stays as <file>.bak-<time>.
    # -Fresh (code SYSTEM runs): always a new file from the kit, so nothing done to the old one, or a handle another
    # account opened on it before its folder was locked, carries over.
    param([string]$Path, [string[]]$Lines, [switch]$Fresh)
    $new = ($Lines -join "`n") + "`n"
    $same = (Test-Path $Path) -and ([System.IO.File]::ReadAllText($Path) -eq $new)
    if ($same -and -not $Fresh) { Write-Same "$Path (unchanged)"; return }
    if ((Test-Path $Path) -and -not $same) {
        $bak = "$Path.bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
        Invoke-Change "back up $Path -> $bak" { Copy-Item -LiteralPath $Path -Destination $bak }
    }
    Invoke-Change "write $Path$(if ($Fresh) { ' (a new file, from the kit)' })" {
        if ($Fresh -and (Test-Path -LiteralPath $Path)) { Remove-Item -LiteralPath $Path -Force }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
        [System.IO.File]::WriteAllText($Path, $new)
    }
}

# ------------------------------------------------------------------ who may change what SYSTEM runs
# The wake-watch task runs as SYSTEM, from $Root and its data folder. A new folder under C:\ lets every signed-in user
# change what it holds, and ProgramData lets users add files, so these get an exact ACL instead: owner
# Administrators, nothing inherited, SYSTEM and Administrators full control, Users read and execute. A folder or file
# that was already there is used only if no other account could have changed it: its owner is SYSTEM, Administrators
# or TrustedInstaller, and no entry gives anyone else a right to change it (Get-UnsafeReason). That includes you: what
# you run without elevation runs as your account, so a file your account may change is not one SYSTEM may run (audit
# S2, round 3). Only what SYSTEM never runs, the Compose files and .env Docker Desktop uses as you, also trusts you
# (-Trust). SIDs, not names, so it works in every Windows language.
$SidSystem = 'S-1-5-18'
$SidAdmins = 'S-1-5-32-544'
$SidUsers = 'S-1-5-32-545'
$SidTrustedInstaller = 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
$SidOwnerStandIns = @('S-1-3-0', 'S-1-3-4')   # CREATOR OWNER, OWNER RIGHTS: they stand for the owner
$SidMe = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value   # the operator: you, running this elevated
# Rights that let an account change an item: write or append data, (extended) attributes, delete it or (a folder)
# what it holds, change its permissions or owner; generic all and generic write.
$WriteRights = 0x500D0156
# Rights on a folder above a privileged path that let an account replace or redirect what lies below it: delete or
# rename it (DELETE), delete or rename what it holds (FILE_DELETE_CHILD), change its permissions or owner, generic
# all. Creating files and folders in it is not among them: stock C:\ and ProgramData let every user do that (R5-1).
$RedirectRights = 0x100D0040
# How SYSTEM starts Python for wake-watch, which needs only the standard library (audit S2, round 4). -I: nothing from
# the script's folder or a user site-packages, no PYTHON* variable (PYTHONPATH, PYTHONHOME, ...) and no registry
# PythonPath. -S: no site initialization either, so no .pth file runs (an import line, or a folder it adds outside the
# checked Python folder) and no sitecustomize. A .pth file is protected like the rest of the folder, but the folder it
# names is not: an editable install pointing at your own project folder would let you put code there for SYSTEM.
$PyIsolation = '-I -S'
# Python's path setup reads PYTHONEXECUTABLE and __PYVENV_LAUNCHER__ even with -I (they point it at another pyvenv.cfg,
# whose 'home' says where the standard library is), so the wrapper clears them, and the two -I already ignores.
$PyEnvClear = @('PYTHONHOME', 'PYTHONPATH', 'PYTHONEXECUTABLE', '__PYVENV_LAUNCHER__')

function Get-LinkAttribute([string]$Path) {
    # The item's own attributes, also for a link whose target is missing (GetAttributes does not follow links);
    # $null when nothing is there.
    try { return [IO.File]::GetAttributes($Path) } catch [IO.FileNotFoundException], [IO.DirectoryNotFoundException] { return $null }
}

function Get-UnsafeReason {
    # Why an account other than SYSTEM, Administrators, TrustedInstaller (and the SIDs in -Trust) could have changed an
    # item, from its owner (a SID) and its entries ($Rules: Sid, Rights = the access mask, Allow): '' when none could.
    # Every Allow entry counts, inherited or not, also one only for what a folder holds: it handed that right to its
    # items. An owner may always change the permissions, so the owner must be trusted too.
    # No file access here, so servers\windows\test_setup.ps1 can run it anywhere.
    param([string]$Owner, [object[]]$Rules, [string[]]$Trust = @())
    $trusted = @($SidSystem, $SidAdmins, $SidTrustedInstaller) + $Trust
    $mine = ' (your account, so anything you run without elevation too)'
    if ($trusted -notcontains $Owner) {
        if ($Owner -eq $SidMe) { return "it belongs to your account ($Owner): anything you run without elevation could change it" }
        return "it belongs to another account ($Owner)"
    }
    foreach ($r in $Rules) {
        if (-not $r.Allow -or ($trusted + $SidOwnerStandIns) -contains $r.Sid) { continue }
        if (([int64]$r.Rights -band $WriteRights) -ne 0) { return "$($r.Sid)$(if ($r.Sid -eq $SidMe) { $mine }) may write to it, delete it or change its permissions or owner" }
    }
    return ''
}

function Get-AclFacts([string]$Path) {
    # $Path's owner and entries as SIDs and access masks, for Get-UnsafeReason.
    $acl = Get-Acl -LiteralPath $Path
    $sid = [Security.Principal.SecurityIdentifier]
    [pscustomobject]@{
        Owner = $acl.GetOwner($sid).Value
        Rules = @($acl.GetAccessRules($true, $true, $sid) | ForEach-Object {
                [pscustomobject]@{ Sid = $_.IdentityReference.Value; Rights = [int]$_.FileSystemRights; Allow = "$($_.AccessControlType)" -eq 'Allow'
                    InheritOnly = (([int]$_.PropagationFlags) -band 2) -ne 0 } })   # 2: InheritOnly, only for what it holds
    }
}

function Get-UntrustedReason {
    # Why $Path (with -Recurse: or anything under it) could hold what another account put there, '' when nothing
    # could or $Path is missing (Get-UnsafeReason, with -Trust). A folder is searched only once it passed, so no other
    # account can add, remove or swap what it holds while it is searched. A link (junction, symlink, mount point)
    # stops the script: it could redirect what this script writes and SYSTEM runs.
    param([string]$Path, [switch]$Recurse, [string[]]$Trust = @())
    $todo = New-Object System.Collections.Generic.Stack[string]
    $todo.Push($Path)
    while ($todo.Count -gt 0) {
        $p = $todo.Pop()
        $attr = Get-LinkAttribute $p
        if ($null -eq $attr) { continue }
        if ($attr -band [IO.FileAttributes]::ReparsePoint) {
            throw "$p is a link (junction or symlink): it could redirect what this script writes and SYSTEM runs. Look at it, remove it, re-run."
        }
        try { $facts = Get-AclFacts $p }   # not even administrators may read them: its owner is someone else
        catch { return "${p}: its permissions cannot be read ($($_.Exception.Message))" }
        $why = Get-UnsafeReason $facts.Owner $facts.Rules -Trust $Trust
        if ($why) { return "${p}: $why" }
        if ($Recurse -and ($attr -band [IO.FileAttributes]::Directory)) {
            foreach ($c in [IO.Directory]::EnumerateFileSystemEntries($p)) { $todo.Push($c) }
        }
    }
    return ''
}

function Assert-Trusted {
    # Stop when Get-UntrustedReason has a reason: what another account could change, it could have set up for SYSTEM.
    # -Trust: SIDs that may change it anyway (only for what SYSTEM never runs).
    param([string]$Path, [switch]$Recurse, [string[]]$Trust = @())
    $why = Get-UntrustedReason $Path -Recurse:$Recurse -Trust $Trust
    if ($why) { throw "$why, so it could hold what that account put there for SYSTEM to run. Removing that access now would not undo what it may have changed: look at it (icacls), move it away (or reinstall what it belongs to), re-run." }
}

# ------------------------------------------------------------------ where privileged paths come from (audit R5-1)
# An elevated shell still takes $env:ProgramData, $env:ProgramFiles and the like from your account's persistent
# environment, which anything you run without elevation may change. SYSTEM would then keep its state, or find its
# Python, wherever your account pointed it, and a path holding CMD syntax (C:\Users\you\pd&whoami&rem) would run as a
# command in wake-watch.cmd. So privileged roots come from Windows' known-folder API (Get-TrustedRoot), never from
# $env:; no folder above a privileged path may be one another account could swap (Get-AncestorReason); and a path
# CMD would read as syntax is refused, never escaped (Get-CmdUnsafeReason).

function Get-KnownFolderPath([string]$Name) { [Environment]::GetFolderPath($Name) }   # its own function for the tests
# Windows' own drive (C:\), from GetSystemDirectory, which no environment variable changes; its own function for the tests
function Get-WindowsDrive { [IO.Path]::GetPathRoot([Environment]::SystemDirectory) }

function Get-CmdUnsafeReason([string]$Value) {
    # Why $Value may not be written into a .cmd file SYSTEM runs, '' when it may: CMD expands % (and ! when delayed
    # expansion is on) even inside quotes, a " ends them, and ^ & | < > and a line break are command syntax.
    if ($Value -match '[%!"^&|<>\r\n]') {
        $c = $Matches[0]
        if ($c -eq "`r" -or $c -eq "`n") { $c = 'a line break' } else { $c = "'$c'" }
        return "it holds $c, which CMD reads as syntax"
    }
    return ''
}

function Get-TrustedRoot([string]$Name) {
    # The privileged root $Name ('CommonApplicationData' = ProgramData, 'ProgramFiles') from Windows' known-folder API,
    # never from $env:. Refused unless it is a plain path on Windows' own drive (C:\...) with nothing CMD reads as
    # syntax: another drive letter may be a mapped share or a removable disk, whose owner and entries say whatever its
    # server or whoever formatted it chose, so Get-AncestorReason could not vouch for it. What lies above it is checked
    # where it is used (Get-AncestorReason).
    $p = "$(Get-KnownFolderPath $Name)"
    $drive = "$(Get-WindowsDrive)"
    $why = Get-CmdUnsafeReason $p
    if (-not $why -and $p -notmatch '^[A-Za-z]:\\[^\\]') { $why = 'it is not a local folder path such as C:\ProgramData' }
    if (-not $why -and -not ($drive -match '^[A-Za-z]:\\$' -and $p.StartsWith($drive, [StringComparison]::OrdinalIgnoreCase))) {
        $why = "it is not on Windows' own drive ($drive)"
    }
    if ($why) { throw "Windows gives '$p' as its $Name folder, but $why. Something changed where Windows keeps that folder: find out what, put it back, re-run." }
    return $p.TrimEnd('\')
}

function Get-AncestorReason {
    # Why another account could replace or redirect $Path through a folder above it, '' when none could (audit R5-1).
    # From the drive root down, every folder above $Path must exist, be no link (junction, symlink, mount point), belong
    # to SYSTEM, Administrators or TrustedInstaller, and give no other account $RedirectRights on itself (an entry only
    # for what it holds lands on the next folder, which is checked in turn). Unlike Get-UnsafeReason, adding files or
    # folders to it is allowed: stock C:\ and ProgramData let every user do that, and $Path gets its own check. $Path
    # itself, when there, must be no link either.
    param([string]$Path)
    $above = @(); $p = Split-Path -Parent $Path
    while ($p) { $above = @($p) + $above; $q = Split-Path -Parent $p; if ($q -eq $p) { break }; $p = $q }
    $trusted = @($SidSystem, $SidAdmins, $SidTrustedInstaller)
    foreach ($a in $above) {
        $attr = Get-LinkAttribute $a
        if ($null -eq $attr) { return "${a} is missing" }
        if ($attr -band [IO.FileAttributes]::ReparsePoint) { return "${a} is a link (junction or symlink)" }
        try { $facts = Get-AclFacts $a }
        catch { return "${a}: its permissions cannot be read ($($_.Exception.Message))" }
        if ($trusted -notcontains $facts.Owner) {
            if ($facts.Owner -eq $SidMe) { return "${a}: it belongs to your account ($($facts.Owner))" }
            return "${a}: it belongs to another account ($($facts.Owner))"
        }
        foreach ($r in $facts.Rules) {
            if (-not $r.Allow -or $r.InheritOnly -or ($trusted + $SidOwnerStandIns) -contains $r.Sid) { continue }
            if (([int64]$r.Rights -band $RedirectRights) -ne 0) {
                return "${a}: $($r.Sid)$(if ($r.Sid -eq $SidMe) { ' (your account)' }) may delete or rename it or what it holds, or change its permissions or owner"
            }
        }
    }
    $attr = Get-LinkAttribute $Path
    if ($null -ne $attr -and ($attr -band [IO.FileAttributes]::ReparsePoint)) { return "$Path is a link (junction or symlink)" }
    return ''
}

function Assert-SafeAncestors([string]$Path) {
    # Stop when Get-AncestorReason has a reason: checked before anything at or under $Path is created, judged or written.
    $why = Get-AncestorReason $Path
    if ($why) { throw "$why, so another account could swap it for a link to a folder it controls and redirect $Path, which this script writes with administrator rights and SYSTEM uses. Find out why (icacls, dir /AL), put it right, re-run." }
}

function Assert-PythonTrusted([string]$Dir) {
    # SYSTEM runs this Python and imports from its folder (Lib, DLLs; site-packages, whose .pth files would run at
    # startup without -S), so every file in it counts, not only python.exe: a grant on site-packages alone is a common
    # pip fix. Only SYSTEM, Administrators and TrustedInstaller may own or change any of it; not you either, or
    # anything you run without elevation could put code there for SYSTEM (audit S2, round 3). Nothing here changes its
    # permissions: a Python that fails is reinstalled for all users, never patched up. Then nothing may point it
    # outside the folder (Get-PythonRedirect, rounds 4 and 5). First of all, no folder above it may be one another
    # account could swap (Get-AncestorReason, R5-1): C:\Program Files as a junction would move all of it.
    Assert-SafeAncestors $Dir
    Write-Host "    checking that only administrators can change $Dir (every file in it, can take a minute)"
    $why = Get-UntrustedReason $Dir -Recurse
    if ($why) {
        throw "$why. SYSTEM runs this Python every 2 minutes, so only SYSTEM, Administrators and TrustedInstaller may own or change anything in it, your own account included. Removing that access now would not undo what may have been changed: uninstall this Python, delete what is left of $Dir (the uninstaller keeps what it did not install, such as packages and anything put there), reinstall it machine-wide for all users from an elevated prompt (winget install --id Python.Python.3.13 --exact --scope machine, or the python.org installer with 'Install for all users'), install packages into it only from an elevated prompt, and do not give your account write access to it. Then re-run."
    }
    # checked once the folder passed, so no other account can add or remove what is read here
    $why = Get-PythonRedirect $Dir
    if ($why) {
        throw "$why. SYSTEM would then load Python's standard library or startup code from outside $Dir, which these checks do not cover. A machine-wide Python 3.13.15 or later from the python.org installer or winget has none of this: upgrade an older one from an elevated prompt (winget upgrade --id Python.Python.3.13 --exact --scope machine). It has no ._pth, pyvenv.cfg or pybuilddir.txt file (the embeddable zip, virtual environments and build output do), it has its Lib\os.py, and only administrators may add files beside it: move such a file away, or reinstall Python for all users (winget install --id Python.Python.3.13 --exact --scope machine). Then re-run."
    }
}

function Get-PythonCoreVersion([string]$Dir) {
    # The file version of $Dir's python313.dll, where Python's path setup is compiled in: 3.13.<micro*1000 + release
    # level*10 + serial> (3.13.15 is 3.13.15150); $null when the DLL is missing.
    $dll = Join-Path $Dir 'python313.dll'
    if ($null -eq (Get-LinkAttribute $dll)) { return $null }
    $v = [Diagnostics.FileVersionInfo]::GetVersionInfo($dll)
    return New-Object Version($v.FileMajorPart, $v.FileMinorPart, $v.FileBuildPart)
}

function Get-PythonRedirect([string]$Dir) {
    # Why the Python in $Dir could load its standard library or startup code from outside $Dir whatever the ACLs in it
    # say, '' when nothing does (audit S2, rounds 4 and 5): every file CPython's path setup (Modules\getpath.py, 3.13
    # and 3.14) reads beside or above python.exe that can move its prefix, standard library or sys.path.
    # - Before 3.13.15 Python takes itself for a source build when ..\..\Modules\Setup.local exists,
    #   C:\Modules\Setup.local for C:\Program Files\Python313, and any account may create a folder in C:\; it then
    #   loads its standard library from ..\..\Lib, -I -S or not (CVE-2026-12003).
    # - A pybuilddir.txt beside python.exe still does that in 3.13.15 and later, empty or not.
    # - A ._pth file beside python.exe or its DLL replaces the whole search path, and its 'import site' line turns
    #   site back on, -S or not.
    # - A pyvenv.cfg beside python.exe or one folder up makes it a virtual environment whose 'home' names the real
    #   Python (and in 3.14 one without 'home' moves sys.prefix up there).
    # - With none of python313.zip, Lib\os.py and Lib\os.pyc in $Dir, Python looks for Lib\os.py in each folder
    #   above it: C:\Program Files, then C:\, where any account may create Lib.
    # None of these can be told to stay inside $Dir, so each is refused, and the folder above must let no other
    # account add a pyvenv.cfg later. -I already ignores PYTHONHOME, PYTHONPATH and the registry's PythonPath keys,
    # and the wrapper clears the two variables it does not ($PyEnvClear). servers\windows\test_setup.ps1 -CPython runs
    # CPython's own getpath.py on each of these.
    $v = Get-PythonCoreVersion $Dir
    if ($null -eq $v) { return "$(Join-Path $Dir 'python313.dll') is missing, so it cannot be told whether this Python is 3.13.15 or later" }
    if ($v -lt [Version]'3.13.15150') {
        return "$(Join-Path $Dir 'python313.dll'): file version $v, so this Python is older than 3.13.15 (file version 3.13.15150). Before 3.13.15 it loads its standard library from ..\..\Lib when ..\..\Modules\Setup.local exists, a file any account may create (CVE-2026-12003)"
    }
    foreach ($f in [IO.Directory]::EnumerateFiles($Dir)) {
        if ([IO.Path]::GetFileName($f) -like '*._pth') { return "${f}: a ._pth file replaces Python's search path" }
    }
    $bd = Join-Path $Dir 'pybuilddir.txt'
    if ($null -ne (Get-LinkAttribute $bd)) {
        return "${bd}: a pybuilddir.txt, empty or not, makes Python take itself for a source build whose standard library is ..\..\Lib (C:\Lib for C:\Program Files\Python313)"
    }
    $lib = Join-Path $Dir 'Lib'
    $marks = @((Join-Path $Dir 'python313.zip'), (Join-Path $lib 'os.py'), (Join-Path $lib 'os.pyc'))
    if (@($marks | Where-Object { $a = Get-LinkAttribute $_; ($null -ne $a) -and -not ($a -band [IO.FileAttributes]::Directory) }).Count -eq 0) {
        return "$(Join-Path $lib 'os.py') is missing (and there is no os.pyc or python313.zip), so Python would look for its standard library in the folders above $Dir, up to C:\Lib, which any account may create"
    }
    $up = Split-Path -Parent $Dir
    foreach ($cfg in @((Join-Path $Dir 'pyvenv.cfg'), (Join-Path $up 'pyvenv.cfg'))) {
        if ($null -ne (Get-LinkAttribute $cfg)) { return "${cfg}: a pyvenv.cfg makes Python a virtual environment of the Python its 'home' line names" }
    }
    $why = Get-UntrustedReason $up
    if ($why) { return "$why, so it could add a pyvenv.cfg there, beside the Python folder" }
    return ''
}

function Get-WakeWatchWrapper([string]$Py, [string]$Conf, [string]$State, [string]$Alert, [string]$Script) {
    # The lines of wake-watch.cmd, which SYSTEM's task runs: clear what Python's path setup reads even in isolated mode,
    # then Python with $PyIsolation (no site initialization, no PYTHON* variables, no imports from the script's folder).
    # Every path goes in as set "NAME=value" or between quotes, and one that CMD would still read as syntax there is
    # refused, never escaped (Get-CmdUnsafeReason, audit R5-1).
    foreach ($p in @($Py, $Conf, $State, $Alert, $Script)) {
        $why = Get-CmdUnsafeReason $p
        if ($why) { throw "wake-watch.cmd, which SYSTEM runs, would hold the path $p, but $why. setup.ps1 refuses such a path rather than escape it: find out where it came from, put it right, re-run." }
    }
    @('@echo off') + @($PyEnvClear | ForEach-Object { "set `"$_=`"" }) + @(
        "set `"WAKE_WATCH_CONF=$Conf`"",
        "set `"WAKE_WATCH_STATE=$State`"",
        "set `"WAKE_WATCH_ALERT=$Alert`"",
        "`"$Py`" $PyIsolation `"$Script`" %*")
}

function Stop-WakeWatch([string]$Wrapper, [string]$Why) {
    # What SYSTEM runs every 2 minutes was just refused, so its task must not keep running it: an existing wrapper
    # (C:\homeserver is locked by now) becomes one that runs nothing, until a re-run that passes writes the real one.
    if ($DryRun -or $null -eq (Get-LinkAttribute $Wrapper)) { return }
    Write-Warning "wake-watch is off until a re-run passes: $Wrapper now runs nothing."
    Write-TextFile -Fresh -Path $Wrapper -Lines @('@echo off', "rem wake-watch is off: setup.ps1 refused what it runs ($Why). Fix that, then re-run setup.ps1.", 'exit /b 1')
}

function Get-AllowRule([string]$Sid, [string]$Rights, [string]$Inherit) {
    New-Object Security.AccessControl.FileSystemAccessRule((New-Object Security.Principal.SecurityIdentifier($Sid)), $Rights, $Inherit, 'None', 'Allow')
}

function Get-ExactAcl {
    # An exact ACL: owner Administrators, nothing inherited from the parent, SYSTEM and Administrators full control,
    # plus read-and-execute for the SIDs in -Read and modify for those in -Modify. A folder's ACL passes these
    # entries down to what it holds.
    param([switch]$Folder, [string[]]$Read = @(), [string[]]$Modify = @())
    if ($Folder) { $acl = New-Object Security.AccessControl.DirectorySecurity; $inherit = 'ContainerInherit, ObjectInherit' }
    else { $acl = New-Object Security.AccessControl.FileSecurity; $inherit = 'None' }
    $acl.SetOwner((New-Object Security.Principal.SecurityIdentifier($SidAdmins)))
    $acl.SetAccessRuleProtection($true, $false)
    $acl.AddAccessRule((Get-AllowRule $SidSystem 'FullControl' $inherit))
    $acl.AddAccessRule((Get-AllowRule $SidAdmins 'FullControl' $inherit))
    foreach ($sid in $Read) { $acl.AddAccessRule((Get-AllowRule $sid 'ReadAndExecute' $inherit)) }
    foreach ($sid in $Modify) { $acl.AddAccessRule((Get-AllowRule $sid 'Modify' $inherit)) }
    return $acl
}

function Write-ExactAcl {
    # Replace the ACL of $Path with Get-ExactAcl's.
    param([string]$Path, [string[]]$Read = @(), [string[]]$Modify = @())
    $item = Get-Item -LiteralPath $Path -Force
    Set-Acl -LiteralPath $Path -AclObject (Get-ExactAcl -Folder:$item.PSIsContainer -Read $Read -Modify $Modify)
}

function Protect-Folder {
    # Make $Path a folder SYSTEM can run code from (or write state to) safely. First, no folder above it may be one
    # another account could swap (Assert-SafeAncestors, R5-1). Created already locked when missing.
    # Refused when it, a child named in -Children (all children when omitted) or anything under one fails
    # Assert-Trusted (without -Trust: SYSTEM runs code or keeps state there), checked before any permission here
    # changes (the lock replaces inherited entries, which may be what let another account change something) and from
    # the top down (Get-UntrustedReason). Then locked (Write-ExactAcl with Users read) and those children reset to
    # owner Administrators, inherited entries only.
    param([string]$Path, [string[]]$Children)
    $every = -not $PSBoundParameters.ContainsKey('Children')
    Invoke-Change "lock $Path (owner Administrators; SYSTEM and Administrators full control; Users read only)" {
        Assert-SafeAncestors $Path   # before it is created or judged: what is above it stays as checked
        if ($null -eq (Get-LinkAttribute $Path)) {
            # Created with its final ACL in the same step. Made first and locked after, a new folder under C:\ or
            # ProgramData is open to every user in between: long enough to swap it for a junction, which the lock
            # would then follow and apply to the junction's target.
            $acl = Get-ExactAcl -Folder -Read $SidUsers
            if ($PSVersionTable.PSEdition -eq 'Core') { [IO.FileSystemAclExtensions]::Create([IO.DirectoryInfo]::new($Path), $acl) }
            else { [void][IO.Directory]::CreateDirectory($Path, $acl) }
        }
        Assert-Trusted $Path   # first: once it passed, no other account can add, remove or swap its children
        # the children to check; a link whose target is missing counts too (Test-Path would skip it)
        if ($every) { $kids = @(Get-ChildItem -LiteralPath $Path -Force | ForEach-Object { $_.FullName }) }
        else { $kids = @($Children | ForEach-Object { Join-Path $Path $_ } | Where-Object { $null -ne (Get-LinkAttribute $_) }) }
        foreach ($k in $kids) { Assert-Trusted $k -Recurse }
        Write-ExactAcl $Path -Read $SidUsers
        foreach ($k in $kids) {
            & icacls.exe $k /setowner '*S-1-5-32-544' /T /C /Q | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "icacls /setowner $k failed (exit $LASTEXITCODE)" }
            & icacls.exe $k /reset /T /C /Q | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "icacls /reset $k failed (exit $LASTEXITCODE)" }
        }
    }
}

function Protect-Secret {
    # A file with secrets in it: SYSTEM, Administrators and the SIDs in -Modify only (no Users entry).
    param([string]$Path, [string[]]$Modify = @())
    if (-not $DryRun -and $null -eq (Get-LinkAttribute $Path)) { return }   # a link with a missing target counts
    Invoke-Change "lock $Path (SYSTEM, Administrators$(if ($Modify) { ', you' }) only)" {
        Assert-Trusted $Path -Trust $Modify   # who may change it from now on may have changed it before
        Write-ExactAcl $Path -Modify $Modify
    }
}

function Install-AlertCmd {
    # alert.cmd is yours (your ntfy topic), so unlike the rest of what SYSTEM runs it is kept, not rewritten from the
    # kit: but only when $Unsafe, Get-UntrustedReason's verdict on it from before $Root was locked, is empty.
    # Otherwise it is set aside as <file>.unsafe-<time> (administrators only) for you to read, never run, and a new
    # one written. Then locked: SYSTEM runs it and it holds your topic, so administrators only.
    param([string]$Path, [string]$Unsafe)
    $template = @(
        '@echo off',
        'rem alert MESSAGE: edit the topic URL below (ntfy app on your phone, subscribe to the same topic)',
        'set NTFY_URL=https://ntfy.sh/pick-a-long-random-topic-name',
        'curl.exe -s -m 15 -o NUL -d "%~1" %NTFY_URL%')
    if (-not (Test-Path -LiteralPath $Path)) { Write-TextFile -Path $Path -Lines $template }
    elseif ($Unsafe) {
        $aside = "$Path.unsafe-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
        Write-Warning "$Unsafe. SYSTEM will not run it: it is set aside as $aside."
        Invoke-Change "set $Path aside as $aside (never run; administrators only)" {
            Move-Item -LiteralPath $Path -Destination $aside
            try {   # whatever its owner and entries: take it, reset it, lock it
                & takeown.exe /F $aside /A | Out-Null
                if ($LASTEXITCODE -ne 0) { throw "takeown failed (exit $LASTEXITCODE)" }
                & icacls.exe $aside /reset /C /Q | Out-Null
                if ($LASTEXITCODE -ne 0) { throw "icacls /reset failed (exit $LASTEXITCODE)" }
                Write-ExactAcl $aside
            }
            catch { Write-Warning "could not lock ${aside} ($_). It is not run either way; delete it once you have read it." }
        }
        Write-TextFile -Path $Path -Lines $template
        Add-NextStep "$Path was set aside as $aside, because another account could have changed it. Read it with care and copy only your ntfy topic into the new $Path."
    }
    else {
        Write-Same "$Path (exists, kept$(if ($DryRun) { ' if no other account could have changed it' }))"
        Add-NextStep "$Path was kept from before. SYSTEM runs it: check it still holds only your ntfy lines."
    }
    Protect-Secret $Path
}

function Add-FirewallRule {
    param([string]$Name, [int]$Port, [string]$Protocol = 'TCP', [string]$InterfaceAlias = '', [string]$NetProfile = '')
    if (Get-NetFirewallRule -DisplayName $Name -ErrorAction SilentlyContinue) { Write-Same "firewall rule '$Name'"; return }
    $p = @{ DisplayName = $Name; Direction = 'Inbound'; Protocol = $Protocol; LocalPort = $Port; Action = 'Allow' }
    if ($InterfaceAlias) { $p.InterfaceAlias = $InterfaceAlias }
    if ($NetProfile) { $p.Profile = $NetProfile }
    Invoke-Change "firewall: allow $Protocol $Port ($Name)" { New-NetFirewallRule @p | Out-Null }
}

# ------------------------------------------------------------------ preflight
if (-not $DryRun -and -not (Test-Admin)) { throw 'Run from an elevated PowerShell (Run as administrator), or add -DryRun to preview.' }
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { throw 'winget is missing: install "App Installer" from the Microsoft Store, then re-run.' }
# ProgramData and Program Files from Windows' known-folder API, never $env:ProgramData or $env:ProgramFiles, which your
# account's persistent environment can replace even in an elevated shell (audit R5-1). Nothing uses $env: below.
$ProgramData = Get-TrustedRoot 'CommonApplicationData'
$ProgramFiles = Get-TrustedRoot 'ProgramFiles'
if ($DryRun) { Write-Host "DRY RUN: nothing will be changed. Lane: $Lane" }

# ------------------------------------------------------------------ base
function Install-Base {
    Write-Step 'Power: never sleep, lid closed = do nothing, hibernate and Fast Startup off'
    $powerSettings = @(
        @('/change', 'standby-timeout-ac', '0'),
        @('/change', 'standby-timeout-dc', '0'),
        @('/change', 'hibernate-timeout-ac', '0'),
        @('/change', 'monitor-timeout-ac', '10'),
        @('/setacvalueindex', 'SCHEME_CURRENT', 'SUB_BUTTONS', 'LIDACTION', '0'),
        @('/setdcvalueindex', 'SCHEME_CURRENT', 'SUB_BUTTONS', 'LIDACTION', '0'),
        @('/setactive', 'SCHEME_CURRENT'),
        @('/hibernate', 'off')   # also turns off Fast Startup, which breaks Wake-on-LAN from "shut down"
    )
    foreach ($s in $powerSettings) {
        Invoke-Change "powercfg $($s -join ' ')" { & powercfg.exe @s; if ($LASTEXITCODE -ne 0) { throw "powercfg $($s -join ' ') failed" } }
    }

    Write-Step 'Wake-on-LAN on wired ports'
    $wired = @(Get-NetAdapter -Physical -ErrorAction SilentlyContinue | Where-Object { $_.PhysicalMediaType -eq '802.3' })
    if ($wired.Count -eq 0) { Write-Host '    no wired adapter found' }
    foreach ($a in $wired) {
        $pm = Get-NetAdapterPowerManagement -Name $a.Name -ErrorAction SilentlyContinue
        if ($pm -and "$($pm.WakeOnMagicPacket)" -eq 'Enabled') { Write-Same "Wake-on-LAN on $($a.Name)" }
        elseif ($pm) { Invoke-Change "enable Wake-on-LAN on $($a.Name)" { Set-NetAdapterPowerManagement -Name $a.Name -WakeOnMagicPacket Enabled } }
        Add-NextStep "Wake-on-LAN: MAC of '$($a.Name)' is $($a.MacAddress) (for heimdall's wake-watch.conf). Also enable WoL in the BIOS."
    }

    Write-Step 'OpenSSH server (keys only)'
    $cap = $null
    try { $cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' -ErrorAction Stop | Select-Object -First 1 }
    catch { Write-Host '    (cannot query Windows features without admin rights)' }
    $capName = 'OpenSSH.Server~~~~0.0.1.0'
    if ($cap) { $capName = $cap.Name }
    if ($cap -and $cap.State -eq 'Installed') { Write-Same 'OpenSSH server installed' }
    else { Invoke-Change "Add-WindowsCapability -Online -Name $capName" { Add-WindowsCapability -Online -Name $capName | Out-Null } }
    Invoke-Change 'Set-Service sshd -StartupType Automatic; Start-Service sshd' {
        Set-Service -Name sshd -StartupType Automatic
        Start-Service -Name sshd   # the first start creates the default sshd_config
    }
    $cfg = Join-Path $ProgramData 'ssh\sshd_config'
    $text = $null
    try { $text = [System.IO.File]::ReadAllText($cfg) } catch { Write-Host "    ($cfg not readable yet)" }
    # Administrators log in with ProgramData\ssh\administrators_authorized_keys (the stock "Match Group
    # administrators" block), everyone else with their own ~\.ssh\authorized_keys.
    $keyFile = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.ssh\authorized_keys'
    if ($text -match '(?m)^\s*Match\s+Group\s+administrators') { $keyFile = Join-Path $ProgramData 'ssh\administrators_authorized_keys' }
    $hasKey = (Test-Path $keyFile) -and ((Get-Item $keyFile).Length -gt 0)
    if ($null -ne $text) {
        if ($text -match '(?m)^PasswordAuthentication\s+no') { Write-Same 'password logins off' }
        elseif (-not $hasKey) {
            Write-Warning "NO SSH KEY in $keyFile. Password logins stay ON, or this headless box would lock you out."
            Write-Warning 'Add your key (next steps below), then re-run this script to turn password logins off.'
        }
        else {
            # Replace the global-section line; appending at the end would land inside the "Match" block.
            if ($text -match '(?m)^#?\s*PasswordAuthentication\s+\S+') {
                $text = $text -replace '(?m)^#?\s*PasswordAuthentication\s+\S+', 'PasswordAuthentication no'
            } else { $text = "PasswordAuthentication no`r`n" + $text }
            Invoke-Change "PasswordAuthentication no in $cfg (old one kept as $cfg.bak-<time>); restart sshd" {
                Assert-SafeAncestors $cfg   # written with admin rights: no link, and no folder above it another account could swap
                Copy-Item -LiteralPath $cfg -Destination "$cfg.bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
                [System.IO.File]::WriteAllText($cfg, $text)
                Restart-Service -Name sshd
            }
        }
    }
    Add-NextStep "SSH keys: for an administrator account, put your public key in $ProgramData\ssh\administrators_authorized_keys, then: icacls `"$ProgramData\ssh\administrators_authorized_keys`" /inheritance:r /grant `"Administrators:F`" /grant `"SYSTEM:F`""

    Write-Step 'Firewall on (all profiles)'
    $off = @(Get-NetFirewallProfile -ErrorAction SilentlyContinue | Where-Object { -not $_.Enabled })
    if ($off.Count -eq 0) { Write-Same 'firewall on' }
    else { Invoke-Change 'Set-NetFirewallProfile -Enabled True' { Set-NetFirewallProfile -Profile Domain, Private, Public -Enabled True } }

    Write-Step 'Tailscale'
    Install-WingetPackage -Id 'Tailscale.Tailscale'
    Add-NextStep "Tailscale: & `"$ProgramFiles\Tailscale\tailscale.exe`" up --unattended   (unattended = stays connected with nobody signed in). Approve the link, then disable key expiry or tag the machine (servers\tailscale.md)."

    Add-NextStep 'Updates: Windows Update is on by default; set Active Hours so it never reboots mid-job. Monthly: winget upgrade --all'
    Add-NextStep 'BIOS: power on after AC loss = On; Wake-on-LAN = On; ErP/deep sleep = Off; battery charge limit if offered (servers\common.md).'
    Add-NextStep 'Battery: kept at 100% 24/7 it can swell. Look at it monthly.'
}

function Install-DockerDesktop {
    Write-Step 'Docker Desktop (needs WSL2 and a signed-in user)'
    Install-WingetPackage -Id 'Docker.DockerDesktop'
    Add-NextStep 'Docker Desktop: open it once, accept the terms, Settings > General > "Start Docker Desktop when you sign in". For a headless box turn on automatic sign-in (Sysinternals Autologon) and read the tradeoff in servers\common.md.'
}

function Copy-Stack([string]$Name) {
    Write-Step "Compose files -> $Root\$Name"
    $src = Join-Path $Kit "compose\$Name"
    $dest = "$Root\$Name"
    # written with admin rights, so no link may stand where the files go; you (the operator) may change the folder:
    # Docker Desktop, running as you, keeps the services' data in it
    Invoke-Change "lock $dest (SYSTEM, Administrators and you may change it; Users read)" {
        if (-not (Test-Path -LiteralPath $dest)) { New-Item -ItemType Directory -Path $dest | Out-Null }
        Assert-Trusted $dest -Trust $SidMe   # SYSTEM runs nothing in here, and you may change it from now on anyway
        foreach ($f in (Get-ChildItem -Path $src -File -Force)) { Assert-Trusted (Join-Path $dest $f.Name) -Trust $SidMe }
        Assert-Trusted (Join-Path $dest '.env') -Trust $SidMe
        Write-ExactAcl $dest -Read $SidUsers -Modify $SidMe
    }
    foreach ($f in (Get-ChildItem -Path $src -File -Force)) {
        $lines = [System.IO.File]::ReadAllLines($f.FullName)
        if ($f.Name -eq 'compose.yaml' -and $Name -eq 'heimdall') {
            # Docker Desktop: publish ports on 127.0.0.1 instead of host networking
            $lines = $lines | Where-Object { $_ -notmatch 'network_mode: host' } | ForEach-Object { $_ -replace '^    # ports: ', '    ports: ' }
        }
        Write-TextFile -Path (Join-Path "$Root\$Name" $f.Name) -Lines $lines
    }
    $envFile = Join-Path "$Root\$Name" '.env'
    if (Test-Path $envFile) { Write-Same "$envFile (exists, kept)" }
    else {
        $lines = [System.IO.File]::ReadAllLines((Join-Path $src 'env.example')) | ForEach-Object { $_ -replace '^NTFY_BASE_URL=.*', "NTFY_BASE_URL=http://$([Environment]::MachineName.ToLower()):8090" }
        Write-TextFile -Path $envFile -Lines $lines
        Add-NextStep "Edit $envFile (TZ: an IANA name like Europe/Berlin)."
    }
    Protect-Secret $envFile -Modify $SidMe   # passwords go in there: not readable by other accounts
}

# ------------------------------------------------------------------ lanes
function Install-Media {
    Write-Step 'Jellyfin'
    Install-WingetPackage -Id 'Jellyfin.Server'
    Add-FirewallRule -Name 'Jellyfin (home network)' -Port 8096 -NetProfile 'Private'
    Add-FirewallRule -Name 'Jellyfin (tailnet)' -Port 8096 -InterfaceAlias 'Tailscale'
    Add-NextStep 'Jellyfin: open http://localhost:8096 and finish setup. For a box nobody signs in to, reinstall Jellyfin choosing "Install as a Service" in its installer.'
    Add-NextStep 'Set your home network to Private (Settings > Network > Properties) so TVs can reach Jellyfin.'
    if ($WithDownloads) {
        Write-Step 'Download stack (native)'
        foreach ($id in 'TeamSonarr.Sonarr', 'TeamRadarr.Radarr', 'TeamProwlarr.Prowlarr', 'qBittorrent.qBittorrent') { Install-WingetPackage -Id $id }
        Add-NextStep 'KILLSWITCH: install your VPN app, then qBittorrent > Options > Advanced > Network interface = the VPN adapter. Without that, a VPN drop leaks your real address. (profiles\loki\README.md)'
        Add-NextStep 'Sonarr/Radarr/Prowlarr web UIs: 8989 / 7878 / 9696 on localhost; set a login in each (Settings > General > Authentication).'
    }
    Add-NextStep 'Tag this machine tag:quarantine in the Tailscale admin console if it runs downloads (servers\tailscale.md).'
}

function Install-Heimdall {
    Install-DockerDesktop
    Write-Step "Lock ${Root}: SYSTEM runs wake-watch from it, so only administrators may change what is in it"
    $alertCmd = Join-Path $Root 'alert.cmd'
    $alertUnsafe = ''
    if (-not $DryRun) {
        Assert-SafeAncestors $Root   # no folder above it another account could swap (R5-1)
        Assert-Trusted $Root   # then: once it passed, no other account can swap alert.cmd before or after it is judged
        # Judged now, before the lock below replaces its inherited entries, which may be what let another account change it.
        $alertUnsafe = Get-UntrustedReason $alertCmd
    }
    Protect-Folder $Root -Children 'tools', 'wake-watch.cmd'
    Copy-Stack 'heimdall'
    Add-NextStep "Start: cd $Root\heimdall; docker compose up -d   then share with the tailnet: tailscale serve --bg --http=3001 http://127.0.0.1:3001 (and 8090 for ntfy)"

    Write-Step 'wake-watch every 2 minutes (Task Scheduler, runs as SYSTEM, no sign-in needed)'
    # The task runs as SYSTEM, so it needs the machine-wide Python (a per-user one is invisible to it).
    $py = Join-Path $ProgramFiles 'Python313\python.exe'
    if (Test-Path $py) { Write-Same "machine-wide Python: $py" }
    else {
        Invoke-Change 'winget install Python.Python.3.13 --scope machine' {
            & winget install --id Python.Python.3.13 --exact --silent --scope machine --accept-package-agreements --accept-source-agreements
            if (-not (Test-Path $py)) {
                throw "machine-wide Python not found at $py (winget exit $LASTEXITCODE). A per-user Python 3.13 can block it: uninstall that one, or run the python.org installer with 'Install for all users', then re-run."
            }
        }
    }
    $data = Join-Path $ProgramData 'wake-watch'
    $conf = Join-Path $data 'wake-watch.conf'
    $wrapper = Join-Path $Root 'wake-watch.cmd'
    try {
        # first: a path CMD would read as syntax stops the run before anything SYSTEM runs is written (R5-1)
        $wrapperLines = Get-WakeWatchWrapper $py $conf $data $alertCmd "$Root\tools\wake-watch"
        if (-not $DryRun) { Assert-PythonTrusted (Split-Path -Parent $py) }
        Protect-Folder $data   # SYSTEM writes its state there: users must not plant files or links in it
    }
    catch {
        $refused = $_
        try { Stop-WakeWatch $wrapper 'its Python, its state folder or a path in its wrapper' } catch { Write-Warning "could not turn wake-watch off ($_): disable the task homeserver-wake-watch by hand" }
        throw $refused
    }
    Write-TextFile -Fresh -Path "$Root\tools\wake-watch" -Lines ([System.IO.File]::ReadAllLines((Join-Path $Kit 'tools\wake-watch')))
    if (Test-Path $conf) { Write-Same "$conf (exists, kept)" }
    else {
        Write-TextFile -Path $conf -Lines @(
            '# wake-watch hosts: name  address-to-probe  wired-MAC',
            '#thor  thor  XX:XX:XX:XX:XX:XX',
            '#BROADCAST=255.255.255.255')
    }
    Install-AlertCmd $alertCmd $alertUnsafe
    # Python with -I -S ($PyIsolation): no site initialization, no PYTHON* variables, nothing from the script's folder
    Write-TextFile -Fresh -Path $wrapper -Lines $wrapperLines

    if (Get-ScheduledTask -TaskName 'homeserver-wake-watch' -ErrorAction SilentlyContinue) { Write-Same 'scheduled task homeserver-wake-watch' }
    else {
        Invoke-Change 'Register-ScheduledTask homeserver-wake-watch (every 2 min + at startup, as SYSTEM)' {
            $action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$wrapper`" check"
            $every = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 2)
            $boot = New-ScheduledTaskTrigger -AtStartup
            $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
            $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -ExecutionTimeLimit (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
            Register-ScheduledTask -TaskName 'homeserver-wake-watch' -Action $action -Trigger @($every, $boot) `
                -Principal $principal -Settings $settings -Description 'wake-watch: Wake-on-LAN recovery' | Out-Null
        }
    }
    Add-NextStep "wake-watch: add boxes to $conf (an elevated editor: only administrators can change it), then check from an elevated prompt: $wrapper status"
    Add-NextStep "Alerts: in an elevated editor, put your ntfy topic in $alertCmd (only administrators can read or change it); test from an elevated PowerShell: & `"$alertCmd`" `"hello`""
}

function Install-Workhorse {
    Write-Step 'Ollama (reachable from the tailnet, not the home network)'
    Install-WingetPackage -Id 'Ollama.Ollama'
    $vars = [ordered]@{ OLLAMA_HOST = '0.0.0.0:11434'; OLLAMA_KEEP_ALIVE = '5m'; OLLAMA_NUM_PARALLEL = '1' }
    foreach ($k in $vars.Keys) {
        if ([Environment]::GetEnvironmentVariable($k, 'Machine') -eq $vars[$k]) { Write-Same "$k=$($vars[$k])" }
        else { Invoke-Change "set machine variable $k=$($vars[$k])" { [Environment]::SetEnvironmentVariable($k, $vars[$k], 'Machine') } }
    }
    Add-FirewallRule -Name 'Ollama (tailnet only)' -Port 11434 -InterfaceAlias 'Tailscale'
    Add-NextStep 'Ollama: quit and reopen the Ollama app (tray) so it picks up OLLAMA_HOST. It runs only while a user is signed in.'
    Add-NextStep 'Pull a model: ollama pull qwen3:8b   (profiles\thor\README.md: size it to your RAM/VRAM)'
    Add-NextStep 'BIOS: "Restore on AC power loss" = Power On.'
}

Install-Base
switch ($Lane) {
    'loki' { Install-Media }
    'heimdall' { Install-Heimdall }
    'thor' { Install-Workhorse }
}

Write-Step "Done ($Lane). Next steps:"
if ($DryRun) { Write-Host '    (dry run: nothing was changed)' }
foreach ($n in $NextSteps) { Write-Host "    - $n" }
