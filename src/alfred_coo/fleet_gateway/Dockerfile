FROM python:3.12-slim
WORKDIR /app
COPY src/alfred_coo/fleet_gateway/ .
RUN pip install fastapi uvicorn
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8090"]
