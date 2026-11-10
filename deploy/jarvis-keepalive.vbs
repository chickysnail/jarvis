' Keeps WSL (and so the Jarvis server) running after Windows login, with no visible window.
' Put a copy in the Startup folder: Win+R, shell:startup
CreateObject("WScript.Shell").Run "wsl.exe -d Ubuntu --exec sleep infinity", 0, False
