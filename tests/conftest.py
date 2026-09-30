"""Test-wide defaults.

Set before anything imports ``app``, because ``get_settings`` is cached and the
scrypt parameters are read at call time.
"""

from __future__ import annotations

import os

# Production uses 2**14, which costs ~50ms per hash and makes the suite crawl.
# The tests cover the logic around hashing rather than its cost; the production
# default is asserted explicitly in test_security.py.
os.environ.setdefault("SGU_ACW_SCRYPT_N", "1024")
