"""Session-wide TEST_MODE enforcement.

QA finding: nothing in this codebase ever wrote TEST_MODE into the .env
file — the "something" flipping it back to true across sessions was a
human/agent manually editing .env, treating a deliberately-set
TEST_MODE=false as an anomaly to correct. That's backwards: real-mode work
depends on .env staying whatever a human last set it to. The test suite
must not need .env to say anything in particular at all.

Setting the environment variable here, before any test module (and
therefore before src.config / lib_snapshot.config) is ever imported, is what
makes it take effect: lib_snapshot.config's load_dotenv() call defaults to
override=False, so an env var already set here is never clobbered by
.env's own on-disk value. The .env file's TEST_MODE stays exactly whatever
a human set it to, regardless of what the test suite needs.

This MUST stay a plain module-level statement, not a fixture: pytest
imports every conftest.py in a test directory before importing any test
module in that directory or its subdirectories, which is what guarantees
this line runs before src.config's own module-level TEST_MODE assignment.
A fixture (even autouse, even session-scoped) runs too late — by the time
any fixture executes, every test module has already been collected and
imported, and src.config.TEST_MODE has already been baked in as a fixed
boolean.
"""
import os

os.environ["TEST_MODE"] = "true"
