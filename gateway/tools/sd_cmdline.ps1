Get-CimInstance Win32_Process -Filter "Name='sd-server.exe'" | Select-Object -ExpandProperty CommandLine
