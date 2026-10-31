<#
.SYNOPSIS
  Checks for servers\windows\setup.ps1 that need no Windows: pwsh on Linux or macOS runs them.
    pwsh -NoProfile -File servers/windows/test_setup.ps1
    pwsh -NoProfile -File servers/windows/test_setup.ps1 -CPython /src/cpython-3.13.15,/src/cpython-3.14.7
  -CPython: CPython source trees (a git checkout of a v3.13.x or v3.14.x tag, or just its Modules\getpath.py and
  Lib\test\test_getpath.py) for section 9's proof with CPython's own path setup; needs a python3 here.
  1. setup.ps1 parses, with no PowerShell 7-only operator (it must run in Windows PowerShell 5.1)
  2. Get-UnsafeReason, the rule for "could another account have changed this", on hand-made owners and entries:
     the exact ACLs setup.ps1 writes, a Program Files folder and deny or read-only entries pass; an administrator-owned
     item that gives Users, Authenticated Users, Everyone or another account write, append, delete, delete-children,
     change-permissions, take-ownership, generic write or generic all fails, inherited or only for what a folder
     holds; so does a foreign owner. You (the operator) count as another account for what SYSTEM runs: owned by you,
     or an administrator-owned item with an entry letting you modify it, fails; only with -Trust (Compose files and
     .env, which SYSTEM never runs) do those pass, and there the other accounts still fail
  3. Get-UntrustedReason on a real folder tree with mocked ACLs: the first unsafe item is named, one whose permissions
     cannot be read is unsafe too, a folder is searched only after it passed, a link stops it
  4. Protect-Folder with mocked ACL and icacls calls: every check runs before the lock and the lock before the resets;
     a write entry the lock would wipe (inherited from an older, open parent) still stops it, with nothing locked
  5. the review's case: an administrator-owned alert.cmd that let Users change it, holding an injected marker, under a
     C:\homeserver that passes: it is set aside (marker kept there for reading), alert.cmd is a fresh template that
     never holds the marker, and SYSTEM's wrapper and script are new files from the kit; a safe alert.cmd is kept
  6. Install-Heimdall checks C:\homeserver itself, then judges alert.cmd, both before locking C:\homeserver; rewrites
     the wrapper and script with -Fresh; runs Python with -I and checks every file in Python's folder; a refusal says
     that removing the access would not undo what was changed (so it does not lead to a clean-looking alert.cmd);
     only Compose files and .env trust you (-Trust), never C:\homeserver, alert.cmd, Protect-Folder or Python
  7. the round-3 review's case, on a real Python313 tree with mocked ACLs (Assert-PythonTrusted, what Install-Heimdall
     runs): administrator-owned with an explicit Modify entry for you on Lib\site-packages, and owned by you, both
     fail with the reinstall-for-all-users instruction; a clean tree passes. The same entry for you passes on a
     Compose folder and .env; an alert.cmd only you could change is set aside; a tools file owned by you stops
     Protect-Folder before anything is locked. The refusal tells you to delete what the uninstaller leaves. When the
     Python or the state folder is refused, SYSTEM's existing wrapper is replaced by one that runs nothing (wake-watch
     off until a re-run passes), only after C:\homeserver was locked, and the refusal still stops the run
  8. the round-4 review's case: a protected .pth file can name a folder outside the checked tree. The wrapper SYSTEM
     runs (Get-WakeWatchWrapper) starts Python with -I -S and first clears PYTHONHOME, PYTHONPATH, PYTHONEXECUTABLE and
     __PYVENV_LAUNCHER__ (Python's path setup reads the last two even with -I). With a python3 here, the real startup:
     a protected site-packages .pth naming an operator-writable folder, with an import line, code imported from that
     folder and a sitecustomize there, runs all three under the round-3 flags (-I) and none under the wrapper's, also
     with PYTHONPATH and PYTHONHOME set. Assert-PythonTrusted refuses a ._pth file beside python.exe or its DLL, a
     pyvenv.cfg beside python.exe or one folder up, and a folder above that another account may add files to;
     a refusal turns wake-watch off like the others. It also refuses a python313.dll older than 3.13.15 (file version
     3.13.15150), or none: before that, a ..\..\Modules\Setup.local any account may create (C:\Modules) makes
     Python load its standard library from ..\..\Lib, -I -S or not (CVE-2026-12003); 3.13.15 and later pass
  9. the round-5 review's S2 case and every other file CPython's getpath.py (3.13, 3.14) reads beside or above
     python.exe that moves it out of its folder, from one table: a pybuilddir.txt beside python.exe (empty or not),
     a ._pth file beside the DLL or python.exe, a pyvenv.cfg beside it or one folder up, and no Lib\os.py, os.pyc or
     python313.zip (Python then searches the folders above, up to C:\Lib) are refused; a stock machine-wide layout,
     and one with only os.pyc or only python313.zip, passes. A refused pybuilddir.txt turns SYSTEM's existing
     wrapper off (Install-Heimdall's own try block). With -CPython, CPython's Lib\test\test_getpath.py
     MockNTNamespace runs that tree's real Modules\getpath.py on the same table under -I -S: each refused layout
     moves prefix, the standard library, sys.path or site out of C:\Program Files\Python313 (C:\Lib for
     pybuilddir.txt) and each accepted one stays inside, with C:\Lib, C:\Modules\Setup.local and the HKCU PythonPath
     key all present; __PYVENV_LAUNCHER__ and PYTHONEXECUTABLE would move it (the wrapper clears them), PYTHONHOME
     would not (-I); a getpath.py from before 3.13.15 moves even the stock layout (Setup.local), as the version
     check assumes
 10. the round-5 review's R5-1 case: setup.ps1 reads no environment variable (no env: in its code); ProgramData and
     Program Files come from the known-folder API, so a hostile ProgramData value (C:\Users\operator\pd&whoami&rem)
     changes nothing, and a known folder holding % ! " ^ & | < > or a line break, or not on Windows' own drive
     (GetSystemDirectory's: another letter may be a mapped share or a removable disk), is refused; the
     wrapper sets each path as set "NAME=value" and refuses (never escapes) a path with one of those characters,
     which also turns the existing wrapper off. Every folder above a privileged path must exist, be no link and
     belong to SYSTEM, Administrators or TrustedInstaller with no entry letting another account delete, rename or
     re-permission it or delete what it holds: a stock C:\, ProgramData and Program Files pass (users may add
     folders there; the leaf rule would refuse that); a plain user-owned parent and a junction above ProgramData or
     Program Files are refused before anything is created, judged or locked
#>
param([string]$CPython = '')
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here 'setup.ps1'
$results = New-Object System.Collections.Generic.List[object]
function Check([string]$Name, [bool]$Ok, [string]$Detail = '') {
    $results.Add([pscustomobject]@{ Name = $Name; Ok = $Ok })
    if ($Ok) { Write-Host "ok   $Name" } else { Write-Host "FAIL $Name$(if ($Detail) { "`n     $Detail" })" }
}

# ------------------------------------------------------------------ 1. parse
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($script, [ref]$tokens, [ref]$errors)
Check 'setup.ps1 parses' ($errors.Count -eq 0) (($errors | ForEach-Object { "$($_.Extent.StartLineNumber): $($_.Message)" }) -join '; ')
$ps7 = @('QuestionDot', 'QuestionLBracket', 'QuestionQuestion', 'QuestionQuestionEquals', 'QuestionMark', 'AndAnd', 'OrOr')
$bad = @($tokens | Where-Object { $ps7 -contains "$($_.Kind)" } | ForEach-Object { "$($_.Extent.StartLineNumber): $($_.Text)" })
Check 'no PowerShell 7-only operators (?. ?? ?: && ||)' ($bad.Count -eq 0) ($bad -join '; ')

# Load the pieces under test from setup.ps1 itself (its body installs things, so it is not run).
$want = 'Write-Same', 'Add-NextStep', 'Invoke-Change', 'Write-TextFile', 'Get-LinkAttribute', 'Get-UnsafeReason',
'Get-UntrustedReason', 'Assert-Trusted', 'Assert-PythonTrusted', 'Protect-Folder', 'Protect-Secret', 'Install-AlertCmd',
'Stop-WakeWatch', 'Get-PythonRedirect', 'Get-WakeWatchWrapper', 'Get-PythonCoreVersion', 'Get-KnownFolderPath',
'Get-CmdUnsafeReason', 'Get-TrustedRoot', 'Get-AncestorReason', 'Assert-SafeAncestors', 'Get-WindowsDrive'
$defs = @{}
foreach ($f in $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) { $defs[$f.Name] = $f }
foreach ($n in $want) { . ([scriptblock]::Create($defs[$n].Extent.Text)) }
foreach ($a in $ast.EndBlock.Statements | Where-Object { $_ -is [System.Management.Automation.Language.AssignmentStatementAst] }) {
    if (@('$SidSystem', '$SidAdmins', '$SidUsers', '$SidTrustedInstaller', '$SidOwnerStandIns', '$WriteRights', '$PyIsolation',
            '$PyEnvClear', '$RedirectRights') -contains $a.Left.Extent.Text) {
        . ([scriptblock]::Create($a.Extent.Text))
    }
}
$SidMe = 'S-1-5-21-1000-1000-1000-1001'   # the operator
$DryRun = $false
$NextSteps = New-Object System.Collections.Generic.List[string]

# ------------------------------------------------------------------ 2. the rule
$auth = 'S-1-5-11'; $everyone = 'S-1-1-0'; $stranger = 'S-1-5-21-1000-1000-1000-1002'
$FC = 0x1F01FF; $Modify = 0x1301BF; $RX = 0x1200A9
function Ace([string]$Sid, [int64]$Rights, [bool]$Allow = $true, [switch]$InheritOnly) {
    [pscustomobject]@{ Sid = $Sid; Rights = $Rights; Allow = $Allow; InheritOnly = [bool]$InheritOnly }
}
$exact = @((Ace $SidSystem $FC), (Ace $SidAdmins $FC), (Ace $SidUsers $RX))
$safe = [ordered]@{
    'exact ACL setup.ps1 writes (Users read)'       = @($SidAdmins, $exact)
    'owned by SYSTEM'                                = @($SidSystem, $exact)
    'Program Files\Python313 (TrustedInstaller, CREATOR OWNER, generic read)' = @($SidAdmins, @((Ace $SidTrustedInstaller $FC),
            (Ace $SidSystem $FC), (Ace $SidAdmins $FC), (Ace $SidUsers $RX), (Ace $SidUsers 0xA0000000), (Ace 'S-1-3-0' 0x10000000),
            (Ace 'S-1-15-2-1' $RX)))
    'Users denied full control'                      = @($SidAdmins, @($exact + (Ace $SidUsers $FC $false)))
    'Users read, synchronize and read permissions'   = @($SidAdmins, @($exact + (Ace $SidUsers 0x120089)))
}
foreach ($k in $safe.Keys) { $r = Get-UnsafeReason $safe[$k][0] $safe[$k][1]; Check "passes: $k" ($r -eq '') $r }
$unsafe = [ordered]@{
    'administrator-owned, Users modify (the review case)' = @($SidAdmins, @($exact + (Ace $SidUsers $Modify)))
    'Authenticated Users modify, inherited from C:\'      = @($SidAdmins, @($exact + (Ace $auth $Modify)))
    'Everyone full control'                               = @($SidAdmins, @($exact + (Ace $everyone $FC)))
    'another account write data only'                     = @($SidAdmins, @($exact + (Ace $stranger 0x2)))
    'Users append only'                                   = @($SidAdmins, @($exact + (Ace $SidUsers 0x4)))
    'Users write attributes only'                         = @($SidAdmins, @($exact + (Ace $SidUsers 0x100)))
    'Users delete only'                                   = @($SidAdmins, @($exact + (Ace $SidUsers 0x10000)))
    'Users delete children (a folder)'                    = @($SidAdmins, @($exact + (Ace $SidUsers 0x40)))
    'Users change permissions'                            = @($SidAdmins, @($exact + (Ace $SidUsers 0x40000)))
    'Users take ownership'                                = @($SidAdmins, @($exact + (Ace $SidUsers 0x80000)))
    'Users generic write (only for what a folder holds)'  = @($SidAdmins, @($exact + (Ace $SidUsers 0x40000000)))
    'Users generic all'                                   = @($SidAdmins, @($exact + (Ace $SidUsers 0x10000000)))
    'Users generic read and write'                        = @($SidAdmins, @($exact + (Ace $SidUsers ([int64][int]-1073741824))))
    'ProgramData folder: Users add files and folders'     = @($SidAdmins, @($exact + (Ace $SidUsers 0x116)))
    'CREATOR GROUP full control'                          = @($SidAdmins, @($exact + (Ace 'S-1-3-1' $FC)))
    'owned by another account, entries clean'             = @($stranger, $exact)
    'what SYSTEM runs: administrator-owned, you allowed to modify' = @($SidAdmins, @($exact + (Ace $SidMe $Modify)))
    'what SYSTEM runs: you may write data only'           = @($SidAdmins, @($exact + (Ace $SidMe 0x2)))
    'what SYSTEM runs: owned by you, entries clean'       = @($SidMe, $exact)
}
foreach ($k in $unsafe.Keys) { $r = Get-UnsafeReason $unsafe[$k][0] $unsafe[$k][1]; Check "fails: $k" ($r -ne '') }
$r = Get-UnsafeReason $SidAdmins @($exact + (Ace $SidMe $Modify))
Check 'the reason names your account and why it counts' ($r -like "$SidMe (your account, so anything you run without elevation too) may write*") $r
$r = Get-UnsafeReason $SidMe $exact
Check 'owned by you: the reason says so' ($r -like "it belongs to your account ($SidMe)*without elevation*") $r
# data SYSTEM never runs (Compose files, .env): you may change it from now on anyway, so -Trust you
$data = [ordered]@{
    'data, -Trust you: exact ACL with you allowed to modify (.env)' = @($SidAdmins, @($exact + (Ace $SidMe $Modify)))
    'data, -Trust you: owned by you'                               = @($SidMe, $exact)
}
foreach ($k in $data.Keys) { $r = Get-UnsafeReason $data[$k][0] $data[$k][1] -Trust $SidMe; Check "passes: $k" ($r -eq '') $r }
$r = Get-UnsafeReason $SidMe @($exact + (Ace $SidUsers $Modify)) -Trust $SidMe
Check 'fails: data, -Trust you: Users modify still fails' ($r -like "$SidUsers may write*") $r
$r = Get-UnsafeReason $stranger $exact -Trust $SidMe
Check 'fails: data, -Trust you: another account still fails' ($r -ne '') $r

# ------------------------------------------------------------------ mocks for what touches Windows ACLs
$Acl = @{}          # path -> facts; anything not listed gets the exact ACL
$Calls = New-Object System.Collections.Generic.List[string]
$WipeOnLock = $false
function Get-AclFacts([string]$Path) {
    $Calls.Add("acl $Path")
    if ($Acl[$Path] -eq 'unreadable') { throw 'Attempted to perform an unauthorized operation.' }
    if ($Acl.ContainsKey($Path)) { return $Acl[$Path] }
    [pscustomobject]@{ Owner = $SidAdmins; Rules = $exact }
}
function Write-ExactAcl([string]$Path) {
    $Calls.Add("lock $Path")
    if ($WipeOnLock) { $Acl.Clear() }   # what a real lock does to inherited entries below it
}
function icacls.exe { $Calls.Add("icacls $($args -join ' ')"); $global:LASTEXITCODE = 0 }
function takeown.exe { $Calls.Add("takeown $($args -join ' ')"); $global:LASTEXITCODE = 0 }
# python313.dll's file version: Windows' version resource, which .NET reads only on Windows. 3.13.15 unless a check says.
$RealCoreVersion = ${function:Get-PythonCoreVersion}
$PyCore = [Version]'3.13.15150'
function Get-PythonCoreVersion([string]$Dir) { $PyCore }
$open = [pscustomobject]@{ Owner = $SidAdmins; Rules = @($exact + (Ace $SidUsers $Modify)) }
# the known-folder API: Windows' answer for each folder, unless a check says otherwise
$KnownFolders = @{ CommonApplicationData = 'C:\ProgramData'; ProgramFiles = 'C:\Program Files' }
function Get-KnownFolderPath([string]$Name) { $KnownFolders[$Name] }
$WindowsDrive = 'C:\'   # GetSystemDirectory's drive, unless a check says otherwise
function Get-WindowsDrive { $WindowsDrive }

$tmpBase = [IO.Path]::GetTempPath()
$realpath = Get-Command realpath -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if ([IO.Path]::DirectorySeparatorChar -eq '/' -and $realpath) {
    $tmpBase = "$(& $realpath.Source $tmpBase)".Trim()   # macOS: /var is a link to /private/var, which the folder-above check refuses
}
$tmp = Join-Path $tmpBase "kit-win-test-$([guid]::NewGuid().ToString('N'))"
function New-Tree {
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    $root = Join-Path $tmp 'homeserver'
    New-Item -ItemType Directory -Force (Join-Path (Join-Path $root 'tools') 'sub') | Out-Null
    Set-Content -LiteralPath (Join-Path $root 'wake-watch.cmd') -Value 'old wrapper'
    Set-Content -LiteralPath (Join-Path (Join-Path $root 'tools') 'wake-watch') -Value 'old script'
    Set-Content -LiteralPath (Join-Path $root 'alert.cmd') -Value "@echo off`r`necho KIT-INJECTED-MARKER > C:\pwned.txt"
    $Acl.Clear(); $Calls.Clear(); $NextSteps.Clear(); $script:WipeOnLock = $false
    return $root
}
function Index([string]$Prefix) { for ($i = 0; $i -lt $Calls.Count; $i++) { if ($Calls[$i].StartsWith($Prefix)) { return $i } }; return -1 }
function LastIndex([string]$Prefix) { for ($i = $Calls.Count - 1; $i -ge 0; $i--) { if ($Calls[$i].StartsWith($Prefix)) { return $i } }; return -1 }

try {
    # -------------------------------------------------------------- 3. Get-UntrustedReason on a tree
    $root = New-Tree
    $tools = Join-Path $root 'tools'; $sub = Join-Path $tools 'sub'
    Set-Content -LiteralPath (Join-Path $sub 'x.py') -Value 'x'
    Check 'a clean tree passes' ((Get-UntrustedReason $root -Recurse) -eq '')
    $Acl[(Join-Path $sub 'x.py')] = $open
    $r = Get-UntrustedReason $root -Recurse
    Check 'an open file deep in the tree is named' ($r -like "*x.py: $SidUsers may write*") $r
    $Acl.Clear(); $Calls.Clear(); $Acl[$tools] = $open
    $r = Get-UntrustedReason $root -Recurse
    Check 'an open folder is named and not searched' (($r -like "*tools: *") -and -not ($Calls | Where-Object { $_ -like "acl $sub*" })) "$r | $($Calls -join ', ')"
    $Acl.Clear(); $Calls.Clear()
    $Acl[(Join-Path $sub 'x.py')] = 'unreadable'
    $r = Get-UntrustedReason $root -Recurse
    Check 'an item whose permissions cannot be read is untrusted' ($r -like '*x.py: its permissions cannot be read*') $r
    $Acl.Clear(); $Calls.Clear()
    [void](Get-UntrustedReason $root -Recurse); $order = @($Calls)
    $ok = $true
    foreach ($c in $order) { $p = $c.Substring(4); $parent = Split-Path -Parent $p; if ($p -ne $root -and ([array]::IndexOf($order, "acl $parent") -gt [array]::IndexOf($order, $c))) { $ok = $false } }
    Check 'every folder is checked before what it holds' $ok ($order -join ', ')
    $link = Join-Path $tools 'link'
    $linked = $true
    try { New-Item -ItemType SymbolicLink -Path $link -Target $sub | Out-Null } catch { $linked = $false }
    if ($linked) {
        $threw = ''
        try { [void](Get-UntrustedReason $root -Recurse) } catch { $threw = "$_" }
        Check 'a link stops it' ($threw -like '*is a link*') $threw
        Remove-Item -LiteralPath $link -Force
    }
    else { Write-Host 'skip a link stops it (no symlinks here)' }

    # -------------------------------------------------------------- 4. Protect-Folder order
    $root = New-Tree
    Protect-Folder $root -Children 'tools', 'wake-watch.cmd'
    $lock = Index "lock $root"
    Check 'Protect-Folder: all checks before the lock, the lock before the resets' (($lock -gt (LastIndex 'acl ')) -and ($lock -lt (Index 'icacls ')) -and ((Index 'icacls ') -ge 0)) ($Calls -join ', ')
    Check 'Protect-Folder: alert.cmd is not among the reset children' (-not ($Calls | Where-Object { $_ -like 'icacls *alert.cmd*' }))

    $root = New-Tree
    $Acl[(Join-Path (Join-Path $root 'tools') 'wake-watch')] = $open; $WipeOnLock = $true
    $threw = ''
    try { Protect-Folder $root -Children 'tools', 'wake-watch.cmd' } catch { $threw = "$_" }
    Check 'Protect-Folder: a write entry the lock would wipe still stops it, nothing locked or reset' (($threw -like '*wake-watch: *may write*') -and (Index 'lock ') -lt 0 -and (Index 'icacls ') -lt 0) "$threw | $($Calls -join ', ')"

    $root = New-Tree
    $Acl[$root] = [pscustomobject]@{ Owner = $SidAdmins; Rules = @($exact + (Ace $auth $Modify)) }
    $threw = ''
    try { Protect-Folder $root -Children 'tools', 'wake-watch.cmd' } catch { $threw = "$_" }
    Check 'Protect-Folder: an open C:\homeserver (an older install) is refused before its children are read' (($threw -like "*${auth} may write*") -and -not ($Calls | Where-Object { $_ -like "acl ${root}?*" }) -and (Index 'lock ') -lt 0) "$threw | $($Calls -join ', ')"

    # -------------------------------------------------------------- 5. the review's case, in Install-Heimdall's order
    $root = New-Tree
    $alert = Join-Path $root 'alert.cmd'
    $Acl[$alert] = $open; $WipeOnLock = $true   # the lock below would wipe the entry that let Users change it
    $unsafe = Get-UntrustedReason $alert
    Protect-Folder $root -Children 'tools', 'wake-watch.cmd'
    Check 'review case: judged after the lock instead, alert.cmd would pass (why the order matters)' ((Get-UntrustedReason $alert) -eq '')
    Install-AlertCmd $alert $unsafe
    Write-TextFile -Fresh -Path (Join-Path (Join-Path $root 'tools') 'wake-watch') -Lines @('kit script')
    Write-TextFile -Fresh -Path (Join-Path $root 'wake-watch.cmd') -Lines @('kit wrapper')
    $aside = @(Get-ChildItem -LiteralPath $root -Filter 'alert.cmd.unsafe-*')
    $now = [IO.File]::ReadAllText($alert)
    Check 'review case: alert.cmd is set aside with the marker, for reading' (($aside.Count -eq 1) -and ([IO.File]::ReadAllText($aside[0].FullName) -like '*KIT-INJECTED-MARKER*')) "$($aside.Count)"
    Check 'review case: alert.cmd is a fresh template without the marker' (($now -notlike '*MARKER*') -and ($now -like '*ntfy.sh/pick-a-long-random-topic-name*')) $now
    Check 'review case: the set-aside copy is taken, reset and locked' (((Index 'takeown ') -ge 0) -and ((Index "icacls $($aside[0].FullName) /reset") -ge 0) -and ((Index "lock $($aside[0].FullName)") -ge 0)) ($Calls -join ', ')
    Check 'review case: the next steps say it was set aside' (@($NextSteps | Where-Object { $_ -like '*set aside*' }).Count -eq 1)
    Check 'review case: wrapper and script are new files from the kit' (([IO.File]::ReadAllText((Join-Path $root 'wake-watch.cmd')) -eq "kit wrapper`n") -and ([IO.File]::ReadAllText((Join-Path (Join-Path $root 'tools') 'wake-watch')) -eq "kit script`n"))

    $root = New-Tree
    $alert = Join-Path $root 'alert.cmd'
    Set-Content -LiteralPath $alert -Value 'my own alert'
    Install-AlertCmd $alert (Get-UntrustedReason $alert)
    Check 'a safe alert.cmd is kept and locked' (([IO.File]::ReadAllText($alert) -like 'my own alert*') -and ((Index "lock $alert") -ge 0) -and -not (Get-ChildItem -LiteralPath $root -Filter 'alert.cmd.unsafe-*'))
    Remove-Item -LiteralPath $alert
    Install-AlertCmd $alert ''
    Check 'a missing alert.cmd gets the template' ([IO.File]::ReadAllText($alert) -like '*pick-a-long-random-topic-name*')

    # -------------------------------------------------------------- 6. Install-Heimdall wiring
    $h = $defs['Install-Heimdall'].Extent.Text
    Check 'Install-Heimdall judges alert.cmd before locking C:\homeserver' (($h.IndexOf('Get-UntrustedReason $alertCmd') -ge 0) -and ($h.IndexOf('Get-UntrustedReason $alertCmd') -lt $h.IndexOf('Protect-Folder $Root')))
    Check "Install-Heimdall does not keep alert.cmd among Protect-Folder's children" ($h -notmatch "Protect-Folder \`$Root[^\r\n]*alert\.cmd")
    Check 'Install-Heimdall writes the script and the wrapper with -Fresh' (($h -match 'Write-TextFile -Fresh -Path "\$Root\\tools\\wake-watch"') -and ($h -match 'Write-TextFile -Fresh -Path \$wrapper'))
    Check 'the wrapper comes from Get-WakeWatchWrapper' (($h -match '\$wrapperLines = Get-WakeWatchWrapper \$py ') -and ($h -match 'Write-TextFile -Fresh -Path \$wrapper -Lines \$wrapperLines'))
    Check 'Install-Heimdall checks every file in the Python folder SYSTEM runs from' ($h -match 'Assert-PythonTrusted \(Split-Path -Parent \$py\)')
    Check 'Assert-PythonTrusted checks every file in it, trusting no one else' ($defs['Assert-PythonTrusted'].Extent.Text -match 'Get-UntrustedReason \$Dir -Recurse\r?\n')
    $trusting = '(?m)^[^#\r\n]*(Assert-Trusted|Get-UntrustedReason)[^#\r\n]*-Trust'   # in code, not comments
    Check 'Install-Heimdall and Protect-Folder trust no one else (no -Trust)' (($h -notmatch $trusting) -and ($defs['Protect-Folder'].Extent.Text -notmatch $trusting))
    $cs = $defs['Copy-Stack'].Extent.Text
    Check 'Copy-Stack trusts you for the Compose folder, its files and .env (Docker Desktop runs as you)' ((([regex]::Matches($cs, 'Assert-Trusted [^\r\n]* -Trust \$SidMe')).Count -eq 3) -and ($cs -match 'Protect-Secret \$envFile -Modify \$SidMe'))
    Check 'Protect-Secret trusts exactly whom it lets modify' ($defs['Protect-Secret'].Extent.Text -match 'Assert-Trusted \$Path -Trust \$Modify')
    $iRoot = $h.IndexOf('Assert-Trusted $Root'); $iAlert = $h.IndexOf('Get-UntrustedReason $alertCmd')
    Check 'Install-Heimdall checks C:\homeserver itself before judging alert.cmd (nothing can swap it once that passed)' (($iRoot -ge 0) -and ($iRoot -lt $iAlert))
    $root = New-Tree
    $Acl[$root] = [pscustomobject]@{ Owner = $SidAdmins; Rules = @($exact + (Ace $auth $Modify)) }
    $threw = ''
    try { Assert-Trusted $root } catch { $threw = "$_" }
    Check 'a refusal says removing the access would not undo it, and to move it away' (($threw -like '*would not undo*') -and ($threw -like '*move it away*')) $threw

    # -------------------------------------------------------------- 7. round 3: your account may not change what SYSTEM runs
    $py = Join-Path $tmp 'Python313'
    $site = Join-Path (Join-Path $py 'Lib') 'site-packages'
    New-Item -ItemType Directory -Force $site | Out-Null
    Set-Content -LiteralPath (Join-Path $py 'python.exe') -Value 'x'
    Set-Content -LiteralPath (Join-Path (Join-Path $py 'Lib') 'os.py') -Value 'x'   # the standard library's landmark (round 5)
    Set-Content -LiteralPath (Join-Path $site 'distutils-precedence.pth') -Value 'import os'
    $mod = [pscustomobject]@{ Owner = $SidAdmins; Rules = @($exact + (Ace $SidMe $Modify)) }
    $mine = [pscustomobject]@{ Owner = $SidMe; Rules = $exact }
    $Acl.Clear(); $threw = ''
    try { Assert-PythonTrusted $py } catch { $threw = "$_" }
    Check 'Python: a tree only administrators can change passes' ($threw -eq '') $threw
    $Acl[$site] = $mod; $threw = ''
    try { Assert-PythonTrusted $py } catch { $threw = "$_" }
    Check 'Python: administrator-owned site-packages with a Modify entry for you fails' ($threw -like "*site-packages: $SidMe (your account*may write*") $threw
    Check 'Python: the refusal says to reinstall it machine-wide for all users, elevated' (($threw -like '*reinstall it machine-wide for all users from an elevated prompt*') -and ($threw -like '*--scope machine*') -and ($threw -like '*would not undo*')) $threw
    Check 'Python: the refusal says to delete what the uninstaller leaves (a reinstall would reuse it)' ($threw -like "*delete what is left of $py *") $threw
    $Acl.Clear(); $Acl[$py] = $mine; $Calls.Clear(); $threw = ''
    try { Assert-PythonTrusted $py } catch { $threw = "$_" }
    Check 'Python: a tree owned by you fails, before anything in it is read' (($threw -like "*Python313: it belongs to your account ($SidMe)*for all users*") -and -not ($Calls | Where-Object { $_ -like "acl ${py}?*" })) "$threw | $($Calls -join ', ')"
    $Acl.Clear(); $Acl[(Join-Path $site 'distutils-precedence.pth')] = $mine; $threw = ''
    try { Assert-PythonTrusted $py } catch { $threw = "$_" }
    Check 'Python: a .pth file owned by you fails' ($threw -like '*distutils-precedence.pth: it belongs to your account*') $threw

    # the same entries where SYSTEM runs nothing: a Compose folder and .env stay yours to change
    $stack = Join-Path $tmp 'heimdall'
    New-Item -ItemType Directory -Force $stack | Out-Null
    $envFile = Join-Path $stack '.env'
    Set-Content -LiteralPath $envFile -Value 'TZ=Etc/UTC'
    $Acl.Clear(); $Acl[$stack] = $mod; $Acl[$envFile] = $mine; $Calls.Clear(); $threw = ''
    try { Assert-Trusted $stack -Trust $SidMe; Protect-Secret $envFile -Modify $SidMe } catch { $threw = "$_" }
    Check 'data: a Compose folder you may modify and a .env owned by you pass, and .env is locked' (($threw -eq '') -and ((Index "lock $envFile") -ge 0)) "$threw | $($Calls -join ', ')"
    $Acl.Clear(); $Acl[$envFile] = [pscustomobject]@{ Owner = $SidAdmins; Rules = @($exact + (Ace $SidUsers $Modify)) }; $Calls.Clear(); $threw = ''
    try { Protect-Secret $envFile -Modify $SidMe } catch { $threw = "$_" }
    Check 'data: a .env Users may modify still fails, nothing locked' (($threw -like "*${SidUsers} may write*") -and (Index 'lock ') -lt 0) "$threw | $($Calls -join ', ')"

    # alert.cmd: SYSTEM runs it, so one that only you could change is set aside like any other
    $root = New-Tree
    $alert = Join-Path $root 'alert.cmd'
    $Acl[$alert] = $mod
    $unsafe = Get-UntrustedReason $alert
    Check 'alert.cmd you may modify is judged unsafe' ($unsafe -like "*alert.cmd: $SidMe (your account*") $unsafe
    $Acl.Remove($alert)   # the entry goes with the file set aside; the new alert.cmd inherits the folder's
    Install-AlertCmd $alert $unsafe
    $aside = @(Get-ChildItem -LiteralPath $root -Filter 'alert.cmd.unsafe-*')
    Check 'alert.cmd you may modify is set aside and replaced by the template' (($aside.Count -eq 1) -and ([IO.File]::ReadAllText($alert) -notlike '*MARKER*')) "$($aside.Count)"
    $root = New-Tree
    $Acl[(Join-Path $root 'alert.cmd')] = $mine; $threw = ''
    try { Protect-Secret (Join-Path $root 'alert.cmd') } catch { $threw = "$_" }
    Check 'Protect-Secret refuses an alert.cmd owned by you (no -Modify, no trust)' ($threw -like '*alert.cmd: it belongs to your account*') $threw

    # C:\homeserver: a tools file owned by you stops Protect-Folder before anything is locked or reset
    $root = New-Tree
    $Acl[(Join-Path (Join-Path $root 'tools') 'wake-watch')] = $mine; $threw = ''
    try { Protect-Folder $root -Children 'tools', 'wake-watch.cmd' } catch { $threw = "$_" }
    Check 'Protect-Folder: a file owned by you stops it, nothing locked or reset' (($threw -like '*wake-watch: it belongs to your account*') -and (Index 'lock ') -lt 0 -and (Index 'icacls ') -lt 0) "$threw | $($Calls -join ', ')"
    $root = New-Tree
    $Acl[$root] = $mod; $threw = ''
    try { Assert-Trusted $root } catch { $threw = "$_" }
    Check 'C:\homeserver you may modify is refused' ($threw -like "*homeserver: $SidMe (your account*") $threw

    # a refused Python (or state folder) must not stay what SYSTEM runs every 2 minutes: the wrapper runs nothing
    $root = New-Tree
    $wrap = Join-Path $root 'wake-watch.cmd'
    Set-Content -LiteralPath $wrap -Value "@echo off`r`n`"C:\Program Files\Python313\python.exe`" -I x"
    try { Stop-WakeWatch $wrap 'its Python or its state folder' } catch { Write-Host "     $_" }
    $now = [IO.File]::ReadAllText($wrap)
    Check 'refused: the wrapper SYSTEM runs becomes one that runs nothing' (($now -like '*exit /b 1*') -and ($now -notlike '*python.exe*') -and ($now -like '*re-run setup.ps1*')) $now
    $DryRun = $true; Set-Content -LiteralPath $wrap -Value 'real wrapper'
    try { Stop-WakeWatch $wrap 'x' } catch { Write-Host "     $_" }; $DryRun = $false
    $dry = [IO.File]::ReadAllText($wrap)
    Remove-Item -LiteralPath $wrap
    try { Stop-WakeWatch $wrap 'x' } catch { Write-Host "     $_" }
    Check 'refused: a dry run changes nothing, and no wrapper is made where none was' (($dry -like 'real wrapper*') -and -not (Test-Path -LiteralPath $wrap)) $dry
    $h2 = $defs['Install-Heimdall'].Extent.Text
    $iTry = $h2.IndexOf('try {'); $iPy = $h2.IndexOf('Assert-PythonTrusted'); $iData = $h2.IndexOf('Protect-Folder $data')
    $iStop = $h2.IndexOf('Stop-WakeWatch $wrapper'); $iRethrow = $h2.IndexOf('throw $refused')
    Check 'Install-Heimdall turns wake-watch off when Python or the state folder is refused, after locking C:\homeserver, and still stops' (
        ($h2.IndexOf('Protect-Folder $Root') -lt $iTry) -and ($iTry -lt $iPy) -and ($iPy -lt $iData) -and ($iData -lt $iStop) -and ($iStop -lt $iRethrow)) "$iTry $iPy $iData $iStop $iRethrow"

    # -------------------------------------------------------------- 8. round 4: nothing points SYSTEM's Python outside its folder
    $pyexe = 'C:\Program Files\Python313\python.exe'
    $ww = 'C:\homeserver\tools\wake-watch'
    $lines = @(Get-WakeWatchWrapper $pyexe 'C:\ProgramData\wake-watch\wake-watch.conf' 'C:\ProgramData\wake-watch' 'C:\homeserver\alert.cmd' $ww)
    $flags = @(); $ok = $false
    if ($lines[-1] -match '^"([^"]+)" (.+) "([^"]+)" %\*$') {
        $flags = @($Matches[2] -split ' '); $ok = ($Matches[1] -eq $pyexe) -and ($Matches[3] -eq $ww)
    }
    Check 'wrapper: its last line runs Python with -I -S on wake-watch, passing the arguments on' ($ok -and (($flags -join ' ') -eq '-I -S')) ($lines -join ' | ')
    $cleared = @($lines[1..($lines.Count - 2)] | Where-Object { $_ -match '^set "([A-Z_]+)="$' } | ForEach-Object { $Matches[1] })
    Check 'wrapper: PYTHONHOME, PYTHONPATH, PYTHONEXECUTABLE and __PYVENV_LAUNCHER__ are cleared before Python starts' (
        ($lines[0] -eq '@echo off') -and ((($cleared | Sort-Object) -join ',') -eq '__PYVENV_LAUNCHER__,PYTHONEXECUTABLE,PYTHONHOME,PYTHONPATH')) ($lines -join ' | ')

    # the real startup, where a python3 is at hand: a protected global .pth naming a folder the operator may write
    $python = Get-Command python3, python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $python) { Write-Host 'skip the real Python startup checks (no python3 here)' }
    else {
        $venv = Join-Path $tmp 'protected-python'   # its site-packages is that Python's global one
        & $python.Source -m venv --without-pip $venv
        $vpy = Join-Path (Join-Path $venv 'bin') 'python'
        if (-not (Test-Path -LiteralPath $vpy)) { $vpy = Join-Path (Join-Path $venv 'Scripts') 'python.exe' }
        $vsite = "$(& $vpy -I -c "import sysconfig; print(sysconfig.get_path('purelib'))")".Trim()
        $op = Join-Path $tmp 'operator-project'      # the operator may write here; no check of setup.ps1 reaches it
        $pp = Join-Path $tmp 'pythonpath'
        New-Item -ItemType Directory -Force $op, $pp | Out-Null
        $mk = [ordered]@{ 'import line' = (Join-Path $tmp 'ran-pth-line'); 'code from the folder' = (Join-Path $tmp 'ran-pth-import')
            'sitecustomize' = (Join-Path $tmp 'ran-sitecustomize'); 'PYTHONPATH' = (Join-Path $tmp 'ran-pythonpath') }
        Set-Content -LiteralPath (Join-Path $op 'kit_editable.py') -Value "open(r'$($mk['code from the folder'])', 'w').close()"
        Set-Content -LiteralPath (Join-Path $op 'sitecustomize.py') -Value "open(r'$($mk['sitecustomize'])', 'w').close()"
        Set-Content -LiteralPath (Join-Path $pp 'sitecustomize.py') -Value "open(r'$($mk['PYTHONPATH'])', 'w').close()"
        Set-Content -LiteralPath (Join-Path $vsite 'zz-kit-editable.pth') -Value @(
            $op,                                                                             # a folder entry (an editable install)
            "import sys; sys.path.insert(0, r'$op'); open(r'$($mk['import line'])', 'w').close()",  # an import line
            'import kit_editable')                                                           # code from that folder
        $probe = Join-Path $tmp 'probe.py'
        Set-Content -LiteralPath $probe -Value "import sys; print(int('site' in sys.modules), int(r'$op' in sys.path), int(r'$pp' in sys.path))"
        $saved = @{}
        foreach ($v in 'PYTHONPATH', 'PYTHONHOME') { $saved[$v] = [Environment]::GetEnvironmentVariable($v) }
        try {
            $env:PYTHONPATH = $pp; $env:PYTHONHOME = Join-Path $tmp 'no-such-home'   # -I ignores both, or Python would not start
            $out3 = "$(& $vpy -I $probe)"
            $ran3 = @($mk.Keys | Where-Object { Test-Path -LiteralPath $mk[$_] })
            foreach ($m in $mk.Values) { if (Test-Path -LiteralPath $m) { Remove-Item -LiteralPath $m } }
            $out4 = "$(& $vpy @flags $probe)"
            $ran4 = @($mk.Keys | Where-Object { Test-Path -LiteralPath $mk[$_] })
        }
        finally { foreach ($v in $saved.Keys) { [Environment]::SetEnvironmentVariable($v, $saved[$v]) } }
        Check 'real Python, round-3 flags (-I): the protected .pth runs its import line, code from the folder it names and a sitecustomize there (the review case)' (
            (($ran3 -join ',') -eq 'import line,code from the folder,sitecustomize') -and ($out3 -eq '1 1 0')) "ran: $($ran3 -join ', ') | $out3"
        Check "real Python, the wrapper's flags ($($flags -join ' ')): none of it runs, site is not imported, neither folder is on the path, also with PYTHONPATH and PYTHONHOME set" (
            ($ran4.Count -eq 0) -and ($out4 -eq '0 0 0')) "ran: $($ran4 -join ', ') | $out4"
    }

    # a ._pth or pyvenv.cfg file would point Python elsewhere whatever -I -S says: refused, like an unsafe ACL
    New-Item -ItemType Directory -Force $site | Out-Null   # the Python313 tree of section 7 (New-Tree removed it)
    Set-Content -LiteralPath (Join-Path $py 'python.exe') -Value 'x'
    Set-Content -LiteralPath (Join-Path (Join-Path $py 'Lib') 'os.py') -Value 'x'   # the standard library's landmark (round 5)
    Set-Content -LiteralPath (Join-Path $site 'distutils-precedence.pth') -Value 'import os'
    $Acl.Clear(); $threw = ''
    try { Assert-PythonTrusted $py } catch { $threw = "$_" }
    Check 'Python: a clean tree with nothing pointing outside it passes' ($threw -eq '') $threw
    foreach ($name in 'python._pth', 'Python313._PTH') {
        $f = Join-Path $py $name
        Set-Content -LiteralPath $f -Value @('C:\Users\me\project', 'import site')
        $threw = ''
        try { Assert-PythonTrusted $py } catch { $threw = "$_" }
        Check "Python: a $name beside python.exe or its DLL is refused" (($threw -like "*${name}: a ._pth file replaces Python's search path*") -and
            ($threw -like '*from outside*') -and ($threw -like '*--scope machine*')) $threw
        Remove-Item -LiteralPath $f
    }
    foreach ($where in @(@('its folder', $py), @('the folder above it', $tmp))) {
        $f = Join-Path $where[1] 'pyvenv.cfg'
        Set-Content -LiteralPath $f -Value 'home = C:\Users\me\python'
        $threw = ''
        try { Assert-PythonTrusted $py } catch { $threw = "$_" }
        Check "Python: a pyvenv.cfg in $($where[0]) is refused" ($threw -like "*${f}: a pyvenv.cfg makes Python a virtual environment*") $threw
        Remove-Item -LiteralPath $f
    }
    $Acl[$tmp] = [pscustomobject]@{ Owner = $SidAdmins; Rules = @($exact + (Ace $SidUsers 0x116)) }   # Users may add files
    $threw = ''
    try { Assert-PythonTrusted $py } catch { $threw = "$_" }
    Check 'Python: a folder above it that Users may add files to is refused (a pyvenv.cfg could come later)' ($threw -like "*${SidUsers} may write*so it could add a pyvenv.cfg there*") $threw
    $Acl.Clear()

    # before 3.13.15, a ..\..\Modules\Setup.local (C:\Modules\Setup.local: any account may create a folder in C:\) made
    # Python a "source build" whose standard library is ..\..\Lib, -I -S or not (CVE-2026-12003): only 3.13.15 or later
    foreach ($ver in '3.13.14150', '3.13.7150', '3.13.15121') {
        $PyCore = [Version]$ver; $threw = ''
        try { Assert-PythonTrusted $py } catch { $threw = "$_" }
        Check "Python: python313.dll $ver (before 3.13.15 final) is refused, with the upgrade command" (
            ($threw -like "*python313.dll: file version $ver, so this Python is older than 3.13.15*Modules\Setup.local*CVE-2026-12003*") -and
            ($threw -like '*winget upgrade --id Python.Python.3.13 --exact --scope machine*')) $threw
    }
    foreach ($ver in '3.13.15150', '3.13.16150') {
        $PyCore = [Version]$ver; $threw = ''
        try { Assert-PythonTrusted $py } catch { $threw = "$_" }
        Check "Python: python313.dll $ver passes" ($threw -eq '') $threw
    }
    $PyCore = $null; $threw = ''
    try { Assert-PythonTrusted $py } catch { $threw = "$_" }
    Check 'Python: no python313.dll (its version unknown) is refused' ($threw -like '*python313.dll is missing, so it cannot be told whether this Python is 3.13.15 or later*') $threw
    $PyCore = [Version]'3.13.15150'
    $real = $none = 'no Get-PythonCoreVersion in setup.ps1'
    if ($RealCoreVersion) {
        $real = & $RealCoreVersion $py
        $dll = Join-Path $py 'python313.dll'
        Set-Content -LiteralPath $dll -Value 'not a PE file'
        $none = & $RealCoreVersion $py
        Remove-Item -LiteralPath $dll
    }
    Check 'Get-PythonCoreVersion: $null without python313.dll; a DLL with no version in it reads as older than 3.13.15' (
        ($null -eq $real) -and ($none -is [Version]) -and ($none -lt [Version]'3.13.15150')) "$real | $none"

    $apt = $defs['Assert-PythonTrusted'].Extent.Text
    Check 'Assert-PythonTrusted looks for redirections only after the folder passed (nothing can be added meanwhile)' (
        ($apt.IndexOf('Get-UntrustedReason $Dir -Recurse') -ge 0) -and ($apt.IndexOf('Get-UntrustedReason $Dir -Recurse') -lt $apt.IndexOf('Get-PythonRedirect $Dir')))

    # -------------------------------------------------------------- 9. round 5 (S2): every file getpath.py reads that moves Python out
    # One table for both sides: what setup.ps1 refuses (Refused: the end of its reason) or accepts ($false), and, with
    # -CPython, what CPython's own getpath.py makes of the same layout. Paths are relative to the Python folder.
    $layouts = @(
        @{ Name = 'a stock machine-wide Python313'; Refused = $false }
        @{ Name = 'an empty pybuilddir.txt beside python.exe (the review case)'; Refused = 'pybuilddir.txt: a pybuilddir.txt, empty or not,*'; Put = @{ 'pybuilddir.txt' = @() } }
        @{ Name = 'a pybuilddir.txt naming a folder'; Refused = 'pybuilddir.txt: a pybuilddir.txt, empty or not,*'; Put = @{ 'pybuilddir.txt' = @('.') } }
        @{ Name = 'python313._pth beside the DLL'; Refused = 'python313._pth: a ._pth file replaces*'; Put = @{ 'python313._pth' = @('C:\Users\op\py\Lib', 'import site') } }
        @{ Name = 'python._pth beside python.exe'; Refused = 'python._pth: a ._pth file replaces*'; Put = @{ 'python._pth' = @('C:\Users\op\py\Lib') } }
        @{ Name = 'a pyvenv.cfg beside python.exe'; Refused = 'pyvenv.cfg: a pyvenv.cfg makes*'; Put = @{ 'pyvenv.cfg' = @('home = C:\Users\op\py') } }
        @{ Name = 'a pyvenv.cfg one folder up'; Refused = 'pyvenv.cfg: a pyvenv.cfg makes*'; Put = @{ '..\pyvenv.cfg' = @('home = C:\Users\op\py') } }
        @{ Name = 'no Lib\os.py, os.pyc or python313.zip'; Refused = 'os.py is missing (and there is no os.pyc or python313.zip)*'; Drop = @('Lib\os.py') }
        @{ Name = 'Lib\os.pyc instead of Lib\os.py'; Refused = $false; Drop = @('Lib\os.py'); Put = @{ 'Lib\os.pyc' = @() } }
        @{ Name = 'python313.zip instead of Lib\os.py'; Refused = $false; Drop = @('Lib\os.py'); Put = @{ 'python313.zip' = @() } }
    )
    function Join-Rel([string]$Dir, [string]$Rel) {
        [IO.Path]::GetFullPath([IO.Path]::Combine($Dir, $Rel.Replace('\', [string][IO.Path]::DirectorySeparatorChar)))
    }
    function New-PythonLayout($Layout, [string]$Top = (Join-Path $tmp 'getpath')) {
        # A Python313 folder as the python.org installer leaves it (python.exe, python313.dll, Lib\os.py, DLLs), changed by $Layout
        if (Test-Path -LiteralPath $Top) { Remove-Item -LiteralPath $Top -Recurse -Force }
        $dir = Join-Path $Top 'Python313'
        $files = [ordered]@{ 'python.exe' = @('x'); 'python313.dll' = @('x'); 'Lib\os.py' = @('x'); 'DLLs\_ssl.pyd' = @('x') }
        foreach ($f in @($Layout['Drop'] | Where-Object { $_ })) { $files.Remove($f) }
        if ($Layout['Put']) { foreach ($k in $Layout['Put'].Keys) { $files[$k] = @($Layout['Put'][$k]) } }
        foreach ($k in $files.Keys) {
            $p = Join-Rel $dir $k
            New-Item -ItemType Directory -Force (Split-Path -Parent $p) | Out-Null
            [IO.File]::WriteAllLines($p, [string[]]@($files[$k]))
        }
        return $dir
    }
    $Acl.Clear(); $PyCore = [Version]'3.13.15150'
    foreach ($l in $layouts) {
        $dir = New-PythonLayout $l
        $threw = ''
        try { Assert-PythonTrusted $dir } catch { $threw = "$_" }
        if ($l['Refused']) {
            Check "Python, round 5: $($l['Name']) is refused" (($threw -like "*$($l['Refused'])") -and ($threw -like '*from outside*') -and ($threw -like '*--scope machine*')) $threw
        }
        else { Check "Python, round 5: $($l['Name']) passes" ($threw -eq '') $threw }
    }
    $dir = New-PythonLayout @{ Put = @{ 'pybuilddir.txt' = @() } }
    Remove-Item -LiteralPath (Join-Path $dir 'pybuilddir.txt'); New-Item -ItemType Directory (Join-Path $dir 'pybuilddir.txt') | Out-Null
    $threw = ''
    try { Assert-PythonTrusted $dir } catch { $threw = "$_" }
    Check 'Python, round 5: a folder named pybuilddir.txt is refused too' ($threw -like '*pybuilddir.txt: a pybuilddir.txt*') $threw

    # Install-Heimdall's own try block: a refused Python turns SYSTEM's existing wrapper off and still stops the run
    $tryAst = $defs['Install-Heimdall'].Body.Find({ $args[0] -is [System.Management.Automation.Language.TryStatementAst] }, $true)
    $heimdallTry = [scriptblock]::Create($tryAst.Extent.Text)
    function Invoke-HeimdallTry([string]$PyDir, [string]$StateDir) {
        $Root = Join-Path $tmp 'hs-try'
        New-Item -ItemType Directory -Force $Root | Out-Null
        $wrapper = Join-Path $Root 'wake-watch.cmd'
        Set-Content -LiteralPath $wrapper -Value 'the wrapper an earlier run wrote'
        $alertCmd = Join-Path $Root 'alert.cmd'
        $py = Join-Path $PyDir 'python.exe'
        $data = $StateDir; $conf = [IO.Path]::Combine($StateDir, 'wake-watch.conf')   # also for a C:\ path here
        $threw = ''
        try { . $heimdallTry } catch { $threw = "$_" }
        [pscustomobject]@{ Threw = $threw; Wrapper = [IO.File]::ReadAllText($wrapper) }
    }
    $state = Join-Path $tmp 'state'
    New-Item -ItemType Directory -Force $state | Out-Null
    foreach ($bdLine in '', '.') {
        $Calls.Clear()
        $r = Invoke-HeimdallTry (New-PythonLayout @{ Put = @{ 'pybuilddir.txt' = @($bdLine | Where-Object { $_ }) } }) $state
        Check "Install-Heimdall: a pybuilddir.txt ($(if ($bdLine) { 'naming a folder' } else { 'empty' })) stops the run and SYSTEM's wrapper runs nothing" (
            ($r.Threw -like '*pybuilddir.txt: a pybuilddir.txt*') -and ($r.Wrapper -like '*exit /b 1*') -and ($r.Wrapper -notlike '*earlier run*') -and ((Index "lock $state") -lt 0)) "$($r.Threw) | $($r.Wrapper)"
    }
    $Calls.Clear()
    $r = Invoke-HeimdallTry (New-PythonLayout @{}) $state
    Check 'Install-Heimdall: a stock Python passes, its state folder is locked and the wrapper is left for the rewrite' (
        ($r.Threw -eq '') -and ($r.Wrapper -like '*earlier run*') -and ((Index "lock $state") -ge 0)) "$($r.Threw) | $($Calls -join ', ')"

    # with -CPython: CPython's own getpath.py on the same table, through its own test helper (MockNTNamespace)
    $probe = @'
import importlib.util, json, os, sys, types
src, case_file = sys.argv[1], sys.argv[2]
# test_getpath.py needs only test.support.verbose: a quiet stand-in, also where this Python has no test package
support = types.ModuleType('test.support'); support.verbose = False
pkg = types.ModuleType('test'); pkg.support = support
sys.modules['test'] = pkg; sys.modules['test.support'] = support
spec = importlib.util.spec_from_file_location('kit_test_getpath', os.path.join(src, 'Lib', 'test', 'test_getpath.py'))
tg = importlib.util.module_from_spec(spec); spec.loader.exec_module(tg)   # it reads src/Modules/getpath.py
code = tg.SOURCE.decode('utf-8') if isinstance(tg.SOURCE, bytes) else ''
D = 'C:\\Program Files\\Python313'
def full(rel): return tg.ntpath.normpath(tg.ntpath.join(D, rel))
def listed(v): return [] if v is None else [v] if isinstance(v, str) else list(v)
# what another account may put outside the folder: C:\Lib and C:\Modules (any account may add folders to C:\), its own
outside = ['C:\\Lib\\os.py', 'C:\\Modules\\Setup.local', 'C:\\DLLs\\x.pyd', 'C:\\Users\\op\\py\\python.exe',
           'C:\\Users\\op\\py\\Lib\\os.py', 'C:\\Users\\op\\py\\DLLs\\x.pyd']
stock = [D + '\\python.exe', D + '\\python313.dll', D + '\\Lib\\os.py', D + '\\DLLs\\_ssl.pyd']
hk = 'HKCU\\Software\\Python\\PythonCore\\3.13\\PythonPath'
keys = ['home', 'executable', 'base_executable', 'prefix', 'exec_prefix', 'base_prefix', 'base_exec_prefix',
        'stdlib_dir', 'platstdlib_dir']
result = {'build_landmark': 'BUILD_LANDMARK' in code, 'cases': {}}
with open(case_file, encoding='utf-8-sig') as f:
    cases = json.load(f)
for c in cases:
    files = {p: [] for p in stock + outside}
    for rel in listed(c.get('remove')):
        files.pop(full(rel), None)
    for rel, lines in (c.get('add') or {}).items():
        files[full(rel)] = listed(lines)
    ns = tg.MockNTNamespace(argv0=D + '\\python.exe', real_executable=D + '\\python.exe', library=D + '\\python313.dll',
                            VERSION_MAJOR=3, VERSION_MINOR=13, PYWINVER='3.13',
                            winreg=tg.MockWinreg({hk: None, hk + '\\op': 'C:\\Users\\op\\reg'}),
                            config=dict(isolated=1, use_environment=0, site_import=0),   # -I -S
                            **(c.get('env') or {}))
    for p, lines in files.items():
        if p.lower().endswith('.exe'):
            ns.add_known_xfile(p)
        else:
            ns.add_known_file(p, lines)
    r = tg.getpath(ns, keys + ['module_search_paths', 'site_import'])
    seen = [(k, r[k]) for k in keys if r[k]] + [('sys.path', p) for p in r['module_search_paths']]
    inside = lambda v: v.casefold() == D.casefold() or v.casefold().startswith(D.casefold() + '\\')
    out = ['%s=%s' % (k, v) for k, v in seen if not inside(v)]
    if r['site_import']:
        out.append('site_import=1')
    result['cases'][c['name']] = out
print(json.dumps(result))
'@
    $sources = @($CPython -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($sources.Count -eq 0) { Write-Host 'skip CPython getpath.py proof (no -CPython source trees given)' }
    elseif (-not $python) { Check '-CPython: a python3 here to run CPython''s getpath.py' $false }
    else {
        $probeFile = Join-Path $tmp 'getpath_probe.py'
        $caseFile = Join-Path $tmp 'getpath_cases.json'
        Set-Content -LiteralPath $probeFile -Value $probe
        $cases = @(foreach ($l in $layouts) {
                $put = @{}; if ($l['Put']) { $put = $l['Put'] }
                [ordered]@{ name = $l['Name']; add = $put; remove = @($l['Drop'] | Where-Object { $_ }) } })
        $cases += [ordered]@{ name = 'env: __PYVENV_LAUNCHER__'; env = @{ ENV___PYVENV_LAUNCHER__ = 'C:\Users\op\py\python.exe' } }
        $cases += [ordered]@{ name = 'env: PYTHONEXECUTABLE'; env = @{ ENV_PYTHONEXECUTABLE = 'C:\Users\op\py\python.exe' } }
        $cases += [ordered]@{ name = 'env: PYTHONHOME'; env = @{ ENV_PYTHONHOME = 'C:\Users\op\py' } }
        Set-Content -LiteralPath $caseFile -Value (ConvertTo-Json -InputObject $cases -Depth 6)
        foreach ($given in $sources) {
            $label = Split-Path -Leaf $given
            $src = Resolve-Path $given -ErrorAction SilentlyContinue | Select-Object -First 1
            if (-not $src) { Check "getpath.py of ${label}: $given exists" $false; continue }
            $raw = @(& $python.Source -I -B $probeFile $src.Path $caseFile) -join "`n"   # -B: no __pycache__ in the source tree
            $res = $null
            if ($LASTEXITCODE -eq 0) { try { $res = $raw | ConvertFrom-Json } catch { $res = $null } }
            if (-not $res) { Check "getpath.py of ${label}: CPython's own path setup ran" $false $raw; continue }
            $moved = @{}
            foreach ($p in $res.cases.PSObject.Properties) { $moved[$p.Name] = @($p.Value | Where-Object { $_ }) }
            if ($res.build_landmark) {
                $all = @($layouts | Where-Object { $moved[$_['Name']].Count -eq 0 })
                Check "getpath.py of ${label} (before 3.13.15: it still reads Modules\Setup.local): C:\Modules\Setup.local moves even the stock layout to C:\Lib, which is why setup.ps1 refuses python313.dll older than 3.13.15" (
                    ($all.Count -eq 0) -and ($moved[$layouts[0]['Name']] -contains 'stdlib_dir=C:\Lib')) ($moved[$layouts[0]['Name']] -join '; ')
                continue
            }
            foreach ($l in $layouts) {
                $m = $moved[$l['Name']]
                if ($l['Refused']) { Check "getpath.py of ${label}: $($l['Name']) moves Python out of its folder (setup.ps1 refuses it)" ($m.Count -gt 0) ($m -join '; ') }
                else { Check "getpath.py of ${label}: $($l['Name']) keeps it inside, with C:\Lib, C:\Modules\Setup.local and an HKCU PythonPath key there (setup.ps1 accepts it)" ($m.Count -eq 0) ($m -join '; ') }
            }
            $bdMoved = @($layouts | Where-Object { $_['Name'] -like '*pybuilddir.txt*' } | Where-Object { $moved[$_['Name']] -notcontains 'stdlib_dir=C:\Lib' })
            Check "getpath.py of ${label}: a pybuilddir.txt, empty or naming a folder, loads the standard library from C:\Lib (the review case)" ($bdMoved.Count -eq 0) (($layouts | Where-Object { $_['Name'] -like '*pybuilddir*' } | ForEach-Object { $moved[$_['Name']] -join '; ' }) -join ' | ')
            Check "getpath.py of ${label}: __PYVENV_LAUNCHER__ and PYTHONEXECUTABLE would move it (read even with -I), so the wrapper clears both" (
                ($moved['env: __PYVENV_LAUNCHER__'].Count -gt 0) -and ($moved['env: PYTHONEXECUTABLE'].Count -gt 0) -and ($PyEnvClear -contains '__PYVENV_LAUNCHER__') -and ($PyEnvClear -contains 'PYTHONEXECUTABLE'))
            Check "getpath.py of ${label}: PYTHONHOME does not (-I ignores it)" ($moved['env: PYTHONHOME'].Count -eq 0) ($moved['env: PYTHONHOME'] -join '; ')
        }
    }

    # -------------------------------------------------------------- 10. round 5 (R5-1): privileged paths come from Windows, never from $env:
    $envCode = @($tokens | Where-Object { "$($_.Kind)" -ne 'Comment' -and $_.Text -match '(?i)(?<![a-z])env:' } | ForEach-Object { "$($_.Extent.StartLineNumber): $($_.Text)" })
    Check 'setup.ps1 reads no environment variable: no env: anywhere in its code, only in comments' ($envCode.Count -eq 0) ($envCode -join '; ')
    $envApi = @([regex]::Matches($ast.Extent.Text, '(?i)(Get|Expand)Environment(Variable|Variables)\([^)\r\n]*\)') | Where-Object { $_.Value -notmatch "'Machine'\)$" } | ForEach-Object { $_.Value })
    Check 'setup.ps1 reads environment variables through .NET only from the machine scope' ($envApi.Count -eq 0) ($envApi -join '; ')
    $rootStmts = @($ast.EndBlock.Statements | Where-Object { $_ -is [System.Management.Automation.Language.AssignmentStatementAst] -and @('$ProgramData', '$ProgramFiles') -contains $_.Left.Extent.Text })
    Check 'ProgramData and Program Files come from the known-folder API (Get-TrustedRoot, [Environment]::GetFolderPath)' (
        (($rootStmts | ForEach-Object { $_.Extent.Text }) -join ' | ') -eq "`$ProgramData = Get-TrustedRoot 'CommonApplicationData' | `$ProgramFiles = Get-TrustedRoot 'ProgramFiles'" -and
        ($defs['Get-KnownFolderPath'].Extent.Text -match '\[Environment\]::GetFolderPath\(\$Name\)') -and ($defs['Get-TrustedRoot'].Extent.Text -match 'Get-KnownFolderPath \$Name')) (($rootStmts | ForEach-Object { $_.Extent.Text }) -join ' | ')
    $h3 = $defs['Install-Heimdall'].Extent.Text; $ib = $defs['Install-Base'].Extent.Text
    Check 'Install-Heimdall and Install-Base build privileged paths on those roots' (
        ($h3 -match "Join-Path \`$ProgramFiles 'Python313\\python.exe'") -and ($h3 -match "Join-Path \`$ProgramData 'wake-watch'") -and ($ib -match "Join-Path \`$ProgramData 'ssh\\sshd_config'"))

    # the review's case: a persistent per-user ProgramData of C:\Users\operator\pd&whoami&rem
    $savedEnv = @{}
    foreach ($v in 'ProgramData', 'ProgramFiles') { $savedEnv[$v] = [Environment]::GetEnvironmentVariable($v) }
    try {
        [Environment]::SetEnvironmentVariable('ProgramData', 'C:\Users\operator\pd&whoami&rem')
        [Environment]::SetEnvironmentVariable('ProgramFiles', 'C:\Users\operator\pf')
        $ProgramData = $ProgramFiles = $null
        foreach ($s in $rootStmts) { . ([scriptblock]::Create($s.Extent.Text)) }
        Check 'hostile ProgramData and ProgramFiles in the environment: setup.ps1 still takes C:\ProgramData and C:\Program Files from Windows' (
            ($ProgramData -eq 'C:\ProgramData') -and ($ProgramFiles -eq 'C:\Program Files')) "$ProgramData | $ProgramFiles"
        $lines = @(Get-WakeWatchWrapper "$ProgramFiles\Python313\python.exe" "$ProgramData\wake-watch\wake-watch.conf" "$ProgramData\wake-watch" 'C:\homeserver\alert.cmd' 'C:\homeserver\tools\wake-watch')
        Check 'hostile ProgramData: the wrapper sets WAKE_WATCH_STATE=C:\ProgramData\wake-watch, quoted, and never mentions whoami' (
            ($lines -contains 'set "WAKE_WATCH_STATE=C:\ProgramData\wake-watch"') -and (@($lines | Where-Object { $_ -match 'whoami' }).Count -eq 0)) ($lines -join ' | ')
        $KnownFolders['CommonApplicationData'] = 'C:\Users\operator\pd&whoami&rem'   # had Windows itself answered with it
        $threw = ''
        try { . ([scriptblock]::Create($rootStmts[0].Extent.Text)) } catch { $threw = "$_" }
        Check 'a known folder holding CMD syntax (C:\Users\operator\pd&whoami&rem) is refused before anything runs' ($threw -like "*pd&whoami&rem*holds '&', which CMD reads as syntax*") $threw
    }
    finally {
        foreach ($v in $savedEnv.Keys) { [Environment]::SetEnvironmentVariable($v, $savedEnv[$v]) }
        $KnownFolders['CommonApplicationData'] = 'C:\ProgramData'
    }
    $bad = [ordered]@{}
    foreach ($c in '%', '!', '"', '^', '&', '|', '<', '>') { $bad["'$c'"] = "C:\Program${c}Data" }
    $bad['a line break'] = "C:\ProgramData`nx"; $bad['a carriage return'] = "C:\ProgramData`rx"
    $bad['a share (\\server\share)'] = '\\server\share\ProgramData'; $bad['a relative path'] = 'ProgramData'
    $bad['a bare drive (C:\)'] = 'C:\'; $bad['nothing'] = ''; $bad['a Unix path'] = '/usr/share'
    $bad['another drive (D:\Data, maybe a removable disk)'] = 'D:\Data\'; $bad['a mapped share (Z:\ProgramData)'] = 'Z:\ProgramData'
    foreach ($k in $bad.Keys) {
        $KnownFolders['CommonApplicationData'] = $bad[$k]; $threw = ''
        try { [void](Get-TrustedRoot 'CommonApplicationData') } catch { $threw = "$_" }
        Check "Get-TrustedRoot refuses a known folder with $k" ($threw -like '*Windows gives*') $threw
    }
    foreach ($ok in 'C:\ProgramData', 'C:\Program Files (x86)', 'c:\programdata\') {
        $KnownFolders['CommonApplicationData'] = $ok; $got = ''
        try { $got = Get-TrustedRoot 'CommonApplicationData' } catch { $got = "$_" }
        Check "Get-TrustedRoot takes $ok" ($got -eq $ok.TrimEnd('\')) $got
    }
    # Windows' own drive comes from GetSystemDirectory: Windows on D:\ takes D:\ProgramData and refuses C:\ProgramData,
    # and an unknown drive refuses everything
    Check 'Get-WindowsDrive is GetSystemDirectory''s drive ([Environment]::SystemDirectory), never an environment variable' (
        $defs['Get-WindowsDrive'].Extent.Text -match '\[IO\.Path\]::GetPathRoot\(\[Environment\]::SystemDirectory\)')
    $drives = [ordered]@{ 'D:\|D:\ProgramData' = $true; 'D:\|C:\ProgramData' = $false; '|C:\ProgramData' = $false; 'C:|C:\ProgramData' = $false }
    foreach ($k in $drives.Keys) {
        $WindowsDrive, $KnownFolders['CommonApplicationData'] = $k -split '\|'; $got = ''
        try { $got = Get-TrustedRoot 'CommonApplicationData' } catch { $got = "$_" }
        if ($drives[$k]) { Check "Windows on '$WindowsDrive': Get-TrustedRoot takes $($KnownFolders['CommonApplicationData'])" ($got -eq $KnownFolders['CommonApplicationData']) $got }
        else { Check "Windows on '$WindowsDrive': Get-TrustedRoot refuses $($KnownFolders['CommonApplicationData'])" ($got -like "*not on Windows' own drive*") $got }
    }
    $WindowsDrive = 'C:\'
    $KnownFolders['CommonApplicationData'] = 'C:\ProgramData'

    # the wrapper: set "NAME=value" for each path, and a path CMD would read as syntax is refused, never escaped
    $good = @('C:\Program Files\Python313\python.exe', 'C:\ProgramData\wake-watch\wake-watch.conf', 'C:\ProgramData\wake-watch', 'C:\homeserver\alert.cmd', 'C:\homeserver\tools\wake-watch')
    $lines = @(Get-WakeWatchWrapper @good)
    $sets = @($lines | Where-Object { $_ -match '^set "WAKE_WATCH_' })
    Check 'wrapper: each path is set as set "NAME=value"' ((($sets) -join '|') -eq 'set "WAKE_WATCH_CONF=C:\ProgramData\wake-watch\wake-watch.conf"|set "WAKE_WATCH_STATE=C:\ProgramData\wake-watch"|set "WAKE_WATCH_ALERT=C:\homeserver\alert.cmd"') ($lines -join ' | ')
    $loose = @($lines[1..($lines.Count - 2)] | Where-Object { $_ -notmatch '^set "[A-Z_]+=[^"%!^&|<>]*"$' })
    Check 'wrapper: every line between @echo off and Python is a quoted set with nothing CMD would read as syntax' ($loose.Count -eq 0) ($loose -join ' | ')
    $names = 'Python', 'its conf', 'its state folder', 'alert.cmd', 'the script'
    $notRefused = New-Object System.Collections.Generic.List[string]
    foreach ($i in 0..4) {
        foreach ($c in @('%', '!', '"', '^', '&', '|', '<', '>', "`n")) {
            $args5 = @($good); $args5[$i] = $good[$i] + "${c}whoami"
            $threw = ''; $out = $null
            try { $out = Get-WakeWatchWrapper @args5 } catch { $threw = "$_" }
            if (-not (($threw -like '*which CMD reads as syntax*refuses such a path rather than escape it*') -and ($null -eq $out))) {
                $notRefused.Add("$($names[$i]) with $(if ($c -eq "`n") { 'a line break' } else { $c })")
            }
        }
    }
    Check 'wrapper: a path for any of its five entries holding % ! " ^ & | < > or a line break is refused, never escaped (45 cases)' ($notRefused.Count -eq 0) ($notRefused -join '; ')
    $Calls.Clear()
    $r = Invoke-HeimdallTry (New-PythonLayout @{}) 'C:\Users\operator\pd&whoami&rem\wake-watch'
    Check 'Install-Heimdall: a state path holding CMD syntax stops the run before Python or the folder is checked, and SYSTEM''s wrapper runs nothing' (
        ($r.Threw -like "*holds '&', which CMD reads as syntax*") -and ($r.Wrapper -like '*exit /b 1*') -and ((Index 'acl ') -lt 0) -and ((Index 'lock ') -lt 0)) "$($r.Threw) | $($Calls -join ', ')"

    # the folders above a privileged path: a stock C:\, ProgramData and Program Files, on a real tree with their ACLs
    $ti = $SidTrustedInstaller
    $stockC = [pscustomobject]@{ Owner = $ti; Rules = @((Ace $auth $Modify -InheritOnly), (Ace $auth 0x4), (Ace $SidSystem $FC), (Ace $SidAdmins $FC), (Ace $SidUsers $RX)) }
    $stockPD = [pscustomobject]@{ Owner = $SidSystem; Rules = @((Ace $SidSystem $FC), (Ace $SidAdmins $FC), (Ace 'S-1-3-0' $FC -InheritOnly), (Ace $SidUsers $RX), (Ace $SidUsers 0x116)) }
    $stockPF = [pscustomobject]@{ Owner = $ti; Rules = @((Ace $ti $FC), (Ace $SidSystem $Modify), (Ace $SidAdmins $Modify), (Ace $SidUsers $RX),
            (Ace $SidUsers 0xA0000000 -InheritOnly), (Ace 'S-1-3-0' 0x10000000 -InheritOnly), (Ace 'S-1-15-2-1' $RX)) }
    $C = Join-Path (Join-Path $tmp 'win') 'C'
    $PD = Join-Path $C 'ProgramData'; $PF = Join-Path $C 'Program Files'
    $ww = Join-Path $PD 'wake-watch'
    New-Item -ItemType Directory -Force $ww | Out-Null
    $pyStock = New-PythonLayout @{} $PF
    function Set-StockAcl { $Acl.Clear(); $Acl[$C] = $stockC; $Acl[$PD] = $stockPD; $Acl[$PF] = $stockPF; $Calls.Clear() }
    Set-StockAcl
    $r = Get-AncestorReason $ww
    Check 'folders above: a stock C:\ and ProgramData pass (users may add folders there)' ($r -eq '') $r
    Check 'folders above: the rule for what SYSTEM runs would refuse that stock C:\ and ProgramData, so it is not applied to them' (
        ((Get-UnsafeReason $stockC.Owner $stockC.Rules) -ne '') -and ((Get-UnsafeReason $stockPD.Owner $stockPD.Rules) -ne ''))
    $order = @($Calls | Where-Object { $_ -like 'acl *' } | ForEach-Object { $_.Substring(4) })
    Check 'folders above: checked from the drive root down, ending with ProgramData' (($order.Count -ge 2) -and ($order[-1] -eq $PD) -and ($order[-2] -eq $C) -and ($order[0].Length -lt $order[1].Length)) ($order -join ', ')
    Set-StockAcl; $threw = ''
    try { Protect-Folder $ww } catch { $threw = "$_" }
    Check 'folders above: Protect-Folder of ProgramData\wake-watch under a stock layout locks it, the folders above checked first' (
        ($threw -eq '') -and ((Index "lock $ww") -ge 0) -and ((Index "acl $PD") -lt (Index "acl $ww")) -and ((Index "acl $C") -lt (Index "acl $PD"))) "$threw | $($Calls -join ', ')"
    Set-StockAcl; $threw = ''
    try { Assert-PythonTrusted $pyStock } catch { $threw = "$_" }
    Check 'folders above: a stock Program Files\Python313 passes' ($threw -eq '') $threw

    # a plain user-owned parent (the review's case without CMD syntax), owned by you or by another account
    $userPd = Join-Path (Join-Path (Join-Path $C 'Users') 'operator') 'pd'
    New-Item -ItemType Directory -Force $userPd | Out-Null
    foreach ($who in @(@('you', $SidMe, 'your account'), @('another account', $stranger, 'another account'))) {
        Set-StockAcl; $Acl[(Split-Path -Parent $userPd)] = [pscustomobject]@{ Owner = $who[1]; Rules = $exact }
        $leaf = Join-Path $userPd 'wake-watch'; $threw = ''
        try { Protect-Folder $leaf } catch { $threw = "$_" }
        Check "folders above: a parent owned by $($who[0]) stops Protect-Folder before anything is created, judged or locked" (
            ($threw -like "*operator: it belongs to $($who[2])*redirect*") -and -not (Test-Path -LiteralPath $leaf) -and ((Index 'lock ') -lt 0) -and ((Index "acl $leaf") -lt 0)) "$threw | $($Calls -join ', ')"
    }

    # a junction (here a symlink) above ProgramData\wake-watch, above Program Files\Python313, and further up
    $J = Join-Path $tmp 'junction'
    New-Item -ItemType Directory -Force (Join-Path (Join-Path $J 'pd-real') 'wake-watch') | Out-Null
    $pyReal = New-PythonLayout @{} (Join-Path $J 'pf-real')
    New-Item -ItemType Directory -Force (Join-Path $J 'c-real') | Out-Null
    [void](New-PythonLayout @{} (Join-Path (Join-Path $J 'c-real') 'Program Files'))
    $linked = $true
    try {
        New-Item -ItemType SymbolicLink -Path (Join-Path $J 'ProgramData') -Target (Join-Path $J 'pd-real') | Out-Null
        New-Item -ItemType SymbolicLink -Path (Join-Path $J 'Program Files') -Target (Join-Path $J 'pf-real') | Out-Null
        New-Item -ItemType SymbolicLink -Path (Join-Path $J 'C') -Target (Join-Path $J 'c-real') | Out-Null
    }
    catch { $linked = $false }
    if ($linked) {
        Set-StockAcl; $threw = ''
        try { Protect-Folder (Join-Path (Join-Path $J 'ProgramData') 'wake-watch') } catch { $threw = "$_" }
        Check 'folders above: a junction as ProgramData stops Protect-Folder, nothing locked' (($threw -like '*ProgramData is a link*') -and ((Index 'lock ') -lt 0)) $threw
        foreach ($case in @(@('Program Files', (Join-Path (Join-Path $J 'Program Files') 'Python313')), @('C:\ itself (an earlier folder)', (Join-Path (Join-Path (Join-Path $J 'C') 'Program Files') 'Python313')))) {
            Set-StockAcl; $threw = ''
            try { Assert-PythonTrusted $case[1] } catch { $threw = "$_" }
            Check "folders above: a junction as $($case[0]) stops the Python check before anything in it is read" (
                ($threw -like '* is a link (junction or symlink), so another account could swap it*') -and -not ($Calls | Where-Object { $_ -like "acl $($case[1])*" })) "$threw | $($Calls -join ', ')"
        }
        $lnk = Join-Path $J 'sshd_config'
        New-Item -ItemType SymbolicLink -Path $lnk -Target (Join-Path $J 'pd-real') | Out-Null
        Set-StockAcl
        Check 'folders above: a link at the path itself is refused too (sshd_config, written with admin rights)' ((Get-AncestorReason $lnk) -like '*sshd_config is a link*') (Get-AncestorReason $lnk)
    }
    else { Write-Host 'skip the junction checks (no symlinks here)' }

    # which entries on a folder above count: ProgramData's, varied
    $leafPD = $ww
    $aboveOk = [ordered]@{
        'Users add files and folders (stock ProgramData)'  = @($SidSystem, @($exact + (Ace $SidUsers 0x116)))
        'Users write data and append only'                 = @($SidAdmins, @($exact + (Ace $SidUsers 0x6)))
        'Authenticated Users modify, only for what it holds' = @($SidAdmins, @($exact + (Ace $auth $Modify -InheritOnly)))
        'Users full control, only for what it holds'       = @($SidAdmins, @($exact + (Ace $SidUsers $FC -InheritOnly)))
        'Users denied full control'                        = @($SidAdmins, @($exact + (Ace $SidUsers $FC $false)))
        'owned by TrustedInstaller'                        = @($ti, $exact)
    }
    foreach ($k in $aboveOk.Keys) {
        Set-StockAcl; $Acl[$PD] = [pscustomobject]@{ Owner = $aboveOk[$k][0]; Rules = $aboveOk[$k][1] }
        $r = Get-AncestorReason $leafPD
        Check "folders above, passes: ProgramData with $k" ($r -eq '') $r
    }
    $aboveBad = [ordered]@{
        'owned by you'                                   = @($SidMe, $exact, '*belongs to your account*')
        'owned by another account'                       = @($stranger, $exact, '*belongs to another account*')
        'Users delete (rename it, put a junction there)' = @($SidAdmins, @($exact + (Ace $SidUsers 0x10000)), '*may delete or rename it*')
        'Users delete what it holds'                     = @($SidAdmins, @($exact + (Ace $SidUsers 0x40)), '*may delete or rename it*')
        'Users change permissions'                       = @($SidAdmins, @($exact + (Ace $SidUsers 0x40000)), '*change its permissions or owner*')
        'Users take ownership'                           = @($SidAdmins, @($exact + (Ace $SidUsers 0x80000)), '*change its permissions or owner*')
        'Users generic all on itself'                    = @($SidAdmins, @($exact + (Ace $SidUsers 0x10000000)), '*may delete*')
        'Authenticated Users modify on itself'           = @($SidAdmins, @($exact + (Ace $auth $Modify)), '*may delete*')
        'Everyone full control'                          = @($SidAdmins, @($exact + (Ace $everyone $FC)), '*may delete*')
        'you allowed to modify'                          = @($SidAdmins, @($exact + (Ace $SidMe $Modify)), '*(your account) may delete*')
    }
    foreach ($k in $aboveBad.Keys) {
        Set-StockAcl; $Acl[$PD] = [pscustomobject]@{ Owner = $aboveBad[$k][0]; Rules = $aboveBad[$k][1] }
        $r = Get-AncestorReason $leafPD
        Check "folders above, fails: ProgramData $k" ($r -like "*ProgramData: $($aboveBad[$k][2])") $r
    }
    Set-StockAcl; $Acl[$PD] = 'unreadable'
    $r = Get-AncestorReason $leafPD
    Check 'folders above, fails: ProgramData whose permissions cannot be read' ($r -like '*ProgramData: its permissions cannot be read*') $r
    Set-StockAcl
    $r = Get-AncestorReason (Join-Path (Join-Path $C 'NoSuchFolder') 'wake-watch')
    Check 'folders above, fails: one that is missing (it could be created as a junction after the check)' ($r -like '*NoSuchFolder is missing') $r

    $pfd = $defs['Protect-Folder'].Extent.Text; $iSafe = $pfd.IndexOf('Assert-SafeAncestors $Path')
    Check 'Protect-Folder checks the folders above before it creates or judges the folder' (
        ($iSafe -ge 0) -and ($iSafe -lt $pfd.IndexOf('(Get-LinkAttribute $Path)')) -and ($iSafe -lt $pfd.IndexOf('Assert-Trusted $Path')))
    Check 'Assert-PythonTrusted checks the folders above before anything in the Python folder' (
        ($apt.IndexOf('Assert-SafeAncestors $Dir') -ge 0) -and ($apt.IndexOf('Assert-SafeAncestors $Dir') -lt $apt.IndexOf('Get-UntrustedReason $Dir -Recurse')))
    Check 'Install-Heimdall checks the folders above C:\homeserver before C:\homeserver itself' (
        ($h3.IndexOf('Assert-SafeAncestors $Root') -ge 0) -and ($h3.IndexOf('Assert-SafeAncestors $Root') -lt $h3.IndexOf('Assert-Trusted $Root')))
    Check 'Install-Base checks the folders above sshd_config before writing it with admin rights' (
        ($ib.IndexOf('Assert-SafeAncestors $cfg') -ge 0) -and ($ib.IndexOf('Assert-SafeAncestors $cfg') -lt $ib.IndexOf('WriteAllText($cfg')))
    $iTry3 = $h3.IndexOf('try {'); $iWrap = $h3.IndexOf('$wrapperLines = Get-WakeWatchWrapper')
    Check 'Install-Heimdall builds the wrapper first inside its refusal block, before Python is checked' (($iTry3 -lt $iWrap) -and ($iWrap -lt $h3.IndexOf('Assert-PythonTrusted')))
}
finally {
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
}

$failed = @($results | Where-Object { -not $_.Ok })
Write-Host "`n$($results.Count - $failed.Count)/$($results.Count) passed"
exit ([int]($failed.Count -gt 0))
