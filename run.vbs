' WhisperFree: silent start, no console window.
' For debugging with a visible log use run.bat.

Dim shell, fso, here, runner
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

here = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = here

runner = here & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(runner) Then
    MsgBox "Environment not found: " & runner & vbCrLf & vbCrLf & _
           "Run setup first:" & vbCrLf & _
           "  python -m venv .venv" & vbCrLf & _
           "  .venv\Scripts\python -m pip install -e .", 16, "WhisperFree"
    WScript.Quit 1
End If

shell.Run """" & runner & """ -m whisperfree", 0, False
