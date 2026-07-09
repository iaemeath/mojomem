@echo off
REM QMem Python fallback MCP server launcher for Windows
REM Usage: start_python_mcp.bat [working_dir]
REM Requires: Python 3.10+ with sqlite-vec, onnxruntime, tokenizers
REM IMPORTANT: Run from directory containing core_memory.db and bge-small-zh-v1.5-onnx\
REM Use absolute Python path to avoid Windows Store stub (exit code 49)

SET "SCRIPT_DIR=%~dp0"
SET "WORK_DIR=%~1"
IF "%WORK_DIR%"=="" SET "WORK_DIR=%SCRIPT_DIR%"

cd /d "%WORK_DIR%"
"C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe" -u "%SCRIPT_DIR%mcp_server.py"
