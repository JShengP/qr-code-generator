<#
.SYNOPSIS
  Smoke test the running QR code generator server.

.DESCRIPTION
  Hits the 8 PROMPT.md scenarios plus a tz-aware-expiry regression
  against a server you've already started (uvicorn app.main:app).
  Each scenario prints PASS or FAIL with a one-line diagnostic; exits
  non-zero on any failure so it can gate a release pipeline.

.PARAMETER BaseUrl
  Base URL of the running server. Defaults to http://127.0.0.1:8000.

.EXAMPLE
  # In one terminal:
  #   .\.venv\Scripts\Activate.ps1
  #   uvicorn app.main:app --reload
  # In another:
  #   .\scripts\smoke.ps1
#>
[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://127.0.0.1:8000'
)

$ErrorActionPreference = 'Stop'
$script:passes = 0
$script:fails  = 0

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

# Invoke-WebRequest raises on 3xx/4xx/5xx by default; we want to inspect the
# status code without that, so we catch and unwrap.
function Get-StatusAndLocation {
    param([string]$Url)
    try {
        $r = Invoke-WebRequest -Uri $Url -MaximumRedirection 0 -UseBasicParsing -ErrorAction Stop
        return @{ Status = [int]$r.StatusCode; Location = $r.Headers.Location }
    } catch [System.Net.WebException] {
        $resp = $_.Exception.Response
        if ($null -eq $resp) { throw }
        $loc = $resp.Headers['Location']
        return @{ Status = [int]$resp.StatusCode; Location = $loc }
    } catch {
        # PS 7+ surfaces HttpResponseException
        $resp = $_.Exception.Response
        if ($null -ne $resp) {
            return @{ Status = [int]$resp.StatusCode; Location = $resp.Headers.Location }
        }
        throw
    }
}

Write-Host "Smoke-testing $BaseUrl" -ForegroundColor Cyan
Write-Host ''

# ----- 1. POST create -------------------------------------------------------
$create = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/qr/create" `
    -ContentType 'application/json' -Body '{"url": "https://example.com"}'
$token = $create.token
$editToken = $create.edit_token
$authHeaders = @{ 'Authorization' = "Bearer $editToken" }
Assert-Eq '1. POST create returns 7-char token' $token.Length 7
Assert-Eq '   short_url ends with token' $create.short_url.EndsWith("/r/$token") $true
Assert-Eq '   create response includes edit_token' ($editToken.Length -ge 32) $true

# ----- 2. GET /r/{token} -> 302 --------------------------------------------
$r2 = Get-StatusAndLocation "$BaseUrl/r/$token"
Assert-Eq '2. GET /r/{token} status 302' $r2.Status 302
Assert-Eq '   Location header = original URL' $r2.Location 'https://example.com'

# ----- 3. GET /api/qr/{token} info -----------------------------------------
$info = Invoke-RestMethod -Uri "$BaseUrl/api/qr/$token"
Assert-Eq '3. GET info returns matching token' $info.token $token

# ----- 4. PATCH url (requires edit_token) -----------------------------------
$patched = Invoke-RestMethod -Method Patch -Uri "$BaseUrl/api/qr/$token" `
    -ContentType 'application/json' `
    -Headers $authHeaders `
    -Body '{"url": "https://new-target.com"}'
Assert-Eq '4. PATCH returns updated original_url' $patched.original_url 'https://new-target.com'

# ----- 5. Redirect now goes to new URL --------------------------------------
$r5 = Get-StatusAndLocation "$BaseUrl/r/$token"
Assert-Eq '5. Redirect after PATCH points at new URL' $r5.Location 'https://new-target.com'

# ----- 6. DELETE (requires edit_token) --------------------------------------
$del = Invoke-RestMethod -Method Delete -Uri "$BaseUrl/api/qr/$token" -Headers $authHeaders
Assert-Eq '6. DELETE returns "Deleted"' $del.detail 'Deleted'

# ----- 7. Redirect after delete -> 410 --------------------------------------
$r7 = Get-StatusAndLocation "$BaseUrl/r/$token"
Assert-Eq '7. Redirect after DELETE is 410' $r7.Status 410

# ----- 8. Non-existent token -> 404 -----------------------------------------
$r8 = Get-StatusAndLocation "$BaseUrl/r/NOPE123"
Assert-Eq '8. Unknown token is 404' $r8.Status 404

# ----- 9. QR image returns PNG ----------------------------------------------
$create2 = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/qr/create" `
    -ContentType 'application/json' -Body '{"url": "https://example.com"}'
$img = Invoke-WebRequest -Uri "$BaseUrl/api/qr/$($create2.token)/image" -UseBasicParsing
Assert-Eq '9. /image content-type is image/png' $img.Headers['Content-Type'] 'image/png'
$png_magic = ($img.Content[0..3] -join ',')
Assert-Eq '   PNG magic bytes present' $png_magic '137,80,78,71'

# ----- 10. tz-aware ISO with Z suffix does not crash (Stage 4 regression) --
$createTz = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/qr/create" `
    -ContentType 'application/json' -Body '{"url": "https://example.com", "expires_at": "2099-12-31T23:59:59Z"}'
$r10 = Get-StatusAndLocation "$BaseUrl/r/$($createTz.token)"
Assert-Eq '10. Z-suffix expires_at does not crash redirect' $r10.Status 302

# ----- 11. edit_token rotation invalidates the old token --------------------
$createR = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/qr/create" `
    -ContentType 'application/json' -Body '{"url": "https://example.com"}'
$rotated = Invoke-RestMethod -Method Post `
    -Uri "$BaseUrl/api/qr/$($createR.token)/rotate-edit-token" `
    -Headers @{ 'Authorization' = "Bearer $($createR.edit_token)" }
Assert-Eq '11. rotate returns a different edit_token' ($rotated.edit_token -ne $createR.edit_token) $true
# Old token must now fail
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

Write-Host ''
if ($script:fails -eq 0) {
    Write-Host "All $script:passes assertions passed." -ForegroundColor Green
    exit 0
} else {
    Write-Host "$script:fails failure(s), $script:passes pass(es)." -ForegroundColor Red
    exit 1
}
