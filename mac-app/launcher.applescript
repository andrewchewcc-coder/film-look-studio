-- Film Look Studio as a Mac app: click to start (no Terminal window), Quit from the Dock to stop.
-- Built by make_app.sh into a stay-open applet; all the work is in launch.sh next to this file
-- (so changes there need no rebuild). The server's messages go to ~/Library/Logs/Film Look Studio.log.

property appURL : "http://localhost:8765"

on launcher()
	return quoted form of "__PROJECT_DIR__/mac-app/launch.sh"
end launcher

on isRunning()
	try
		do shell script launcher() & " status"
		return true
	on error
		return false
	end try
end isRunning

on run
	try
		do shell script launcher() & " start"
	on error
		set msg to "Film Look Studio didn't start." & return & return & ¬
			"The first time, macOS asks whether it may use your Documents folder (where the app lives) and your external drives (your photos). Click Allow for both — the boxes can hide behind other windows." & return & return & ¬
			"If you clicked Don't Allow: open Privacy Settings › Files and Folders, find Film Look Studio and switch on Documents Folder and Removable Volumes. Then click the icon again."
		set b to button returned of (display dialog msg buttons {"Open Privacy Settings", "OK"} default button "OK" with title "Film Look Studio" with icon caution)
		if b is "Open Privacy Settings" then open location "x-apple.systempreferences:com.apple.preference.security?Privacy_FilesAndFolders"
		quit
		return
	end try
	open location appURL
end run

on reopen
	-- clicking the icon again (or in the Dock) just shows the app
	run
end reopen

on idle
	-- if the server stopped on its own, take the Dock icon away too
	if not isRunning() then quit
	return 30
end idle

on quit
	try
		do shell script launcher() & " stop"
	end try
	continue quit
end quit
