' Argus — run the alert checks with no visible window.
'
' The scheduled task ("Argus Alert Checks") calls this instead of run_checks.bat
' directly, so a console window doesn't flash on screen every 15 minutes.
'
' Manual test:  wscript.exe run_checks_silent.vbs   (silent — check exit_checker.log)
' To see output instead, just run run_checks.bat.

Dim fso, sh, scriptDir
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = scriptDir

' 0 = hidden window, True = wait so the task's run state reflects the real result
sh.Run "cmd /c """ & scriptDir & "\run_checks.bat""", 0, True
