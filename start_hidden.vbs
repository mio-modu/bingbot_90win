Dim WshShell, sFolder
Set WshShell = CreateObject("WScript.Shell")

' VBS 파일이 있는 폴더를 자동으로 읽음 (한글 경로 하드코딩 회피)
sFolder = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
WshShell.CurrentDirectory = sFolder

' python을 PATH에서 찾아 watchdog 실행 (창 없음)
WshShell.Run "python watchdog.py --auto", 0, False
