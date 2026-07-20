#!/usr/bin/env bash
exec python3 "$(dirname "$0")/host_stress_executor.py" "$@"
