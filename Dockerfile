# finmodel — образ сервиса (TASK_F0 §3). Точный тег базы; без лишних системных пакетов.
FROM python:3.12.16-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FINMODEL_DATA_DIR=/data \
    FINMODEL_SECRETS_DIR=/run/secrets \
    FINMODEL_CONFIG_DIR=/app/config \
    TZ=Europe/Moscow

# Корневой сертификат Минцифры — включается в Ф1 (файл + проверка SHA-256, см. certs/README.md).
# Проверку TLS НИКОГДА не отключать.
# COPY certs/russian_trusted_root_ca.crt /usr/local/share/ca-certificates/russian_trusted_root_ca.crt
# RUN echo "<SHA-256>  /usr/local/share/ca-certificates/russian_trusted_root_ca.crt" | sha256sum -c - \
#     && update-ca-certificates

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --system --gid 10001 finmodel \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin finmodel \
    && mkdir -p /data && chown 10001:10001 /data

COPY app/ /app/app/
COPY config/ /app/config/

USER 10001:10001
ENTRYPOINT ["python", "-m", "app"]
CMD ["version"]
