FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY deploy/storage_init.py ./deploy/storage_init.py
RUN pip install --no-cache-dir '.[distributed,media]'
RUN useradd -m video && mkdir /runtime && chown video:video /runtime
USER video
ENTRYPOINT ["python", "-m", "video_streaming.cli", "--root", "/runtime"]
