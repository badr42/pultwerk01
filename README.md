# Pultwerk01 – software

Round 2.1" screen, needle gauge, speaker, light sensor and two keys on a Raspberry Pi Zero 2 W, controlled from your phone.

## Install (on the Pi)

```
cd ~
curl -fsSL https://github.com/badr42/pultwerk01/archive/refs/heads/stable.tar.gz | tar xz
cd pultwerk01-stable
bash install.sh
sudo reboot                # only if the installer says interfaces changed
```

The installer sets up `~/desk-display`, installs the packages, switches on SPI (screen), I²C (light sensor) and I²S (amp), and starts the `desk-display@<you>` service and the update timer. Your `config.toml`, `settings.json` and photos are kept. Code from an older install is moved to `releases/pre-update-<date>`.

## Wi-Fi setup

A Pultwerk with no network to join sets itself up from a phone:

1. It opens a hotspot called `Pultwerk-Setup` and shows a QR code. Scan it to join; the name and password are also on the screen.
2. Most phones then open the setup page by themselves. If yours doesn't, the screen shows a second QR code to it (`http://10.42.0.1:8080/wifi`).
3. Pick your network and enter its password. Only 2.4 GHz networks work on the Zero 2 W.
4. On success the screen shows the QR code to the control page for 3 minutes. Put your phone back on your usual Wi-Fi to use it. On failure the hotspot comes back and the page shows what went wrong.

With no saved network the hotspot opens within seconds. If a saved network can't be reached, it waits 3 minutes, then opens the hotspot and tries the saved network again every 5 minutes while nobody is using it, so a router restart sorts itself out. To change networks while connected: **More → Change Wi-Fi network** on the control page. The timings are under `[wifi]` in `config.toml`.

### Making cards for new units

The setup has to be on the card already, because a new Pi can't download anything until it has Wi-Fi. Make one master card and copy it:

1. Flash Raspberry Pi OS Lite (64-bit) with Imager, install Pultwerk as above, and set the Wi-Fi country you are building for (`sudo raspi-config nonint do_wifi_country SA`).
2. Run `bash ~/desk-display/current/prepare-image.sh`. It wipes your networks, settings, photos and SSH keys, then shuts the Pi down.
3. On your computer, copy the card to an image, and flash that image to each new card. On a Mac, with `N` from `diskutil list`:

```
diskutil unmountDisk /dev/diskN
sudo dd if=/dev/rdiskN of=pultwerk.img bs=4m
```

Every copy has the same login, so change the password on the master before step 2. New cards must be at least as large as the master, so build the master on the smallest card you plan to use.

## Updates

The Pi checks the `stable` branch about once an hour and installs anything new by itself, so you decide when a version goes out by moving that branch (on your computer):

```
git push origin main:stable
```

```
~/desk-display/
  config.toml  settings.json  photos/     yours; updates never replace them
  current -> releases/a91c2e04b7d1        the running version
  releases/                               the last 5 versions
```

A new version must stay running for 60 seconds. If it doesn't, the Pi switches back to the previous version, restores `settings.json`, and skips that commit until a newer one appears.

| On the Pi | |
|---|---|
| See what happened | `journalctl -u desk-update@$USER` |
| Check now | `sudo systemctl start desk-update@$USER` |
| Running version | `readlink ~/desk-display/current` |
| Retry a skipped commit | `rm ~/desk-display/.bad-version` |
| Stop automatic updates | `sudo systemctl disable --now desk-update@$USER.timer` |

To go back on purpose, move the branch to an older commit: `git push --force origin <commit>:stable`.

Updates cover the code and Python packages in `requirements.txt`. New `apt` packages, boot settings and changed service files need `bash install.sh` again; the update log says when a service file changed. New keys added to `config.example.toml` take effect by themselves.

Then open the control page on your phone (same Wi-Fi): the address is printed by the installer and shown as a QR code on the screen for 20 s after every start. Easiest is to name the Pi once: `sudo hostnamectl set-hostname pultwerk01`, then use **http://pultwerk01.local:8080**. On iPhone, Share → Add to Home Screen makes it an app icon.

## The control page

