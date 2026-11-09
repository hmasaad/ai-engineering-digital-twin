FROM python:3.12-slim
WORKDIR /app
COPY . .
USER root
EXPOSE 8080
CMD ["python", "-m", "services.gateway.main"]
