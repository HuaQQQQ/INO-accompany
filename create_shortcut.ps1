$desktop = [Environment]::GetFolderPath('Desktop')
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut("$desktop\INO Companion.lnk")
$targetDir = $PSScriptRoot
$sc.TargetPath = Join-Path $targetDir 'start_ino.bat'
$sc.WorkingDirectory = $targetDir
$sc.Description = 'Launch INO Desktop Companion Agent'
$sc.IconLocation = 'shell32.dll,14'
$sc.Save()
Write-Host "Desktop shortcut created successfully at: $desktop\INO Companion.lnk"
