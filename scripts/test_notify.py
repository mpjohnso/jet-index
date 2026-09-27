#!/usr/bin/env python3
"""One-off manual test: exercises the real send_change_notifications() path with the
real RESEND_API_KEY secret and the real subscriber CSV, using dummy before/after
numbers. Not part of the daily pipeline -- delete after use."""
from notify import send_change_notifications

send_change_notifications((0.46, 40.58, 1.01), (0.46, 39.90, 1.15))
print("Test notification attempt complete -- see log lines above for per-subscriber results.")
