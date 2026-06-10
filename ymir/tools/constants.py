import aiohttp

AIOHTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)
AIOHTTP_RETRYABLE_STATUS_CODES = frozenset({503})
AIOHTTP_MAX_RETRIES = 3
AIOHTTP_RETRY_BACKOFF_BASE = 2  # seconds; delay = base * 2^attempt
YMIR_USER_AGENT = "redhat-ymir-agent"
