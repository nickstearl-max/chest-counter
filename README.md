<img src="docs/icon.png" width="72" align="right" alt="">

# Chest Counter

Counts the chests you open in Fortnite. A small overlay under your minimap shows **This game** and **All time**, and the numbers roll up each time you open one.

<img src="docs/screenshot.png" width="340" alt="Chest Counter window">

## Download
Get **ChestCounter.exe** from the [latest release](../../releases/latest) and double-click it. You don't need to install anything.

> Windows may say **"Windows protected your PC"** because the app isn't code-signed. Click **More info → Run anyway**.

## How to use
1. Open Chest Counter. It starts **ON** (the green button).
2. Play Fortnite. Every chest you open adds 1. **CHEST X2** adds 2.
3. **This game** goes back to 0 by itself when you get on the Battle Bus. **All time** keeps counting.

| | |
|---|---|
| **Ctrl + Alt + C** | turn it on or off, even in game |
| **+1 / −1** | fix a count by hand |
| **New game** | set *This game* to 0 |
| **Reset all** | set both counts to 0 |

It counts **CHEST**, **CHEST X2**, **RARE CHEST** and **SPRITE CHEST**.

## How it works
When you open a chest, Fortnite shows a purple popup: **"+600 XP · CHEST"**. Chest Counter looks at that one small spot on the screen (280×140 pixels) every 0.6 seconds and recognises the word by its purple outline. Because it only looks at the outline, whatever is behind the popup doesn't matter.

- **It only reads the screen**, like a recording app does. It never touches the game's memory, files or network traffic, and it never presses keys.
- **It's light.** It only looks while Fortnite is the active window, and it runs at below-normal priority with no GPU use. It uses about 50 MB of RAM.
- **Nothing leaves your PC.** Counts and settings are saved in `%APPDATA%\ChestCounter`.

## If it doesn't count
- **Display mode:** use **Windowed Fullscreen** in Fortnite's display settings. Some exclusive-fullscreen setups give screen-capture tools a black screen.
- **HUD scale:** keep it at **100%**. Any 16:9 resolution works, and so does ultrawide.
- **Match score:** while a chest popup is on screen, the number in the app should be **0.70** or higher.
- **Snapshots:** tick **Save snapshots of what it sees**, open some chests, then click **snapshots** in the bottom corner to see exactly what it captured. Attach those to an issue if something's off.

## Run from source
```bash
pip install -r requirements.txt
python chest_counter.py
```
To build the .exe, run `build.bat`. It needs PyInstaller, and the output lands in `dist\ChestCounter.exe`.

To test it against your own screenshots or clip frames:
```bash
python chest_counter.py --test screenshot1.png screenshot2.png
python chest_counter.py --count 20 frame_001.png frame_002.png ...
```

---
Not affiliated with or endorsed by Epic Games. Fortnite is a trademark of Epic Games, Inc.
