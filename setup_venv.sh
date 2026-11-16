#!/usr/bin/env bash
# Retired: this legacy GPU installer bypassed the reviewed dependency locks.
set -eu
printf '%s\n' \
  'This unpinned GPU installer is retired; no environment was created.' \
  'Use the README.md Quickstart with Python 3.14 and requirements-test.lock.' \
  'That supported camera-free environment does not install the optional GPU/CLIP stack.' >&2
exit 2
