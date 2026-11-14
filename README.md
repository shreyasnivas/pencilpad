# PencilPad

Use an iPad and Apple Pencil as a trackpad and command pad for your Mac.

PencilPad is one Python script that runs on the Mac. The iPad opens a page it
serves, over Wi-Fi, and that page becomes a trackpad: drag to move the pointer,
tap to click, with four large corner keys you can hit without looking.

It was built for coding on a Mac whose display is shown in an Apple Vision Pro,
with the iPad flat on the desk, but it works with any Mac display.

## Install and run

Needs macOS and Python 3.10 or newer. On the Mac:

```sh
git clone https://github.com/shreyasnivas/pencilpad.git
cd pencilpad
./run.sh
```

The first run creates a virtual environment and installs the dependencies. A
setup page then opens in your browser with a QR code:

1. Point the iPad's camera at the code and open the link. The iPad must be on
   the same Wi-Fi network as the Mac. No cable is needed.
2. Follow the short tour on the iPad. It teaches each gesture in about a minute.
3. In Safari, tap Share, then Add to Home Screen, for a full-screen pad.

macOS asks for two permissions for the terminal app that runs the script, under
System Settings > Privacy & Security:

- **Accessibility**: needed to move the pointer and press keys.
- **Screen Recording**: only needed for the Screenshot corner.

The setup page shows whether each one is allowed.

## Gestures

| Do this | To get this |
| --- | --- |
| Drag the Pencil or one finger | Move the pointer (relative, like a trackpad) |
| Tap | Click. Tap again quickly for double and triple click |
| Hold still until the pad turns blue, then lift | Right-click |
| Hold still until the pad turns blue, then slide | Drag |
| Slide two fingers | Scroll |
| Tap with two fingers | Right-click |

While the Pencil is down, finger and palm touches are ignored.

## Corner keys

| Corner | Key |
| --- | --- |
| Top left | Escape |
| Top right | Screenshot: pick a region, it is copied and pasted into the front app with Ctrl+V |
| Bottom left | Dictate: holds a key while pressed (F13 by default) for push-to-talk dictation apps |
| Bottom right | Return |

## Settings

Edit the block at the top of `pencilpad.py`:

- `POINTER_SPEED`, `POINTER_ACCEL`, `SCROLL_SPEED`: how far the pointer and
  scrolling travel.
- `HOVER_MOVES`: let a hovering Pencil move the pointer.
- `DICTATION`: the key the Dictate corner presses, and whether it is held or
  toggled.
- `SCREENSHOT_PASTE`: whether a screenshot is pasted after capture.

Environment variables: `PENCILPAD_PORT` (default 8765), `PENCILPAD_NO_OPEN=1`
(do not open the setup page), `PENCILPAD_DRY_RUN=1` (serve the page but send
nothing to the Mac).

## Security

Read this before using PencilPad on a network you do not control.

- The link contains a random token, saved in `~/.pencilpad-token`. Anyone on
  the same network who has the link can control the Mac's pointer and press
  the corner keys. Keep it private. Delete the file to get a new token.
- The connection is plain HTTP and is not encrypted.
- Use it on a trusted home or office network only.

## Tests

```sh
./run_tests.sh
```

The tests start the server in dry-run mode and drive the real page in Chromium
with simulated touch and Pencil input. Nothing is sent to your Mac.

## Licence

MIT. See [LICENSE](LICENSE).

PencilPad is an independent project and is not affiliated with Apple. iPad,
Apple Pencil, Mac and Apple Vision Pro are trademarks of Apple Inc.
