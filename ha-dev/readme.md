# Luma dev access (maintainer only)

`custom_components/luma_dev_access` is a small Home Assistant integration that lets cloud
development sessions (Claude Code on the web) SSH to the development TV for up to 8 hours, only
after the maintainer taps **Approve** on their phone. It is not part of the published Luma
integration: HACS installs only the repository's root `custom_components/luma`.

## How it works

1. A cloud session runs `node tools/tv-access.mjs request --reason "…"`. It calls
   `POST https://ha.elischleifer.com/api/luma_dev/request` with the dev token in `X-Luma-Dev-Token`.
2. Home Assistant sends an actionable notification with **Approve** and **Deny** to the chosen
   phone. The action names hold a random nonce that only the phone sees, so nothing but that tap
   can approve. Unanswered requests expire after 10 minutes.
3. On approval, Home Assistant generates a new ed25519 key pair and adds the public key to the
   TV's `/home/root/.ssh/authorized_keys` over the home network, using its own management key:

   ```
   no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 AAAA… luma-dev:<request id>:<expiry>
   ```

   It then logs in with the new key to check the TV accepts it. If this Dropbear build rejects
   option-prefixed lines, it uses a plain line instead and remembers that.

4. The session's long poll (`GET /api/luma_dev/result/<id>?wait=25`) receives, **once**, the private
   key, the tunnel hostname, the Cloudflare Access service token and the streaming search keys
   (read from the Luma integration's options). The tool writes `~/.ssh/luma_tv`, a ProxyCommand
   script and a `Host tv` block in `~/.ssh/config`, all mode 600/700, and never prints a secret.
5. The key is removed from the TV when the session runs `release`, when its expiry passes, or by
   the sweep at startup and every 15 minutes, which drops any `luma-dev:` line past its expiry.
   Dropbear has no expiry option of its own, so this sweep is what enforces it.
   Lines without the `luma-dev:` tag are never touched.

The dev token can only ask. It is stored in Home Assistant as a SHA-256 digest. Ten wrong tokens
in an hour lock the endpoints for an hour and notify the phone. At most five requests per hour,
and two waiting at once, are accepted.

## Setup

1. Copy `ha-dev/custom_components/luma_dev_access` into Home Assistant's `config/custom_components/`
   and restart Home Assistant.
2. **Settings → Devices & services → Add integration → Luma dev access.** Enter:
   - **TV address on your home network** (the TV's LAN IP) and **SSH port** (22).
   - **Tunnel hostname**: `tv.elischleifer.com`.
   - **Phone that approves requests**: your Companion app's `notify.mobile_app_…` service.
   - **Cloudflare Access service token** Client ID and Secret: handed to approved sessions so
     `cloudflared access ssh` needs no login. Create it under Zero Trust → Access → Service Auth,
     and add a **Service Auth** policy for it to the `tv.elischleifer.com` Access application.
3. The last step shows two things, once:
   - the **dev token**: add it to the cloud environment as `LUMA_DEV_TOKEN` (environment menu in the
     session title bar → Edit → environment variables);
   - **Home Assistant's key line**: append it to `/home/root/.ssh/authorized_keys` on the TV.
     Dropbear does not support `from=`, so this key is protected by staying inside Home Assistant.
4. Optional: remove `TV_SSH_PRIVATE_KEY` and `TUNNEL_SERVICE_TOKEN_*` from the cloud environment.
   Sessions then hold only `LUMA_DEV_TOKEN` (and `LUMA_DEV_URL` if Home Assistant moves).

The options let you change any of this, **make a new dev token** (the old one stops working
immediately), and **forget the TV host key** after a TV reset. Home Assistant pins the TV's host
key on first contact and refuses to continue if it changes.

## Using it from a cloud session

```sh
node tools/tv-access.mjs request --reason "check Prime Video deep links" --ttl 4h
node tools/tv-access.mjs check      # luna-send getForegroundAppInfo over ssh -tt
ssh -tt tv 'luna-send -n 1 luna://com.webos.applicationManager/getForegroundAppInfo "{}"; exit'
node tools/tv-access.mjs release    # when done
```

`request` installs `cloudflared` into `~/.local/bin` if it is missing. The OpenSSH client must be
installed (`apt-get install -y openssh-client`). Streaming keys go into a managed block in the
repository's gitignored `.env`, only for variables not already set; `release` removes it.

## API

All endpoints need `X-Luma-Dev-Token` and answer `Cache-Control: no-store`.

| Endpoint                          | Body / query                       | Answer                                                                                                                                               |
| --------------------------------- | ---------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST /api/luma_dev/request`      | `reason`, `ttl` (seconds, ≤ 28800) | `202` `request_id`, `ttl`, `answer_within`                                                                                                           |
| `GET /api/luma_dev/result/<id>`   | `wait` (seconds, ≤ 30)             | `202` pending · `200` `expires_at`, `ssh`, `cloudflare`, `streaming` (once) · `403` denied · `410` expired/handed out · `502` TV refused/unreachable |
| `POST /api/luma_dev/release/<id>` |                                    | `200` (key removed from the TV)                                                                                                                      |
| any                               |                                    | `401` wrong token · `429` rate limit or lockout · `503` not set up                                                                                   |

Tests: `tests/home-assistant/test_luma_dev_access.py` (run with the Home Assistant workflow) and
`tests/tv-access.test.mjs`.
