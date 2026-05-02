FROM python:3.12-slim
WORKDIR /app
COPY src/alfred_coo/fleet_voice/ ./alfred_coo/fleet_voice/
RUN pip install fastapi 'uvicorn[standard]'
ENV PYTHONUNBUFFERED=1
EXPOSE 8091
CMD ["uvicorn", "alfred_coo.fleet_voice.server:app", "--host", "0.0.0.0", "--port", "8091"]
