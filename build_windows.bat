@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul

where py >nul 2>nul
if errorlevel 1 (
  echo Python 3.12+ est requis : https://www.python.org/downloads/windows/
  echo Cochez "Add python.exe to PATH" pendant l'installation.
  pause
  exit /b 1
)

if not exist ".venv-build\Scripts\python.exe" (
  echo Creation de l'environnement de build...
  py -3.12 -m venv .venv-build 2>nul
  if errorlevel 1 py -3 -m venv .venv-build
  if errorlevel 1 goto :error
)

call ".venv-build\Scripts\activate.bat"
python -m pip install --upgrade pip
if errorlevel 1 goto :error
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :error

echo.
echo Execution des tests...
python -m unittest discover -s tests
if errorlevel 1 (
  echo Les tests ont echoue : build annule.
  goto :error
)

echo.
echo Construction de l'executable...
pyinstaller --noconfirm --clean XboxImageBrowser.spec
if errorlevel 1 goto :error

if not exist dist mkdir dist
copy /Y demo\demo.iso dist\demo.iso >nul

echo.
echo Build termine :
echo   dist\XboxImageBrowser.exe
echo   dist\demo.iso
echo.
pause
exit /b 0

:error
echo.
echo Echec du build. Consultez les messages ci-dessus.
pause
exit /b 1
