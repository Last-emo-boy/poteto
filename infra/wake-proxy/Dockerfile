FROM caddy:2.11.2-builder AS builder
RUN GOMAXPROCS=2 xcaddy build v2.11.2 --with github.com/sablierapp/sablier-caddy-plugin@v1.0.2
FROM caddy:2.11.2
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
