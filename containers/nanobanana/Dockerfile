# AgentCore Runtime requires linux/arm64 images.
FROM --platform=linux/arm64 python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
RUN useradd --uid 10001 --no-create-home runtime && chown -R runtime /srv
USER runtime

# MCP protocol contract for AgentCore Runtime: 0.0.0.0:8000/mcp
EXPOSE 8000
CMD ["python", "-m", "app.server"]
