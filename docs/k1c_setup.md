# Shake&Tune K1C Setup Guide

This guide covers setting up klippain-shaketune on a rooted **Creality K1C** printer.

## Architecture Overview

The K1C uses a MIPS32 processor (Ingenic X2000E) with 256 MB RAM. The scientific Python
libraries required for graph generation (numpy, matplotlib) have no prebuilt PyPI wheels for
MIPS32, so Shake&Tune uses a **split architecture**:

- **K1C** — collects accelerometer data, writes `.stdata` files, uploads to remote server
- **Docker container** (on your PC, home server, or cloud) — processes `.stdata`, generates graphs, returns PNGs
- **One-click workflow** — transparent to the user; graphs appear in Fluidd/Mainsail automatically

---

## Prerequisites

1. Rooted K1C with SSH access (Settings → Root Account, or the 2025 exploit for newer firmware)
2. [Guilouz Helper Script](https://github.com/Guilouz/Creality-Helper-Script-Wiki) installed:
   - Klipper (vanilla, not the frozen Creality 2022 build)
   - Moonraker
   - Fluidd or Mainsail

---

## Part 1: Deploy the Docker Processing Server

### Option A — Local PC or Home Server (Recommended)

**Requirements:** Docker + Docker Compose

```bash
# Clone the fork on your PC
git clone https://github.com/SmoothBrainIT/klippain-shaketune-k1c.git
cd klippain-shaketune-k1c

# (Optional) Set an API key for security
export SHAKETUNE_API_KEY=your_secret_key_here

# Build and start the server
docker compose -f docker/docker-compose.yml up -d
```

The server listens on port **8080**. Verify it's running:
```bash
curl http://localhost:8080/health
```

For input shaper and belts graphs, the server needs Klipper's `shaper_calibrate` module.
Mount your local Klipper directory by uncommenting the volume in `docker/docker-compose.yml`:
```yaml
volumes:
  - ~/klipper:/klipper:ro
```

### Option B — Cloud Deployment (Fly.io / Railway / Render)

Any platform that supports Docker containers works. The server is stateless (no persistent
storage required). Example with Fly.io:

```bash
fly launch --image your-registry/shaketune-k1c:latest
fly secrets set SHAKETUNE_API_KEY=your_secret_key
fly deploy
```

Take note of your deployment URL (e.g. `https://shaketune-k1c.fly.dev`).

---

## Part 2: Install Shake&Tune on the K1C

SSH into your K1C (default credentials: root / creality_2023):

```bash
ssh root@<k1c-ip>
```

Run the K1C install script:
```bash
wget -O /tmp/install_k1c.sh https://raw.githubusercontent.com/SmoothBrainIT/klippain-shaketune-k1c/main/install_k1c.sh
sh /tmp/install_k1c.sh
```

The script will:
1. Auto-detect the Klipper extras directory (tries `/usr/data/klipper/klippy/extras` then `/usr/share/klipper/klippy/extras`)
2. Auto-detect the Python interpreter (`/usr/lib/python3.8` or venv)
3. Download and install a prebuilt MIPS32 `zstandard` wheel
4. Symlink the `shaketune` module into Klipper's extras
5. Restart Klipper and Moonraker

---

## Part 3: Configure printer.cfg

Add these sections to `/usr/data/printer_data/config/printer.cfg`:

```ini
# K1C built-in ADXL345 accelerometer (SPI pins — verify against your mainboard version)
[adxl345]
# cs_pin: ...   # Set based on your K1C mainboard SPI wiring
# spi_bus: ...

[resonance_tester]
accel_chip: adxl345
probe_points: 110, 110, 20  # K1C bed center at 20mm Z

[shaketune]
result_folder: /usr/data/printer_data/config/ShakeTune_results
number_of_results_to_keep: 5
measurements_chunk_size: 2          # Critical: keeps RAM usage low on 256MB K1C
timeout: 600
show_macros_in_webui: True

# Remote processing — point at your Docker server
remote_processing_url: http://192.168.1.x:8080  # Replace with your server's IP/URL
# remote_api_key: your_secret_key              # Uncomment if you set an API key
```

Restart Klipper from Fluidd/Mainsail or via SSH:
```bash
/etc/init.d/S55klipper_service restart
```

---

## Part 4: Verify

1. Open Fluidd/Mainsail → check the Klippy log for any shaketune errors
2. Run `AXES_SHAPER_CALIBRATION` from the macros panel
3. Watch the K1C console — you should see:
   ```
   Shake&Tune version: ...
   Measuring X...
   Measuring Y...
   Sending data to remote processing server at http://192.168.1.x:8080/process...
   input shaper graphs created successfully (via remote processing)!
   ```
4. Graphs appear in Fluidd/Mainsail under the ShakeTune_results folder

---

## Troubleshooting

### "Could not reach server"
- Verify Docker container is running: `docker ps`
- Check K1C can reach the server: `wget -q -O- http://192.168.1.x:8080/health`
- Check firewall rules on your PC (allow port 8080 from the printer's IP)

### "ImportError: No module named 'zstandard'"
The MIPS32 wheel failed to install. Try building it manually:
```bash
# On a Linux x86 machine with the MIPS cross-toolchain:
pip install crossenv
python -m crossenv /path/to/mips/python3 mips_env
source mips_env/bin/activate
pip install zstandard==0.23.0
# Copy the resulting .whl to the K1C and install with pip
```

### "shaper_calibrate module not found" (on Docker server)
Mount your Klipper directory to the container:
```yaml
# In docker/docker-compose.yml:
volumes:
  - ~/klipper:/klipper:ro
```

### "Timer too close" errors during test
Reduce measurements_chunk_size in [shaketune]:
```ini
measurements_chunk_size: 2  # Already at minimum — if still failing, try increasing timeout
```

### Graphs look different from standard Shake&Tune
Minor visual differences may occur when graph parameters (SCV, sweeping mode) differ from
defaults. This is cosmetic — the resonance analysis is performed on the actual measured data.

---

## Building the MIPS32 zstandard Wheel Manually

If the prebuilt wheel doesn't work for your K1C firmware:

```bash
# Install cross-compilation toolchain on Ubuntu
sudo apt install gcc-mipsel-linux-gnu g++-mipsel-linux-gnu

# Download zstandard source
pip download --no-binary :all: zstandard==0.23.0

# Cross-compile
MIPSEL_CC=mipsel-linux-gnu-gcc
MIPSEL_CXX=mipsel-linux-gnu-g++
# ... (see full instructions in the project wiki)
```

The resulting `.whl` file can be installed on the K1C with:
```bash
pip3 install /path/to/zstandard-0.23.0-mipsel.whl
```

---

## K1C Accelerometer SPI Configuration

The ADXL345 on the K1C is built into the toolhead PCB. The SPI pins vary by firmware version.
Check the Guilouz wiki or your Klipper `printer.cfg` for the correct values:

```ini
[adxl345]
cs_pin: <your_cs_pin>
spi_bus: <your_spi_bus>
```

Common values found in the K1C community (verify before use):
- `spi_bus: spi0a` with `cs_pin: PA15`

Refer to [Guilouz's K1C documentation](https://guilouz.github.io/Creality-Helper-Script-Wiki/)
for authoritative pin assignments for your specific firmware version.
