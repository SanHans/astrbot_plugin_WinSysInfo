param(
  [string]$HostIp = "127.0.0.1",
  [int]$Port = 8765,
  [string]$Token = ""
)

if ($Token -ne "") {
  $env:WINSYSINFO_TOKEN = $Token
}

python -m uvicorn agent:app --host $HostIp --port $Port
