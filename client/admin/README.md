# Admin Web (Phase A)

Vite-based admin web for Eidolon Hub.

## Features

- View probe health (`/api/admin/probe/health`)
- View metrics (`/api/admin/metrics`)
- View devices (`/api/admin/devices`)
- Send command to a specific device (`/api/admin/devices/{device_id}/commands`)
- Receive SSE events (`/api/admin/stream/events`)

## Run

```bash
npm install
npm run dev
```

Open:

- `http://localhost:5174`

## Environment

Default admin API base URL is `http://localhost:8081/api/admin`.

You can override with:

```bash
VITE_ADMIN_API_BASE=http://localhost:8081/api/admin
```
