# Build stage: compile dlib and the other dependencies as wheels
FROM python:3.12-slim AS build

# Install the compilers and math libraries required to build dlib
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        libopenblas-dev \
        liblapack-dev && \
    rm -rf /var/lib/apt/lists/*

# Build the wheels of the Python dependencies
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt


# Runtime stage: only what the application needs to run
FROM python:3.12-slim

# Install the math libraries that dlib uses at runtime
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libopenblas0 \
        liblapack3 && \
    rm -rf /var/lib/apt/lists/*

# Set the working directory
WORKDIR /app

# Install Python dependencies from the prebuilt wheels
COPY requirements.txt .
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt && \
    rm -rf /wheels

# Create an unprivileged user and copy the project files
RUN useradd --create-home --uid 1000 app && \
    chown app:app /app
COPY --chown=app:app . .

# Collect static files (the placeholder key is only used for this command)
RUN DJANGO_SECRET_KEY=build-only-placeholder python manage.py collectstatic --noinput

# Run the container as the unprivileged user
USER app

# Gunicorn workers (face encoding is CPU-bound)
ENV GUNICORN_CMD_ARGS="--workers 2 --timeout 60"

# Expose port 8000
EXPOSE 8000

# Apply migrations and start the server
CMD python manage.py migrate --noinput && \
    exec gunicorn face_auth.wsgi --bind 0.0.0.0:${PORT:-8000} --access-logfile -
