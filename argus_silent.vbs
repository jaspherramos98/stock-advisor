' Argus — silent launcher (no terminal window).
'
' argus.bat opens a console window that has to stay open for Streamlit to keep
' running, which is annoying when you want Argus always online. This wrapper runs
' the same batch file with the window hidden, so Argus runs in the background.
'
' Usage:
'   Double-click this file to start Argus with no visible terminal.
'   To start it automatically at login, put a SHORTCUT to this file in:
'       Win+R  ->  shell:startup
'   To stop Argus (there's no window to close), run argus_stop.bat.
'
' Note: argus.bat already reuses a running instance, so launching twice is safe —
' the second launch just opens the browser at the existing session.

Dim fso, sh, scriptDir
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = scriptDir

' 0 = hidden window, False = don't wait for it to finish
sh.Run "cmd /c """ & scriptDir & "\argus.bat""", 0, False
