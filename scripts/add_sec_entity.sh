#!/usr/bin/env bash
# Add a company to SEC EDGAR monitoring
# Usage: ./add_sec_entity.sh <CIK_NUMBER> <COMPANY_NAME>

set -euo pipefail

CIK="${1:?Usage: $0 <CIK> <COMPANY_NAME>}"
NAME="${2:?Usage: $0 <CIK> <COMPANY_NAME>}"

STATE_FILE="$(cd "$(dirname "$0")/.." && pwd)/workspaces/legal-agent/data/sec_state.json"
mkdir -p "$(dirname "$STATE_FILE")"

if [ ! -f "$STATE_FILE" ]; then
    echo '{"tracked_entities": [], "seen_filings": []}' > "$STATE_FILE"
fi

# Add entity using python (avoids jq dependency)
python3 -c "
import json
with open('$STATE_FILE') as f:
    state = json.load(f)
entity = {'cik': '$CIK', 'name': '$NAME'}
if not any(e['cik'] == '$CIK' for e in state['tracked_entities']):
    state['tracked_entities'].append(entity)
    with open('$STATE_FILE', 'w') as f:
        json.dump(state, f, indent=2)
    print(f'Added: {entity}')
else:
    print(f'Already tracking CIK $CIK')
"
