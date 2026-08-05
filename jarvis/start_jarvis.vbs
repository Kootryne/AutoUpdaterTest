Option Explicit
Dim shell, fso, folder, pythonw, script, command, launchMode
Dim dataFolder, resultPath, tempPath, output

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = folder & "\.venv\Scripts\pythonw.exe"
script = folder & "\jarvis.py"
launchMode = LCase(shell.Environment("PROCESS")("JARVIS_LAUNCH_MODE"))

' During an update started from run_jarvis.bat, the batch window is still alive.
' Signal it to restart Jarvis in that same console instead of starting pythonw.
If launchMode = "console" Then
    dataFolder = folder & "\data"
    If Not fso.FolderExists(dataFolder) Then
        fso.CreateFolder(dataFolder)
    End If

    resultPath = dataFolder & "\update_result.json"
    tempPath = dataFolder & "\update_result.tmp"
    Set output = fso.CreateTextFile(tempPath, True, True)
    output.Write "{""status"":""finished"",""message"":""Update finished. Restarting Jarvis in this window.""}"
    output.Close

    If fso.FileExists(resultPath) Then
        fso.DeleteFile resultPath, True
    End If
    fso.MoveFile tempPath, resultPath
    WScript.Quit 0
End If

If Not fso.FileExists(pythonw) Then
    MsgBox "Jarvis is not installed correctly. Run install.bat first.", 16, "Jarvis"
    WScript.Quit 1
End If

shell.CurrentDirectory = folder
shell.Environment("PROCESS")("JARVIS_LAUNCH_MODE") = "background"
command = Chr(34) & pythonw & Chr(34) & " " & Chr(34) & script & Chr(34)
shell.Run command, 0, False