| Section | What you can do |
|---|---|
| Top | Live picture of the round screen, Prev / Next / Radar / Sleep |
| Screens | Switch each screen on or off, change the order (arrows), set how long each stays; tap a name to show it now |
| Display | Automatic brightness (light sensor, with darkest/brightest-room levels) or a fixed level; night sleep times; 24-hour clock; start-up QR code |
| Gauge | Dial light on/off; what the needle shows (16 choices); sweep test |
| Radar | Centre it on home or any spot (latitude, longitude), and set the range from 5 to 100 nm |
| Sound | Mute, volume, quiet hours, which events make a sound (each with a Play button), share-price alert threshold |
| Tickers | Up to 8 symbols with labels, in order; one slide each. Tadawul codes end in `.SR` (2222.SR Aramco, 1120.SR Al Rajhi, 7010.SR STC); also BTC-USD, GC=F (gold), EURSAR=X |
| Photos | Upload from the phone (resized to 1440 px on the phone first), delete, tap one to show it now |
| More | Three world clocks, the countdown, location (set in config.toml), refresh data now |

Every change saves at once and shows on the screen within a second. To lock the page, set `password = "..."` under `[web]` in `config.toml` and restart.

## Screens

Photos · Clock · Date · Month calendar · World clocks · Day/month/year progress · Countdown · Hijri date (Umm al-Qura, Arabic month) · Arabic date (weekday, date and Hijri date in Arabic) · Next prayer · Occasions (days to Ramadan, Eid al-Fitr, Arafah, Eid al-Adha, Hijri New Year, Founding Day, National Day; counts Ramadan days during the month) · Share prices · Weather · 3-day forecast · UV index · Wind & humidity · Air quality · Sun · Moon · System (QR code to the page, address, CPU temperature). Radar is on key B.

Hijri dates and occasions use the Umm al-Qura calendar from aladhan.com (an offline estimate is shown with ≈ until it answers); the real start of Ramadan and the Eids can still move by a day with the moon sighting. Weather, UV, forecast and air quality come from Open-Meteo, prices from Yahoo Finance, aircraft from airplanes.live. None need an account.

## Keys

- **A (NEXT)**: next screen · hold 1.5 s = sleep/wake · in radar = leave radar
- **B (RADAR)**: radar on/off

## Wiring (BCM numbers, header pins in brackets)

| Part | Connection |
|---|---|
| Screen (SPI0) | SCL → GPIO11 (23), SDA → GPIO10 (19), CS → GPIO8 (24), DC → GPIO25 (22), RST → GPIO24 (18), BL → GPIO12 (32), VCC/GND as on your current wiring, 100 µF across VCC–GND |
| Amp MAX98357A | Vin → 5 V, GND, BCLK → GPIO18 (12), LRC → GPIO19 (35), DIN → GPIO21 (40); speaker on the + / − terminals |
| Light sensor GY-302 | VCC → **3.3 V** (1), GND (9), SDA → GPIO2 (3), SCL → GPIO3 (5), ADDR open (address 0x23) |
| Servo | signal (orange) → GPIO13 (33), red → 5 V, brown → GND; 1000 µF across 5 V–GND nearby |
| Dial light | GPIO27 (13) → 68 Ω → white LED (long leg) → GND (14) |
| Key A / Key B | GPIO22 (15) / GPIO23 (16) to GND |
| Power | USB-C VBUS → 3 A polyfuse → Pi 5 V (2 or 4); GND → (6). Never also plug the Pi's own micro-USB |

## Checks from the command line

```
cd ~/desk-display/current
~/.venvs/display/bin/python desk_display.py --sound-test     # plays every sound
~/.venvs/display/bin/python desk_display.py --gauge-test     # sweeps the needle
~/.venvs/display/bin/python desk_display.py --light-test     # prints lux and the brightness it would pick
~/.venvs/display/bin/python desk_display.py --preview out/   # renders every screen to PNGs
python3 desk_display.py --demo                               # on a laptop: demo data + the page at http://localhost:8080
aplay -l          # the amp should appear as MAX98357A
i2cdetect -y 1    # the light sensor should appear as 23
```

Stop the service first for the hardware tests: `sudo systemctl stop desk-display@$USER`, then `start` it again.

## Files

`desk_display.py` display, gauge, sound, sensors · `settings.py` everything the page can change (saved in `settings.json`) · `web.py` + `web_ui.html` the control page (standard library only) · `wifi_setup.py` + `wifi_ui.html` Wi-Fi setup hotspot · `prepare-image.sh` readies a master card · `config.example.toml` template for the Pi's `config.toml` (hardware and location) · `update.sh`, `desk-update.service`, `desk-update.timer` automatic updates · `fonts/` Inter and Noto Kufi Arabic (both SIL Open Font License).
