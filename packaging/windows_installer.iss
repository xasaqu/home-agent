; Inno Setup 脚本：把 PyInstaller 生成的 dist\HomeAgent 文件夹做成一个安装程序。
; 只安装到当前用户目录，不需要管理员权限。
#define AppName "家庭管家"
#define AppVersion "0.2.0"

[Setup]
AppId={{6C1F3C2B-9D0B-4E7A-9C51-2F3A7B1E8D41}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\HomeAgent
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=家庭管家-安装程序
SetupIconFile=build-assets\icon.ico
UninstallDisplayIcon={app}\HomeAgent.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
#if FileExists(CompilerPath + "Languages\ChineseSimplified.isl")
Name: "chs"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
#else
Name: "en"; MessagesFile: "compiler:Default.isl"
#endif

[Files]
Source: "dist\HomeAgent\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\HomeAgent.exe"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\HomeAgent.exe"

[Run]
Filename: "{app}\HomeAgent.exe"; Description: "启动家庭管家"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{cmd}"; Parameters: "/c reg delete HKCU\Software\Microsoft\Windows\CurrentVersion\Run /v HomeAgent /f"; Flags: runhidden
