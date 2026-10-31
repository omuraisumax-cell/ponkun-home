Option Explicit

Dim shell, fso, root, pythonw, script, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
pythonw = shell.Environment("PROCESS")("PONKUN_PYTHONW")
If Len(pythonw) = 0 Then pythonw = "pythonw.exe"
script = root & "\ponkun_hub\control_center.pyw"
command = Chr(34) & pythonw & Chr(34) & " " & Chr(34) & script & Chr(34)
shell.CurrentDirectory = root
shell.Run command, 0, False
