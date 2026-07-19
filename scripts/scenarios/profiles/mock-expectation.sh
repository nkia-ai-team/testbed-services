#!/usr/bin/env bash
exec python3 "$(dirname "$0")/mock_expectation_executor.py" "$@"
