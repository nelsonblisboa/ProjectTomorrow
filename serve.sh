#!/bin/bash
# Garante que o servidor de preview esteja rodando na porta 8000
if curl -s -o /dev/null --max-time 2 http://127.0.0.1:8000/; then
  echo "Servidor já está ativo em http://localhost:8000/"
else
  nohup python3 -m http.server 8000 --bind 0.0.0.0 --directory /workspace > /tmp/server.log 2>&1 &
  sleep 1
  echo "Servidor iniciado em http://localhost:8000/"
fi
