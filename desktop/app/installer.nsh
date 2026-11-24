; PrometheOS desktop installer additions (CP19.2, decision 0054).
; electron-builder keeps a copy of the installer for its updater under %LOCALAPPDATA%; its own
; uninstaller leaves it behind (decision 0050, review s3b2-a F2), so this one removes it.
; The PrometheOS home (projects, settings) is never touched: uninstalling keeps project data.
!macro customUnInstall
  RMDir /r "$LOCALAPPDATA\prometheos-desktop-updater"
!macroend
