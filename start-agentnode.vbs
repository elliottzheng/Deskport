' local agent node launcher (agent_node/agent_node.py)
' watchdog checks http://127.0.0.1:8622/health and starts this when down.
Dim shell, fso, appDir, pyPath, logPath, http, portInUse, tokenPath, nodeDir
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
appDir = fso.GetParentFolderName(WScript.ScriptFullName)
pyPath = appDir & "\.venv\Scripts\python.exe"
logPath = appDir & "\agentnode.log"
nodeDir = appDir & "\agent_node"
tokenPath = shell.ExpandEnvironmentStrings("%USERPROFILE%") & "\.deskport-agent-node\token"

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
http.open "GET", "http://127.0.0.1:8622/health", False
http.send
If Err.Number = 0 Then portInUse = True
On Error Goto 0
If portInUse Then WScript.Quit 0
If Not fso.FileExists(pyPath) Then WScript.Quit 1
If Not fso.FileExists(nodeDir & "\agent_node.py") Then WScript.Quit 1
shell.CurrentDirectory = nodeDir
shell.Run "cmd /c """"" & pyPath & """ -u agent_node.py --port 8622 --token-file """ & _
          tokenPath & """ >> """ & logPath & """ 2>&1""", 0, False
