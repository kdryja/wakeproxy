FROM python:3.12-slim
# ssh: for the ssh shutdown/check plugins and the windows_session source
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY wakeproxy ./wakeproxy
RUN pip install --no-cache-dir . && rm -rf /src
WORKDIR /
ENV PYTHONUNBUFFERED=1
EXPOSE 8009 8010
CMD ["python", "-m", "wakeproxy", "--config", "/config/config.yaml"]
