#!/bin/bash
# MAG Tracker — sobe o servidor web (FastAPI+uvicorn) com scheduler de coleta a cada 30 min
cd "$(dirname "$0")"
if ! curl -s -o /dev/null http://localhost:8000/health; then
  nohup python3 -m uvicorn app.web:app --host 0.0.0.0 --port 8000 > /tmp/web.log 2>&1 &
  sleep 3
fi
echo "MAG Tracker em http://localhost:8000 (login: admin@magtracker.local / admin123)"
