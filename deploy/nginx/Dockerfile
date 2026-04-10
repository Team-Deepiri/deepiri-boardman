# Build SPA then serve with nginx (see docs/PLAN.md)
FROM node:20-alpine AS ui
WORKDIR /ui
COPY boardman-ui/package.json ./
RUN npm install
COPY boardman-ui/ ./
RUN npm run build

FROM nginx:1.27-alpine
COPY deploy/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --from=ui /ui/dist /usr/share/nginx/html
EXPOSE 80
