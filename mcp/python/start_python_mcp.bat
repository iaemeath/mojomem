@echo off
REM QMem Python MCP server launcher (portable)
REM Usage: start_python_mcp.bat [python_path]
REM Requires: Python 3.10+ with sqlite-vec, onnxruntime, tokenizers, numpy, huggingface-hub
REM Portable: python path from arg1 or PYTHON env var or PATH

SET "SCRIPT_DIR=%~dp0"
SET "PYTHON_EXE=%~1"
IF "%PYTHON_EXE%"=="" SET "PYTHON_EXE=%PYTHON%"
IF "%PYTHON_EXE%"=="" SET "PYTHON_EXE=python"

cd /d "%SCRIPT_DIR%"
"%PYTHON_EXE%" -u "%SCRIPT_DIR%mcp_server.py"
