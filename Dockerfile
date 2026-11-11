FROM python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home mcp
USER 10001:10001
ENV PYTHONUNBUFFERED=1 MCP_HOST=0.0.0.0 MCP_PORT=8000
EXPOSE 8000
CMD ["python", "-m", "zerobyte_mcp.server"]
