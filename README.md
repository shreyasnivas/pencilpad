# PencilPad

Use an iPad and Apple Pencil as a trackpad and command pad for your Mac.

PencilPad is one Python script that runs on the Mac. The iPad opens a page it
serves, over Wi-Fi, and that page becomes a trackpad: drag to move the pointer,
tap to click, with a rail of large keys down the left edge that you can hit
without looking, and a keyboard when you need to type.

It was built for coding on a Mac whose display is shown in an Apple Vision Pro,
with the iPad flat on the desk, but it works with any Mac display.

## Quick start

Needs macOS and Python 3.10 or newer (`./run.sh` finds it, or tells you how to
get it). On the Mac:

```sh
git clone https://github.com/shreyasnivas/pencilpad.git
cd pencilpad
./run.sh
```

A setup page opens with a QR code. Point the iPad's camera at it and tap the
link. PencilPad opens in the iPad's browser and works straight away. The iPad
must be on the same Wi-Fi as the Mac. That is all the setup there is.

A short tour on the iPad teaches each gesture in about a minute.

macOS asks for two permissions for the terminal app that runs the script, under
System Settings > Privacy & Security:

- **Accessibility**: needed to move the pointer and press keys.
- **Screen Recording**: only needed for the Screenshot key.

The setup page shows whether each one is allowed.

Everything below is optional.

## Install as an app on the iPad (optional)

In the browser PencilPad already does everything you need. Installed as an app,
it also:

- opens full screen from the Home Screen,
- opens even when the Mac was asleep,
- can use the iPad's microphone for Dictate,
- reaches several Macs from one app.

On the iPad, open PencilPad, tap the gear at the top, then **Install as an
app**. The page that opens walks you through four steps, all on the iPad:
download a certificate, install it, then trust it in the iPad's Settings
(General > About > Certificate Trust Settings), and come back. The trust switch
is the step that is easy to miss: installing the profile is not enough.

When it is done, open PencilPad from that page and add it to the Home Screen
(Share, then Add to Home Screen). If you already had a PencilPad icon, delete
the old one: it was the plain connection.

PencilPad makes a small certificate authority on the Mac (in `~/.pencilpad`)
for this, and the iPad is asked to trust it. See Security, below.

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
- **Show Setup Code** opens the setup page with the QR code.

It also lets Dictate use the iPad's microphone: it turns the recording into
text with Apple's speech recognition, and asks for Speech Recognition the first
time. Recognition runs on the Mac itself when the language supports it;
otherwise macOS sends the audio to Apple.

The app is built on your Mac and is not signed with a developer certificate. It
runs the script from this folder, so keep the folder where it is; if you move
it, or after an update that changes `app/PencilPad.swift`, run `./app/build.sh`
again and allow Accessibility again.

## A second Mac (optional)

The quickest way: run PencilPad on the second Mac and scan its QR code with the
iPad. It opens in the browser and works straight away.

To have one iPad app that reaches both Macs, pair them, on the **new** Mac's
setup page: paste the link of the Mac your iPad app was added from into the
Pair box, and press Pair. The link is under "This Mac's link" on that Mac's
setup page. The new Mac takes over that link, so the app you already have
reaches both. Do not do it the other way round: the Mac that pairs gets a new
link, and an app added from its old link stops working.

Then install PencilPad as an app for the new Mac too (above), so the iPad
trusts it. Installing a Mac's certificate only lets the iPad trust that Mac; it
does not add the Mac to the app. Pairing does. The app's list of Macs (tap the
Mac's name at the top of the pad) offers to set up the certificate of a paired
Mac it cannot reach yet.

The Pair button works at once. From a terminal, `./run.sh --pair '<the other
Mac's link>'` on the new Mac does the same, and so does **Pair with Another Mac**
in its menu bar app; after `--pair`, restart PencilPad on that Mac.

The pad connects to the Mac you chose last, and to another paired Mac when that
one is not reachable. It tries each Mac's `.local` name, its Tailscale name and
address when Tailscale is installed, and its Wi-Fi address, so it keeps working
when one of those is unavailable, for example behind a VPN.

If one of your Macs runs an older PencilPad, Escape, Return, Dictate and
Screenshot still work with it. The newer keys, pinch and the Mac's settings
are dimmed and ask you to update PencilPad on that Mac.

## Gestures

| Do this | To get this |
| --- | --- |
| Drag the Pencil or one finger | Move the pointer (relative, like a trackpad) |
| Tap | Click. Tap again quickly for double and triple click |
| Hold still until the pad turns blue, then lift | Right-click |
| Hold still until the pad turns blue, then slide | Drag |
| Slide two fingers | Scroll |
| Tap with two fingers | Right-click |
| Pinch with two fingers | Zoom in or out (Command+= and Command+-), a step at a time |

While the Pencil is down, finger and palm touches are ignored.

## The rail

The keys sit in a rail down the left edge of the pad. The rest of the screen is
the trackpad.

