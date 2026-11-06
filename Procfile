web: python manage.py migrate --noinput && gunicorn face_auth.wsgi --bind 0.0.0.0:$PORT --workers 2 --timeout 60 --access-logfile -
