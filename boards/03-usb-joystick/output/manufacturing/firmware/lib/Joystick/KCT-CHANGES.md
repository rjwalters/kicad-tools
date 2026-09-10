# Vendored Joystick library

Upstream: https://github.com/MHeironimus/ArduinoJoystickLibrary
Commit: 12cf2bbdb8910619d32ba3bf4b7d669c8e813b99 (v2.1.1).

Only `src/DynamicHID/DynamicHID.cpp`, function `DynamicHID_::getShortName`,
is changed: it returns `kicad-tools.org:03` instead of the generic descriptor-size
string. This meets the shared joystick ID's domain-prefixed serial convention.
The string is shorter than the Arduino USB core's 20-byte serial buffer. All
upstream copyright and license notices remain. The original unmodified source
is also included in `../../dependency-sources.zip` for comparison/relinking.
