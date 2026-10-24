' watchdog launcher: checks web/locald/sshd every 30s, restarts via their vbs.
Dim shell, fso, appDir, pyPath, http, portInUse
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
appDir = fso.GetParentFolderName(WScript.ScriptFullName)
pyPath = appDir & "\.venv\Scripts\python.exe"

' one instance only: use a tiny lock file presence check via WMIC is overkill;
' instead check the python child count through a marker port is not available.
' The script is idempotent enough: a second watchdog only double-checks.
If Not fso.FileExists(pyPath) Then WScript.Quit 1
shell.CurrentDirectory = appDir
shell.Run "cmd /c """"" & pyPath & """ -u watchdog.py >> watchdog.log 2>&1""", 0, False