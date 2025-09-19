FROM python:3.12-slim
WORKDIR /app
COPY ship_proxy.py /app/ship_proxy.py
EXPOSE 8080
CMD ["python", "ship_proxy.py", "offshore_host", "9999"]
