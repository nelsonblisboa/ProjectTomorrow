#!/bin/bash
# Garante que o servidor de preview esteja rodando na porta 8000.
# Uso: ./serve.sh
PORT=8000

if curl -s -o /dev/null http://localhost:$PORT/; then
  echo "Servidor já está ativo em http://localhost:$PORT/"
  exit 0
fi

echo "Servidor não encontrado. Iniciando..."
nohup python3 -m http.server $PORT --bind 0.0.0.0 --directory /workspace > /tmp/server.log 2>&1 &
sleep 2

if curl -s -o /dev/null -w "HTTP %{http_code}\n" http://localhost:$PORT/; then
  echo "Preview disponível em http://localhost:$PORT/"
else
  echo "Falha ao iniciar o servidor. Log:"
  cat /tmp/server.log
  exit 1
fi
