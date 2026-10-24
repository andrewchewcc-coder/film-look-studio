-- Film Look Studio as a Mac app: click to start (no Terminal window) and open it in the browser.
-- The icon doesn't stay in the Dock: quit with the app's Quit button, or close the browser — it
-- stops by itself after 10 minutes with no page open. All the work is in launch.sh next to this
-- file (changes there need no rebuild). Messages go to ~/Library/Logs/Film Look Studio.log.

on run
	try
		do shell script quoted form of "__PROJECT_DIR__/mac-app/launch.sh" & " start"
	on error
		set msg to "Film Look Studio didn't start." & return & return & ¬
			"The first time, macOS asks whether it may use your Documents folder (where the app lives) and your external drives (your photos). Click Allow for both — the boxes can hide behind other windows." & return & return & ¬
			"If you clicked Don't Allow: open Privacy Settings › Files and Folders, find Film Look Studio and switch on Documents Folder and Removable Volumes. Then click the icon again."
		set b to button returned of (display dialog msg buttons {"Open Privacy Settings", "OK"} default button "OK" with title "Film Look Studio" with icon caution)
		if b is "Open Privacy Settings" then open location "x-apple.systempreferences:com.apple.preference.security?Privacy_FilesAndFolders"
		return
	end try
	open location "http://localhost:8765"
end run
