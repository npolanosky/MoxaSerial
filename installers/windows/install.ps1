<#
.SYNOPSIS
    Install the MoxaSerial add-in into Autodesk Fusion's per-user AddIns folder.

.DESCRIPTION
    The no-Inno-Setup fallback: it does exactly what install.py does, for people
    who have a zip or a git clone but no Python on PATH. Nothing is written
    outside %APPDATA% and no administrator rights are needed.

        %APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\MoxaSerial

    Run it from an unzipped release (next to the MoxaSerial folder) or from a
    source checkout. An existing install is renamed to MoxaSerial.bak-<stamp>
    before the new files are written, so an upgrade is always reversible.

    The file list below must stay in step with installers\payload.py, which is
    the single source of truth for what ships.

.PARAMETER Dest
    AddIns folder to install into. Defaults to the per-user Fusion folder; set
    it to install somewhere else (handy for testing).

.PARAMETER Uninstall
    Remove an installed MoxaSerial and its MoxaSerial.bak-* folders.

.PARAMETER KeepBackups
    With -Uninstall, leave the MoxaSerial.bak-* folders alone.

.PARAMETER DryRun
    Print what would happen without changing anything.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File installers\windows\install.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File installers\windows\install.ps1 -Uninstall
#>

[CmdletBinding()]
param(
    [string] $Dest,
    [switch] $Uninstall,
    [switch] $KeepBackups,
    [switch] $DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$AddInName = 'MoxaSerial'

# Mirrors installers/payload.py -- keep the two in step.
$IncludeFiles = @(
    'MoxaSerial.py',
    'MoxaSerial.manifest',
    'moxaserial_loader.py',
    'LICENSE',
    'README.md'
)
$IncludeTrees = @('moxaserial', 'resources')
$ExcludeDirNames = @('__pycache__', '.git', '.pytest_cache', '.ruff_cache', '.devdata', '.venv', 'venv')
$ExcludeSuffixes = @('.pyc', '.pyo', '.pyd', '.log', '.orig', '.rej')
$ExcludeFileNames = @('.DS_Store', 'Thumbs.db', 'desktop.ini')


function Get-AddInsDir {
    param([string] $Explicit)

    if ($Explicit) { return [IO.Path]::GetFullPath($Explicit) }
    if ($env:MOXASERIAL_ADDINS_DIR) { return [IO.Path]::GetFullPath($env:MOXASERIAL_ADDINS_DIR) }

    $roaming = $env:APPDATA
    if (-not $roaming) { $roaming = Join-Path $HOME 'AppData\Roaming' }

    # Autodesk has shipped two product folder names; prefer one that exists.
    $candidates = @(
        (Join-Path $roaming 'Autodesk\Autodesk Fusion 360\API\AddIns'),
        (Join-Path $roaming 'Autodesk\Autodesk Fusion\API\AddIns')
    )
    foreach ($c in $candidates) { if (Test-Path -LiteralPath $c -PathType Container) { return $c } }
    return $candidates[0]
}


function Test-Excluded {
    param([IO.FileInfo] $File)

    if ($ExcludeFileNames -contains $File.Name) { return $true }
    foreach ($suffix in $ExcludeSuffixes) {
        if ($File.Name.EndsWith($suffix, [StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
    $rel = $File.FullName
    foreach ($dir in $ExcludeDirNames) {
        if ($rel -like "*\$dir\*") { return $true }
    }
    return $false
}


function Get-Payload {
    <#
      Returns @{ Root = <source root>; Files = @(@{ Source; Relative }, ...) }.

      Two layouts, same as install.py: a ready-made MoxaSerial folder beside
      this script (unzipped release), or a source checkout.
    #>
    # From installers\windows\ the repo root is two levels up; from an unzipped
    # release the script sits beside the MoxaSerial folder. Try both.
    $searchRoots = @(
        $PSScriptRoot,
        (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..') -ErrorAction SilentlyContinue | ForEach-Object Path),
        (Get-Location).Path
    ) | Where-Object { $_ } | Select-Object -Unique

    foreach ($root in $searchRoots) {
        # Unzipped release: a staged, already-filtered MoxaSerial folder.
        $staged = Join-Path $root $AddInName
        if (Test-Path -LiteralPath (Join-Path $staged 'MoxaSerial.manifest') -PathType Leaf) {
            $files = Get-ChildItem -LiteralPath $staged -Recurse -File |
                Where-Object { -not (Test-Excluded $_) } |
                ForEach-Object {
                    @{ Source = $_.FullName; Relative = $_.FullName.Substring($staged.Length).TrimStart('\') }
                }
            return @{ Root = $staged; Files = @($files) }
        }

        # Source checkout.
        if (Test-Path -LiteralPath (Join-Path $root 'MoxaSerial.manifest') -PathType Leaf) {
            $files = New-Object System.Collections.ArrayList
            foreach ($name in $IncludeFiles) {
                $p = Join-Path $root $name
                if (-not (Test-Path -LiteralPath $p -PathType Leaf)) {
                    throw "missing payload file: $name (looked in $root)"
                }
                [void] $files.Add(@{ Source = $p; Relative = $name })
            }
            foreach ($tree in $IncludeTrees) {
                $base = Join-Path $root $tree
                if (-not (Test-Path -LiteralPath $base -PathType Container)) {
                    throw "missing payload directory: $tree (looked in $root)"
                }
                Get-ChildItem -LiteralPath $base -Recurse -File |
                    Where-Object { -not (Test-Excluded $_) } |
                    ForEach-Object {
                        [void] $files.Add(@{
                            Source   = $_.FullName
                            Relative = $_.FullName.Substring($root.Length).TrimStart('\')
                        })
                    }
            }
            return @{ Root = $root; Files = @($files) }
        }
    }

    throw ("nothing to install: expected a '$AddInName' folder beside install.ps1 " +
           "(unzipped release) or a MoxaSerial.manifest in the repository root")
}


function Get-PayloadVersion {
    param([string] $Root)
    try {
        $manifest = Get-Content -LiteralPath (Join-Path $Root 'MoxaSerial.manifest') -Raw | ConvertFrom-Json
        return $manifest.version
    } catch {
        return '?'
    }
}


function Test-ReparsePoint {
    param($Item)
    if (-not $Item) { return $false }
    return (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
}


function Get-LinkTarget {
    # .Target is an ETS property on FileSystemInfo; guard it so Set-StrictMode
    # does not blow up on a PowerShell edition that does not expose it.
    param($Item)
    $prop = $Item.PSObject.Properties['Target']
    if ($prop -and $prop.Value) { return ($prop.Value -join ', ') }
    return '(unknown)'
}


function Backup-Existing {
    param([string] $Target)

    # -Force so a hidden or reparse-point entry is still seen. A junction or
    # symlink to a source checkout must be renamed, not written through.
    $item = Get-Item -LiteralPath $Target -Force -ErrorAction SilentlyContinue
    if (-not $item) { return $null }

    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backup = "$Target.bak-$stamp"
    $n = 1
    while (Test-Path -LiteralPath $backup) {
        $n++
        $backup = "$Target.bak-$stamp-$n"
    }

    if (Test-ReparsePoint $item) {
        Write-Host "  $AddInName is a link to $(Get-LinkTarget $item)"
        Write-Host "  moving the link aside -> $(Split-Path -Leaf $backup)  (rename it back to undo)"
    } else {
        Write-Host "  backing up existing install -> $(Split-Path -Leaf $backup)"
    }
    if (-not $DryRun) { Move-Item -LiteralPath $Target -Destination $backup -Force }
    return $backup
}


function Invoke-Install {
    param([string] $AddIns)

    $payload = Get-Payload
    $root = $payload.Root
    $files = $payload.Files
    $version = Get-PayloadVersion -Root $root
    $target = Join-Path $AddIns $AddInName

    Write-Host "MoxaSerial $version"
    Write-Host "  source      $root"
    Write-Host "  destination $target"
    Write-Host "  files       $($files.Count)"
    if ($DryRun) { Write-Host '  (dry run -- nothing written)' }

    if (-not (Test-Path -LiteralPath $AddIns -PathType Container)) {
        Write-Host "  creating $AddIns"
        if (-not $DryRun) { New-Item -ItemType Directory -Path $AddIns -Force | Out-Null }
    }

    $backup = Backup-Existing -Target $target

    if (-not $DryRun) {
        try {
            foreach ($f in $files) {
                $dst = Join-Path $target $f.Relative
                $dstDir = Split-Path -Parent $dst
                if (-not (Test-Path -LiteralPath $dstDir -PathType Container)) {
                    New-Item -ItemType Directory -Path $dstDir -Force | Out-Null
                }
                Copy-Item -LiteralPath $f.Source -Destination $dst -Force
            }
        } catch {
            # Put the old install back rather than leaving a half-written one.
            if (Test-Path -LiteralPath $target) {
                Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction SilentlyContinue
            }
            if ($backup -and (Test-Path -LiteralPath $backup)) {
                Move-Item -LiteralPath $backup -Destination $target -Force
                Write-Warning 'restored the previous install'
            }
            throw
        }
    }

    Write-Host ''
    Write-Host 'Installed. Next, in Fusion:'
    Write-Host '  1. Utilities tab -> ADD-INS -> Scripts and Add-Ins  (or Shift+S)'
    Write-Host '  2. Add-Ins tab -> MoxaSerial -> Run'
    Write-Host "  3. Tick 'Run on Startup' so it comes back with Fusion"
    Write-Host ''
    Write-Host "The buttons appear in the Manufacture workspace: a 'Moxa DNC' panel on the"
    Write-Host "P3DTools tab, plus 'Send Last Program' beside Post Process."
    if ($backup) {
        Write-Host ''
        Write-Host "Your previous install is at $backup -- delete it once the new one works."
    }
    Write-Host ''
    Write-Host 'If Fusion was already running, restart it (or stop and re-run the add-in).'
}


function Invoke-Uninstall {
    param([string] $AddIns)

    $target = Join-Path $AddIns $AddInName
    $removed = $false

    $item = Get-Item -LiteralPath $target -Force -ErrorAction SilentlyContinue
    if ($item) {
        if (Test-ReparsePoint $item) {
            # Delete only the link; whatever it points at is someone's checkout.
            Write-Host "  removing link $target -> $(Get-LinkTarget $item)"
            Write-Host '  (the linked folder itself is left alone)'
            if (-not $DryRun) { [IO.Directory]::Delete($target) }
        } else {
            Write-Host "  removing $target"
            if (-not $DryRun) { Remove-Item -LiteralPath $target -Recurse -Force }
        }
        $removed = $true
    } else {
        Write-Host "  nothing installed at $target"
    }

    if (-not $KeepBackups) {
        # foreach, not ForEach-Object: the pipeline block has its own scope and
        # could not set $removed.
        $backups = @(Get-ChildItem -LiteralPath $AddIns -Filter "$AddInName.bak-*" -Force -ErrorAction SilentlyContinue)
        foreach ($b in $backups) {
            Write-Host "  removing backup $($b.Name)"
            if (-not $DryRun) {
                if (Test-ReparsePoint $b) {
                    [IO.Directory]::Delete($b.FullName)
                } else {
                    Remove-Item -LiteralPath $b.FullName -Recurse -Force -ErrorAction SilentlyContinue
                }
            }
            $removed = $true
        }
    }

    if ($removed) {
        Write-Host ''
        Write-Host 'Removed. Your settings and logs were NOT touched; they live in'
        Write-Host '  %APPDATA%\MoxaSerial\'
        Write-Host 'Delete that folder too if you want a completely clean slate.'
    }
}


# --------------------------------------------------------------------------

$addInsDir = Get-AddInsDir -Explicit $Dest

if ($Uninstall) {
    Invoke-Uninstall -AddIns $addInsDir
} else {
    Invoke-Install -AddIns $addInsDir
}
