@echo off
setlocal
cd /d "%~dp0"

set "FW=C:\Windows\Microsoft.NET\Framework64\v4.0.30319"
set "CSC=%FW%\csc.exe"
set "WPF=%FW%\WPF"

if not exist "lib\Microsoft.Web.WebView2.Wpf.dll" goto :nolib
if not exist dist mkdir dist

"%CSC%" /nologo /target:winexe /platform:x64 /optimize+ /codepage:65001 /out:dist\MiyunShell.exe /win32icon:app.ico /lib:"%WPF%" /lib:"lib" /r:System.dll /r:System.Core.dll /r:System.Xml.dll /r:WindowsBase.dll /r:PresentationCore.dll /r:PresentationFramework.dll /r:System.Xaml.dll /r:Microsoft.Web.WebView2.Core.dll /r:Microsoft.Web.WebView2.Wpf.dll Shell.cs
if errorlevel 1 goto :fail

copy /y "lib\Microsoft.Web.WebView2.Core.dll" dist\ >nul
copy /y "lib\Microsoft.Web.WebView2.Wpf.dll" dist\ >nul
copy /y "lib\WebView2Loader.dll" dist\ >nul
copy /y app.ico dist\ >nul
copy /y backend.cmd dist\ >nul
echo BUILD_OK
exit /b 0

:nolib
echo MISSING_LIB
exit /b 1

:fail
echo COMPILE_FAILED
exit /b 1
