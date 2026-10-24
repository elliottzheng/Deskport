' local session daemon launcher (sessiond.py local)
' Started at logon via Startup shortcut; independent from web.
Dim shell, fso, appDir, pyPath, logPath, http, portInUse
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
appDir = fso.GetParentFolderName(WScript.ScriptFullName)
pyPath = appDir & "\.venv\Scripts\python.exe"
logPath = appDir & "\locald.log"

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

portInUse = False
On Error Resume Next
Set http = CreateObject("MSXML2.XMLHTTP")
http.open "GET", "http://127.0.0.1:6791/health", False
http.send
If Err.Number = 0 Then portInUse = True
On Error Goto 0
If portInUse Then WScript.Quit 0
If Not fso.FileExists(pyPath) Then WScript.Quit 1
shell.CurrentDirectory = appDir
shell.Run "cmd /c """"" & pyPath & """ -u sessiond.py local >> """ & logPath & """ 2>&1""", 0, False