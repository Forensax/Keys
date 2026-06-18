from __future__ import annotations

import os


os.environ.setdefault("DATABASE_URL", "sqlite:///./data/test-keys.db")
os.environ.setdefault("SESSION_SECRET", "test-secret")
