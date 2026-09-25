; 1401 Probe: Windows installer. Built by windows/build-inside.sh:
;   makensis -DVERSION=<v> -DSTAGE=<dir with 1401Probe.exe, probe.json, payload/> -DOUT=<setup.exe> installer.nsi
; Installs the wizard + the stick files and nothing else: no service, no driver, no startup entry, no network.
Unicode true
SetCompressor /SOLID lzma
; the packing nonce sits at the head of the solid stream, so every later compressed byte depends on it. It is never
; installed. build-inside.sh changes it when the raw exe carries a chance name fragment (see the scan there).
!ifdef NONCE
ReserveFile "${NONCE}"
!endif
RequestExecutionLevel admin
!include "MUI2.nsh"
!include "x64.nsh"
!include "LogicLib.nsh"

!define UNINST "Software\Microsoft\Windows\CurrentVersion\Uninstall\1401Probe"
!define MUI_ICON "${STAGE}/app.ico"
!define MUI_UNICON "${STAGE}/app.ico"

Name "1401 Probe"
OutFile "${OUT}"
InstallDir "$PROGRAMFILES64\NullMoth Systems\1401 Probe"
BrandingText "1401 Probe ${VERSION}"
VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "1401 Probe"
VIAddVersionKey "FileDescription" "1401 Probe setup - hardware scanner USB maker"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "CompanyName" "NullMoth Systems"
VIAddVersionKey "LegalCopyright" "© 2026 NullMoth Systems"

!define MUI_WELCOMEPAGE_TEXT "This installs 1401 Probe, which makes a USB stick that scans this computer's hardware and saves one report file.$\r$\n$\r$\nThe scanner works offline and never touches your drives or Windows. Nothing is uploaded; you send the report yourself.$\r$\n$\r$\nClick Next to continue."
!define MUI_FINISHPAGE_RUN "$INSTDIR\1401Probe.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Open 1401 Probe now"
!define MUI_FINISHPAGE_SHOWREADME "$INSTDIR\docs\instructions.html"
!define MUI_FINISHPAGE_SHOWREADME_TEXT "Read how to use it"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${STAGE}/docs/LICENSE.txt"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "1401 Probe needs 64-bit Windows 10 or 11."
    Abort
  ${EndIf}
  SetRegView 64
  SetShellVarContext all
FunctionEnd

Function un.onInit
  SetRegView 64
  SetShellVarContext all
FunctionEnd

Section "Install"
  SetOutPath "$INSTDIR"
  ; a re-install must not leave an old kernel beside a new SHA256SUMS
  RMDir /r "$INSTDIR\payload"
  File "${STAGE}/1401Probe.exe"
  File /nonfatal "${STAGE}/1401Probe.exe.config"
  File "${STAGE}/probe.json"
  SetOutPath "$INSTDIR\docs"
  File "${STAGE}/docs/*"
  SetOutPath "$INSTDIR\payload"
  File /r "${STAGE}/payload/*"
  SetOutPath "$INSTDIR"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateShortcut "$SMPROGRAMS\1401 Probe.lnk" "$INSTDIR\1401Probe.exe"
  CreateShortcut "$DESKTOP\1401 Probe.lnk" "$INSTDIR\1401Probe.exe"
  CreateShortcut "$SMPROGRAMS\1401 Probe - How to use it.lnk" "$INSTDIR\docs\instructions.html"
  WriteRegStr HKLM "${UNINST}" "DisplayName" "1401 Probe"
  WriteRegStr HKLM "${UNINST}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKLM "${UNINST}" "Publisher" "NullMoth Systems"
  WriteRegStr HKLM "${UNINST}" "URLInfoAbout" "https://nullmothsystems.com"
  WriteRegStr HKLM "${UNINST}" "DisplayIcon" "$INSTDIR\1401Probe.exe"
  WriteRegStr HKLM "${UNINST}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegDWORD HKLM "${UNINST}" "NoModify" 1
  WriteRegDWORD HKLM "${UNINST}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  Delete "$SMPROGRAMS\1401 Probe.lnk"
  Delete "$DESKTOP\1401 Probe.lnk"
  Delete "$SMPROGRAMS\1401 Probe - How to use it.lnk"
  RMDir /r "$INSTDIR\docs"
  ; this used to be RMDir /r "$COMMONAPPDATA\...", but NSIS has no $COMMONAPPDATA, kept the text literally, and so ran a recursive
  ; delete, as admin, on a relative path in whatever folder the uninstaller started in. The marks now live in HKLM.
  DeleteRegKey HKLM "Software\NullMoth\1401 Probe"
  DeleteRegKey /ifempty HKLM "Software\NullMoth"
  ; the first beta kept its two marks as files in ProgramData ($APPDATA under SetShellVarContext all): remove exactly
  ; those two names, never a recursive delete in a folder a standard user may have made
  Delete "$APPDATA\NullMoth\1401 Probe\secureboot-was-on"
  Delete "$APPDATA\NullMoth\1401 Probe\bitlocker-paused"
  RMDir "$APPDATA\NullMoth\1401 Probe"
  RMDir "$APPDATA\NullMoth"
  RMDir /r "$INSTDIR\payload"
  Delete "$INSTDIR\1401Probe.exe"
  Delete "$INSTDIR\1401Probe.exe.config"
  Delete "$INSTDIR\probe.json"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  RMDir "$PROGRAMFILES64\NullMoth Systems"
  DeleteRegKey HKLM "${UNINST}"
SectionEnd
