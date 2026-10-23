' Launch QQ-DeepSeek bridge with no console window.
' Double-click to start silently; use "stop-all.bat" (Stop) to stop.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
base = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = base
sh.Run """" & base & "\service.bat""", 0, False
