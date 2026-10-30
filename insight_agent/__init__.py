"""Insight Agent desktop workbench. Network access belongs to the model gateway."""
import os

# Do not inherit tracing/export from a developer's LangChain environment.
# Checkpoints stay local; only the explicit OutboundGateway may send content.
for _flag in ("LANGCHAIN_TRACING", "LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING", "LANGSMITH_OTEL_ENABLED"):
    os.environ[_flag] = "false"
