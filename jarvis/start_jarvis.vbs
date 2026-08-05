Option Explicit
Dim shell, fso, folder, pythonw, script, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = folder & "\.venv\Scripts\pythonw.exe"
script = folder & "\jarvis.py"

If Not fso.FileExists(pythonw) Then
    MsgBox "Jarvis is not installed correctly. Run install.bat first.", 16, "Jarvis"
    WScript.Quit 1
End If

shell.CurrentDirectory = folder
shell.Environment("PROCESS")("JARVIS_LAUNCH_MODE") = "background"
command = Chr(34) & pythonw & Chr(34) & " " & Chr(34) & script & Chr(34)
shell.Run command, 0, False
