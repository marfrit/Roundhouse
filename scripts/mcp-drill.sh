#!/bin/bash
# MCP drill: container drill checklist — spawns roundhouse_mcp.py against a container URL
# with a scripted JSON-RPC session. Asserts key strings in output with a stdlib-python JSON reader.
# Prints PASS/FAIL per step. Zero actuation beyond what the MCP tools themselves do.

set -e

# Configuration
MCP_SCRIPT="${1:-.}/mvp1/roundhouse_mcp.py"
MCP_URL="${ROUNDHOUSE_URL:-http://127.0.0.1:8090}"
MCP_TOKEN="${ROUNDHOUSE_TOKEN:-}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m' # No Color

# Counters
TESTS_PASSED=0
TESTS_FAILED=0

echo "MCP Drill: Container checklist"
echo "URL: $MCP_URL"
echo "Script: $MCP_SCRIPT"
echo ""

# Helper: Run MCP tool and check result
run_mcp_tool() {
    local tool_name="$1"
    local args="$2"
    local expected_key="$3"
    local step_name="${4:-$tool_name}"

    # Build JSON-RPC request
    local id=$((RANDOM % 10000))
    local request=""

    if [ -z "$args" ]; then
        request="{\"jsonrpc\":\"2.0\",\"id\":$id,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool_name\",\"arguments\":{}}}"
    else
        request="{\"jsonrpc\":\"2.0\",\"id\":$id,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool_name\",\"arguments\":$args}}"
    fi

    # Run MCP server with input
    local response
    if [ -n "$MCP_TOKEN" ]; then
        response=$(echo "$request" | ROUNDHOUSE_TOKEN="$MCP_TOKEN" python3 "$MCP_SCRIPT" --url "$MCP_URL" 2>/dev/null | head -1)
    else
        response=$(echo "$request" | python3 "$MCP_SCRIPT" --url "$MCP_URL" 2>/dev/null | head -1)
    fi

    # Check if response is valid JSON with a Python one-liner
    if echo "$response" | python3 -c "import sys, json; json.load(sys.stdin)" 2>/dev/null; then
        # Check for expected key in the response
        if [ -n "$expected_key" ]; then
            if echo "$response" | python3 -c "import sys, json; data = json.load(sys.stdin); result = data.get('result', {}).get('content', [{}])[0].get('text', '{}'); parsed = json.loads(result); sys.exit(0 if '$expected_key' in parsed or '$expected_key' in str(data) else 1)" 2>/dev/null; then
                echo -e "${GREEN}PASS${NC} - $step_name"
                ((TESTS_PASSED++))
            else
                echo -e "${RED}FAIL${NC} - $step_name (missing '$expected_key')"
                ((TESTS_FAILED++))
            fi
        else
            echo -e "${GREEN}PASS${NC} - $step_name"
            ((TESTS_PASSED++))
        fi
    else
        echo -e "${RED}FAIL${NC} - $step_name (invalid JSON response)"
        ((TESTS_FAILED++))
    fi
}

# Step 1: Initialize
echo "Step 1: Initialize"
id=1
init_request='{"jsonrpc":"2.0","id":'$id',"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"mcp-drill","version":"1.0"}}}'
init_response=$(echo "$init_request" | python3 "$MCP_SCRIPT" --url "$MCP_URL" 2>/dev/null | head -1)
if echo "$init_response" | python3 -c "import sys, json; data = json.load(sys.stdin); sys.exit(0 if data.get('result', {}).get('serverInfo', {}).get('name') == 'roundhouse-mcp' else 1)" 2>/dev/null; then
    echo -e "${GREEN}PASS${NC} - initialize"
    ((TESTS_PASSED++))
else
    echo -e "${RED}FAIL${NC} - initialize"
    ((TESTS_FAILED++))
fi

# Step 2: fleet_status
echo "Step 2: fleet_status (read)"
run_mcp_tool "fleet_status" "" "http_status" "fleet_status"

# Step 3: port_board
echo "Step 3: port_board (read)"
run_mcp_tool "port_board" "" "http_status" "port_board"

# Step 4: unit_detail (if token is available, test read-only access)
if [ -n "$MCP_TOKEN" ]; then
    echo "Step 4: unit_detail (read)"
    # Assuming 'main.service' or similar exists; we test against any unit
    run_mcp_tool "unit_detail" '{"unit":"main.service"}' "http_status" "unit_detail"
fi

# Step 5: routing_config
echo "Step 5: routing_config (read)"
run_mcp_tool "routing_config" "" "http_status" "routing_config"

# Step 6: warm_state
echo "Step 6: warm_state (read)"
run_mcp_tool "warm_state" "" "http_status" "warm_state"

# Step 7: tools/list (check all 16 tools)
echo "Step 7: tools/list"
tools_request='{"jsonrpc":"2.0","id":7,"method":"tools/list","params":{}}'
tools_response=$(echo "$tools_request" | python3 "$MCP_SCRIPT" --url "$MCP_URL" 2>/dev/null | head -1)
tool_count=$(echo "$tools_response" | python3 -c "import sys, json; data = json.load(sys.stdin); tools = data.get('result', {}).get('tools', []); print(len(tools))" 2>/dev/null || echo "0")
if [ "$tool_count" -ge 16 ]; then
    echo -e "${GREEN}PASS${NC} - tools/list ($tool_count tools)"
    ((TESTS_PASSED++))
else
    echo -e "${RED}FAIL${NC} - tools/list (expected >=16 tools, got $tool_count)"
    ((TESTS_FAILED++))
fi

# Summary
echo ""
echo "========================================"
echo "Drill Results:"
echo "  Passed: $TESTS_PASSED"
echo "  Failed: $TESTS_FAILED"
echo "========================================"

if [ $TESTS_FAILED -eq 0 ]; then
    echo -e "${GREEN}All checks passed!${NC}"
    exit 0
else
    echo -e "${RED}Some checks failed.${NC}"
    exit 1
fi
