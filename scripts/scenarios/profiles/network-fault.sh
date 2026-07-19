#!/usr/bin/env bash
exec "$(dirname "$0")/profile-executor.sh" --profile network.fault "$@"
