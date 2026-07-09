#!/usr/bin/env bash
# QMem Python fallback MCP server launcher
# Usage: ./start_python_mcp.sh [working_dir]
# Requires: Python 3.10+ with sqlite-vec, onnxruntime, tokenizers installed
# Notes:
#   - stdout is reserved for MCP JSON-RPC protocol
#   - stderr used for all diagnostic logs (model loading, etc.)
#   - Must be run from the directory containing core_memory.db and bge-small-zh-v1.5-onnx/

set -e
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
WORK_DIR="${1:-$DIR}"

cd "$WORK_DIR"
exec python3 -u "$DIR/mcp_server.py"
