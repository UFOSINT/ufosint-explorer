web: gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 45 --graceful-timeout 30 --max-requests 10000 --max-requests-jitter 500 --access-logfile '-' --error-logfile '-'
