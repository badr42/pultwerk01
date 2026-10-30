# Pultwerk01 – software

Round 2.1" screen, needle gauge, speaker, light sensor and two keys on a Raspberry Pi Zero 2 W, controlled from your phone.

## Install or update (on the Pi)

```
cd ~/pultwerk01-app        # wherever you unzipped this folder
bash install.sh
sudo reboot                # only if the installer says interfaces changed
```

The installer copies everything to `~/desk-display`, installs the packages, switches on SPI (screen), I²C (light sensor) and I²S (amp), and starts the `desk-display@<you>` service. Your `settings.json` and photos are kept; your old `config.toml` is saved as `config.toml.bak`.

Then open the control page on your phone (same Wi-Fi): the address is printed by the installer and shown as a QR code on the screen for 20 s after every start. Easiest is to name the Pi once: `sudo hostnamectl set-hostname pultwerk01`, then use **http://pultwerk01.local:8080**. On iPhone, Share → Add to Home Screen makes it an app icon.

## The control page

| Section | What you can do |
|---|---|
| Top | Live picture of the round screen, Prev / Next / Radar / Sleep |
| Screens | Switch each screen on or off, change the order (arrows), set how long each stays; tap a name to show it now |
| Display | Automatic brightness (light sensor, with darkest/brightest-room levels) or a fixed level; night sleep times; 24-hour clock; start-up QR code |
| Gauge | Dial light on/off; what the needle shows (16 choices); sweep test |
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
cd ~/desk-display
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

`desk_display.py` display, gauge, sound, sensors · `settings.py` everything the page can change (saved in `settings.json`) · `web.py` + `web_ui.html` the control page (standard library only) · `config.toml` hardware and location · `fonts/` Inter and Noto Kufi Arabic (both SIL Open Font License).
