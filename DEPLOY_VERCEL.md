# Deploy on Vercel

1. Put these files in a GitHub repo (or use `vercel` CLI):
   main.py, config.py, requirements.txt, vercel.json, .vercelignore, api/index.py
2. Vercel -> Add New Project -> import the repo -> Deploy.
3. In the project: Storage tab
   - Create/connect an **Upstash Redis** (Marketplace) database. Stores orders, menu, chat.
   - Create a **Blob** store. Stores item photos and voice notes.
   (They add KV_REST_API_URL / KV_REST_API_TOKEN / BLOB_READ_WRITE_TOKEN for you.)
4. Settings -> Environment Variables (these override config.py, so passwords stay out of GitHub):
   ADMIN_PASSWORD, RIDER_PASSWORD, SECRET_KEY (any long random text),
   STORE_LAT, STORE_LNG, DISCORD_ORDERS_WEBHOOK, DISCORD_SUPPORT_WEBHOOK
5. Redeploy once after adding storage / variables.
