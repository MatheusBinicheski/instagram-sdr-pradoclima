@echo off
echo ================================================
echo   Instagram SDR Bot — ManyChat Webhook Server
echo ================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERRO] Python nao encontrado. Instale em https://python.org
    pause & exit /b 1
)

if not exist "venv" (
    echo Criando ambiente virtual...
    python -m venv venv
)

call venv\Scripts\activate.bat
pip install -r requirements.txt --quiet

if not exist "logs" mkdir logs
if not exist "data" mkdir data

echo.
echo [IMPORTANTE] Verifique o arquivo .env:
echo   ANTHROPIC_API_KEY=sua_chave_aqui
echo.
echo Iniciando servidor na porta 8000...
echo Para expor na internet: abra outro terminal e rode:
echo   ngrok http 8000
echo.
python main.py
pause
