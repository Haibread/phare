# syntax=docker/dockerfile:1
FROM node:22-alpine AS builder
WORKDIR /usr/local/src/phare-frontend
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# The SPA is client-rendered; the API base is baked in at build time. Override for non-localhost
# deploys: `--build-arg VITE_API_BASE_URL=https://phare.example.com`.
ARG VITE_API_BASE_URL=http://localhost:8000
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

# nginx-unprivileged runs as a non-root user (uid 101) and listens on 8080 by default.
FROM nginxinc/nginx-unprivileged:1.27-alpine AS runtime
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /usr/local/src/phare-frontend/dist /usr/share/nginx/html
EXPOSE 8080
