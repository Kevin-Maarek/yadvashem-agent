FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY alembic.ini .
COPY migrations migrations
COPY __init__.py main.py worker.py ./
COPY api api
COPY core core
COPY db db
COPY domain domain
COPY integrations integrations
COPY prompts prompts
COPY schemas schemas
COPY services services
COPY tests tests
