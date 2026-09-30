# Build SPA then serve with nginx (see docs/PLAN.md)
# node:22 matches boardman-ui's engines.node; node:20 predates it and npm warns.
FROM node:22-alpine AS ui
WORKDIR /ui
COPY boardman-ui/package.json ./
RUN npm install
COPY boardman-ui/ ./
RUN npm run build

FROM nginx:1.27-alpine
COPY deploy/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --from=ui /ui/dist /usr/share/nginx/html
EXPOSE 80
