"""Test-session setup that must run before `src.main` is imported."""

import os

# `src/main.py` wires the CloudWatch (X-Ray OTLP) span export at import, and with real AWS
# credentials on the machine running the tests that would export test spans to a real account.
# Tests switch it off here; the ones that exercise it call `_setup_cloudwatch_traces()` directly.
os.environ["AGP_CLOUDWATCH_TRACES"] = "false"  # forced: an ambient "true" must not export test spans