| Key | What it does |
| --- | --- |
| Escape | Presses Escape |
| Space | Presses Space. Hold it and it repeats |
| Return | Presses Return |
| Copy | Command-C |
| Paste | Command-V |
| Dictate | Talk to the Mac. See below |
| Screenshot | Pick a region; it is copied and pasted into the front app with Ctrl+V |
| Windows | Shows all your open windows (Mission Control). Press again to go back |
| Switch | Goes back to the app you used before, like a quick Command-Tab |
| Keyboard | Opens a keyboard at the bottom of the pad. Press again to close it |

Escape, Space and Return work like the same keys on the keyboard: if Shift is
on there, Shift-Return is what the Mac gets. Copy, Paste, Switch and the rest
always do just their own thing.

The arrow at the bottom of the rail folds it away to a slim strip, and the
trackpad takes the space. Tap the strip to bring the rail back. The pad
remembers which way you left it. In Settings you can hide keys you do not use,
and put the rail on the right.

## The keyboard

The Keyboard key puts a Mac keyboard along the bottom of the pad. The trackpad
still works above it. Each key is a real key press on the Mac, so shortcuts
work, and the Mac's own keyboard layout decides which character you get. Hold a
key and it repeats. There is an Escape key where Caps Lock would be.

Shift, Control, Option and Command work like Shift on the iPad's own keyboard:

- **Tap** one and it applies to the next key you press, then lets go.
- **Tap twice** and it stays on until you tap it again.
- **Hold** one down with one finger while you press keys with another, as on
  a real keyboard. For example, hold Command and tap C to copy.

A modifier that is on for the next key is outlined in blue; one that stays on
is filled in blue. Closing the keyboard, or losing the connection, lets go of
every key.

## Dictate

Dictate works in one of two ways. Choose in Settings, under Dictate:

- **The Mac's dictation app.** Dictate presses a key on the Mac, for a
  dictation app there that listens when its shortcut is pressed. Pick the key
  (Fn, F13 to F19, a right-hand modifier, Option-Space or Control-Space) and
  whether you hold Dictate while you talk, or tap it once to start and once to
  stop.
- **The iPad's microphone.** The pad records on the iPad and the Mac types what
  was said. This needs the menu bar app, and PencilPad installed as an app on
  the iPad (both above). Until then, Dictate presses the key instead.

## Settings

Tap the gear at the top of the pad. Settings for how the Mac behaves (pointer
and scroll speed, scroll direction, Pencil hover, Dictate, pasting screenshots)
are kept on that Mac, in `~/.pencilpad/settings.json`, and every iPad using it
sees the same ones. Which keys show, the rail's side and pinch to zoom belong
to each iPad.

The block at the top of `pencilpad.py` holds the defaults:

- `POINTER_SPEED`, `POINTER_ACCEL`, `SCROLL_SPEED`: how far the pointer and
  scrolling travel.
- `HOVER_MOVES`: let a hovering Pencil move the pointer.
- `DICTATION`: the key Dictate presses on the Mac, and whether it is held or
  toggled.
- `SCREENSHOT_PASTE`: whether a screenshot is pasted after capture.

What you change in Settings wins over these.

## Environment variables

- `PENCILPAD_PORT`: the port to use, as given (not saved). Without it,
  PencilPad uses the port it saved in `~/.pencilpad/port`. On the very first
  run that is 8765, or the next free pair of ports when 8765 or the one above
  it is taken by another program; it says which. A saved port never changes by
  itself, because the iPad's link has it in it.
- `PENCILPAD_TLS_PORT`: the secure port, by default one above the port; `off`
  to turn it off.
- `PENCILPAD_HOSTS`: comma-separated names and addresses to offer the iPad,
  instead of the ones PencilPad finds. For paired Macs only `.local` and
  Tailscale (`.ts.net`) names, `localhost` and private network addresses are
  accepted, so a name or address outside those is not offered to your other
  Macs; paired-Mac entries saved by an older version that do not qualify are
  dropped.
- `PENCILPAD_NO_OPEN=1`: do not open the setup page.
- `PENCILPAD_DRY_RUN=1`: serve the page but send nothing to the Mac.

## Security

Read this before using PencilPad on a network you do not control.

- The link contains a random token, saved in `~/.pencilpad-token`. Anyone on
  the same network who has the link can move the Mac's pointer, click, type
  anything on it with the keyboard (shortcuts included), change PencilPad's
  settings, and add a paired Mac. Keep it private. Delete the file to get a new
  token.
- A paired Mac is only accepted at `.local` and Tailscale names, `localhost`,
  and private network addresses, the same ones the certificate authority below
  is limited to. So the link cannot be used to point your iPad at a computer
  on the internet. Pairing from a Mac's setup page only works in that Mac's own
  browser.
- In the browser the connection is plain HTTP and is not encrypted. Installed
  as an app, the pad uses HTTPS.
- Installing as an app asks the iPad to trust a certificate authority made on
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
