' Argus — run the options-agent scheduler cycle with no visible window.
'
' The scheduled task ("Argus Options Agent") calls this so no console flashes every 20 min.
'
' Manual test:  wscript.exe run_agent_silent.vbs   (silent — check agent_scheduler.log)
' To see output instead, run:  venv\Scripts\python.exe scripts\run_agent.py

Dim fso, sh, scriptDir
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = scriptDir

' 0 = hidden window, True = wait so the task's run state reflects the real result
sh.Run "cmd /c """ & scriptDir & "\run_agent.bat""", 0, True
