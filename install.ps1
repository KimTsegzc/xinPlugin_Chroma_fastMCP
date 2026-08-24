# 一键安装 + 启动 xinPlugin_Chroma_fastMCP（FastMCP + Chroma 知识库检索服务）
# 用法: powershell -ExecutionPolicy Bypass -File .\install.ps1
#   参数: -Port 8000   -BindHost 127.0.0.1   -NoLaunch(只安装)   -SkipInstall(跳过安装直接启动)
[CmdletBinding()]
param(
    [int]$Port = 8000,
    [string]$BindHost = '127.0.0.1',
    [switch]$NoLaunch,
    [switch]$SkipInstall
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

Write-Host "== xinPlugin_Chroma_fastMCP 安装/启动 ==" -ForegroundColor Cyan

# 1. 检查 Python
$pyCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pyCmd) {
    Write-Host "[错误] 未找到 python，请先安装 Python 3.10+ 并加入 PATH" -ForegroundColor Red
    exit 1
}
$pyPath = $pyCmd.Source
Write-Host ("[python] " + $pyPath + "  " + (python --version 2>&1))

# 2. 安装依赖
if (-not $SkipInstall) {
    Write-Host "[安装] pip install -r requirements.txt ..." -ForegroundColor Cyan
    python -m pip install --disable-pip-version-check -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[错误] 依赖安装失败" -ForegroundColor Red
        exit 1
    }
}

# 3. 校验关键模块
Write-Host "[校验] chromadb / fastmcp / pypdf / pdfminer ..."
python -c "import chromadb,fastmcp,pypdf,pdfminer;print('  import OK, chromadb', chromadb.__version__)"
if ($LASTEXITCODE -ne 0) {
    Write-Host "[错误] 模块导入失败，请检查依赖" -ForegroundColor Red
    exit 1
}

# 4. 启动服务（HTTP）
if ($NoLaunch) {
    Write-Host "[完成] 已安装（未启动）" -ForegroundColor Green
    exit 0
}

$baseUrl = "http://$BindHost`:$Port"
Write-Host ("[启动] python server.py --http --host $BindHost --port $Port") -ForegroundColor Cyan
Start-Process -FilePath $pyPath -ArgumentList @("server.py", "--http", "--host", $BindHost, "--port", $Port) -WorkingDirectory $here -WindowStyle Hidden

# 等待端口可听
$tries = 0
while ($tries -lt 10) {
    Start-Sleep -Milliseconds 500
    $listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($listening) { break }
    $tries++
}
if (-not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) {
    Write-Host "[错误] 服务未在端口 $Port 上启动，可能端口被占或 python 环境异常" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "[完成] Chroma MCP 服务已启动" -ForegroundColor Green
Write-Host "  MCP 端点(streamable-http) : $baseUrl/mcp" -ForegroundColor Cyan
Write-Host "  DSH 集成(stdio) : 由 standard-chroma preset 的 dsh-mcp-client 以 stdio 方式拉起 server.py，无需本脚本启动" -ForegroundColor Gray
Write-Host "  停止             : Get-Process python* | Stop-Process（或关闭对应 python 进程）" -ForegroundColor Gray
Write-Host ""
Write-Host "  提示: 首次使用可先入库再查询, 例如:" -ForegroundColor Gray
Write-Host "    python ingest.py <你的pdf路径> 文档名.pdf" -ForegroundColor Gray
Write-Host "    python -c \"from chroma_store import search;import json;print(json.dumps(search('广州 人工智能+ 数据要素',5),ensure_ascii=False))\"" -ForegroundColor Gray
