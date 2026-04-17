FROM indemn/harness-base:1.0.0

WORKDIR /app

# Install async harness deps (deepagents, temporal SDK, LangChain chat models)
COPY harnesses/async-deepagents/pyproject.toml /tmp/async/
RUN cd /tmp/async && uv pip install --system .

# Copy harness code
COPY harnesses/async-deepagents/ /app/harness/

# Entry point
CMD ["python", "-m", "harness.main"]
