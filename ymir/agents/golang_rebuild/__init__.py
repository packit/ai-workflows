"""
Golang CVE Rebuild Agent

Deterministic orchestrator for rebuilding RHEL 9.x/10.x z-stream components
affected by Golang CVE fixes. Uses GitLab MR workflow for all submissions.

This agent does NOT use BeeAI framework — it is a pure Python orchestrator
since golang rebuilds are deterministic (no LLM reasoning needed).
"""

__version__ = "0.1.0"
