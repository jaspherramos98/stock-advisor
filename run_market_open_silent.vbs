' Argus — run the market-open routine (launch app + pipeline + arm) with no visible window.
' The "Argus Market Open" scheduled task calls this at 6:30 AM PT.
' Manual test:  wscript.exe run_market_open_silent.vbs

Dim fso, sh, scriptDir
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = scriptDir

' 0 = hidden window, True = wait so the task's run state reflects the real result
sh.Run "cmd /c """ & scriptDir & "\market_open.bat""", 0, True
