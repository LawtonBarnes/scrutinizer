# CENTRAL SCRUTINIZER (`scrutinizer`)

The boot-default root app on a Metal Shop CRT Pi -- an MS-DOS/OpenVMS
monitor-style amber-phosphor system dashboard (CPU/memory/WiFi/network/
storage) merged with an app-selector menu for launching the Pi's other
appliances, all in one process for instant screen switching.

Built for a Raspberry Pi 3B+ running Raspberry Pi OS Bookworm, output via
the analog composite video jack to a CRT. Shares its console/framebuffer
architecture with [BARS](https://github.com/LawtonBarnes/bars) -- headless
pygame, direct `/dev/fb0` writes, raw `evdev` keyboard input.

![Running on a real CRT](./img/TV_SCRUTE.jpg)

![Framebuffer capture](./img/SCREEN_SCRUTE.png)

## Two screens, one process

- **Dashboard** (default): live CPU temp/clock/load/per-core usage,
  memory, WiFi signal/network throughput, and disk usage, paged with
  `←`/`→`.
- **App menu**: cursor-navigable list of sibling apps, each launched as a
  real subprocess (this screen releases the console first, then
  reacquires it when the app exits).

| Key | Action |
|---|---|
| `↑` / `↓` | Move selection (menu screen) |
| `←` / `→` | Change dashboard page |
| Menu / hamburger | Switch between dashboard and app menu |
| `Enter` | Launch selected app (menu screen) |
| `Home` / `Back` | Jump straight to the dashboard from anywhere |
| `Q` / `Esc` | Quit to shell |
| `Power` | Shutdown/restart confirm dialog |

## Sibling apps launched from the menu

Each is its own repo -- paths and launcher commands are read from the
`APPS` table at the top of `scrutinizer.py`:

- [BARS](https://github.com/LawtonBarnes/bars) -- NTSC/SMPTE test patterns
- [LOUDNESS](https://github.com/LawtonBarnes/loudness) -- audio spectrum visualizer
- WEATHERSTAR 4000 -- current conditions (not a separate repo, built on [ws4kp](https://github.com/netbymatt/ws4kp))
- [CHANNEL 38](https://github.com/LawtonBarnes/channel38) -- Ole Miss sports/news ticker

## Boot behavior

`~/.bashrc` launches `scrutinizer` directly on physical tty1 login
(console autologin is configured separately via systemd) -- this is what
makes the Pi boot straight into the dashboard with no manual steps.
