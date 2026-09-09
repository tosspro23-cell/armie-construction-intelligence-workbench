FROM node:20-alpine AS build
WORKDIR /app
COPY apps/web/package.json apps/web/package-lock.json /app/
RUN npm ci
COPY apps/web /app
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
# Rendered to /etc/nginx/conf.d/default.conf at container start by the
# base image's built-in envsubst-on-templates step (SPEC-M3 §4E).
COPY apps/web/default.conf.template /etc/nginx/templates/default.conf.template
# Overridable at deploy time (Container Apps env var); this default keeps
# a plain `docker run` of this image alone from crashing nginx's config
# render, even though nothing is listening on it without the API container.
ENV API_BACKEND_URL=http://localhost:8000
EXPOSE 80

