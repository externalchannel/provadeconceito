@echo off
setlocal EnableExtensions
title ExternalChannel - Prova de conceito

rem ===========================================================================
rem  iniciar_experimento.bat
rem  Sobe as plataformas na ordem correta, cada uma em sua propria janela:
rem    1. Servidor XMPP local do SPADE (apenas se SPADE_JID nao for definido)
rem    2. Frota MASPY            (porta 9000)
rem    3. Hub SPADE              (porta 5000)
rem    4. JaCaMo / jacamo-web    (porta 8080) - dispara o fluxo
rem
rem  Pode ser executado de qualquer pasta: todos os caminhos sao relativos
rem  ao local deste arquivo.
rem
rem  Opcional - para usar uma conta em outro servidor XMPP, preencha abaixo
rem  (ou defina as variaveis de ambiente antes de executar):
rem    set SPADE_JID=minha_conta@servidor.xmpp
rem    set SPADE_SENHA=minha_senha
rem ===========================================================================

set "RAIZ=%~dp0"
if "%RAIZ:~-1%"=="\" set "RAIZ=%RAIZ:~0,-1%"
set "JACAMO_DIR=%RAIZ%\jacamo-web"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo.
echo  ExternalChannel - MASPY + SPADE + JaCaMo
echo  Pasta do experimento: %RAIZ%
echo.

rem --- Pre-requisitos ---------------------------------------------------------
where python >nul 2>&1 || (
    echo [ERRO] Python nao encontrado no PATH. Instale o Python 3.12+.
    goto :falha
)
where java >nul 2>&1 || (
    echo [ERRO] Java nao encontrado no PATH. Instale o Java 17.
    goto :falha
)

python -c "import maspy" >nul 2>&1 || (
    echo [ERRO] MASPY nao encontrado. Instale com: pip install maspy-ml==2026.5.13
    goto :falha
)
python -c "import spade" >nul 2>&1 || (
    echo [ERRO] SPADE nao encontrado. Instale com: pip install spade==4.1.2
    goto :falha
)
echo [1/5] Python, MASPY, SPADE e Java encontrados.

rem --- jacamo-web: extrai o zip na primeira execucao --------------------------
if not exist "%JACAMO_DIR%\gradlew.bat" (
    echo [2/5] Extraindo jacamo-web.zip...
    rem tar do proprio Windows: o GNU tar (ex.: do Git) interpreta "C:" como host remoto
    "%SystemRoot%\System32\tar.exe" -xf "%RAIZ%\jacamo-web.zip" -C "%RAIZ%" || goto :falha
) else (
    echo [2/5] jacamo-web ja extraido.
)

rem O Java 17 no Windows falha ("Unable to establish loopback connection")
rem quando a pasta temporaria do usuario contem espacos. Usa uma sem espacos.
if not exist "%PUBLIC%\jacamo_tmp" mkdir "%PUBLIC%\jacamo_tmp"
set "JAVA_TOOL_OPTIONS=-Djdk.net.unixdomain.tmpdir=%PUBLIC%\jacamo_tmp"

rem --- IP do JaCaMo (mesma deteccao usada pelos scripts Python) ---------------
if not defined IP_JACAMO (
    for /f "delims=" %%i in ('python -c "import socket; print(socket.gethostbyname(socket.gethostname()))"') do set "IP_JACAMO=%%i"
)
echo       IP do JaCaMo: %IP_JACAMO%

rem --- 1. Servidor XMPP -------------------------------------------------------
if defined SPADE_JID (
    echo [3/5] Usando conta XMPP externa: %SPADE_JID%
) else (
    echo [3/5] Iniciando servidor XMPP local do SPADE...
    start "XMPP (SPADE)" /D "%RAIZ%" cmd /k python -m spade.cli run --memory
    call :aguardar_porta 5222 30 "servidor XMPP" || goto :falha
)

rem --- 2. Frota MASPY ---------------------------------------------------------
echo [4/5] Iniciando frota MASPY e hub SPADE...
start "MASPY - Frota" /D "%RAIZ%" cmd /k python "%RAIZ%\maspy\veiculo_maspy.py"
call :aguardar_porta 9000 30 "frota MASPY" || goto :falha

rem --- 3. Hub SPADE -----------------------------------------------------------
if defined SPADE_JID goto :hub_externo
rem Com o servidor XMPP local (pyjabber 0.4.5) ha uma condicao de corrida na
rem negociacao STARTTLS no Windows que trava a conexao do hub. Gravar o log de
rem depuracao do cliente XMPP em arquivo altera o tempo da negociacao e evita
rem o problema. O codigo do hub e executado sem alteracoes.
start "SPADE - Hub" /D "%RAIZ%" cmd /k python -c "import logging, runpy; logging.basicConfig(level=logging.DEBUG, filename=r'spade\spade_xmpp_debug.log', filemode='w'); runpy.run_path(r'spade\transportadoras_spade.py', run_name='__main__')"
goto :hub_iniciado
:hub_externo
start "SPADE - Hub" /D "%RAIZ%" cmd /k python "%RAIZ%\spade\transportadoras_spade.py"
:hub_iniciado
call :aguardar_porta 5000 60 "hub SPADE" || goto :falha

rem --- 4. JaCaMo --------------------------------------------------------------
echo [5/5] Iniciando JaCaMo (a primeira execucao baixa dependencias)...
rem Caminho completo: com NoDefaultCurrentDirectoryInExePath definido, o cmd
rem nao procura executaveis na pasta atual.
start "JaCaMo" /D "%JACAMO_DIR%" cmd /k call "%JACAMO_DIR%\gradlew.bat" run --args="examples/logistica/gerenciador_frete.jcm"

echo.
echo  Tudo iniciado. Acompanhe o fluxo na janela "JaCaMo".
echo  Para ver o relatorio final e gerar maspy\metricas_experimento.csv,
echo  pressione Ctrl+C na janela "MASPY - Frota".
echo.
pause
exit /b 0

rem ---------------------------------------------------------------------------
rem  :aguardar_porta <porta> <segundos> <descricao>
rem ---------------------------------------------------------------------------
:aguardar_porta
set /a "_restante=%~2"
:aguardar_loop
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort %~1 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
if not errorlevel 1 goto :porta_pronta
if %_restante% LEQ 0 goto :porta_timeout
set /a "_restante-=1"
rem Pausa de ~1 s (ping funciona mesmo sem console interativo, ao contrario de timeout)
ping -n 2 127.0.0.1 >nul
goto :aguardar_loop

:porta_pronta
echo       %~3 pronto na porta %~1.
exit /b 0

:porta_timeout
echo [ERRO] %~3 nao respondeu na porta %~1. Verifique a janela correspondente.
exit /b 1

:falha
echo.
echo  A inicializacao foi interrompida.
pause
exit /b 1
