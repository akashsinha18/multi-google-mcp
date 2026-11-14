# TODO

## Migrate hosting from Fly.io to a Google Compute Engine e2-micro VM

**Why:** keep hosting, the OAuth client and billing in one Google Cloud project;
a real persistent disk suits SQLite, and an always-on VM has no cold start.
**Current:** Fly.io app `multi-google-mcp-s8ic9` (region `sjc`, 1 GB volume at `/data`).

### Before starting
- [ ] Check current free-tier terms: e2-micro regions (e.g. `us-west1`, `us-central1`,
      `us-east1`), 30 GB standard disk, egress limits, and whether the external
      IPv4 address is billed.
- [ ] Decide the hostname (own domain, or a wildcard DNS name such as `<ip>.sslip.io`)
      so Caddy can get a TLS certificate.

### Steps
- [ ] Create the VM (e2-micro, Debian/Ubuntu, standard persistent disk, HTTP/HTTPS
      firewall rules) and reserve a static external IP.
- [ ] Install Docker + Compose plugin; enable unattended security upgrades.
- [ ] Add `deploy/gce/` with `docker-compose.yml` (this image + Caddy) and a
      `Caddyfile` reverse-proxying to port 8080; `/data` on the VM disk.
- [ ] Put secrets in an env file readable only by root (or Secret Manager):
      `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `TOKEN_ENC_KEY`, `ALLOWED_EMAILS`;
      set `BASE_URL`, `MCP_ALLOWED_REDIRECT_URIS`.
- [ ] **Reuse the same `TOKEN_ENC_KEY`** and copy `/data` from Fly
      (`fly ssh sftp get` of `accounts.db` and `oauth/`) to keep connected accounts;
      otherwise reconnect every account.
- [ ] Add the new redirect URIs to the Google OAuth client:
      `https://<host>/auth/callback` and `https://<host>/accounts/callback`.
- [ ] Verify: `/healthz` 200, unauthenticated `/mcp` 401, `/accounts` sign-in,
      `list_accounts` shows the migrated accounts.
- [ ] In claude.ai, remove the old connector and add `https://<host>/mcp`.
- [ ] Set up disk snapshots (schedule) for backup.
- [ ] Remove the old Fly redirect URIs, then `fly apps destroy multi-google-mcp-s8ic9`.
- [ ] Update the README deploy section.
