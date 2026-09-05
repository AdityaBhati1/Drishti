FROM node:20-alpine AS build
WORKDIR /app
ARG VITE_CENTRAL_API_KEY
ARG VITE_CENTRAL_API_URL
ARG VITE_CENTRAL_WS_URL
ENV VITE_CENTRAL_API_KEY=$VITE_CENTRAL_API_KEY
ENV VITE_CENTRAL_API_URL=$VITE_CENTRAL_API_URL
ENV VITE_CENTRAL_WS_URL=$VITE_CENTRAL_WS_URL

COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci
COPY dashboard/ ./
RUN npm run build

FROM nginx:1.27-alpine
COPY dashboard/nginx.conf /etc/nginx/conf.d/default.conf
COPY dashboard/docker-entrypoint.sh /docker-entrypoint.d/40-env-config.sh
RUN chmod +x /docker-entrypoint.d/40-env-config.sh
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
