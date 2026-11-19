ARG PYTHON_VERSION=3.12
ARG NODE_VERSION=22
ARG UV_VERSION=0.11.32

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM node:${NODE_VERSION}-slim AS css
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY main ./main
RUN npm run build:css

FROM python:${PYTHON_VERSION}-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt
COPY . .
COPY --from=css /build/main/static/main/css/style.css main/static/main/css/style.css
# collectstatic only reads settings; these throwaway values never reach the running container.
RUN DJANGO_SECRET_KEY=collectstatic DJANGO_DEBUG=false DJANGO_ALLOWED_HOSTS=localhost \
    DATABASE_URL=sqlite:////tmp/collectstatic.sqlite3 python manage.py collectstatic --noinput \
    && useradd --system --create-home app \
    && chown app /app
USER app
EXPOSE 8000
ENTRYPOINT ["./entrypoint.sh"]
