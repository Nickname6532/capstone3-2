# FastAPI 백엔드 (대시보드 정적 파일도 같은 프로세스가 서빙한다).
# nginx가 앞단에서 이 컨테이너로 리버스 프록시한다 (docker-compose.yml 참고).
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src

WORKDIR /app/src

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
