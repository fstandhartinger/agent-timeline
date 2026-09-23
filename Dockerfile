FROM nginx:1.27-alpine
RUN apk add --no-cache curl
COPY proxy/nginx.conf /etc/nginx/templates/default.conf.template
COPY public/ /usr/share/nginx/html/
ENV AGENT_TIMELINE_API_UPSTREAM=http://127.0.0.1:8890
EXPOSE 80
