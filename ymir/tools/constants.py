import aiohttp

AIOHTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)
AIOHTTP_RETRYABLE_STATUS_CODES = frozenset({503})
AIOHTTP_MAX_RETRIES = 3
AIOHTTP_RETRY_BACKOFF_BASE = 2  # seconds; delay = base * 2^attempt
YMIR_USER_AGENT = "redhat-ymir-agent"

GITLAB_API_URL = "https://gitlab.com/api/v4"
RULES_NAMESPACE = "redhat/centos-stream/rules"
# use for testing:
# RULES_NAMESPACE = "ymir-rules-test"
