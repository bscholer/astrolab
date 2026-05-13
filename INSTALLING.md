# Installing astrolab

astrolab runs inside **Docker** — a tool that packages the app and all its dependencies
(Siril, GraXpert, etc.) into a single container so you don't have to install anything
yourself. If you've never used Docker before, this guide walks you through the whole
thing from scratch.

---

## Step 1 — Install Docker

### macOS

1. Go to [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/)
   and download **Docker Desktop for Mac**.
   - **M1/M2/M3/M4 Mac?** Download the "Apple Silicon" version.
   - **Older Intel Mac?** Download the "Intel Chip" version.
2. Open the downloaded `.dmg`, drag Docker to Applications, and launch it.
3. Docker will ask for your password once to finish setup. After a minute or two
   you'll see a small whale icon in your menu bar — that means Docker is running.

**Give Docker more resources:** Docker Desktop runs a small Linux VM in the background,
and by default it gets a modest slice of your machine. Stacking jobs are CPU- and
memory-intensive, so it's worth bumping those limits. Open Docker Desktop →
**Settings → Resources** and set:
- **CPU limit** — at least half your cores (e.g. 8 of 16)
- **Memory limit** — 8 GB or more if you can spare it
- **Virtual disk size** — 64 GB+ if you plan to process a lot of sessions; the cache
  can grow

Click **Apply & Restart** when done.

> **Apple Silicon (M1/M2/M3/M4) note:** astrolab's image is built for Intel (amd64).
> It runs on M-series Macs through an emulation layer and the core stacking pipeline
> works fine. The AI-based background removal (GraXpert) and star removal (StarNet++)
> nodes will crash on Apple Silicon due to an emulation bug outside our control. If you
> need those nodes, run astrolab on a Linux machine instead.
>
> You will also see a warning like this when you run the container — it is expected and
> harmless:
>
> ```
> WARNING: The requested image's platform (linux/amd64) does not match the detected
> host platform (linux/arm64/v8) and no specific platform was requested
> ```

---

### Windows

astrolab on Windows requires **WSL 2** (Windows Subsystem for Linux). Docker Desktop
sets this up for you, but Windows needs a couple things first.

1. Make sure you're on **Windows 10 version 2004 or later**, or Windows 11.
2. Go to [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/)
   and download **Docker Desktop for Windows**.
3. Run the installer. When it asks, leave "Use WSL 2 instead of Hyper-V" checked.
4. If the installer asks you to install or update WSL 2, follow the link it provides
   and run the update from Microsoft — it only takes a moment.
5. Restart your computer when prompted.
6. After restarting, launch Docker Desktop from the Start menu. The first launch
   takes a minute or two. When you see the Docker whale in the system tray, it's ready.

**Give Docker more resources:** Open Docker Desktop → **Settings → Resources** and set:
- **CPU limit** — at least half your cores
- **Memory limit** — 8 GB or more if you can spare it
- **Virtual disk size** — 64 GB+ if you plan to process a lot of sessions

Click **Apply & Restart** when done.

---

### Linux

Install Docker Engine (no desktop app needed):

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
```

Log out and back in so the group change takes effect. Then confirm Docker is working:

```bash
docker run hello-world
```

---

## Step 2 — Find your captures folder

astrolab needs to know where your raw image files live. Point it at whichever folder
contains your session subfolders — for Dwarf 3 users that's usually `~/Pictures/Siril`,
for NINA or ASIAIR users it's wherever you configured or copied your images to.

> **Note:** You don't need to organize the files in any particular way. astrolab scans
> the folder recursively and figures out sessions on its own.

---

## Step 3 — Run astrolab

Open a terminal (on macOS: **Terminal** in Applications → Utilities; on Windows:
**PowerShell** or the Docker Desktop terminal; on Linux: any terminal).

Paste the command below, replacing `/path/to/your/captures` with the path you found
above (e.g. `~/Pictures/Siril` on macOS):

```bash
docker run -d \
  --name astrolab \
  --restart unless-stopped \
  -p 8000:8000 \
  -v /path/to/your/captures:/captures:ro \
  -v astrolab-data:/data \
  ghcr.io/bscholer/astrolab:latest
```

Then open your browser and go to **http://localhost:8000**.

astrolab will scan your captures folder on first launch — this usually takes a few
seconds, but can take longer if you have thousands of files. Your sessions will appear
in the Library once the scan finishes.

---

## Optional — NVIDIA GPU

If your machine has an NVIDIA GPU, you can enable it for the GraXpert background
removal and StarNet++ star removal nodes. Without it those nodes still work but run
slower on CPU.

1. Install the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html).
2. Use the `:cuda` image tag and add `--gpus all`:

```bash
docker run -d \
  --name astrolab \
  --restart unless-stopped \
  --gpus all \
  -p 8000:8000 \
  -v /path/to/your/captures:/captures:ro \
  -v astrolab-data:/data \
  -e ASTROLAB_ENABLE_STARNET=1 \
  ghcr.io/bscholer/astrolab:cuda
```

`ASTROLAB_ENABLE_STARNET=1` tells the container to download StarNet++ v2 on first
run and store it in your persistent `/data` volume.

---

## Updating

To update to a newer version of astrolab:

```bash
docker stop astrolab
docker rm astrolab
docker pull ghcr.io/bscholer/astrolab:latest
```

Then re-run the `docker run` command from Step 3. Your projects and settings are
stored in the `astrolab-data` volume, so they survive the update.

---

## Troubleshooting

**"Cannot connect to the Docker daemon" or "docker: command not found"**
Docker is not running. On macOS and Windows, open Docker Desktop and wait for the
whale icon to appear in the menu/system tray before trying again.

**The page at http://localhost:8000 isn't loading**
Give it 20–30 seconds after running the `docker run` command — the container needs
a moment to start. If it still doesn't load, check that port 8000 isn't in use by
something else on your machine.

**No sessions appear in the Library**
Double-check that the path in `-v /path/to/your/captures:/captures:ro` points to the
right folder and that it exists. On Windows, paths look like
`-v "C:\Users\you\Pictures\Siril:/captures:ro"` (note the quotes).

**"port is already allocated" error**
Something else on your machine is using port 8000. Change `-p 8000:8000` to
`-p 8001:8000` and open http://localhost:8001 instead.

**Container crashes immediately on Apple Silicon (M1/M2/M3/M4)**
See the Apple Silicon note in the macOS section above. The Siril-only pipeline works;
the AI nodes (GraXpert, StarNet++) do not.
