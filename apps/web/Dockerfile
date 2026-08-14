FROM node:20-alpine AS build
WORKDIR /app
COPY apps/web/package.json /app/package.json
RUN npm install
COPY apps/web /app
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80

