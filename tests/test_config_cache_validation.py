"""Test CacheSettings model_validator (fail-loud on missing redis_url)."""

import pytest
from pydantic import ValidationError

from config.cache import CacheSettings


def test_memory_backend_no_redis_url_ok():
    s = CacheSettings(backend="memory", redis_url=None)
    assert s.backend == "memory"
    assert s.redis_url is None


def test_redis_backend_requires_redis_url():
    with pytest.raises(ValidationError) as exc_info:
        CacheSettings(backend="redis", redis_url=None)
    assert "CACHE_REDIS_URL is required" in str(exc_info.value)


def test_redis_backend_with_url_ok():
    s = CacheSettings(backend="redis", redis_url="redis://localhost:6379/0")
    assert s.backend == "redis"
    assert s.redis_url == "redis://localhost:6379/0"


def test_new_fields_defaults():
    s = CacheSettings()
    assert s.pubsub_timeout_seconds == 5.0
    assert s.publish_payload_max_bytes == 8192
    assert s.key_prefix == "ivy"
