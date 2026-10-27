param([switch]$SkipBuild, [switch]$LtsOnly)
$ErrorActionPreference = 'Stop'
$repository = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$runId = [guid]::NewGuid().ToString('N').Substring(0,12)
$stateVolume = "ndm-test-state-$runId"
$bindVolume = "ndm-test-bind-$runId"
if (-not $SkipBuild) {
    & docker build -t ndm-test-runner:local -f (Join-Path $repository 'tests/Dockerfile') $repository
    if ($LASTEXITCODE -ne 0) { throw 'Test runner build failed' }
}
foreach ($volume in @($stateVolume,$bindVolume)) {
    & docker volume create --label "com.solqubit.ndm.test=$runId" $volume | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create test volume' }
}
try {
    [string[]]$suiteArgs = if ($LtsOnly) { @('--lts-only') } else { @('--major','--bind','--latest') }
    $bindMount = & docker volume inspect --format '{{.Mountpoint}}' $bindVolume
    if ($LASTEXITCODE -ne 0 -or -not $bindMount.StartsWith('/var/lib/docker/volumes/ndm-test-bind-')) { throw 'Unsafe bind fixture' }
    & docker run --rm --name "ndm-test-runner-$runId" --label "com.solqubit.ndm.test=$runId" `
        -v "${repository}:/workspace:ro" -v "${stateVolume}:/ndm-state" `
        -v "${bindVolume}:$bindMount" -v /var/run/docker.sock:/var/run/docker.sock `
        -e "NDM_BIND_FIXTURE=$bindMount" -w /workspace ndm-test-runner:local `
        python3 -u tests/run_integration.py @suiteArgs
    if ($LASTEXITCODE -ne 0) { throw 'Docker acceptance failed; see output above' }
} finally {
    foreach ($volume in @($stateVolume,$bindVolume)) {
        $owner = & docker volume inspect --format '{{index .Labels "com.solqubit.ndm.test"}}' $volume 2>$null
        if ($LASTEXITCODE -eq 0 -and $owner -eq $runId) {
            & docker volume rm $volume | Out-Null
        }
    }
}
