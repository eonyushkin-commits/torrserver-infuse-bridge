FROM python:3.11-alpine

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bridge/ ./bridge/

# Сервису не нужны права root: он только читает API TorrServer и отдаёт WebDAV.
USER 10001:10001
EXPOSE 8080

CMD ["python", "-u", "-m", "bridge"]
