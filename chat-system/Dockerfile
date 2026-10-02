FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[distributed]"
ENTRYPOINT ["python", "-m", "chat_system.cluster"]
