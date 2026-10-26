# Extra sites

Each `*.caddy` file here is a Caddy site block served by this project's Caddy, for running another
tri-coach instance (with its own database and Garmin account) on the same server. Example `friend.caddy`:

```
friend.1-2-3-4.sslip.io {
	encode gzip
	reverse_proxy friend-app:8000
}
```

The other instance's app container must join this project's network (`tri-coach_default`) under a
service name that isn't `app`. Reload with `docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile`.
