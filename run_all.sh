#!/usr/bin/env bash
# Full pipeline: SQL engine -> insight packets -> tests -> AI explanations of unusual changes
set -euo pipefail
cd "$(dirname "$0")"
psql -X -q -v ON_ERROR_STOP=1 -f sql/01_order_facts.sql
psql -X -q -v ON_ERROR_STOP=1 -f sql/02_monthly_metrics.sql
psql -X -q -v ON_ERROR_STOP=1 -f sql/03_drivers.sql
python3 engine/build_packets.py
python3 tests/planted_anomalies.py
python3 engine/explain.py --unusual            # uses LLM_PROVIDER from .env
