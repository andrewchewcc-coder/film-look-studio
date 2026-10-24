-- Film Look Studio as a Mac app: click to start (no Terminal window), Quit from the Dock to stop.
-- Built by make_app.sh into a stay-open applet. The server's messages go to
-- ~/Library/Logs/Film Look Studio.log.

property appURL : "http://localhost:8765"
property startedAt : missing value

on projectDir()
	return "__PROJECT_DIR__"
end projectDir

on isRunning()
	try
		do shell script "/usr/sbin/lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1"
		return true
	on error
		return false
	end try
end isRunning

on run
	if not isRunning() then
		set logFile to (POSIX path of (path to library folder from user domain)) & "Logs/Film Look Studio.log"
		do shell script "cd " & quoted form of projectDir() & " && (echo; echo \"=== started $(date) ===\") >> " & quoted form of logFile & " && nohup ./studio serve --no-browser >> " & quoted form of logFile & " 2>&1 &"
		repeat 240 times -- wait up to two minutes (macOS may be asking for permission)
			if isRunning() then exit repeat
			delay 0.5
		end repeat
		if not isRunning() then
			set msg to "Film Look Studio didn't start." & return & return & ¬
				"The first time, macOS asks whether it may use your Documents folder (where the app lives) and your external drives (your photos). Click Allow for both — the boxes can hide behind other windows." & return & return & ¬
				"If you clicked Don't Allow: open Privacy Settings › Files and Folders, find Film Look Studio and switch on Documents Folder and Removable Volumes. Then click the icon again."
			set b to button returned of (display dialog msg buttons {"Open Privacy Settings", "OK"} default button "OK" with title "Film Look Studio" with icon caution)
			if b is "Open Privacy Settings" then open location "x-apple.systempreferences:com.apple.preference.security?Privacy_FilesAndFolders"
			quit
			return
		end if
	end if
	set startedAt to current date
	open location appURL
end run

on reopen
	-- clicking the icon again (or in the Dock) just shows the app
	if not isRunning() then
		run
	else
		open location appURL
	end if
end reopen

on idle
	-- if the server stopped on its own, take the Dock icon away too
	if startedAt is not missing value and ((current date) - startedAt) > 60 and not isRunning() then
		quit
	end if
	return 20
end idle

on quit
	try
		do shell script "/usr/sbin/lsof -nP -tiTCP:8765 -sTCP:LISTEN | xargs kill"
	end try
	continue quit
end quit
