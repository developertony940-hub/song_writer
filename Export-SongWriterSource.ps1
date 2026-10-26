param(
    [string]$Repo = 'D:\AIOS\development\song_writer',
    [string]$OutputRoot = "$env:USERPROFILE\Desktop"
)

$ErrorActionPreference = 'Stop'
$Repo = (Resolve-Path -LiteralPath $Repo).Path
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$stage = Join-Path $OutputRoot "song_writer_source_$stamp"
$zip = "$stage.zip"
if (Test-Path -LiteralPath $stage) { throw "Destination already exists: $stage" }
New-Item -ItemType Directory -Path $stage | Out-Null

# Enumerate tracked files AND nonignored untracked files; do not commit or push.
$paths = & git -C $Repo -c core.quotepath=false ls-files --cached --others --exclude-standard
if ($LASTEXITCODE -ne 0) { throw 'git ls-files failed' }
$included = [System.Collections.Generic.List[string]]::new()
$excluded = [System.Collections.Generic.List[string]]::new()
$extensions = @('.py','.ps1','.psm1','.psd1','.js','.mjs','.cjs','.ts','.tsx','.jsx',
    '.html','.css','.scss','.md','.rst','.txt','.json','.toml','.yaml','.yml',
    '.ini','.cfg','.conf','.xml','.sql','.sh','.bat','.cmd','.patch','.diff',
    '.j2','.jinja','.jinja2','.svg','.csv','.lock','.example','.sample','.template')

foreach ($relative in ($paths | Sort-Object -Unique)) {
    $normalized = $relative -replace '\\','/'
    $leaf = [IO.Path]::GetFileName($relative)
    $ext = [IO.Path]::GetExtension($relative).ToLowerInvariant()
    $source = Join-Path $Repo $relative
    $blocked = $normalized -match '(^|/)(\.git|\.venv|venv|backend\.venv|node_modules|__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|projects|models|uploads|outputs|runs|logs|\.state|\.idea|\.vscode)(/|$)'
    $sensitive = $leaf -match '(?i)(^\.env($|\.)|secret|credential|authorization|acceptance-record|private|token|\.local\.|^id_rsa|^id_ed25519)' -or $ext -in @('.pem','.key','.pfx','.p12','.sqlite','.db')
    $allowed = $ext -in $extensions -or $leaf -in @('.gitignore','.gitattributes','.editorconfig','Dockerfile','Makefile','LICENSE','NOTICE')
    if ($blocked -or $sensitive -or -not $allowed) {
        $excluded.Add($relative)
        continue
    }
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        $excluded.Add("$relative [missing/deleted]")
        continue
    }
    if ((Get-Item -LiteralPath $source).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        $excluded.Add("$relative [link skipped]")
        continue
    }
    $destination = Join-Path $stage $relative
    New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
    Copy-Item -LiteralPath $source -Destination $destination
    $included.Add($relative)
}

# Include ancestor guidance only, without copying unrelated parent-directory data.
$guidance = Join-Path $stage '_ancestor_guidance'
$parent = Split-Path -Parent $Repo
$index = 0
while ($parent) {
    $instructions = Join-Path $parent 'AGENTS.md'
    if (Test-Path -LiteralPath $instructions -PathType Leaf) {
        New-Item -ItemType Directory -Path $guidance -Force | Out-Null
        Copy-Item -LiteralPath $instructions -Destination (Join-Path $guidance "ancestor-$index-AGENTS.md")
    }
    $next = Split-Path -Parent $parent
    if ($next -eq $parent) { break }
    $parent = $next
    $index++
}

$included | Set-Content -LiteralPath (Join-Path $stage '_included-files.txt') -Encoding UTF8
$excluded | Set-Content -LiteralPath (Join-Path $stage '_excluded-files.txt') -Encoding UTF8
& git -C $Repo status --short | Set-Content -LiteralPath (Join-Path $stage '_git-status.txt') -Encoding UTF8
& git -C $Repo rev-parse HEAD | Set-Content -LiteralPath (Join-Path $stage '_git-head.txt') -Encoding UTF8
& git -C $Repo branch --show-current | Set-Content -LiteralPath (Join-Path $stage '_git-branch.txt') -Encoding UTF8

Write-Host "Copied $($included.Count) source files to: $stage"
Write-Host 'REVIEW before sharing. Filename filters cannot guarantee that code/configuration contains no secrets.'
Write-Host 'Inspect config files, fixtures, docs and scripts. Remove/redact secrets in this STAGING COPY ONLY.'
Write-Host 'Review _excluded-files.txt for missing required source. Ignored files are not enumerated.'
Write-Host 'Add only required nonsecret ignored source/config templates to the staging folder.'
Invoke-Item -LiteralPath $stage
$confirmation = Read-Host 'After inspection, type ZIP to create the archive; anything else leaves only the staging folder'
if ($confirmation -cne 'ZIP') {
    Write-Host 'Archive not created. Source repository unchanged.'
    return
}
Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zip -CompressionLevel Optimal
Write-Host "Upload this file to Tasklet: $zip"
Get-FileHash -LiteralPath $zip -Algorithm SHA256 | Format-List
Write-Host 'Source repository unchanged. No commit, push, install, service startup, or generation performed.'
