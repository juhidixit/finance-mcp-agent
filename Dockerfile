FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml README.md ./
COPY src ./src
COPY policies ./policies
COPY evals ./evals
RUN pip install --no-cache-dir -e . && finagent seed --db /app/data/acme.db
# Default: serve the policy gateway as an MCP server over stdio (e.g. `docker run -i finagent`).
CMD ["finagent", "serve-gateway", "--db", "/app/data/acme.db", "--audit", "/app/data/audit.jsonl", \
     "--approvals", "/app/data/approvals.jsonl"]
