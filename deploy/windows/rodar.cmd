@echo off
rem Painel de Controle no Windows: usado pela tarefa "Painel de Controle" do Agendador
rem de Tarefas (criada por painel.ps1). Roda o painel e o liga de novo se ele cair.
rem Log em minerador.log, na pasta do projeto; passou de 10 MB, vira minerador.log.old.
setlocal
cd /d "%~dp0..\.."
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1

:loop
if exist minerador.log for %%F in (minerador.log) do if %%~zF GTR 10485760 move /y minerador.log minerador.log.old >nul
echo [%date% %time%] iniciando o painel >> minerador.log
".venv\Scripts\python.exe" -m minerador --config config.toml >> minerador.log 2>&1
echo [%date% %time%] o painel parou (codigo %errorlevel%), ligando de novo em 10 s >> minerador.log
rem "timeout" nao funciona sem janela (tarefa de sistema); o ping serve de espera
ping -n 11 127.0.0.1 >nul
goto loop
