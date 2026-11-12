param([Parameter(Mandatory=$true)][string]$Dist)
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
function Assert($Value, [string]$Message) {
    if (-not $Value) { throw $Message }
}
$Dist = (Resolve-Path $Dist).Path
$archive = Get-ChildItem $Dist -Filter 'poteto-*.pyz' | Select-Object -First 1
$expected = (& python $archive.FullName --version).Trim()
$root = Join-Path ([IO.Path]::GetTempPath()) ('Poteto test ' + [char]0x4E2D + [guid]::NewGuid().ToString('N'))
$bin = Join-Path $root 'bin with spaces'
$global:PotetoTestArchive = $archive.FullName
$global:PotetoTestFailDownload = $false
$global:PotetoTestRequested = $null
function global:Invoke-WebRequest {
    param($Uri, $OutFile, [switch]$UseBasicParsing, $MaximumRedirection)
    if ($global:PotetoTestFailDownload) { throw 'synthetic download failure' }
    $global:PotetoTestRequested = $Uri
    Copy-Item -LiteralPath $global:PotetoTestArchive -Destination $OutFile
}
$beforePath = $env:Path
try {
    & (Join-Path $Dist 'install.ps1') -InstallDir $bin -NoPath
    Assert ($global:PotetoTestRequested -like 'https://*/*.pyz') 'Installer did not request an HTTPS archive'
    Assert ($env:Path -eq $beforePath) '-NoPath changed the current PATH'
    $launcher = Join-Path $bin 'poteto.cmd'
    Assert ((& $launcher --version).Trim() -eq $expected) 'Native Windows launcher failed'
    $env:Path = $bin + ';' + $env:Path
    Assert ((& poteto --version).Trim() -eq $expected) 'Command was not discoverable on PATH'
    $site = Join-Path $root ('my site ' + [char]0x4F60 + [char]0x597D)
    & poteto init $site --runtime static | Out-Null
    Assert ($LASTEXITCODE -eq 0) 'Scaffolding failed'
    $config = Join-Path $site 'preview config.json'
    [IO.File]::WriteAllText($config, '{"runtime":"static","target":"preview"}', [Text.UTF8Encoding]::new($true))
    $report = (& poteto plan $site --config $config --preview --json) | ConvertFrom-Json
    Assert ($LASTEXITCODE -eq 0 -and $report.ok -and $report.directory -eq $site) 'Spaces, Unicode, or BOM config failed'
    [IO.File]::WriteAllText((Join-Path $site 'package.json'), '{"name":"poteto-local-test","version":"1.0.0","scripts":{"build":"node build.cjs"}}')
    [IO.File]::WriteAllText((Join-Path $site 'package-lock.json'), '{"name":"poteto-local-test","version":"1.0.0","lockfileVersion":3,"packages":{"":{"name":"poteto-local-test","version":"1.0.0"}}}')
    [IO.File]::WriteAllText((Join-Path $site 'build.cjs'), "const fs=require('node:fs');fs.mkdirSync('dist',{recursive:true});fs.writeFileSync('dist/index.html','built');")
    [IO.File]::WriteAllText($config, '{"runtime":"static","build_command":"npm run build","output_directory":"dist"}')
    $build = (& poteto build $site --config $config) | ConvertFrom-Json
    Assert ($LASTEXITCODE -eq 0 -and $build.ok -and (Test-Path -LiteralPath $build.artifact)) 'Local npm build or artifact generation failed'
    $report = (& poteto plan $site --prebuilt --json) | ConvertFrom-Json
    Assert ($LASTEXITCODE -eq 0 -and $report.ok -and $report.origin -eq 'prebuilt' -and $report.upload_files -eq 1) 'Prebuilt plan included source or failed'
    $node = Join-Path $root 'node app'
    & poteto init $node --runtime node | Out-Null
    $report = (& poteto plan $node --preview --json) | ConvertFrom-Json
    Assert ($report.ok -and $report.runtime -eq 'node') 'Node preflight failed'
    & poteto plan $site --preview --prod 2>$null | Out-Null
    Assert ($LASTEXITCODE -ne 0) 'Launcher did not propagate a nonzero exit code'
    $installed = [Convert]::ToBase64String([IO.File]::ReadAllBytes((Join-Path $bin 'poteto.pyz')))
    $global:PotetoTestFailDownload = $true
    $failed = $false
    try { & (Join-Path $Dist 'install.ps1') -InstallDir $bin -NoPath } catch { $failed = $true }
    Assert $failed 'Failed download was accepted'
    Assert ($installed -eq [Convert]::ToBase64String([IO.File]::ReadAllBytes((Join-Path $bin 'poteto.pyz')))) 'Failed update replaced the working CLI'
    $global:PotetoTestFailDownload = $false
    $bad = Join-Path $root 'wrong-version.pyz'
    & python -c 'import sys,zipfile; z=zipfile.ZipFile(sys.argv[1],"w"); z.writestr("__main__.py","print(\"poteto 0.0.0\")"); z.close()' $bad
    Assert ($LASTEXITCODE -eq 0) 'Could not create the wrong-version test archive'
    $global:PotetoTestArchive = $bad
    $failed = $false
    try { & (Join-Path $Dist 'install.ps1') -InstallDir $bin -NoPath } catch { $failed = $true }
    Assert $failed 'Wrong archive version was accepted'
    Assert ($installed -eq [Convert]::ToBase64String([IO.File]::ReadAllBytes((Join-Path $bin 'poteto.pyz')))) 'Wrong archive version replaced the working CLI'
    Write-Output 'PASS: Windows installation, PATH, Unicode paths, BOM config, static/Node plans, exit codes and update recovery'
}
finally {
    $env:Path = $beforePath
    Remove-Item Function:\Invoke-WebRequest
    if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force }
}
