# TV State Reference Guide

## Understanding TV States

Samsung Frame TVs have **two independent states**:

### 1. Screen State
- **On**: Screen is lit and displaying content
- **Off**: Screen is black (power saving mode)

### 2. Mode State  
- **Art Mode**: TV configured to display artwork
- **TV Mode**: TV configured for regular TV viewing (channels, apps, etc.)

## All Possible Combinations

| Screen | Mode | Description | `is_screen_on()` | `is_art_mode_enabled()` |
|--------|------|-------------|------------------|------------------------|
| **ON** | **Art** | 🖼️ Displaying artwork | ✅ True | ✅ True |
| **ON** | **TV** | 📺 Watching TV/apps | ✅ True | ❌ False |
| **OFF** | **Art** | 😴 Standby (art mode active) | ❌ False | ✅ True |
| **OFF** | **TV** | 💤 Fully off | ❌ False | ❌ False |

## Function Behavior

### `is_art_mode_enabled(ip)`
**What it checks**: Is the TV configured for art mode?  
**Use case**: "Can I upload artwork / change brightness?"

```python
# Returns True if art mode is enabled (screen may be on or off)
if is_art_mode_enabled(tv_ip):
    set_tv_brightness(tv_ip, 5)  # This will work
```

### `is_screen_on(ip)`
**What it checks**: Is the screen physically displaying something?  
**Use case**: "Is the TV visible / using power?"

```python
# Returns True only if screen is actively displaying
if is_screen_on(tv_ip):
    print("TV is on and visible")
else:
    print("TV screen is off (power saving)")
```

## Command Examples

### Scenario 1: Screen Off, Art Mode Active
```bash
$ python scripts/frame_tv_cli.py 192.168.1.249 screen-status
Screen is off (standby/power saving)

$ python scripts/frame_tv_cli.py 192.168.1.249 status  
Art mode is enabled

# Art operations still work!
$ python scripts/frame_tv_cli.py 192.168.1.249 brightness 5
Brightness set to 5
```

### Scenario 2: Screen On, TV Mode
```bash
$ python scripts/frame_tv_cli.py 192.168.1.249 screen-status
Screen is on (displaying content)

$ python scripts/frame_tv_cli.py 192.168.1.249 status
Art mode is not enabled

# Need to switch modes first
$ python scripts/frame_tv_cli.py 192.168.1.249 art-mode
TV switched to art mode
```

### Scenario 3: Complete Workflow
```bash
# Check states
$ python scripts/frame_tv_cli.py 192.168.1.249 screen-status
Screen is off (standby/power saving)

$ python scripts/frame_tv_cli.py 192.168.1.249 status
Art mode is not enabled

# Turn on and switch to art mode
$ python scripts/frame_tv_cli.py 192.168.1.249 on
Power on command sent

$ python scripts/frame_tv_cli.py 192.168.1.249 art-mode
TV switched to art mode

# Now both should be true
$ python scripts/frame_tv_cli.py 192.168.1.249 screen-status
Screen is on (displaying content)

$ python scripts/frame_tv_cli.py 192.168.1.249 status
Art mode is enabled
```

## Python Code Examples

### Check Both States
```python
from custom_components.frame_art_shuffler.frame_tv import (
    is_art_mode_enabled,
    is_screen_on,
)

# is_screen_on takes an IP (uses REST API, no WebSocket needed)
screen_on = await is_screen_on("192.168.1.249")

# is_art_mode_enabled takes a TVConnectionManager (uses WebSocket art channel)
art_enabled = await is_art_mode_enabled(client)

if screen_on and art_enabled:
    print("Screen on, displaying artwork")
elif screen_on and not art_enabled:
    print("Screen on, in TV mode")
elif not screen_on and art_enabled:
    print("Screen off, art mode standby")
else:
    print("Fully off")
```

### Smart Preparation
```python
# The integration handles this automatically via _ensure_art_mode() and
# the power/wake sequence in set_art_on_tv_deleteothers(). All TV operations
# use the async samsungtvws WebSocket client with request/response confirmation,
# so no manual time.sleep() delays are needed.
#
# For manual scripting, see scripts/frame_tv_cli.py.
```

## API Notes

The old sync `is_tv_on()` function has been removed. Current async equivalents:

```python
# Art mode check (via TVConnectionManager)
status = await art.get_artmode()  # returns "on" or "off"

# Screen check (via REST API)
screen_on = await is_screen_on(ip)  # True if screen is lit
```

## Common Patterns

### 1. "Wake up and show art"
```bash
python scripts/frame_tv_cli.py 192.168.1.249 on --mac 28:AF:42:18:64:08
python scripts/frame_tv_cli.py 192.168.1.249 art-mode
python scripts/frame_tv_cli.py 192.168.1.249 upload artwork.jpg
```

### 2. "Update art while screen is off"
```bash
# Works even with screen off if art mode is enabled!
python scripts/frame_tv_cli.py 192.168.1.249 upload artwork.jpg
python scripts/frame_tv_cli.py 192.168.1.249 brightness 5
```

### 3. "Turn off screen but keep art ready"
```bash
python scripts/frame_tv_cli.py 192.168.1.249 off
# Screen goes off, art mode stays enabled
# Art websocket still works for brightness/upload
```
