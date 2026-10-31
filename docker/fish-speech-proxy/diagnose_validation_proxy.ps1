param(
    [switch]$Execute,
    [ValidateSet(18000, 18001, 18002)]
    [int]$Port = 18000
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Net.Http
$uri = "http://127.0.0.1:$Port/v1/audio/speech"
$targetName = "validation-proxy-$Port"

function Get-SafeClassification([int]$ProxyStatus, [Nullable[int]]$UpstreamStatus) {
    if ($null -eq $UpstreamStatus -and $ProxyStatus -eq 422) {
        return 'local-validation-failed'
    }
    $status = if ($null -ne $UpstreamStatus) { [int]$UpstreamStatus } else { $ProxyStatus }
    switch ($status) {
        200 { return 'success' }
        400 { return 'bad-request' }
        401 { return 'authentication-failed' }
        403 { return 'authorization-failed' }
        422 { return 'upstream-validation-failed' }
        429 { return 'rate-limited' }
        default {
            if ($status -ge 500 -and $status -le 599) { return 'upstream-or-server-error' }
            return 'unexpected-status'
        }
    }
}

if (-not $Execute) {
    [pscustomobject]@{
        executed = $false
        target = $targetName
        message = 'No request sent. Re-run with -Execute only after approval.'
    }
    exit 0
}

$handler = New-Object -TypeName System.Net.Http.HttpClientHandler
$client = New-Object -TypeName System.Net.Http.HttpClient -ArgumentList $handler
$client.Timeout = [TimeSpan]::FromSeconds(130)
$response = $null
$content = $null
$watch = [System.Diagnostics.Stopwatch]::StartNew()

try {
    # Fixed non-secret diagnostic input; no prompt or key is read.
    $payload = @{
        model = 's2-pro'
        voice = '5116d0b0e7f64901be4a086b7347ad2e'
        input = 'Diagnostic request.'
        response_format = 'mp3'
    } | ConvertTo-Json -Compress
    $content = New-Object -TypeName System.Net.Http.StringContent -ArgumentList @(
        $payload,
        [System.Text.Encoding]::UTF8,
        'application/json'
    )
    $request = New-Object -TypeName System.Net.Http.HttpRequestMessage -ArgumentList @(
        [System.Net.Http.HttpMethod]::Post,
        $uri
    )
    $request.Content = $content
    $response = $client.SendAsync(
        $request,
        [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead
    ).GetAwaiter().GetResult()
    $watch.Stop()

    [Nullable[int]]$upstreamStatus = $null
    $headerValues = $null
    if ($response.Headers.TryGetValues('X-Ponkun-Upstream-Status', [ref]$headerValues)) {
        $candidate = 0
        if ([int]::TryParse(($headerValues | Select-Object -First 1), [ref]$candidate)) {
            $upstreamStatus = $candidate
        }
    }
    $proxyStatus = [int]$response.StatusCode
    [pscustomobject]@{
        target = $targetName
        proxy_status = $proxyStatus
        upstream_status = $upstreamStatus
        classification = Get-SafeClassification $proxyStatus $upstreamStatus
        elapsed_ms = $watch.ElapsedMilliseconds
    }
}
catch {
    $watch.Stop()
    [pscustomobject]@{
        target = $targetName
        proxy_status = $null
        upstream_status = $null
        classification = 'transport-failure'
        elapsed_ms = $watch.ElapsedMilliseconds
    }
}
finally {
    if ($null -ne $response) { $response.Dispose() }
    if ($null -ne $content) { $content.Dispose() }
    $client.Dispose()
    $handler.Dispose()
}
