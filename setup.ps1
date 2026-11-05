# =========================================================
#  AI動画編集ツール  全自動セットアップ
#  setup.bat をダブルクリックすると、このスクリプトが動きます。
#  何度実行しても大丈夫です（更新したいときも、もう一度実行してください）。
# =========================================================
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
$App = $PSScriptRoot
Set-Location $App
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

function Step($m) { Write-Host ""; Write-Host "■ $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "  [OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [注意] $m" -ForegroundColor Yellow }
function Fail($m) {
    Write-Host ""
    Write-Host "  [失敗] $m" -ForegroundColor Red
    Write-Host "  この画面の内容をそのままコピーして、相談してください。" -ForegroundColor Red
    Write-Host ""
    Read-Host "Enterキーで閉じます"
    exit 1
}
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User") + ";" +
                "$env:LOCALAPPDATA\Microsoft\WinGet\Links"
}
function Need-Winget {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Fail "winget が使えません。Microsoft Store で「アプリ インストーラー」を更新してから、もう一度実行してください。"
    }
}

Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "  AI動画編集ツール セットアップ" -ForegroundColor Cyan
Write-Host "  初回は 20〜30分ほどかかります（約2.5GBダウンロード）" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "  場所: $App"
if (-not (Test-Path "$App\requirements.txt")) {
    Fail "requirements.txt が見つかりません。zipを展開したフォルダの中の setup.bat を実行してください。"
}
if ($App -match "OneDrive") {
    Warn "OneDrive の中に置かれています。大きなファイルが同期されてしまうので、C:\tools などに移してから実行するのがおすすめです。"
}
Refresh-Path

# ---------------------------------------------------------
Step "1/5  Python 3.12"
function Find-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $v = & py -3.12 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $v) { return ($v | Select-Object -First 1).Trim() }
    }
    foreach ($p in @("$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
                     "$env:ProgramFiles\Python312\python.exe")) {
        if (Test-Path $p) { return $p }
    }
    return $null
}
$py = Find-Python
if (-not $py) {
    Need-Winget
    Write-Host "  Python 3.12 をインストールします…"
    winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
    Refresh-Path
    $py = Find-Python
    if (-not $py) { Fail "Python のインストールに失敗しました。" }
}
Ok "Python: $py"

# ---------------------------------------------------------
Step "2/5  FFmpeg（動画の読み書き）"
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Need-Winget
    Write-Host "  FFmpeg をインストールします…"
    winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
    Refresh-Path
}
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { Fail "FFmpeg のインストールに失敗しました。" }
Ok "FFmpeg: $((Get-Command ffmpeg).Source)"

# ---------------------------------------------------------
Step "3/5  ツール専用の環境とライブラリ（ここが一番時間がかかります）"
# 文字起こしのライブラリ（ctranslate2 / onnxruntime）は Visual C++ ランタイム（MSVCP140.dll）が必要
$VCUrl = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
function Test-VCRedist {
    foreach ($k in @("HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64",
                     "HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64")) {
        $r = Get-ItemProperty -Path $k -ErrorAction SilentlyContinue
        if ($r -and $r.Installed -eq 1 -and ($r.Major -gt 14 -or ($r.Major -eq 14 -and $r.Minor -ge 40))) { return $true }
    }
    return $false
}
if (-not (Test-VCRedist)) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "  Visual C++ ランタイム（文字起こしに必要な部品）をインストールします…"
        winget install -e --id Microsoft.VCRedist.2015+.x64 --accept-package-agreements --accept-source-agreements
    }
}
if (Test-VCRedist) {
    Ok "Visual C++ ランタイム"
} else {
    Warn "Visual C++ ランタイムを確認できませんでした。文字起こしで「DLL load failed」と出たら、${VCUrl} をダウンロードして実行してください。"
}
$vpy = "$App\.venv\Scripts\python.exe"
if (-not (Test-Path $vpy)) {
    & $py -m venv "$App\.venv"
    if (-not (Test-Path $vpy)) { Fail "仮想環境（.venv）を作れませんでした。" }
}
& $vpy -m pip install --upgrade pip --quiet
& $vpy -m pip install -r "$App\requirements.txt"
if ($LASTEXITCODE -ne 0) { Fail "ライブラリのインストールに失敗しました（回線が不安定なら、もう一度 setup.bat を実行してください）。" }
Ok "ライブラリのインストール完了"
# 「最新版に更新」で、自分で直したテンプレートを見分けられるように、今のファイルの印を記録しておく（初回だけ）
& $vpy "$App\update.py" --record-manifest 2>$null | Out-Null

# ---------------------------------------------------------
Step "4/5  動作確認"
$check = & $vpy -c "import faster_whisper, anthropic, PySide6; print('ok')" 2>&1
if ($LASTEXITCODE -ne 0) {
    $check | Select-Object -Last 5 | ForEach-Object { Write-Host "  $_" }
    if ("$check" -match "DLL load failed|MSVCP140|VCRUNTIME") {
        Fail "ライブラリの読み込みに失敗しました。Visual C++ ランタイムが足りません。${VCUrl} をダウンロードして実行してから、もう一度 setup.bat を実行してください。"
    }
    Fail "ライブラリの読み込みに失敗しました。Visual C++ ランタイム（${VCUrl}）を入れてから、もう一度 setup.bat を実行してみてください。"
}
Ok "ライブラリの読み込み"
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    $gpu = & nvidia-smi --query-gpu=name --format=csv,noheader 2>$null | Select-Object -First 1
    if ($gpu) { Ok "GPU: $gpu" } else { Warn "GPUの情報が取れませんでした（CPUで動きます）" }
} else {
    Warn "NVIDIAのドライバーが見つかりません。GeForce Experience / NVIDIA App から最新にしてください（CPUでも動きます）"
}
Write-Host "  書き出しのテスト（字幕・テロップの見本を作ります）…"
& $vpy "$App\finish.py" --template-preview | Out-Null
if ($LASTEXITCODE -eq 0 -and (Test-Path "$App\templates\default\template_preview.mp4")) {
    Ok "書き出しのテスト"
} else {
    Warn "書き出しのテストに失敗しました。ツールは起動できますが、書き出しでエラーが出るかもしれません。"
}

# ---------------------------------------------------------
Step "5/5  デスクトップとスタートメニューにアイコンを作成"
$ws = New-Object -ComObject WScript.Shell
$dirs = @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs"))
foreach ($d in $dirs) {
    $lnk = $ws.CreateShortcut((Join-Path $d "AI動画編集.lnk"))
    $lnk.TargetPath = "$App\.venv\Scripts\pythonw.exe"
    $lnk.Arguments = "`"$App\gui.py`""
    $lnk.WorkingDirectory = $App
    $lnk.IconLocation = "$App\app.ico,0"
    $lnk.Description = "AI動画編集ツール"
    $lnk.Save()
}
Ok "「AI動画編集」のアイコンを作りました（デスクトップ・スタートメニュー）"

Write-Host ""
Write-Host "=============================================" -ForegroundColor Green
Write-Host "  セットアップ完了！" -ForegroundColor Green
Write-Host "  次からは、デスクトップの「AI動画編集」をダブルクリックで起動できます。" -ForegroundColor Green
Write-Host "=============================================" -ForegroundColor Green
$ans = Read-Host "今すぐ起動しますか？ (Y/n)"
if ($ans -ne "n" -and $ans -ne "N") {
    Start-Process -FilePath "$App\.venv\Scripts\pythonw.exe" -ArgumentList "`"$App\gui.py`"" -WorkingDirectory $App
}
