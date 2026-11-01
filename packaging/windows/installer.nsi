Unicode true
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "WinVer.nsh"
!include "x64.nsh"
!include "FileFunc.nsh"
!ifndef BUNDLE
  !error "Pass -DBUNDLE=<complete Jarvis folder>"
!endif
!ifndef VERSION
  !define VERSION "0.3.0"
!endif
!ifndef OUTPUT
  !define OUTPUT "Jarvis-${VERSION}-Windows-x64-Setup.exe"
!endif
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\Jarvis"
!define RUN_KEY "Software\Microsoft\Windows\CurrentVersion\Run"
Name "Jarvis ${VERSION}"
OutFile "${OUTPUT}"
InstallDir "$LOCALAPPDATA\Programs\Jarvis"
; An earlier per-user install is upgraded in place.
InstallDirRegKey HKCU "${UNINSTALL_KEY}" "InstallLocation"
RequestExecutionLevel user
SetCompressor /SOLID lzma
SetCompressorDictSize 32
ShowInstDetails show
ShowUninstDetails show
VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "Jarvis"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "FileDescription" "Jarvis offline desktop voice controls setup"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "CompanyName" "Jarvis contributors"
VIAddVersionKey "LegalCopyright" "Jarvis contributors; MIT"

!define MUI_ABORTWARNING
!define MUI_ABORTWARNING_TEXT "Stop installing Jarvis? Nothing on your PC changes until files are copied, and you can run Setup again at any time."
!define MUI_WELCOMEPAGE_TITLE "Install Jarvis ${VERSION}"
!define MUI_WELCOMEPAGE_TEXT "Jarvis is offline voice control for your desktop: say $\"Jarvis$\" followed by a command.$\r$\n$\r$\nSetup installs Jarvis for your Windows account only. It does not need administrator rights, does not download anything and does not restart your PC. Speech recognition runs entirely on this PC.$\r$\n$\r$\nIf Jarvis is already installed, Setup closes it, upgrades it in place and keeps your settings, voice names and recordings."
!define MUI_COMPONENTSPAGE_SMALLDESC
!define MUI_FINISHPAGE_TITLE "Jarvis is installed"
!define MUI_FINISHPAGE_TEXT "Jarvis starts with the microphone off. Open Privacy in Jarvis to allow listening.$\r$\n$\r$\nIf Windows blocks the microphone, turn on Settings > Privacy > Microphone > $\"Let desktop apps access your microphone$\"."
!define MUI_FINISHPAGE_RUN "$INSTDIR\Jarvis.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Open Jarvis now"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "../../LICENSE"
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_COMPONENTS
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "Jarvis requires 64-bit Windows 10 or Windows 11."
    Abort
  ${EndIf}
  ${IfNot} ${AtLeastWin10}
    MessageBox MB_ICONSTOP "Jarvis requires Windows 10 or Windows 11."
    Abort
  ${EndIf}
  SetShellVarContext current
FunctionEnd

Function StopJarvis
  ; The settings window holds files open; the service stops through its own command.
  settings:
  FindWindow $0 "TkTopLevel" "Jarvis · Settings"
  StrCmp $0 "0" service
  MessageBox MB_RETRYCANCEL|MB_ICONINFORMATION "Close the Jarvis Settings window, then choose Retry." IDRETRY settings
  Abort
  service:
  IfFileExists "$INSTDIR\JarvisConsole.exe" 0 done
  DetailPrint "Stopping the running Jarvis service…"
  ExecWait '"$INSTDIR\JarvisConsole.exe" shutdown' $0
  StrCmp $0 "0" done
  MessageBox MB_ICONSTOP "Jarvis could not finish shutting down. Choose Quit from the Jarvis tray icon, then run Setup again. Nothing was changed."
  Abort
  done:
FunctionEnd

Function un.StopJarvis
  FindWindow $0 "TkTopLevel" "Jarvis · Settings"
  StrCmp $0 "0" service
  MessageBox MB_ICONINFORMATION "Close Jarvis Settings before uninstalling. Nothing was removed."
  Abort
  service:
  IfFileExists "$INSTDIR\JarvisConsole.exe" 0 done
  ExecWait '"$INSTDIR\JarvisConsole.exe" shutdown' $0
  StrCmp $0 "0" done
  MessageBox MB_ICONSTOP "Jarvis could not finish shutting down. Choose Quit from the Jarvis tray icon, then try again. Nothing was removed."
  Abort
  done:
FunctionEnd

