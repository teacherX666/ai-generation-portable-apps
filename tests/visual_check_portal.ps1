$ErrorActionPreference = 'Stop'
$chrome = @(
  'C:\Program Files\Google\Chrome\Application\chrome.exe',
  'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
  'C:\Program Files\Microsoft\Edge\Application\msedge.exe'
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $chrome) { throw 'No Chrome or Edge executable found.' }

$cssUrl = 'file:///C:/Users/123/Documents/LTX/ai-generation-portable-apps/portal/static/styles.css'
$cssPath = 'C:\Users\123\Documents\LTX\ai-generation-portable-apps\portal\static\styles.css'
$css = [System.IO.File]::ReadAllText($cssPath)
$favBase = [regex]::Match($css, '\.hist-card-fav\s*\{[^{}]*\}').Value
$favHover = [regex]::Match($css, '\.hist-card-fav:hover,\s*\.hist-card-fav:focus,\s*\.hist-card-fav:focus-visible\s*\{[^{}]*\}').Value
$assertions = @(
  @{ Name='favorite base background transparent'; Ok=$favBase -match 'background:\s*transparent' },
  @{ Name='favorite base border none'; Ok=$favBase -match 'border:\s*0' },
  @{ Name='favorite hover lift'; Ok=$favHover -match 'transform:\s*translateY\(-2px\)' },
  @{ Name='favorite hover no blue background'; Ok=$favHover -notmatch 'var\(--accent\)' }
)
foreach ($a in $assertions) {
  if (-not $a.Ok) { throw ("Visual guard failed: " + $a.Name) }
  Write-Output ("PASS " + $a.Name)
}$outRoot = Join-Path $env:TEMP 'portal_visual_check'
New-Item -ItemType Directory -Force -Path $outRoot | Out-Null

function New-Fixture {
  param([string]$Path, [bool]$Active)
  $activeCls = if ($Active) { ' active' } else { '' }
  $star = if ($Active) { [char]0x2605 } else { [char]0x2606 }
  $activeBtn = if ($Active) { ' active' } else { '' }
  $html = @"
<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><link rel="stylesheet" href="$cssUrl"></head>
<body class="ui-shell">
  <aside class="portal-nav" style="width:260px;height:100vh;position:relative;display:flex;flex-direction:column;">
    <div class="portal-nav__group">
      <div class="portal-nav__title">Creation</div>
      <div class="portal-nav__items">
        <button class="app-tab ui-tab$activeBtn" data-tab="feishu-generation-agent"><span class="portal-nav__label">Feishu Creator</span><span class="portal-nav__desc">Plan tasks and approve before generation</span></button>
        <button class="app-tab ui-tab" data-tab="seedance"><span class="portal-nav__label">Video</span><span class="portal-nav__desc">Text or image to video</span></button>
      </div>
    </div>
  </aside>
  <div style="padding:20px;display:grid;grid-template-columns:260px;gap:16px;">
    <div class="hist-card">
      <div class="hist-media"><div class="hist-media-empty">Preview</div></div>
      <div class="hist-card-body">
        <div class="hist-card-t1">Test prompt</div>
        <div class="hist-card-t2"><span>alice · 10:30</span><span class="hist-card-meta"><span class="hist-card-fav$activeCls" role="button" tabindex="0">$star</span><span class="pill">Done</span></span></div>
      </div>
    </div>
  </div>
</body></html>
"@
  [System.IO.File]::WriteAllText($Path, $html, [System.Text.UTF8Encoding]::new($false))
}

$normal = Join-Path $outRoot 'normal.html'
$active = Join-Path $outRoot 'active.html'
New-Fixture -Path $normal -Active $false
New-Fixture -Path $active -Active $true

$shots = @(
  @{ Html=$normal; Out=(Join-Path $outRoot 'normal.png'); Size='1280,900' },
  @{ Html=$active; Out=(Join-Path $outRoot 'active.png'); Size='1280,900' },
  @{ Html=$normal; Out=(Join-Path $outRoot 'mobile.png'); Size='390,844' }
)
foreach ($s in $shots) {
  & $chrome '--headless=new' '--disable-gpu' '--hide-scrollbars' "--window-size=$($s.Size)" "--screenshot=$($s.Out)" "file:///$($s.Html -replace '\\','/')"
}
Get-ChildItem $outRoot -Filter *.png | Select-Object FullName,Length