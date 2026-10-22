# Build SPA then serve with nginx (see docs/PLAN.md)
FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS ui
WORKDIR /ui
COPY boardman-ui/package.json ./
RUN npm install
COPY boardman-ui/ ./
RUN npm run build

FROM nginx:1.27-alpine@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10
COPY deploy/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --from=ui /ui/dist /usr/share/nginx/html
EXPOSE 80
