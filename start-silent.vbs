' Open the GUI without a console window. Instance protection is in Python.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = here
sh.Run "pythonw.exe """ & here & "\ssh_tunnels.py"" --gui", 0, False
