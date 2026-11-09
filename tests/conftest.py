"""Isolated offline database and explicitly opted-in local auth for tests."""

import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["FORWARDED_AUTH_MODE"] = "none"
os.environ["FORWARDED_AUTH_ALLOW_INSECURE_NONE_MODE"] = "1"
