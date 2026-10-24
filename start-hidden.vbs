' web-platform (RemoteConnecter v2) hidden background launcher
' Called by the Startup shortcut (web-platform.lnk -> wscript.exe this.vbs);
' double-click also works. Runs app.py with a HIDDEN console (Shell.Run style 0)
' and appends stdout/stderr to run.log via cmd redirection. Exits immediately;
' the app keeps running in the background.
Dim shell, fso, appDir, pyPath, logPath, http, portInUse

Set shell = CreateObject("Wscript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

appDir = fso.GetParentFolderName(WScript.ScriptFullName)
pyPath = appDir & "\.venv\Scripts\python.exe"
logPath = appDir & "\run.log"

' Best-effort log append: when the app is running, cmd's >> redirection
' holds run.log and an FSO append open fails (sharing violation) - that
' must never block the launcher logic, so errors are swallowed here.
Sub AppendLog(msg)
    On Error Resume Next
    Dim ts
    Set ts = fso.OpenTextFile(logPath, 8, True)
    If Err.Number = 0 Then
        ts.WriteLine msg
        ts.Close
    End If
    On Error Goto 0
End Sub

' Rotate a log once at startup: >10MB -> .1 (one backup generation).
Sub RotateLog(path, maxBytes)
    On Error Resume Next
    If fso.FileExists(path) Then
        If fso.GetFile(path).Size > maxBytes Then
            If fso.FileExists(path & ".1") Then fso.DeleteFile path & ".1", True
            fso.MoveFile path, path & ".1"
        End If
    End If
    On Error Goto 0
End Sub

RotateLog logPath, 10485760

' This launcher is the only autostart entry that survives cleanup tools, so it
' also brings up the two session daemons and the watchdog. Their own scripts
' check the port first, so repeated calls are harmless.
Dim helper
For Each helper In Array("start-locald.vbs", "start-sshd.vbs", "start-watchdog.vbs")
    If fso.FileExists(appDir & "\" & helper) Then
        shell.Run "wscript.exe """ & appDir & "\" & helper & """", 0, False
    End If
Next

' Skip when the app is already serving on 6789 (avoid double start)
portInUse = False
On Error Resume Next
Set http = CreateObject("MSXML2.XMLHTTP")
http.open "GET", "http://127.0.0.1:6789/login", False
http.send
If Err.Number = 0 Then portInUse = True
On Error Goto 0
If portInUse Then
    AppendLog Now & " [SKIP] port 6789 already in use - app already running, nothing to do"
    WScript.Quit 0
End If

If Not fso.FileExists(pyPath) Then
    AppendLog Now & " [FATAL] venv python not found: " & pyPath & " (run: uv sync)"
    WScript.Quit 1
End If

shell.CurrentDirectory = appDir
AppendLog Now & " [START] launching app.py on port 6789 via hidden cmd"

' NOTE: do NOT use shell.Exec here - Exec allocates a visible console
' window for the child when the host (wscript) has no console.
' Window style 0 = hidden; cmd does the file redirection (python -u = unbuffered).
shell.Run "cmd /c """"" & pyPath & """ -u app.py >> run.log 2>&1""", 0, False
