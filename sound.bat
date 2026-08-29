@echo off
rem Opens the classic Sound control panel on the Recording tab.
rem The microphone level slider lives there, not in modern Settings:
rem   Recording -> your microphone -> Properties -> Levels
rem Set Microphone to 100 and add Microphone Boost (+10..+20 dB) if present.
cd /d "%~dp0"
start "" rundll32.exe shell32.dll,Control_RunDLL mmsys.cpl,,1
