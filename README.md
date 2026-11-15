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

## Menu bar app (optional)

If you would rather not start PencilPad from a terminal, build the small menu
bar app. It keeps PencilPad running in the background, starts it at login, and
shows whether the iPad is connected.

```sh
./app/build.sh
open /Applications/PencilPad.app
```

Needs the Xcode command line tools (`xcode-select --install`). Then:

- Allow **Accessibility** for PencilPad when macOS asks. The app restarts the
  pad by itself once it is allowed.
- Choose **Start at Login** from the menu.
- **Show Setup Code** opens the QR code page.
- **Pair with Another Mac** lets one pad on the iPad reach two Macs.

The app is built on your Mac and is not signed with a developer certificate. It
runs the script from this folder, so keep the folder where it is; if you move
it, or after an update that changes `app/PencilPad.swift`, run `./app/build.sh`
again and allow Accessibility again.

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
| Bottom left | Dictate: hold to talk. See below for which microphone it uses |
| Bottom right | Return |

## The secure connection: iPad microphone and several Macs

Two features need the iPad to trust this Mac, because Safari only gives a page
the microphone, and only lets it talk to more than one computer, over HTTPS:

- **Dictate with the iPad's microphone.** Hold the Dictate corner and speak.
  The pad records on the iPad, the Mac turns the recording into text with
  Apple's speech recognition and types it into the front app. This needs the
  menu bar app, which asks for Speech Recognition the first time. Recognition
  runs on the Mac itself when the language supports it; otherwise macOS sends
  the audio to Apple. Without the secure connection, or when PencilPad runs
  from a terminal, the corner holds a key instead (F13 by default), for
  push-to-talk dictation apps on the Mac.
- **One pad for several Macs.** The pad connects to the Mac you chose last, and
  to another paired Mac when that one is not reachable. Tap the Mac's name at
  the top of the pad to switch. The Home Screen app also keeps a copy of
  itself, so it opens even when the Mac it was added from is asleep.

Set it up once per iPad and Mac: open the pad, tap the Mac's name at the top,
then **Set Up the Secure Connection**, and follow the four steps. PencilPad
makes a small certificate authority on the Mac (in `~/.pencilpad`) and the iPad
is asked to trust it. Then add the pad to the Home Screen again from the secure
page, and delete the old icon.

To add a second Mac, install PencilPad on it, then give it the first Mac's
link (Copy iPad Link in the menu bar app):

```sh
./run.sh --pair 'http://192.168.1.20:8765/your-token'
```

or choose **Pair with Another Mac** in its menu bar app. Both Macs then share
one link. On the iPad, open the second Mac's setup page once and trust its
certificate too. The pad tries each Mac's `.local` name, its Tailscale name and
address when Tailscale is installed, and its Wi-Fi address, so it keeps working
when one of those is unavailable, for example behind a VPN.

## Settings

Edit the block at the top of `pencilpad.py`:

- `POINTER_SPEED`, `POINTER_ACCEL`, `SCROLL_SPEED`: how far the pointer and
  scrolling travel.
- `HOVER_MOVES`: let a hovering Pencil move the pointer.
- `DICTATION`: the key the Dictate corner presses, and whether it is held or
  toggled.
- `SCREENSHOT_PASTE`: whether a screenshot is pasted after capture.

Environment variables: `PENCILPAD_PORT` (default 8765), `PENCILPAD_TLS_PORT`
(the secure port, default one above the port; `off` to disable),
`PENCILPAD_HOSTS` (comma-separated names and addresses to offer the iPad,
instead of the ones PencilPad finds), `PENCILPAD_NO_OPEN=1` (do not open the
setup page), `PENCILPAD_DRY_RUN=1` (serve the page but send nothing to the
Mac).

## Security

Read this before using PencilPad on a network you do not control.

- The link contains a random token, saved in `~/.pencilpad-token`. Anyone on
  the same network who has the link can control the Mac's pointer and press
  the corner keys. Keep it private. Delete the file to get a new token.
- The first connection is plain HTTP and is not encrypted. After you set up
  the secure connection the pad uses HTTPS.
- The secure connection asks the iPad to trust a certificate authority made on
  your Mac. Its private key stays in `~/.pencilpad` on that Mac. The authority
  is restricted to `.local` and Tailscale names and to private network
  addresses, so it cannot be used to impersonate websites. To undo it, remove
  the profile in the iPad's Settings under General, VPN & Device Management.
- Pairing two Macs gives them the same link. Anyone with the link can control
  both.
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
