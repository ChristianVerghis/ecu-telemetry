FROM python:3.13-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY static ./static
ENV ECU_DB_PATH=/srv/data/telemetry.db ECU_UDP_PORT=8781 ECU_TCP_PORT=8782
VOLUME /srv/data
EXPOSE 8780 8781/udp 8782
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8780"]