Section "Jarvis (required)" SecMain
  SectionIn RO
  Call StopJarvis
  SetOutPath "$INSTDIR"
  ; Register uninstall first, so an interrupted copy can still be removed or repaired.
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "Jarvis"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "Publisher" "Jarvis contributors"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "URLInfoAbout" "https://github.com/abdullaho21/jarvis"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\Jarvis.exe"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${UNINSTALL_KEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
  ; Replace program folders wholesale so no file from an older version lingers.
  ; Settings, logs and recordings live outside the program folder.
  DetailPrint "Removing the previous program files (your settings are kept)…"
  RMDir /r "$INSTDIR\_internal"
  RMDir /r "$INSTDIR\models"
  RMDir /r "$INSTDIR\tools"
  RMDir /r "$INSTDIR\licenses"
  File /r "${BUNDLE}/*"
  CreateDirectory "$SMPROGRAMS\Jarvis"
  CreateShortcut "$SMPROGRAMS\Jarvis\Jarvis.lnk" "$INSTDIR\Jarvis.exe" "" "$INSTDIR\Jarvis.exe" 0 SW_SHOWNORMAL "" "Offline voice control"
  CreateShortcut "$SMPROGRAMS\Jarvis\Uninstall Jarvis.lnk" "$INSTDIR\Uninstall.exe"
  ; Same command the app writes; keep an existing choice pointing at this install.
  ReadRegStr $0 HKCU "${RUN_KEY}" "Jarvis"
  ${If} $0 != ""
    WriteRegStr HKCU "${RUN_KEY}" "Jarvis" '"$INSTDIR\Jarvis.exe" --background'
  ${EndIf}
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "EstimatedSize" "$0"
SectionEnd

Section "Desktop shortcut" SecDesktop
  CreateShortcut "$DESKTOP\Jarvis.lnk" "$INSTDIR\Jarvis.exe" "" "$INSTDIR\Jarvis.exe" 0 SW_SHOWNORMAL "" "Offline voice control"
SectionEnd

Section /o "Start Jarvis when I sign in" SecStartup
  ; Writes the same Run entry and setting as Jarvis > Privacy > Start with Windows.
  ExecWait '"$INSTDIR\JarvisConsole.exe" startup on' $0
  ${If} $0 != 0
    DetailPrint "Could not turn on start at sign-in; use Jarvis > Privacy instead."
  ${EndIf}
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecMain} "The Jarvis app, its tray icon, offline speech models and screen-recording tools."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecDesktop} "Put a Jarvis shortcut on your desktop. Start menu shortcuts are always created."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecStartup} "Run Jarvis in the notification area after you sign in. The microphone stays off until you allow it. You can change this later in Jarvis > Privacy."
!insertmacro MUI_FUNCTION_DESCRIPTION_END

Section "un.Jarvis program files (required)" UnSecMain
  SectionIn RO
  SetShellVarContext current
  IfFileExists "$INSTDIR\BUILD-MANIFEST.json" +3 0
    MessageBox MB_ICONSTOP "The Jarvis installation manifest is missing. No files were removed."
    Abort
  Call un.StopJarvis
  DeleteRegValue HKCU "${RUN_KEY}" "Jarvis"
  DeleteRegKey HKCU "${UNINSTALL_KEY}"
  RMDir /r "$SMPROGRAMS\Jarvis"
  Delete "$DESKTOP\Jarvis.lnk"
  ; Program files only. Recordings are never deleted.
  RMDir /r "$INSTDIR\_internal"
  RMDir /r "$INSTDIR\models"
  RMDir /r "$INSTDIR\tools"
  RMDir /r "$INSTDIR\licenses"
  Delete "$INSTDIR\Jarvis.exe"
  Delete "$INSTDIR\JarvisConsole.exe"
  Delete "$INSTDIR\README.txt"
  Delete "$INSTDIR\LICENSE"
  Delete "$INSTDIR\THIRD-PARTY-NOTICES.txt"
  Delete "$INSTDIR\BUILD-MANIFEST.json"
  Delete "$INSTDIR\FFmpeg-Corresponding-Source.tar.xz"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
SectionEnd

Section /o "un.Also delete my Jarvis settings and logs" UnSecData
  ; %LOCALAPPDATA%\Jarvis: settings, voice names, logs and the replay buffer.
  RMDir /r "$LOCALAPPDATA\Jarvis"
SectionEnd

!insertmacro MUI_UNFUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${UnSecMain} "Remove the Jarvis app, shortcuts and start-at-sign-in entry. Saved recordings and clips are never deleted."
  !insertmacro MUI_DESCRIPTION_TEXT ${UnSecData} "Also remove settings, voice names and logs from %LOCALAPPDATA%\Jarvis. Leave this off to keep them for a later reinstall."
!insertmacro MUI_UNFUNCTION_DESCRIPTION_END
