<#
.SYNOPSIS
  Smoke test the running QR code generator server.

.DESCRIPTION
  Hits 11 end-to-end scenarios against a server you've already started
  (uvicorn app.main:app). Each one prints PASS / FAIL with a one-line
  diagnostic; exits non-zero on any failure so it can gate a release.

  Authenticated endpoints (POST create, GET /api/qr/mine) require a
  session cookie. The script doesn't run the magic-link flow itself
  (the email is printed to the server console, which a script can't
  easily read), so you sign in via the browser once and pass the
  cookie value via -SessionCookie or the QRS_SESSION_COOKIE env var.

.PARAMETER BaseUrl
  Base URL of the running server. Defaults to http://127.0.0.1:8001.

.PARAMETER SessionCookie
  The value of the `qrs_session` cookie from a signed-in browser.
  Falls back to the QRS_SESSION_COOKIE env var if not passed.

.EXAMPLE
  # 1. Start the server:
  #      uvicorn app.main:app --reload --port 8001
  # 2. Sign in via http://127.0.0.1:8001/ in your browser.
  # 3. DevTools -> Application -> Cookies -> copy the qrs_session value.
  # 4. Run the smoke script:
  #      $env:QRS_SESSION_COOKIE = '<paste the value>'
  #      .\scripts\smoke.ps1
  #    or with an explicit parameter:
  #      .\scripts\smoke.ps1 -SessionCookie '<paste the value>'
#>
[CmdletBinding()]
param(
    [string]$BaseUrl       = 'http://127.0.0.1:8001',
    [string]$SessionCookie = $env:QRS_SESSION_COOKIE
)

$ErrorActionPreference = 'Stop'
$script:passes = 0
$script:fails  = 0

if (-not $SessionCookie) {
    Write-Host @"
ERROR: no session cookie provided.

Sign in at $BaseUrl in your browser, then copy the `qrs_session`
cookie value from DevTools and either pass it via -SessionCookie
or set `$env:QRS_SESSION_COOKIE`.

POST /api/qr/create has required auth since commit 564d3b5;
the smoke script can't proceed without it.
"@ -ForegroundColor Yellow
    exit 2
}

function Assert-Eq {
    param([string]$Label, $Got, $Want)
    if ($Got -eq $Want) {
        Write-Host "  PASS  $Label" -ForegroundColor Green
        $script:passes++
    } else {
        Write-Host "  FAIL  $Label (got $Got, want $Want)" -ForegroundColor Red
        $script:fails++
    }
}

# Raw HTTP status without auto-follow. PowerShell 5.1's
# Invoke-WebRequest -MaximumRedirection 0 throws on 3xx; drop to the
# .NET HttpWebRequest API for consistent behavior across PS versions.
function Get-StatusAndLocation {
    param([string]$Url)
    $req = [System.Net.HttpWebRequest]::Create($Url)
    $req.AllowAutoRedirect = $false
    $req.Method = 'GET'
    try {
        $resp = $req.GetResponse()
        try {
            return @{ Status = [int]$resp.StatusCode; Location = $resp.Headers['Location'] }
        } finally { $resp.Close() }
    } catch [System.Net.WebException] {
        $resp = $_.Exception.Response
        if ($null -eq $resp) { throw }
        try {
            return @{ Status = [int]$resp.StatusCode; Location = $resp.Headers['Location'] }
        } finally { $resp.Close() }
    }
}

# Common request shape: a WebRequestSession that carries the session
# cookie on every Invoke-{RestMethod,WebRequest} call. We can't just
# pass `Cookie` through `-Headers`: PowerShell treats Cookie as a
# "restricted" header and silently drops it from the request.
$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
$cookieUri = [Uri]$BaseUrl
$session.Cookies.Add(
    [System.Net.Cookie]::new('qrs_session', $SessionCookie, '/', $cookieUri.Host)
)

Write-Host "Smoke-testing $BaseUrl" -ForegroundColor Cyan
$cookiePreview = $SessionCookie.Substring(0, [Math]::Min(10, $SessionCookie.Length))
Write-Host "  using session cookie: ${cookiePreview}..." -ForegroundColor DarkGray
Write-Host ''

# ----- Pre-flight: confirm the session cookie is valid -----------------
$me = Invoke-RestMethod -Uri "$BaseUrl/api/auth/me" -WebSession $session
if (-not $me.user) {
    Write-Host "  FAIL  pre-flight: session cookie is not valid (server says user=null)" -ForegroundColor Red
    Write-Host "        re-sign-in via the browser and grab a fresh qrs_session value." -ForegroundColor Yellow
    exit 2
}
Write-Host "  Signed in as: $($me.user.email) (id=$($me.user.id))" -ForegroundColor DarkGray
Write-Host ''

# ----- 1. POST create (requires auth) ----------------------------------
$create = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/qr/create" `
    -ContentType 'application/json' -WebSession $session `
    -Body '{"url": "https://example.com"}'
$token = $create.token
$editToken = $create.edit_token
$bearerHeaders = @{ 'Authorization' = "Bearer $editToken" }
Assert-Eq '1. POST create returns 7-char token' $token.Length 7
Assert-Eq '   short_url ends with token' $create.short_url.EndsWith("/r/$token") $true
Assert-Eq '   create response includes edit_token' ($editToken.Length -ge 32) $true

# ----- 2. GET /r/{token} -> 302 ----------------------------------------
$r2 = Get-StatusAndLocation "$BaseUrl/r/$token"
Assert-Eq '2. GET /r/{token} status 302' $r2.Status 302
Assert-Eq '   Location header = original URL' $r2.Location 'https://example.com'

# ----- 3. GET /api/qr/{token} info -------------------------------------
$info = Invoke-RestMethod -Uri "$BaseUrl/api/qr/$token"
Assert-Eq '3. GET info returns matching token' $info.token $token

# ----- 4. PATCH url via Bearer (deliberately, NOT session cookie) ------
# Exercises the bearer-only auth path. The owner-shortcut path is
# covered by the pytest suite.
$patched = Invoke-RestMethod -Method Patch -Uri "$BaseUrl/api/qr/$token" `
    -ContentType 'application/json' -Headers $bearerHeaders `
    -Body '{"url": "https://new-target.com"}'
Assert-Eq '4. PATCH (Bearer) returns updated original_url' $patched.original_url 'https://new-target.com'

# ----- 5. Redirect now points at the new URL ---------------------------
$r5 = Get-StatusAndLocation "$BaseUrl/r/$token"
Assert-Eq '5. Redirect after PATCH points at new URL' $r5.Location 'https://new-target.com'

# ----- 6. DELETE via Bearer --------------------------------------------
$del = Invoke-RestMethod -Method Delete -Uri "$BaseUrl/api/qr/$token" -Headers $bearerHeaders
Assert-Eq '6. DELETE returns "Deleted"' $del.detail 'Deleted'

# ----- 7. Redirect after delete -> 410 ---------------------------------
$r7 = Get-StatusAndLocation "$BaseUrl/r/$token"
Assert-Eq '7. Redirect after DELETE is 410' $r7.Status 410

# ----- 8. Non-existent token -> 404 ------------------------------------
$r8 = Get-StatusAndLocation "$BaseUrl/r/NOPE123"
Assert-Eq '8. Unknown token is 404' $r8.Status 404

# ----- 9. QR image returns PNG -----------------------------------------
$create2 = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/qr/create" `
    -ContentType 'application/json' -WebSession $session `
    -Body '{"url": "https://example.com"}'
$img = Invoke-WebRequest -Uri "$BaseUrl/api/qr/$($create2.token)/image" -UseBasicParsing
Assert-Eq '9. /image content-type is image/png' $img.Headers['Content-Type'] 'image/png'
$pngMagic = ($img.Content[0..3] -join ',')
Assert-Eq '   PNG magic bytes present' $pngMagic '137,80,78,71'

# ----- 10. tz-aware ISO with Z suffix does not crash --------------------
$createTz = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/qr/create" `
    -ContentType 'application/json' -WebSession $session `
    -Body '{"url": "https://example.com", "expires_at": "2099-12-31T23:59:59Z"}'
$r10 = Get-StatusAndLocation "$BaseUrl/r/$($createTz.token)"
Assert-Eq '10. Z-suffix expires_at does not crash redirect' $r10.Status 302

# ----- 11. edit_token rotation invalidates the old token ----------------
$createR = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/qr/create" `
    -ContentType 'application/json' -WebSession $session `
    -Body '{"url": "https://example.com"}'
$rotated = Invoke-RestMethod -Method Post `
    -Uri "$BaseUrl/api/qr/$($createR.token)/rotate-edit-token" `
    -Headers @{ 'Authorization' = "Bearer $($createR.edit_token)" }
Assert-Eq '11. rotate returns a different edit_token' ($rotated.edit_token -ne $createR.edit_token) $true
try {
    Invoke-RestMethod -Method Patch -Uri "$BaseUrl/api/qr/$($createR.token)" `
        -ContentType 'application/json' `
        -Headers @{ 'Authorization' = "Bearer $($createR.edit_token)" } `
        -Body '{"url": "https://hijack.com"}' -ErrorAction Stop | Out-Null
    Assert-Eq '    old edit_token rejected after rotation' 'unexpected-success' '401'
} catch {
    $code = [int]$_.Exception.Response.StatusCode
    Assert-Eq '    old edit_token rejected after rotation' $code 401
}

# ----- 12. Anonymous create is 401 -------------------------------------
# Send WITHOUT the session cookie and without a bearer -> should reject.
try {
    Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/qr/create" `
        -ContentType 'application/json' `
        -Body '{"url": "https://example.com"}' -ErrorAction Stop | Out-Null
    Assert-Eq '12. Anonymous create returns 401' 'unexpected-success' '401'
} catch {
    $code = [int]$_.Exception.Response.StatusCode
    Assert-Eq '12. Anonymous create returns 401' $code 401
}

Write-Host ''
if ($script:fails -eq 0) {
    Write-Host "All $script:passes assertions passed." -ForegroundColor Green
    exit 0
} else {
    Write-Host "$script:fails failure(s), $script:passes pass(es)." -ForegroundColor Red
    exit 1
}
